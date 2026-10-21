package com.finn.hdr

import android.graphics.Bitmap
import java.util.Random

/**
 * HDR → SDR 烘焙器：把「SDR 主图 + 增益图」合成后压回 8 bit 普通 JPEG。
 *
 * 与 [UltraHdrPackager] 的区别：
 *  - UltraHdrPackager：保留增益图，交给支持 HDR 的设备还原（依赖接收端能力）
 *  - HdrBaker：在这里就把 HDR 亮度压回 SDR，输出任何设备/微信都能直接看到效果的普通图
 *
 * 算法（遵循 ISO 21496-1 / hdrgm 公式，与 Python 侧 bake.py 一致）：
 *   1. sRGB → 线性（1024 项查表）
 *   2. recovery = 增益图 / 255
 *      log_boost = recovery × (max_log2 − min_log2) + min_log2
 *      gain = 2^(log_boost × strength)
 *   3. HDR = (SDR + offsetSdr) × gain − offsetHdr
 *   4. 色调映射：
 *      - profile=off   仅全局（sdr-white 锚定 + 高光软膝），旧行为
 *      - profile=local / wechat  全局 + [LocalToneMapping] 局部色调映射（观感优先）
 *   5. S 曲线 + 饱和度补偿 —— **必须在 sRGB（感知）域做**。放在线性域会把
 *      0.5 以下亮度强力压向 0（线性 0.5 ≈ sRGB 188），暗部被钉死成纯黑，
 *      这是 Python 侧 P0-1 修过的 bug，Kotlin 侧同步修正。
 *   6. 三角分布抖动（固定种子）→ 8bit 量化，抑制平滑渐变 banding
 */
object HdrBaker {

    data class Options(
        val longEdge: Int = 2048,
        val strength: Float = 0.70f,     // 增益强度，1.0 = 完全按元数据恢复
        /** off = 仅全局映射；local = +局部色调映射（默认，观感优先）；wechat = 克制版，为二次压缩留余量 */
        val profile: String = "local",
        /** sdr-white = 以 1.0 为 SDR 白点（默认）；peak = 按高分位归一化（会压暗全图，慎用） */
        val anchor: String = "sdr-white",
        val whitePercentile: Float = 0.995f,
        val knee: Float = 0.75f,
        val saturation: Float = 1.12f,
        val contrast: Float = 1.06f,
        val dither: Boolean = true,
    )

    data class Stats(
        val outSize: Pair<Int, Int>,
        val hdrPeak: Float,
        val whitePoint: Float,
        val clippedRatio: Float,
        val maxBoost: Float,
        val profile: String = "off",
        val ltmRadiusPx: Int = 0,
        val ltmDetailGain: Float = 0f,
        val ltmShadowLift: Float = 1f,
    )

    private val SRGB_TO_LIN = FloatArray(1024) { i ->
        val v = i / 1023f
        if (v <= 0.04045f) v / 12.92f else Math.pow(((v + 0.055) / 1.055).toDouble(), 2.4).toFloat()
    }

    private val LIN_TO_SRGB = FloatArray(1024) { i ->
        val v = i / 1023f
        if (v <= 0.0031308f) v * 12.92f
        else (1.055 * Math.pow(v.toDouble(), 1.0 / 2.4) - 0.055).toFloat()
    }

    fun bake(
        base: Bitmap,
        gainMap: Bitmap,
        meta: UltraHdrNative.GainMapMeta?,
        opt: Options = Options(),
    ): Pair<Bitmap, Stats> {
        // 1) 只缩不放
        val cur = maxOf(base.width, base.height)
        val k = if (cur > opt.longEdge) opt.longEdge.toFloat() / cur else 1f
        val w = maxOf(1, Math.round(base.width * k))
        val h = maxOf(1, Math.round(base.height * k))

        val baseScaled = if (w != base.width) Bitmap.createScaledBitmap(base, w, h, true) else base
        val gmScaled = if (gainMap.width != w || gainMap.height != h) {
            Bitmap.createScaledBitmap(gainMap, w, h, true)
        } else gainMap

        val n = w * h
        val px = IntArray(n)
        baseScaled.getPixels(px, 0, w, 0, 0, w, h)
        val gmPx = IntArray(n)
        gmScaled.getPixels(gmPx, 0, w, 0, 0, w, h)

        // 2) 元数据
        val maxLog2 = meta?.maxContentBoost ?: 2.0f
        val minLog2 = meta?.minContentBoost ?: 0.0f
        val offSdr = meta?.offsetSdr ?: (1f / 64f)
        val offHdr = meta?.offsetHdr ?: (1f / 64f)
        val span = (maxLog2 - minLog2) * opt.strength

        // gain 只取决于增益图灰度值（0..255），span/minLog2/strength 均为常量 →
        // 256 项查找表，省掉每像素一次 pow（3M 像素约省 100ms）
        val gainLut = FloatArray(256) { g ->
            Math.pow(2.0, (g / 255f * span + minLog2 * opt.strength).toDouble()).toFloat()
        }

        // 3) 合成 HDR（RGB 三个 float 分量）+ 亮度 + 直方图
        val hdr = FloatArray(n * 3)
        val lum = FloatArray(n)
        val hist = IntArray(1024)
        var peak = 0f

        var i = 0
        while (i < n) {
            val p = px[i]
            val lr = SRGB_TO_LIN[(((p shr 16) and 0xFF) * 1023) / 255]
            val lg = SRGB_TO_LIN[(((p shr 8) and 0xFF) * 1023) / 255]
            val lb = SRGB_TO_LIN[((p and 0xFF) * 1023) / 255]

            // 增益图取灰度（三通道同值时取任意一个）
            val gmv = (gmPx[i] shr 16) and 0xFF
            val gain = gainLut[gmv]

            val hr = Math.max((lr + offSdr) * gain - offHdr, 0f)
            val hg = Math.max((lg + offSdr) * gain - offHdr, 0f)
            val hb = Math.max((lb + offSdr) * gain - offHdr, 0f)

            hdr[i * 3] = hr
            hdr[i * 3 + 1] = hg
            hdr[i * 3 + 2] = hb

            val l = 0.2126f * hr + 0.7152f * hg + 0.0722f * hb
            lum[i] = l
            if (l > peak) peak = l
            hist[(l.coerceIn(0f, 1f) * 1023).toInt()]++
            i++
        }

        // 4) 白点锚定（与 Python bake.py 的 anchor 语义一致）：
        //    sdr-white：HDR 重建后 1.0 天然就是 SDR 白点（增益为 0 时 hdr≈sdr），
        //               直接用 1.0，只压缩超出部分；
        //    peak：按高分位归一化。危险——会把整张按峰值压暗，暗部直接钉死在 0，
        //          表现为「黑位糊死 + 整体发闷」，仅在确知源已整体过曝时才用。
        val white: Float
        if (opt.anchor == "peak") {
            val target = (n * opt.whitePercentile).toInt()
            var acc = 0
            var wp = 1f
            for (bIdx in hist.indices) {
                acc += hist[bIdx]
                if (acc >= target) { wp = maxOf(bIdx / 1023f, 1e-3f); break }
            }
            // 直方图被 1.0 截断，峰值超过 1 时用峰值作为下限参考
            white = if (peak > 1f && wp >= 0.999f) peak * 0.85f else wp
        } else {
            white = 1f
        }

        // 5) 色调映射 → 显示线性域 out（w*h*3，值域 [0,1] 附近）
        val ltmParams = LocalToneMapping.paramsForProfile(opt.profile)
        var ltmStats: LocalToneMapping.Stats? = null
        val out: FloatArray
        var clipped = 0
        if (ltmParams != null) {
            // 局部色调映射：输出已是「1.0 = SDR 白点」的显示线性域，
            // 不能再走 hdr * (mapped/lum) 全局归一化，否则会被二次压暗。
            val ltmResult = LocalToneMapping.apply(hdr, lum, w, h, white, ltmParams)
            out = ltmResult.out
            ltmStats = ltmResult.stats
        } else {
            out = FloatArray(n * 3)
            i = 0
            while (i < n) {
                val hr = hdr[i * 3]
                val hg = hdr[i * 3 + 1]
                val hb = hdr[i * 3 + 2]
                val l = lum[i]
                val norm = l / white
                if (norm > 1f) clipped++
                val mapped = softKnee(norm, opt.knee)
                val scale = if (l > 1e-6f) mapped / l else 0f
                out[i * 3] = hr * scale
                out[i * 3 + 1] = hg * scale
                out[i * 3 + 2] = hb * scale
                i++
            }
        }

        // 6) 收尾：线性 → sRGB →（sRGB 域）S 曲线 + 饱和度 → 抖动 → 8bit
        val rng = Random(0) // 固定种子：同参数输出确定性（Golden-file 对照前提）
        val result = IntArray(n)
        i = 0
        while (i < n) {
            var r = clamp01(out[i * 3])
            var g = clamp01(out[i * 3 + 1])
            var b = clamp01(out[i * 3 + 2])

            // LTM 路径的裁切率按输出亮度统计；与 Python 一致：先 clip 再算亮度
            // （LTM 归一化的是亮度而非单通道，个别通道仍可能超 1）
            if (ltmParams != null) {
                val l = 0.2126f * r + 0.7152f * g + 0.0722f * b
                if (l / white > 1f) clipped++
            }

            // 转 sRGB（感知域）
            r = linToSrgb(r)
            g = linToSrgb(g)
            b = linToSrgb(b)

            // 对比与饱和度必须在 sRGB（感知）域做（见文件头注释 5）
            if (opt.contrast != 1f) {
                r = clamp01((r - 0.5f) * opt.contrast + 0.5f)
                g = clamp01((g - 0.5f) * opt.contrast + 0.5f)
                b = clamp01((b - 0.5f) * opt.contrast + 0.5f)
            }
            if (opt.saturation != 1f) {
                val l2 = 0.2126f * r + 0.7152f * g + 0.0722f * b
                r = clamp01(l2 + (r - l2) * opt.saturation)
                g = clamp01(l2 + (g - l2) * opt.saturation)
                b = clamp01(l2 + (b - l2) * opt.saturation)
            }

            // 三角分布抖动（±0.5 LSB）：量化前打散台阶，抑制平滑渐变 banding
            var vr = r * 255f + 0.5f
            var vg = g * 255f + 0.5f
            var vb = b * 255f + 0.5f
            if (opt.dither) {
                vr += (rng.nextFloat() - rng.nextFloat()) * 0.5f
                vg += (rng.nextFloat() - rng.nextFloat()) * 0.5f
                vb += (rng.nextFloat() - rng.nextFloat()) * 0.5f
            }
            val ri = vr.toInt().coerceIn(0, 255)
            val gi = vg.toInt().coerceIn(0, 255)
            val bi = vb.toInt().coerceIn(0, 255)
            result[i] = (0xFF shl 24) or (ri shl 16) or (gi shl 8) or bi
            i++
        }

        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        bmp.setPixels(result, 0, w, 0, 0, w, h)

        val stats = Stats(
            outSize = w to h,
            hdrPeak = peak,
            whitePoint = white,
            clippedRatio = clipped.toFloat() / n,
            maxBoost = Math.pow(2.0, maxLog2.toDouble()).toFloat(),
            profile = opt.profile,
            ltmRadiusPx = ltmStats?.radiusPx ?: 0,
            ltmDetailGain = ltmStats?.detailGain ?: 0f,
            ltmShadowLift = ltmStats?.shadowLift ?: 1f,
        )
        return bmp to stats
    }

    private fun clamp01(v: Float) = if (v < 0f) 0f else if (v > 1f) 1f else v

    private fun linToSrgb(v: Float): Float =
        LIN_TO_SRGB[(clamp01(v) * 1023f).toInt().coerceIn(0, 1023)]

    /** knee 以下线性，以上用指数曲线有界压缩到 1.0。 */
    private fun softKnee(x: Float, knee: Float): Float {
        if (knee >= 1f) return if (x > 1f) 1f else x
        if (x <= knee) return if (x < 0f) 0f else x
        val span = 1f - knee
        return knee + span * (1f - Math.exp((-(x - knee) / span).toDouble()).toFloat())
    }
}
