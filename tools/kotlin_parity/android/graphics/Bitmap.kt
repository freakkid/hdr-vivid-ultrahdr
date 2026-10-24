package android.graphics

/**
 * 极简 `android.graphics.Bitmap` 桩，只为在 JVM 上执行 `HdrBaker` 的像素运算。
 *
 * 只实现 HdrBaker 真正用到的 API：width/height、createBitmap、createScaledBitmap、
 * getPixels、setPixels、Config.ARGB_8888。
 *
 * **不要拿它当 Android 行为参考**：真实设备上的缩放在解码器里完成，颜色空间也可能
 * 不同。做数值对照时请让两侧输入同尺寸，避开缩放路径。
 */
class Bitmap private constructor(
    val width: Int,
    val height: Int,
    internal val pixels: IntArray,
) {
    enum class Config { ARGB_8888 }

    fun getPixels(out: IntArray, offset: Int, stride: Int, x: Int, y: Int, w: Int, h: Int) {
        for (row in 0 until h) {
            val src = (y + row) * width + x
            val dst = offset + row * stride
            System.arraycopy(pixels, src, out, dst, w)
        }
    }

    fun setPixels(src: IntArray, offset: Int, stride: Int, x: Int, y: Int, w: Int, h: Int) {
        for (row in 0 until h) {
            val dst = (y + row) * width + x
            val s = offset + row * stride
            System.arraycopy(src, s, pixels, dst, w)
        }
    }

    fun recycle() {}

    companion object {
        fun createBitmap(w: Int, h: Int, config: Config): Bitmap =
            Bitmap(maxOf(1, w), maxOf(1, h), IntArray(maxOf(1, w) * maxOf(1, h)))

        /** 双线性缩放（与 Android `filter=true` 的常见实现近似）。 */
        fun createScaledBitmap(src: Bitmap, w: Int, h: Int, filter: Boolean): Bitmap {
            val out = createBitmap(w, h, Config.ARGB_8888)
            if (w == src.width && h == src.height) {
                System.arraycopy(src.pixels, 0, out.pixels, 0, src.pixels.size)
                return out
            }
            for (y in 0 until h) {
                val fy = (y + 0.5f) * src.height / h - 0.5f
                val y0 = fy.toInt().coerceIn(0, src.height - 1)
                val y1 = (y0 + 1).coerceAtMost(src.height - 1)
                val wy = (fy - y0).coerceIn(0f, 1f)
                for (x in 0 until w) {
                    val fx = (x + 0.5f) * src.width / w - 0.5f
                    val x0 = fx.toInt().coerceIn(0, src.width - 1)
                    val x1 = (x0 + 1).coerceAtMost(src.width - 1)
                    val wx = (fx - x0).coerceIn(0f, 1f)
                    val p00 = src.pixels[y0 * src.width + x0]
                    val p10 = src.pixels[y0 * src.width + x1]
                    val p01 = src.pixels[y1 * src.width + x0]
                    val p11 = src.pixels[y1 * src.width + x1]
                    var argb = 0xFF shl 24
                    for (shift in intArrayOf(16, 8, 0)) {
                        val c00 = (p00 shr shift) and 0xFF
                        val c10 = (p10 shr shift) and 0xFF
                        val c01 = (p01 shr shift) and 0xFF
                        val c11 = (p11 shr shift) and 0xFF
                        val top = c00 + (c10 - c00) * wx
                        val bottom = c01 + (c11 - c01) * wx
                        val v = (top + (bottom - top) * wy).toInt().coerceIn(0, 255)
                        argb = argb or (v shl shift)
                    }
                    out.pixels[y * w + x] = argb
                }
            }
            return out
        }
    }
}
