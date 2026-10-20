package com.finn.hdr

import android.graphics.Bitmap
import android.graphics.Matrix
import android.graphics.Rect
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * 主图 / 增益图的同步几何变换。
 *
 * 铁律：增益图**必须**由原始增益图重新采样得到，绝不允许从已缩放的主图反推。
 * 所有坐标都用「主图坐标 → 归一化 → 增益图坐标」映射，保证视场一致。
 */
object GainMapGeometry {

    data class Geometry(val baseW: Int, val baseH: Int, val gmW: Int, val gmH: Int) {
        val scaleX: Double get() = gmW.toDouble() / baseW
        val scaleY: Double get() = gmH.toDouble() / baseH
        fun consistent(tol: Double = 0.02) = abs(scaleX - scaleY) <= tol
        override fun toString() = "base=${baseW}x$baseH gm=${gmW}x$gmH " +
            "scale=(${"%.4f".format(scaleX)},${"%.4f".format(scaleY)})"
    }

    data class TransformResult(
        val base: Bitmap,
        val gainMap: Bitmap,
        val plan: Plan,
    )

    data class Plan(
        val op: String,
        val baseSize: Pair<Int, Int>,
        val gainMapSize: Pair<Int, Int>,
        val scale: Double = 1.0,
        val warnings: MutableList<String> = mutableListOf(),
    )

    fun of(base: Bitmap, gm: Bitmap) = Geometry(base.width, base.height, gm.width, gm.height)

    fun requireConsistent(base: Bitmap, gm: Bitmap, tol: Double = 0.02): Geometry {
        val g = of(base, gm)
        require(g.consistent(tol)) { "主图/增益图比例不一致（$g），几何变换会错位" }
        return g
    }

    /** 预压缩：最长边缩到 longEdge，只缩不放。 */
    fun resize(base: Bitmap, gm: Bitmap, longEdge: Int): TransformResult {
        val g = requireConsistent(base, gm)
        val cur = max(base.width, base.height)
        if (cur <= longEdge) {
            val plan = Plan("resize_noop", base.width to base.height, gm.width to gm.height)
            plan.warnings += "最长边已 <= $longEdge，跳过缩放以避免无意义重编码"
            return TransformResult(base, gm, plan)
        }
        val k = longEdge.toDouble() / cur
        val bw = max(1, (base.width * k).roundToInt())
        val bh = max(1, (base.height * k).roundToInt())
        val gw = max(1, (bw * g.scaleX).roundToInt())
        val gh = max(1, (bh * g.scaleY).roundToInt())

        val base2 = scale(base, bw, bh)
        val gm2 = scale(gm, gw, gh)
        val plan = Plan("resize", base2.width to base2.height, gm2.width to gm2.height, k)
        val g2 = of(base2, gm2)
        if (!g2.consistent()) plan.warnings += "缩放后比例漂移：$g2（原 $g）"
        return TransformResult(base2, gm2, plan)
    }

    /** 裁剪：box 为主图坐标系。 */
    fun crop(base: Bitmap, gm: Bitmap, box: Rect): TransformResult {
        val g = requireConsistent(base, gm)
        require(box.left >= 0 && box.top >= 0 && box.right <= base.width && box.bottom <= base.height) {
            "裁剪框越界：$box vs ${base.width}x${base.height}"
        }
        val gmBox = Rect(
            (box.left * g.scaleX).roundToInt().coerceIn(0, gm.width - 1),
            (box.top * g.scaleY).roundToInt().coerceIn(0, gm.height - 1),
            (box.right * g.scaleX).roundToInt().coerceIn(1, gm.width),
            (box.bottom * g.scaleY).roundToInt().coerceIn(1, gm.height),
        )
        val base2 = Bitmap.createBitmap(base, box.left, box.top, box.width(), box.height())
        val gm2 = Bitmap.createBitmap(gm, gmBox.left, gmBox.top, gmBox.width(), gmBox.height())
        val plan = Plan("crop", base2.width to base2.height, gm2.width to gm2.height)
        val g2 = of(base2, gm2)
        if (!g2.consistent(0.03)) plan.warnings += "裁剪后比例漂移：$g2"
        return TransformResult(base2, gm2, plan)
    }

    /** 旋转：仅 90° 整数倍。 */
    fun rotate(base: Bitmap, gm: Bitmap, degrees: Int): TransformResult {
        require(degrees % 90 == 0) { "只支持 90° 整数倍，收到 $degrees" }
        val d = ((degrees % 360) + 360) % 360
        if (d == 0) return TransformResult(base, gm, Plan("rotate_noop", base.width to base.height, gm.width to gm.height))
        val base2 = rotateBitmap(base, d)
        val gm2 = rotateBitmap(gm, d)
        return TransformResult(base2, gm2, Plan("rotate", base2.width to base2.height, gm2.width to gm2.height))
    }

    private fun scale(src: Bitmap, w: Int, h: Int): Bitmap {
        // filter=true：满足规范「双线性或更好」的采样要求
        return Bitmap.createScaledBitmap(src, w, h, true)
    }

    private fun rotateBitmap(src: Bitmap, degrees: Int): Bitmap {
        val m = Matrix().apply { postRotate(degrees.toFloat()) }
        return Bitmap.createBitmap(src, 0, 0, src.width, src.height, m, true)
    }
}
