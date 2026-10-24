import com.finn.hdr.HeifGridRewriter
import java.io.File

/**
 * 在 JVM 上执行真实的 `HeifGridRewriter`（纯 Kotlin，无 Android 依赖）。
 *
 * 用法：
 *   java -jar heif.jar <in.heic>                       # 打摘要
 *   java -jar heif.jar <in.heic> --rewrap <id> <out>   # 重封指定 grid
 */
fun main(args: Array<String>) {
    require(args.isNotEmpty()) { "用法见文件头注释" }
    val data = File(args[0]).readBytes()
    if (args.size >= 4 && args[1] == "--rewrap") {
        val blob = HeifGridRewriter.rewrap(data, args[2].toInt())
            ?: error("重封失败（见 summary）")
        File(args[3]).writeBytes(blob)
        println("rewrapped=${blob.size}")
    } else {
        println(HeifGridRewriter.summary(data))
    }
}
