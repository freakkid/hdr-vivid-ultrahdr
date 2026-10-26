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

    private val SRGB_TO_LIN = FloatArray(4096) { i ->
        val v = i / 4095f
        if (v <= 0.04045f) v / 12.92f else Math.pow(((v + 0.055) / 1.055).toDouble(), 2.4).toFloat()
    }

    private val LIN_TO_SRGB = FloatArray(4096) { i ->
        val v = i / 4095f
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
     * HDR 观感曲线（仅 display 档）。与 Python `bake.HDR_CURVE` 逐项对齐。
     *
     * 参数不是调出来的，是**从用户手机截图反解**的：他把「有 HDR 效果」与「平淡 SDR」
     * 两版放在手机相册「图片对比」里，两格逐像素配准后（相关系数 1.0000，纯色调差），
     * 按源亮度分档统计 stop 差，得到：
     *   L≈8 −0.34 stop / L≈55 −0.01（枢轴）/ L≈120 +0.01 / L≈232 +0.12，饱和 x1.045
     * 同一结论独立出现在增益图真值分析里：真正超过 SDR 白点的像素只有 13~23%，
     * 所以「HDR 效果」是**对比扩张**，不是整体提亮 —— 中位亮度必须原地不动。
     *
     * delta(v) = −toeAmp × (1 − smoothstep(0, toeEnd, v))
     *            + shoulderAmp × smoothstep(shoulderStart, 0.95, v)
     * out = in × 2^delta        （v = sRGB 码值 0~1）
     *
     * shoulderAmp 取 0：实测做不到「高光 +8%」与「保层次」并存 —— 源图高光已占
     * 241~255，抬 +0.06 stop 就需靠天花板压缩，hl_spread 0.50→0.21；抬 +0.20
     * 时掉到 0.08（即用户骂过的「层次少」）。高光那点亮度改由 LTM detailGain 带来。
     */
    private const val TOE_MAX = 0.52f
    private const val TOE_END = 0.30f
    private const val LIFT_MAX = 0.18f
    private const val LIFT_EPS = 0.06f
    private const val SAT_MAX = 0.14f
    private const val REF_EXCESS = 1.3f
    private const val HEADROOM_CEILING = 0.95f
    private const val HEADROOM_SPAN = 0.25f
    private const val CURVE_CEIL_KNEE = 0.97f
    private const val CURVE_CEIL = 1.0f

    /**
     * 整张图的「HDR 含量」→ (scale∈[0,1], excess 的 P99)。
     *
     * excess = max(HDR 亮度 − 1, 0)：只有这部分是 SDR 图**物理上表现不出来**的内容。
     * 用 P99 而非 max：单个镜面高光不该决定整图剂量。与 Python
     * `bake.hdr_content_scale` 同算法（np.percentile 默认线性插值）。
     */
    private fun hdrContentScale(lum: FloatArray, n: Int): Pair<Float, Float> {
        var cnt = 0
        for (i in 0 until n) if (lum[i] - 1f > 1e-4f) cnt++
        if (cnt == 0) return 0f to 0f
        val v = FloatArray(cnt)
        var j = 0
        for (i in 0 until n) {
            val e = lum[i] - 1f
            if (e > 1e-4f) v[j++] = e
        }
        java.util.Arrays.sort(v)
        val pos = (cnt - 1) * 0.99
        val lo = pos.toInt().coerceIn(0, cnt - 1)
        val hi = (lo + 1).coerceAtMost(cnt - 1)
        val s = v[lo] + (v[hi] - v[lo]) * (pos - lo).toFloat()
        return s.coerceAtMost(REF_EXCESS) / REF_EXCESS to s
    }

    /**
     * 逐像素提亮量（stop），由**该像素自己的增益数据**决定，并受头顶空间约束。
     *
     * 两条都满足才发剂量：① 该像素 HDR 真超过 SDR 白点（增益低的地方一分不给）；
     * ② 8bit 里还有余量（code 越接近 HEADROOM_CEILING 余量越小）。
     * 没有余量还硬提只会把它推进 255 —— 实测 A@0.70 不给闸门时 hl_spread 0.50→0.17。
     */
    private fun hdrLiftStops(lum: FloatArray, sdrLum: FloatArray, n: Int,
                             scale: Float, s: Float): FloatArray {
        val out = FloatArray(n)
        if (scale <= 0f || s <= 0f) return out
        val denom = Math.log1p((s / LIFT_EPS).toDouble()).toFloat().coerceAtLeast(1e-6f)
        var i = 0
        while (i < n) {
            val ex = (lum[i] - 1f).coerceAtLeast(0f)
            if (ex > 0f) {
                val norm = Math.log1p((ex / LIFT_EPS).toDouble()).toFloat() / denom
                val code = linToSrgbExact(sdrLum[i].coerceAtLeast(1e-7f))
                val headroom = ((HEADROOM_CEILING - code) / HEADROOM_SPAN).coerceIn(0f, 1f)
                out[i] = LIFT_MAX * scale * norm * headroom
            }
            i++
        }
        return out
    }

    /** 暗部让位量（负 stop）：只有这张图确实有超白内容时才压暗部。 */
    private fun hdrToeStops(sdrLum: FloatArray, n: Int, scale: Float): FloatArray {
        val out = FloatArray(n)
        if (scale <= 0f) return out
        var i = 0
        while (i < n) {
            val code = linToSrgbExact(sdrLum[i].coerceAtLeast(1e-7f))
            out[i] = -TOE_MAX * scale * (1f - smoothstep(0f, TOE_END, code))
            i++
        }
        return out
    }

    /**
     * 施加总剂量：按亮度乘 2^delta（线性域，才等于「亮 k 倍」），高光端软收敛。
     * 天花板判据落在码值域（人眼与直方图都是码值）。与 Python `_apply_hdr_curve` 同序。
     */
    private fun applyHdrCurve(out: FloatArray, lift: FloatArray, toe: FloatArray, n: Int) {
        var i = 0
        while (i < n) {
            val lr = out[i * 3]
            val lg = out[i * 3 + 1]
            val lb = out[i * 3 + 2]
            val lum = 0.2126f * lr + 0.7152f * lg + 0.0722f * lb
            if (lum > 1e-7f) {
                val gain = Math.pow(2.0, (lift[i] + toe[i]).toDouble()).toFloat()
                val liftedCode = softKnee(linToSrgbExact(lum * gain), CURVE_CEIL_KNEE, CURVE_CEIL)
                val scale = srgbToLinExact(liftedCode) / lum
                out[i * 3] = lr * scale
                out[i * 3 + 1] = lg * scale
                out[i * 3 + 2] = lb * scale
            }
            i++
        }
    }

    private fun linToSrgbExact(v: Float): Float {
        val x = clamp01(v)
        return if (x <= 0.0031308f) x * 12.92f
        else (1.055f * Math.pow(x.toDouble(), 1.0 / 2.4) - 0.055f).toFloat()
    }

    private fun srgbToLinExact(v: Float): Float {
        val x = clamp01(v)
        return if (x <= 0.04045f) x / 12.92f
        else Math.pow(((x + 0.055f) / 1.055f).toDouble(), 2.4).toFloat()
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
            val lr = SRGB_TO_LIN[(((p shr 16) and 0xFF) * 4095) / 255]
            val lg = SRGB_TO_LIN[(((p shr 8) and 0xFF) * 4095) / 255]
            val lb = SRGB_TO_LIN[((p and 0xFF) * 4095) / 255]

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
        val useCurve = targetModeForProfile(opt.profile) == "display"
        // 显示档的剂量全部由增益数据推出（见 hdrLiftStops 注释），sdr 档一律 0。
        var curveLift = FloatArray(0)
        var curveToe = FloatArray(0)
        var curveScale = 0f
        if (useCurve) {
            val (sc, sv) = hdrContentScale(lum, n)
            curveScale = sc
            curveLift = hdrLiftStops(lum, sdrLum, n, sc, sv)
            curveToe = hdrToeStops(sdrLum, n, sc)
        }

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
                val wgt = smoothstep(opt.blendStart, opt.blendEnd, sdrLum[i])
                val blended = sdrLum[i] * (1f - wgt) + ml * wgt
                // 增量按 strength 缩放：0 → 完全等同源 SDR；1 → 完整 HDR 效果。
                // display 与 sdr 的差别只在之后那条曲线（applyHdrCurve），
                // 底色映射同一套 —— 这样「中间调不动」两种模式都成立。
                val target = minOf(
                    sdrLum[i] + (blended - sdrLum[i]) * amount,
                    sdrLum[i] * liftCap,
                )
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

        // 5.5) HDR 观感曲线（display 档）：在底色映射之后、量化之前做 stop 域对比扩张。
        //      暗部更黑、饱和稍足；高光段当前取 0（见 hdrCurveDelta 注释）。
        if (useCurve) applyHdrCurve(out, curveLift, curveToe, n)

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
            val satEff = opt.saturation * (1f + SAT_MAX * curveScale)
            if (satEff != 1f) {
                val l2 = 0.2126f * r + 0.7152f * g + 0.0722f * b
                r = clamp01(l2 + (r - l2) * satEff)
                g = clamp01(l2 + (g - l2) * satEff)
                b = clamp01(l2 + (b - l2) * satEff)
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
            targetMode = if (useCurve) "display" else "sdr",
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
        LIN_TO_SRGB[(clamp01(v) * 4095f).toInt().coerceIn(0, 4095)]

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
