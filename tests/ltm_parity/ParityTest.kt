import com.finn.hdr.LocalToneMapping

import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * LocalToneMapping 双实现对照：读 gen_input.py 生成的输入（小端 float32），
 * 跑真实的 LocalToneMapping.apply，输出小端 float32 结果与统计。
 *
 * 编译运行（由 run_parity.sh 编排；kotlinc 缺失时整体跳过）：
 *   kotlinc ParityTest.kt <repo>/src/android/app/src/main/java/com/finn/hdr/LocalToneMapping.kt \
 *     -include-runtime -d parity.jar
 *   java -jar parity.jar local
 */
fun main(args: Array<String>) {
    val tag = args[0]
    val bytes = File(args[1]).readBytes()
    val bb = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
    val w = bb.int
    val h = bb.int
    val white = bb.float
    val p = FloatArray(6) { bb.float }
    val n = w * h
    val hdr = FloatArray(n * 3) { bb.float }
    val lum = FloatArray(n) { bb.float }
    require(bb.remaining() == 0) { "输入文件长度不匹配，剩余 ${bb.remaining()} B" }

    val params = LocalToneMapping.Params(
        radiusFrac = p[0], eps = p[1], rangeCompress = p[2],
        detailGain = p[3], blackPoint = p[4], highlightStart = p[5],
    )
    val (out, stats) = LocalToneMapping.apply(hdr, lum, w, h, white, params)

    val ob = ByteBuffer.allocate(out.size * 4).order(ByteOrder.LITTLE_ENDIAN)
    for (v in out) ob.putFloat(v)
    File(args[2]).writeBytes(ob.array())
    File(args[3]).writeText(
        "${stats.radiusPx} ${stats.detailGain} ${stats.rangeCompress} ${stats.shadowLift}")
}
