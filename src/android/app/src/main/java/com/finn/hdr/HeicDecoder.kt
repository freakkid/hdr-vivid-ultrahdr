package com.finn.hdr

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageDecoder
import android.os.Build
import java.nio.ByteBuffer

/**
 * HEIC / HEIF **主图**解码。
 *
 * 能力边界（务必看清，别把这里的成功当成 HDR 支持）：
 * - 只用系统解码器（`ImageDecoder` / `BitmapFactory`），不引入 libheif。
 * - 解出来的是**主图渲染结果**，10bit HDR 内容会被压到 8bit SDR，观感可能偏灰。
 * - **拿不到增益图**（Android 14 才有 `Bitmap#getGainmap`），所以这里解出来的
 *   不能用于 HDR 烘焙，只能用于「让用户先看到画面 + 确认文件本身可读」。
 *
 * 容器（如鸿蒙卓易通）里系统 HEVC 解码器是否存在完全未知，因此失败要给明确原因，
 * 而不是抛一个语焉不详的异常。
 */
object HeicDecoder {

    data class Result(
        val bitmap: Bitmap?,
        val width: Int,
        val height: Int,
        val engine: String,
        val note: String,
    ) {
        val ok: Boolean get() = bitmap != null
    }

    /**
     * @param targetLong 目标长边，按 2 的幂采样；传 0 表示不采样（原尺寸，风险高）
     */
    fun decodeMain(bytes: ByteArray, targetLong: Int = 2048): Result {
        // 先取原始尺寸：HEIC 的 SOF 在 item 里，BitmapFactory 的 bounds 解码
        // 依赖系统 HEIF 解析器，能拿到就说明解码器在
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        runCatching { BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds) }
        val ow = bounds.outWidth
        val oh = bounds.outHeight

        val attempts = ArrayList<String>()
        val errors = ArrayList<String>()

        // ---- 通道 1：ImageDecoder（API 28+），支持采样且对 HEIF 友好
        if (Build.VERSION.SDK_INT >= 28) {
            try {
                val source = ImageDecoder.createSource(ByteBuffer.wrap(bytes))
                var realW = 0
                var realH = 0
                val bmp = ImageDecoder.decodeBitmap(source) { decoder, info, _ ->
                    realW = info.size.width
                    realH = info.size.height
                    val sample = if (targetLong > 0) sampleFor(realW, realH, targetLong) else 1
                    if (sample > 1) decoder.setTargetSampleSize(sample)
                    // 必须软件位图：后续烘焙要 getPixels，硬件位图读不出来
                    decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
                    decoder.isMutableRequired = false
                }
                if (bmp != null) {
                    return Result(bmp, realW, realH, "ImageDecoder",
                        "主图已由系统解码（${realW}x${realH}）。**增益图未提取**，仅供查看。")
                }
                attempts += "ImageDecoder 返回 null"
            } catch (t: Throwable) {
                val m = "ImageDecoder: ${t.javaClass.simpleName}: ${t.message ?: ""}"
                attempts += m
                errors += m
            }
        } else {
            attempts += "ImageDecoder 需要 API 28+（当前 ${Build.VERSION.SDK_INT}），跳过"
        }

        // ---- 通道 2：BitmapFactory（API 28 以下或 ImageDecoder 失败时）
        if (ow > 0 && oh > 0) {
            try {
                val sample = if (targetLong > 0) sampleFor(ow, oh, targetLong) else 1
                val opt = BitmapFactory.Options().apply {
                    inSampleSize = sample
                    inPreferredConfig = Bitmap.Config.ARGB_8888
                }
                val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opt)
                if (bmp != null) {
                    return Result(bmp, ow, oh, "BitmapFactory",
                        "主图已由系统解码（${ow}x${oh}, sample=$sample）。**增益图未提取**，仅供查看。")
                }
                attempts += "BitmapFactory(sample=$sample) 返回 null"
            } catch (t: Throwable) {
                val m = "BitmapFactory: ${t.javaClass.simpleName}: ${t.message ?: ""}"
                attempts += m
                errors += m
            }
        } else {
            attempts += "BitmapFactory 连宽高都读不出（outWidth=${ow} outHeight=${oh}）" +
                " —— 系统缺少 HEIF 解析器"
        }

        val hint = buildString {
            append("HEIC 主图解码失败。\n尝试记录：\n")
            attempts.forEach { append("  · ").append(it).append('\n') }
            append("结论：")
            if (ow <= 0 && oh <= 0) {
                append("系统没有 HEIF 解析能力（容器裁剪掉了 HEVC 解码器）。")
                append("这条路在容器内走不通，需要自研 ISOBMFF 提取 + 重封装，或改用桌面临时处理。")
            } else {
                append("系统能读尺寸但不能出像素，可能是解码器缺失或被内存限制拦住。")
            }
        }
        return Result(null, ow, oh, "none", hint)
    }

    /** 采样倍数：保证解码后长边 >= 目标，且是 2 的幂。 */
    private fun sampleFor(w: Int, h: Int, targetLong: Int): Int {
        if (w <= 0 || h <= 0 || targetLong <= 0) return 1
        var s = 1
        while (maxOf(w, h) / (s * 2) >= targetLong) s *= 2
        return s
    }
}
