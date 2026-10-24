import android.graphics.Bitmap
import com.finn.hdr.HdrBaker
import com.finn.hdr.UltraHdrNative
import java.awt.image.BufferedImage
import java.io.File
import javax.imageio.ImageIO

/**
 * 在 JVM 上执行真实的 `HdrBaker`，用于和 Python `bake.py` 做数值对照。
 *
 * 用法：
 *   java -jar parity.jar <base.png> <gain.png> <out.png> <profile> <strength> \
 *        <minLog2> <maxLog2> <gamma> <offSdr> <offHdr> <longEdge>
 *
 * 输入输出都用 PNG（无损），避免 JPEG 解码差异污染对照结果。
 */
fun main(args: Array<String>) {
    require(args.size >= 11) { "参数不足：见文件头注释" }
    val base = readPng(File(args[0]))
    val gain = readPng(File(args[1]))
    val profile = args[3]
    val strength = args[4].toFloat()
    val meta = UltraHdrNative.GainMapMeta(
        minContentBoost = args[5].toFloat(),
        maxContentBoost = args[6].toFloat(),
        gamma = args[7].toFloat(),
        offsetSdr = args[8].toFloat(),
        offsetHdr = args[9].toFloat(),
        hdrCapacityMin = 0f,
        hdrCapacityMax = 0f,
    )
    val (out, stats) = HdrBaker.bake(
        base, gain, meta,
        HdrBaker.Options(
            longEdge = args[10].toInt(),
            strength = strength,
            profile = profile,
        ),
    )
    writePng(out, File(args[2]))
    println(
        "meanLift=${stats.meanLift} hdrPeak=${stats.hdrPeak} clipped=${stats.clippedRatio} " +
            "liftCap=${stats.liftCap} size=${stats.outSize.first}x${stats.outSize.second}"
    )
}

private fun readPng(f: File): Bitmap {
    val img = ImageIO.read(f)
    requireNotNull(img) { "读不出图片：$f" }
    val w = img.width
    val h = img.height
    val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
    val px = IntArray(w * h)
    img.getRGB(0, 0, w, h, px, 0, w)
    bmp.setPixels(px, 0, w, 0, 0, w, h)
    return bmp
}

private fun writePng(bmp: Bitmap, f: File) {
    val img = BufferedImage(bmp.width, bmp.height, BufferedImage.TYPE_INT_ARGB)
    val px = IntArray(bmp.width * bmp.height)
    bmp.getPixels(px, 0, bmp.width, 0, 0, bmp.width, bmp.height)
    img.setRGB(0, 0, bmp.width, bmp.height, px, 0, bmp.width)
    ImageIO.write(img, "png", f)
}
