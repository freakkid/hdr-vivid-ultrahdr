package com.finn.hdr

import android.graphics.Bitmap

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
 *   4. 以高分位亮度为白点 → 归一化 → 高光软膝压缩
 *   5. S 曲线提对比 + 饱和度补偿 → 线性 → sRGB
 */
object HdrBaker {

    data class Options(
        val longEdge: Int = 2048,
        val strength: Float = 0.70f,     // 增益强度，1.0 = 完全按元数据恢复
        val whitePercentile: Float = 0.995f,
        val knee: Float = 0.75f,
        val saturation: Float = 1.12f,
        val contrast: Float = 1.06f,
    )

    data class Stats(
        val outSize: Pair<Int, Int>,
        val hdrPeak: Float,
        val whitePoint: Float,
        val clippedRatio: Float,
        val maxBoost: Float,
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

        // 3) 合成 HDR（RGB 三个 float 分量）+ 亮度直方图
        val hdr = FloatArray(n * 3)
        val hist = IntArray(1024)
        var peak = 0f

        var i = 0
        while (i < n) {
            val p = px[i]
            val lr = SRGB_TO_LIN[(((p shr 16) and 0xFF) * 1023) / 255]
            val lg = SRGB_TO_LIN[(((p shr 8) and 0xFF) * 1023) / 255]
            val lb = SRGB_TO_LIN[((p and 0xFF) * 1023) / 255]

            // 增益图取灰度（三通道同值时取任意一个）
            val gmv = ((gmPx[i] shr 16) and 0xFF) / 255f
            val gain = Math.pow(2.0, (gmv * span + minLog2 * opt.strength).toDouble()).toFloat()

            val hr = Math.max((lr + offSdr) * gain - offHdr, 0f)
            val hg = Math.max((lg + offSdr) * gain - offHdr, 0f)
            val hb = Math.max((lb + offSdr) * gain - offHdr, 0f)

            hdr[i * 3] = hr
            hdr[i * 3 + 1] = hg
            hdr[i * 3 + 2] = hb

            val lum = 0.2126f * hr + 0.7152f * hg + 0.0722f * hb
            if (lum > peak) peak = lum
            hist[(lum.coerceIn(0f, 1f) * 1023).toInt()]++
            i++
        }

        // 4) 白点：按亮度直方图取分位
        val target = (n * opt.whitePercentile).toInt()
        var acc = 0
        var white = 1f
        for (bIdx in hist.indices) {
            acc += hist[bIdx]
            if (acc >= target) { white = maxOf(bIdx / 1023f, 1e-3f); break }
        }
        // 直方图被 1.0 截断，峰值超过 1 时用峰值作为下限参考
        if (peak > 1f && white >= 0.999f) white = peak * 0.85f

        // 5) 色调映射 + 写回
        val out = IntArray(n)
        var clipped = 0
        i = 0
        while (i < n) {
            val hr = hdr[i * 3]
            val hg = hdr[i * 3 + 1]
            val hb = hdr[i * 3 + 2]
            val lum = 0.2126f * hr + 0.7152f * hg + 0.0722f * hb
            val norm = lum / white
            if (norm > 1f) clipped++

            val mapped = softKnee(norm, opt.knee)
            val scale = if (lum > 1e-6f) mapped / lum else 0f

            var r = clamp01(hr * scale)
            var g = clamp01(hg * scale)
            var b = clamp01(hb * scale)

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

            val ri = (linToSrgb(r) * 255f + 0.5f).toInt().coerceIn(0, 255)
            val gi = (linToSrgb(g) * 255f + 0.5f).toInt().coerceIn(0, 255)
            val bi = (linToSrgb(b) * 255f + 0.5f).toInt().coerceIn(0, 255)
            out[i] = (0xFF shl 24) or (ri shl 16) or (gi shl 8) or bi
            i++
        }

        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        bmp.setPixels(out, 0, w, 0, 0, w, h)

        val stats = Stats(
            outSize = w to h,
            hdrPeak = peak,
            whitePoint = white,
            clippedRatio = clipped.toFloat() / n,
            maxBoost = Math.pow(2.0, maxLog2.toDouble()).toFloat(),
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
