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
 *   4. 色调映射（两种目标模式，见 [targetModeForProfile]）：
 *      - display（local / wechat，出货默认）：目标即「HDR 映射结果本身」，
 *        按 strength 在 log 亮度域向 SDR 底图几何插值
 *      - sdr（off / faithful，旧行为）：按 sdrLum 分流混合 + 增量缩放 + liftCap，
 *        把结果锚回底图亮度，用于 A/B 对照与「不提亮」逃生阀
 *   5. S 曲线 + 饱和度补偿 —— **必须在 sRGB（感知）域做**。放在线性域会把
 *      0.5 以下亮度强力压向 0（线性 0.5 ≈ sRGB 188），暗部被钉死成纯黑，
 *      这是 Python 侧 P0-1 修过的 bug，Kotlin 侧同步修正。
 *   6. 三角分布抖动（固定种子）→ 8bit 量化，抑制平滑渐变 banding
 *
 * 第 4 步是成败关键。2026-10-25 深夜之前全链默认 sdr 锚定：验收指标全部对比
 * SDR 底图，把烘焙图死死摁在平坦底图上 —— 实测中间调与底图只差 ±1 灰阶，
 * 用户装机后观感是「灰灰的、没效果」。而用户眼睛的参照物是手机上的 HDR 显示
 * （整体更亮、高光耀眼），两个目标根本不同。因此出货档（local/wechat）改用
 * display 目标：让烘焙图真正带上 HDR 的亮度结构，验收基准也随之改为
 * 「可见增强 + 自然护栏」（tests/acceptance/accept.py）。
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
        /** 线性对比度拉伸。1.0 = 关：它会把接近白的高光推成纯白（实测纯白占比超源 3.6 个百分点）。 */
        val contrast: Float = 1.0f,
        /**
         * 高光加权区间（线性亮度）。低于 blendStart 完全保持 SDR，高于 blendEnd 完全采信 HDR。
         *
         * **仅 sdr 锚定路径（off / faithful）使用**，见 [targetModeForProfile]。
         * 出货档 local / wechat 已改用 display 目标（log 域几何插值），不再按亮度分流 ——
         * 分流假设「手机显示 HDR 时中间调与 SDR 一致」，但用户的参照物是手机上更亮的
         * HDR 显示效果，分流把 HDR 增量摁死后观感就是「灰灰的、没效果」。
         */
        val blendStart: Float = 0.35f,
        val blendEnd: Float = 0.90f,
        /**
         * 单像素相对源 SDR 的最大提亮倍数（安全垫，防止 LTM 把中高光推成大片接近白）。
         * 实测依据：192~223 这档被推 +8.5% 就能被肉眼察觉。
         *
         * **仅 sdr 锚定路径使用**；display 路径的目标天然 ≤1（LTM ceiling 钳制 +
         * 几何插值 ≤ sdr^(1−strength)），不需要逐像素封顶。
         *
         * 注意：**它不负责「强度」**。strength 的可见性靠下面那条「增量按 strength 缩放」，
         * 因为色调映射会把 HDR 增量压掉，只靠增益乘 strength 时 0.3 与 1.0 的预览几乎一样。
         */
        val maxLift: Float = 1.25f,
        val dither: Boolean = true,
    )

    data class Stats(
        val outSize: Pair<Int, Int>,
        val hdrPeak: Float,
        val whitePoint: Float,
        val clippedRatio: Float,
        val maxBoost: Float,
        /** 实际生效的提亮上限（faithful 1.05，其余 1.25）。**仅 sdr 目标模式使用** */
        val liftCap: Float = 1f,
        /** 本次生效的色调映射目标模式：display（出货档）/ sdr（off / faithful 旧行为） */
        val targetMode: String = "sdr",
        /** 输出平均亮度 / 源 SDR 平均亮度 —— 用来一眼看出 strength 是否真的起作用 */
        val meanLift: Float = 1f,
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

    /**
     * profile → 色调映射目标模式。与 Python `bake.target_mode_for_profile` 同源同义。
     *
     * display = 显示导向：目标即 LTM/全局映射结果本身，按 strength 在 log 亮度域
     *           向 SDR 底图几何插值（strength 0 → 等同底图，1 → 完整映射结果）。
     *           local / wechat 走这条 —— 烘焙图要带的是手机上 HDR 显示的亮度结构，
     *           不是平坦的 SDR 底图。
     * sdr     = 底图锚定（旧行为）：按 sdrLum 分流混合 + 增量缩放 + liftCap。
     *           off 是 A/B 对照旧行为，faithful 是「嫌亮」用户的不提亮逃生阀，
     *           两档都明确承诺「不改变整体明暗」，必须留在旧路径。
     */
    fun targetModeForProfile(profile: String): String =
        if (profile == "off" || profile == "faithful") "sdr" else "display"

    /**
     * 显示导向目标：log 亮度域把 SDR 底图按 amount 几何插值到 HDR 映射结果。
     *
     *     target = exp(ln sdr + amount × (ln mapped − ln sdr))
     *
     * 为什么用几何（log 域）插值而不是线性：
     *  1. 亮度是乘性感知量。线性插值按绝对差值分配，暗部的 HDR 增量绝对值小、
     *     会被摁死；几何插值按比例缩放，暗部也拿到成比例的提亮；
     *  2. amount=0 精确回到底图亮度（strength 滑杆底端 = 无效果），
     *     amount=1 精确等于映射结果 —— 两端行为可预期，滑杆语义即「HDR 剂量」；
     *  3. 映射结果 ≤1（LTM ceiling 钳制），插值结果 ≤ sdr^(1−amount) ≤ 1，
     *     天然不会过曝，不需要逐像素 liftCap。
     *
     * 纯黑像素（源亮度 ≤1e-6，即 sRGB 0）保持 0：LTM 的局部提亮会把纯黑邻域
     * 一起抬起（夜模式式），几何插值会把它们保留成灰黑，这里显式摁回纯黑，
     * 与 sdr 路径（smoothstep 在 0 处权重为 0，黑位不变）行为一致。
     *
     * 试过并否决：超白像素豁免收缩（直接采信映射结果）。实测 hl_spread 从 0.50
     * 掉到 0.17~0.29 —— 豁免后输出全跟随肩部压缩区间（传递函数斜率仅 0.15），
     * 底图锚定带来的源变化反而更多。几何插值本身已是高光区保层次的最优形式。
     */
    internal fun displayTarget(sdrLum: Float, mappedLum: Float, amount: Float): Float {
        if (sdrLum <= 1e-6f) return 0f
        val logSdr = Math.log(maxOf(sdrLum, 1e-6f).toDouble())
        val logMapped = Math.log(maxOf(mappedLum, 1e-6f).toDouble())
        return Math.exp(logSdr + amount.toDouble() * (logMapped - logSdr)).toFloat()
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

        // 3) 合成 HDR（RGB 三个 float 分量）+ 亮度 + 直方图 + SDR 基准亮度
        val hdr = FloatArray(n * 3)
        val lum = FloatArray(n)
        val sdrLum = FloatArray(n)   // 基准：SDR 主图亮度，决定哪里才采信 HDR
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

            sdrLum[i] = 0.2126f * lr + 0.7152f * lg + 0.0722f * lb
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
        //    关键：按 SDR 亮度加权。SDR 暗部/中间调保持原样，只有亮部才采信 HDR 结果，
        //    否则增益图（全图有值）会把整幅画面提亮 —— 观感与手机上的 HDR 不一致。
        val ltmParams = LocalToneMapping.paramsForProfile(opt.profile)
        var ltmStats: LocalToneMapping.Stats? = null
        val out: FloatArray
        var clipped = 0
        // 安全垫：逐像素最多比源 SDR 亮 maxLift 倍。faithful 档压到 1.05（几乎不提亮）。
        val liftCap = if (opt.profile == "faithful") minOf(opt.maxLift, 1.05f) else opt.maxLift
        // strength 作用在「HDR 相对 SDR 的增量」上：不这么做的话，色调映射会把增益差异
        // 压平，0.30 与 1.00 的预览几乎一致（真机反馈「强度不生效」的根因）。
        val amount = opt.strength.coerceIn(0f, 1f)
        // 目标模式：出货档走 display（几何插值，带 HDR 亮度结构），
        // off/faithful 走 sdr（锚回底图，旧行为）。见 targetModeForProfile。
        val useDisplayTarget = targetModeForProfile(opt.profile) == "display"
        if (ltmParams != null) {
            val ltmResult = LocalToneMapping.apply(hdr, lum, w, h, white, ltmParams)
            val mapped = ltmResult.out
            ltmStats = ltmResult.stats
            out = FloatArray(n * 3)
            i = 0
            while (i < n) {
                // 与 Python 对齐：bake.py 先把 LTM 输出 clip 到 [0,1] 再算亮度，
                // 这里若不裁剪，饱和高光处两端会算出不同亮度（Kotlin 偏亮）。
                val mr = clamp01(mapped[i * 3])
                val mg = clamp01(mapped[i * 3 + 1])
                val mb = clamp01(mapped[i * 3 + 2])
                val ml = 0.2126f * mr + 0.7152f * mg + 0.0722f * mb
                val target: Float
                if (useDisplayTarget) {
                    // 出货档（local / wechat）：目标即 HDR 映射结果，按 strength 几何插值。
                    // 不做 liftCap —— 几何插值结果天然 ≤ sdr^(1−amount) ≤ 1。
                    target = displayTarget(sdrLum[i], ml, amount)
                } else {
                    val wgt = smoothstep(opt.blendStart, opt.blendEnd, sdrLum[i])
                    val blended = sdrLum[i] * (1f - wgt) + ml * wgt
                    // 增量按 strength 缩放：0 → 完全等同源 SDR；1 → 完整 HDR 效果
                    target = minOf(
                        sdrLum[i] + (blended - sdrLum[i]) * amount,
                        sdrLum[i] * liftCap,
                    )
                }
                val scale = if (ml > 1e-6f) target / ml else 0f
                val rr = mr * scale
                val gg = mg * scale
                val bb = mb * scale
                // 按单通道计裁切：亮度归一化后个别通道仍会 >1，用亮度判会恒为 0
                if (rr > 1f || gg > 1f || bb > 1f) clipped++
                out[i * 3] = rr
                out[i * 3 + 1] = gg
                out[i * 3 + 2] = bb
                i++
            }
        } else {
            out = FloatArray(n * 3)
            i = 0
            while (i < n) {
                val l = lum[i]
                val norm = l / white
                val mapped = softKnee(norm, opt.knee)
                val wgt = smoothstep(opt.blendStart, opt.blendEnd, sdrLum[i])
                val blended = sdrLum[i] * (1f - wgt) + mapped * wgt
                val target = minOf(
                    sdrLum[i] + (blended - sdrLum[i]) * amount,
                    sdrLum[i] * liftCap,
                )
                val scale = if (l > 1e-6f) target / l else 0f
                val rr = hdr[i * 3] * scale
                val gg = hdr[i * 3 + 1] * scale
                val bb = hdr[i * 3 + 2] * scale
                if (rr > 1f || gg > 1f || bb > 1f) clipped++
                out[i * 3] = rr
                out[i * 3 + 1] = gg
                out[i * 3 + 2] = bb
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

            // 裁切率已在上面按单通道统计（压缩前 >1 即算），此处不再重复计数

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

        var sumOut = 0.0
        var sumSdr = 0.0
        i = 0
        while (i < n) {
            sumOut += 0.2126f * out[i * 3] + 0.7152f * out[i * 3 + 1] + 0.0722f * out[i * 3 + 2]
            sumSdr += sdrLum[i]
            i++
        }
        val meanLift = if (sumSdr > 1e-9) (sumOut / sumSdr).toFloat() else 1f

        val stats = Stats(
            outSize = w to h,
            hdrPeak = peak,
            whitePoint = white,
            clippedRatio = clipped.toFloat() / n,
            maxBoost = Math.pow(2.0, maxLog2.toDouble()).toFloat(),
            liftCap = liftCap,
            targetMode = if (useDisplayTarget) "display" else "sdr",
            meanLift = meanLift,
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

    /**
     * knee 以下线性，以上用指数曲线压缩到 ceiling。
     *
     * ceiling 不取 1.0 是有意的：收敛到 1.0 时 0.9~1.0 会被挤在 0.99+，
     * 表现为大片纯白、高光丢层次。留 4% 余量后高光重新拉开差距。
     */
    private fun softKnee(x: Float, knee: Float, ceiling: Float = 0.96f): Float {
        if (knee >= 1f) return if (x > ceiling) ceiling else x
        if (x <= knee) return if (x < 0f) 0f else x
        val span = 1f - knee
        return knee + (ceiling - knee) * (1f - Math.exp((-(x - knee) / span).toDouble()).toFloat())
    }

    /** 平滑阶跃：暗部 0（保持 SDR），亮部 1（采信 HDR），中间平滑过渡。 */
    private fun smoothstep(e0: Float, e1: Float, x: Float): Float {
        val t = ((x - e0) / maxOf(e1 - e0, 1e-6f)).coerceIn(0f, 1f)
        return t * t * (3f - 2f * t)
    }
}
