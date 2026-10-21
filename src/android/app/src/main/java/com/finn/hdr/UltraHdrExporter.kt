package com.finn.hdr

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.graphics.Paint
import java.io.ByteArrayOutputStream

/**
 * 导出编排：源 → （几何变换）→ 预压缩 → 重编码 → Ultra HDR 封装 → 自检。
 *
 * 设计取舍：
 *  - 缩放在 Bitmap 层做（单次重采样），不叠加系统相册的多轮压缩
 *  - 主图与增益图各自独立编码，再由 native 侧 API-4 组装，避免库内部再次 tone mapping
 *  - 任何阶段异常都返回 [Result.ok] = false 且带原因，不静默降级成「假 HDR」
 */
class UltraHdrExporter {

    data class Options(
        val longEdge: Int = 2048,
        val baseQuality: Int = 92,
        val gainMapQuality: Int = 95,
        val maxBoostLog2: Float = 2.0f,
        val gamma: Float = 1.0f,
        val preferRecomputeBoost: Boolean = false,
    )

    data class Result(
        val jpeg: ByteArray?,
        val ok: Boolean,
        val validation: MetadataValidator.Report?,
        val steps: MutableList<String> = mutableListOf(),
        val warnings: MutableList<String> = mutableListOf(),
    )

    /** 按目标长边采样解码，避免全分辨率解码耗尽内存。 */
    private fun decodeScaled(bytes: ByteArray, targetLong: Int): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= targetLong) sample *= 2
        val opt = BitmapFactory.Options().apply {
            inSampleSize = sample
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opt)
    }

    fun export(src: HdrSourceParser.HdrSource, opt: Options = Options()): Result {
        val steps = mutableListOf<String>()
        val warnings = mutableListOf<String>()

        if (!src.hasGainMap) {
            return Result(null, false, null, steps, mutableListOf("源图无增益图，无法生成 Ultra HDR"))
        }

        // 按目标长边采样解码。原实现直接解全分辨率：5760×4320 ARGB_8888 ≈ 99 MB，
        // 主图+增益图+resize 结果叠起来，容器 256 MB 堆必 OOM。
        val base0 = decodeScaled(src.sdrJpeg, opt.longEdge)
            ?: return Result(null, false, null, steps, mutableListOf("主图解码失败"))
        val gm0 = decodeScaled(src.gainMapJpeg!!, opt.longEdge)
            ?: return Result(null, false, null, steps, mutableListOf("增益图解码失败"))
        steps += "decode(sampled): base=${base0.width}x${base0.height} gm=${gm0.width}x${gm0.height}"

        val geo0 = GainMapGeometry.requireConsistent(base0, gm0)
        steps += "geometry(source): $geo0"

        val t = GainMapGeometry.resize(base0, gm0, opt.longEdge)
        steps += "resize: base=${t.base.width}x${t.base.height} gm=${t.gainMap.width}x${t.gainMap.height}"
        warnings += t.plan.warnings

        // resize 在“无需缩放”时会返回同一个对象，回收前必须判是否是同一实例
        if (t.base !== base0) base0.recycle()
        if (t.gainMap !== gm0) gm0.recycle()

        val baseJpeg = compress(t.base, opt.baseQuality)
        val gmJpeg = compressGray(t.gainMap, opt.gainMapQuality)
        steps += "encode: base=${baseJpeg.size}B q=${opt.baseQuality} gm=${gmJpeg.size}B q=${opt.gainMapQuality}"

        if (src.needsBoostRecompute && !opt.preferRecomputeBoost) {
            warnings += "源为 CUVA 语义，当前按原增益图直接封装；色彩/亮度可能有偏差，建议开启反解重算路径"
        }

        // 封装：优先 native（libultrahdr API-4）；无 NDK 时回退纯 Kotlin 封装器，
        // 保证「不装 NDK 也能编出可安装 APK 做真机结构验证」。
        // 增益上限优先沿用源图（ISO 21496-1 / XMP 解出的 log2 值），缺失才用默认。
        // 注意 hdrgm 的 GainMapMax 与 HDRCapacityMax 都是 **log2 域**，
        // 把线性 boost（如 4.0）写进去等于宣称 16 倍容量，是隐蔽的语义错误。
        val maxLog2 = src.meta?.maxContentBoost ?: opt.maxBoostLog2
        val gamma = src.meta?.gamma ?: opt.gamma
        if (src.meta != null) {
            steps += "meta(source): GainMapMax=${"%.4f".format(maxLog2)} Gamma=${"%.4f".format(gamma)}"
        } else {
            steps += "meta(default): GainMapMax=${"%.4f".format(maxLog2)}"
        }

        var engine = "native(API-4)"
        var out: ByteArray? = null
        try {
            out = UltraHdrNative.nativeEncode(
                baseJpeg, gmJpeg,
                minBoost = 1.0f,
                maxBoost = Math.pow(2.0, maxLog2.toDouble()).toFloat(),
                gamma = gamma,
                baseQuality = opt.baseQuality,
                gainmapQuality = opt.gainMapQuality,
            )
            if (out == null) {
                warnings += "native 封装返回空：${runCatching { UltraHdrNative.lastError() }.getOrNull()}"
            }
        } catch (t: Throwable) {
            warnings += "native 不可用（${t.javaClass.simpleName}），回退纯 Kotlin 封装（仅路线 B）"
        }
        if (out == null) {
            engine = "kotlin-packager"
            out = UltraHdrPackager.pack(
                baseJpeg, gmJpeg,
                UltraHdrPackager.Meta(
                    gainMapMax = maxLog2.toDouble(),
                    gamma = gamma.toDouble(),
                    hdrCapacityMax = maxLog2.toDouble(),
                ),
            ).first
        }
        val packaged: ByteArray = out
            ?: return Result(null, false, null, steps, (warnings + "封装失败").toMutableList())
        steps += "package[$engine]: ${packaged.size}B"

        val rep = MetadataValidator.validate(packaged)
        steps += "validate: ok=${rep.ok} errors=${rep.errors.size}"
        if (!rep.ok) warnings += "自检未通过，导出文件可能已退化为纯 SDR"

        return Result(packaged, rep.ok, rep, steps, warnings)
    }

    /**
     * 主图编码。注意：Bitmap.compress 不写 EXIF，生产实现需在输出后
     * 用 ExifInterface 或自研段拼接把源 EXIF 的 APP1 回插（并强制 Orientation=1）。
     */
    private fun compress(bmp: Bitmap, quality: Int): ByteArray {
        val bos = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.JPEG, quality, bos)
        return bos.toByteArray()
    }

    /**
     * 增益图编码为单通道 JPEG。
     * Android 的 Bitmap.compress 只会输出 3 通道，这里先置灰保证三通道同值；
     * 生产版本建议在 native 侧用 libjpeg 直接编码单通道（体积约省 1/3，且语义更规范）。
     */
    private fun compressGray(bmp: Bitmap, quality: Int): ByteArray {
        val gray = Bitmap.createBitmap(bmp.width, bmp.height, Bitmap.Config.ARGB_8888)
        val paint = Paint().apply {
            colorFilter = ColorMatrixColorFilter(ColorMatrix().apply { setSaturation(0f) })
        }
        Canvas(gray).drawBitmap(bmp, 0f, 0f, paint)
        return compress(gray, quality)
    }
}
