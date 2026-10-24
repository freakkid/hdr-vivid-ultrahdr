package com.finn.hdr

import kotlin.math.exp
import kotlin.math.log2
import kotlin.math.max
import kotlin.math.min

/**
 * 局部色调映射（Local Tone Mapping, LTM）—— Python 侧 src/python/uhdr/ltm.py 的 Kotlin 移植。
 *
 * 为什么需要它：纯全局映射（归一化 + 软膝 + S 曲线）只能把 HDR 的亮度范围压回 SDR，
 * 画面会「高光不爆但整体平、暗部糊成一片」。人眼感知的「HDR 感」主要来自局部对比度
 * 而非绝对亮度——SDR 屏幕物理亮度上限无法突破，能改的只有同一画面内相邻区域的亮度差。
 *
 * 做法：对数域 base/detail 分解
 * ```
 * x      = log2(L / white)
 * base   = guidedFilter(x)     // 保边平滑，只留大尺度亮度分布
 * detail = x - base            // 局部细节（纹理、边缘、局部明暗差）
 * base   = compressHighlights(base * rangeCompress)  // 先向白点收缩，再软膝压高光
 * x'     = base + detail * detailGain                // 局部对比增强——「HDR 感」来源
 * L'     = (exp2(x') - blackPoint) / (1 - blackPoint) // 黑位重锚，消除灰雾
 * out    = hdr * (L' / lum)                          // 按亮度比例缩放 RGB，保色相
 * ```
 *
 * 两个容易写错的点（Python 侧都踩过，移植时逐条对齐）：
 *  1. 高光软膝必须收敛到 0（白点）：k + s*(1 - e^(-(x-k)/s))，符号写反会把
 *     高光压得比中间调还黑；
 *  2. 顺序必须是「先 base 收缩、后压高光」。反序会让被压缩的高光被 rangeCompress
 *     再次提起，导致白点漂移。
 *
 * guided filter 用可分离 box blur（积分图等价滑动窗口）实现，O(1)/像素；
 * 单帧 2048x1536 预计 200-400 ms（中端 ARM）。
 */
object LocalToneMapping {

    /** 局部色调映射参数（与 Python LtmParams 同名字段、同默认值）。 */
    data class Params(
        /** guided filter 半径（相对短边），越大越不易出 halo，但越慢 */
        val radiusFrac: Float = 1f / 48f,
        /**
         * 保边阈值（log2 亮度方差单位）。标定值 0.2（2026-10-21 由 4e-3 上调，
         * 依据 docs/04 §8.5 实测）：eps 必须与真实纹理的 log2 方差（~0.5-1.0）
         * 同量级，否则 a=var/(var+eps)≈1，guided filter 退化为恒等、detail≈0，
         * range_compress 压掉纹理本身而 detail_gain 空转。上调后真实样张细节
         * 指标全面改善且强边缘振铃低于 off 档（无可测 halo）；合成「平坦+硬
         * 台阶」最坏情况下仍有 ~15% 台阶高过冲。
         */
        val eps: Float = 0.2f,
        /** base 压缩系数，1.0 = 不压缩 */
        val rangeCompress: Float = 0.88f,
        /** detail 增益，1.0 = 不变 */
        val detailGain: Float = 1.32f,
        /** 黑位重锚（线性归一化亮度） */
        val blackPoint: Float = 0.0020f,
        /** 高光软膝起点（线性亮度，1.0 = SDR 白点），1.0 = 不压 */
        val highlightStart: Float = 0.78f,
    )

    /** 按使用场景返回参数；null 表示不启用 LTM（保持纯全局映射的旧行为）。 */
    fun paramsForProfile(profile: String): Params? = when (profile) {
        "off" -> null
        "wechat" -> Params(
            // 微信会重压缩：局部对比别做太狠，否则压缩后出块状伪影
            radiusFrac = 1f / 40f,
            eps = 0.15f,
            rangeCompress = 0.92f,
            detailGain = 1.12f,
            blackPoint = 0.0025f,
            highlightStart = 0.72f,
        )
        else -> Params() // local：观感优先
    }

    /** LTM 统计（用于日志与「两端同参」对照）。 */
    data class Stats(
        val radiusPx: Int,
        val detailGain: Float,
        val rangeCompress: Float,
        /** 暗部（x < -4）平均亮度提升倍数 */
        val shadowLift: Float,
    )

    /** [apply] 的返回值：显示线性域结果 + 统计（不用全局可变状态，避免并发烘焙串数据）。 */
    data class Result(val out: FloatArray, val stats: Stats)

    /**
     * 把线性 HDR 压到显示线性域（1.0 = SDR 白点）。
     *
     * 内存画像（2048x1536 = 3.1 MP，FloatArray 4 B/元素）：调用方 hdr 37.7 MB +
     * lum 12.6 MB 常驻；本函数峰值另需约 100 MB（x / base / guided filter
     * 中转 / xOut / 结果）。off 档不走本函数，峰值维持旧水平约 90 MB。
     *
     * @param hdr   w*h*3 交错的线性光 float（1.0 近似 SDR 白）
     * @param lum   w*h 的亮度（与 hdr 同索引），调用方已算好可复用
     * @param white 归一化白点（sdr-white 锚定为 1.0）
     * @return 显示线性域结果（交错 RGB，是否截断到 1.0 由调用方决定）+ 统计
     */
    fun apply(
        hdr: FloatArray,
        lum: FloatArray,
        w: Int,
        h: Int,
        white: Float,
        params: Params,
        /** 高光上限（线性）。不取 1.0：顶到 1.0 会让大片高光变纯白、丢层次。 */
        ceiling: Float = 0.96f,
    ): Result {
        val n = w * h
        val invWhite = 1f / max(white, 1e-6f)
        val ceilingLog = log2(max(ceiling, 0.5f))

        // x = log2(max(lum/white, 1e-5))
        val x = FloatArray(n)
        var i = 0
        while (i < n) {
            x[i] = log2(max(lum[i] * invWhite, 1e-5f))
            i++
        }

        val r = max(2, Math.round(min(w, h) * params.radiusFrac))
        val base = guidedFilter(x, w, h, r, params.eps)

        // base 先收缩再压高光（顺序不能反，见文件头注释 2）
        val xOut = FloatArray(n)
        var shadowSumX = 0.0
        var shadowSumOut = 0.0
        var shadowCount = 0
        i = 0
        while (i < n) {
            val b = compressHighlights(base[i] * params.rangeCompress, params.highlightStart)
            val o = min(b + (x[i] - base[i]) * params.detailGain, ceilingLog)
            xOut[i] = o
            if (x[i] < -4.0) { // 暗部统计：平均提升倍数
                shadowSumX += x[i]
                shadowSumOut += o
                shadowCount++
            }
            i++
        }

        // L' = exp2(x')，再黑位重锚。x 此时已死（xOut 已建成），原地改写节省 12.6 MB
        val lOut = x
        val bp = params.blackPoint
        val inv1bp = 1f / (1f - bp)
        i = 0
        while (i < n) {
            val l = (pow2(xOut[i]) - bp) * inv1bp
            lOut[i] = if (l < 0f) 0f else if (l > 1f) 1f else l
            i++
        }

        // out = hdr * (L'/lum)，lum≈0 处退化为 0
        val out = FloatArray(n * 3)
        i = 0
        while (i < n) {
            val scale = if (lum[i] > 1e-7f) lOut[i] / max(lum[i], 1e-7f) else 0f
            out[i * 3] = hdr[i * 3] * scale
            out[i * 3 + 1] = hdr[i * 3 + 1] * scale
            out[i * 3 + 2] = hdr[i * 3 + 2] * scale
            i++
        }

        val lift = if (shadowCount > 0) pow2(((shadowSumOut - shadowSumX) / shadowCount).toFloat())
        else 1f
        return Result(out, Stats(r, params.detailGain, params.rangeCompress, lift))
    }

    /**
     * 自引导滤波（self-guided）：保边平滑。
     *
     * a = var / (var + eps)——方差大的区域（强边缘）a→1，滤波退化为恒等，
     * 因此边缘不被模糊，从根上减少 halo。
     */
    private fun guidedFilter(guide: FloatArray, w: Int, h: Int, r: Int, eps: Float): FloatArray {
        val n = w * h
        val meanI = boxBlur(guide, w, h, r)
        val ii = FloatArray(n)
        var i = 0
        while (i < n) {
            val g = guide[i]
            ii[i] = g * g
            i++
        }
        val meanIi = boxBlur(ii, w, h, r)

        // a = var/(var+eps) 原地写回 meanIi；b = meanI - a*meanI 原地写回 meanI
        // （少分配两个 n 数组，3.1 MP 时省 25 MB 峰值）
        i = 0
        while (i < n) {
            val m = meanI[i]
            val v = max(meanIi[i] - m * m, 0f)
            val av = v / (v + eps)
            meanIi[i] = av
            meanI[i] = m - av * m
            i++
        }
        val meanA = boxBlur(meanIi, w, h, r)   // 输入即 a
        val meanB = boxBlur(meanI, w, h, r)    // 输入即 b
        val out = FloatArray(n)
        i = 0
        while (i < n) {
            out[i] = meanA[i] * guide[i] + meanB[i]
            i++
        }
        return out
    }

    /**
     * 盒式模糊：可分离滑动窗口（积分图等价），O(1)/像素，边界用 edge 复制。
     * 与 Python _box_blur（pad edge + 双 cumsum）数值等价。
     */
    private fun boxBlur(src: FloatArray, w: Int, h: Int, r: Int): FloatArray {
        if (r <= 0) return src.copyOf()
        val win = 2 * r + 1
        val tmp = FloatArray(w * h)
        // 水平 pass
        var y = 0
        while (y < h) {
            val off = y * w
            var sum = 0f
            var k = -r
            while (k <= r) {
                sum += src[off + clamp(k, 0, w - 1)]
                k++
            }
            var x = 0
            while (x < w) {
                tmp[off + x] = sum / win
                sum += src[off + clamp(x + r + 1, 0, w - 1)] -
                    src[off + clamp(x - r, 0, w - 1)]
                x++
            }
            y++
        }
        // 垂直 pass
        val out = FloatArray(w * h)
        var x = 0
        while (x < w) {
            var sum = 0f
            var k = -r
            while (k <= r) {
                sum += tmp[clamp(k, 0, h - 1) * w + x]
                k++
            }
            var yy = 0
            while (yy < h) {
                out[yy * w + x] = sum / win
                sum += tmp[clamp(yy + r + 1, 0, h - 1) * w + x] -
                    tmp[clamp(yy - r, 0, h - 1) * w + x]
                yy++
            }
            x++
        }
        return out
    }

    /**
     * log2 域高光软膝：start 以下不动作，以上单调收敛到 0（= 白点）。
     *
     * x_out = k + s*(1 - e^(-(x-k)/s))，k = log2(start)（负值），s = -k。
     * 在 x = k 处一阶导连续，不会出现可见折线接缝。
     */
    private fun compressHighlights(x: Float, startLinear: Float): Float {
        val k = log2(max(startLinear, 1e-4f))
        if (k >= 0f) return min(x, 0f)
        if (x <= k) return x
        val s = -k
        val over = x - k
        return k + s * (1f - exp(-over / s))
    }

    private fun pow2(x: Float): Float = Math.pow(2.0, x.toDouble()).toFloat()

    private fun clamp(v: Int, lo: Int, hi: Int) = if (v < lo) lo else if (v > hi) hi else v
}
