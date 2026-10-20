package com.finn.hdr

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * HDR 源图解析：把「华为 HDR Vivid 静图」拆成 SDR 主图 + 增益图 + 元数据。
 *
 * 双通道策略：
 *  1. 优先 libultrahdr probe（对 ISO Ultra HDR / 标准 MPF 最可靠）
 *  2. 回退自研 CUVA 扫描（华为老机型：副图以 APP5 起始 + EXIF Make=HUAWEI/_cuva）
 *
 * 注意：CUVA 的增益图语义与 hdrgm 并不等价，直接搬运会引入亮度/色度偏差。
 * 本类的输出因此保留 [HdrSource.needsBoostRecompute] 标记，由导出层决定走
 * 「直接封装」还是「反解 HDR → 重算增益图」。
 */
object HdrSourceParser {

    private const val M_SOI = 0xFF
    private const val APP0 = 0xE0
    private const val APP1 = 0xE1
    private const val APP2 = 0xE2
    private const val APP5 = 0xE5
    private const val SOS = 0xDA
    private const val EOI = 0xD9

    enum class Container { CUVA, ISO_ULTRA_HDR, PLAIN_JPEG, UNKNOWN_MPO }

    data class HdrSource(
        val container: Container,
        val sdrJpeg: ByteArray,
        val gainMapJpeg: ByteArray?,
        val meta: UltraHdrNative.GainMapMeta?,
        val exifMake: String?,
        val needsBoostRecompute: Boolean,
        val notes: MutableList<String> = mutableListOf(),
    ) {
        val hasGainMap: Boolean get() = gainMapJpeg != null
    }

    /** 图像在字节流中的区间。 */
    private data class Range(val start: Int, val end: Int)

    fun parse(bytes: ByteArray): HdrSource {
        val notes = mutableListOf<String>()

        // ---- 通道 1：libultrahdr probe（native 不可用时静默跳过）----
        val (probeOk, probeMeta) = try {
            UltraHdrNative.probe(bytes)
        } catch (t: Throwable) {
            notes += "native probe 不可用（${t.javaClass.simpleName}），仅用本地扫描"
            false to null
        }
        val ranges = findImages(bytes)
        if (ranges.isEmpty()) throw IllegalArgumentException("不是合法 JPEG")

        val sdr = bytes.copyOfRange(ranges[0].start, ranges[0].end)
        val make = parseExifMake(bytes, ranges[0])

        if (probeOk && ranges.size >= 2) {
            val gm = bytes.copyOfRange(ranges[1].start, ranges[1].end)
            notes += "probe 成功：按 ISO Ultra HDR 处理"
            return HdrSource(Container.ISO_ULTRA_HDR, sdr, gm, probeMeta, make,
                needsBoostRecompute = false, notes = notes)
        }

        if (ranges.size < 2) {
            notes += "单图 JPEG，无增益图"
            return HdrSource(Container.PLAIN_JPEG, sdr, null, null, make,
                needsBoostRecompute = false, notes = notes)
        }

        // ---- 通道 2：CUVA 扫描 ----
        val second = ranges[1]
        val firstMarker = firstAppMarker(bytes, second.start)
        if (firstMarker == APP5) {
            notes += "副图首段为 APP5(0xFFE5)，判定 CUVA / 华为 HDR Vivid"
            notes += "CUVA 增益图需换算为 hdrgm 语义，建议反解 HDR 后重算增益图"
            return HdrSource(Container.CUVA, sdr, stripApp5(bytes, second), null, make,
                needsBoostRecompute = true, notes = notes)
        }

        if (make?.uppercase()?.startsWith("HUAWEI") == true || containsCuvaTag(bytes)) {
            notes += "通过 EXIF/字节标记判定 CUVA，但副图缺少 APP5"
            return HdrSource(Container.CUVA, sdr, stripApp5(bytes, second), null, make,
                needsBoostRecompute = true, notes = notes)
        }

        notes += "存在副图但未识别容器类型，按未知 MPO 处理（不生成 HDR 元数据）"
        return HdrSource(Container.UNKNOWN_MPO, sdr, stripApp5(bytes, second), null, make,
            needsBoostRecompute = false, notes = notes)
    }

    // ---------------- JPEG 结构扫描 ----------------

    private fun findImages(b: ByteArray): List<Range> {
        val out = mutableListOf<Range>()
        var pos = 0
        while (pos + 2 < b.size) {
            if (b[pos] == 0xFF.toByte() && b[pos + 1] == 0xD8.toByte()) {
                val end = imageEnd(b, pos) ?: break
                out += Range(pos, end)
                pos = end
            } else {
                pos++
            }
        }
        return out
    }

    private fun imageEnd(b: ByteArray, start: Int): Int? {
        var pos = start + 2
        while (pos + 3 < b.size) {
            if (b[pos] != 0xFF.toByte()) { pos++; continue }
            val m = b[pos + 1].toInt() and 0xFF
            if (m == EOI) return pos + 2
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            if (len < 2) return null
            if (m == SOS) {
                pos += 2 + len
                while (pos + 1 < b.size) {
                    if (b[pos] == 0xFF.toByte()) {
                        val n = b[pos + 1].toInt() and 0xFF
                        if (n != 0x00 && n !in 0xD0..0xD7) break
                        pos += 2
                        continue
                    }
                    pos++
                }
                continue
            }
            pos += 2 + len
        }
        return null
    }

    private fun firstAppMarker(b: ByteArray, start: Int): Int {
        var pos = start + 2
        while (pos + 3 < b.size) {
            if (b[pos] != 0xFF.toByte()) return -1
            val m = b[pos + 1].toInt() and 0xFF
            if (m in 0xE0..0xEF) return m
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            pos += 2 + len
        }
        return -1
    }

    /** 去掉副图自带的 APP5 私有段，输出干净 JPEG。 */
    private fun stripApp5(b: ByteArray, r: Range): ByteArray {
        val out = java.io.ByteArrayOutputStream(r.end - r.start)
        out.write(b, r.start, 2)
        var pos = r.start + 2
        while (pos + 3 < r.end) {
            if (b[pos] != 0xFF.toByte()) break
            val m = b[pos + 1].toInt() and 0xFF
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            val segEnd = pos + 2 + len
            if (m == SOS) { out.write(b, pos, r.end - pos); return out.toByteArray() }
            if (m != APP5) out.write(b, pos, segEnd - pos)
            pos = segEnd
        }
        return out.toByteArray()
    }

    private fun parseExifMake(b: ByteArray, r: Range): String? {
        var pos = r.start + 2
        while (pos + 3 < r.end) {
            if (b[pos] != 0xFF.toByte()) return null
            val m = b[pos + 1].toInt() and 0xFF
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            if (m == APP1) {
                val p = pos + 4
                if (p + 6 < b.size && b[p] == 'E'.code.toByte() && b[p + 1] == 'x'.code.toByte()) {
                    return readTiffMake(b, p + 6, pos + 2 + len)
                }
            }
            if (m == SOS) return null
            pos += 2 + len
        }
        return null
    }

    private fun readTiffMake(b: ByteArray, tiff: Int, limit: Int): String? {
        if (tiff + 8 > limit) return null
        val little = b[tiff] == 'I'.code.toByte()
        val order = if (little) ByteOrder.LITTLE_ENDIAN else ByteOrder.BIG_ENDIAN
        // slice()：让 position=0 对齐 TIFF 起点，之后所有索引都是 TIFF 内相对偏移
        val buf = ByteBuffer.wrap(b, tiff, limit - tiff).slice().order(order)
        val ifdOff = buf.getInt(4)
        if (ifdOff <= 0 || ifdOff + 2 > buf.limit()) return null
        val count = buf.getShort(ifdOff).toInt() and 0xFFFF
        for (i in 0 until count) {
            val base = ifdOff + 2 + i * 12
            if (base + 12 > buf.limit()) return null
            if ((buf.getShort(base).toInt() and 0xFFFF) != 0x010F) continue
            val n = buf.getInt(base + 4)
            val off = buf.getInt(base + 8)
            if (off + n > buf.limit() || n <= 0) return null
            return String(b, tiff + off, n).trim('\u0000', ' ')
        }
        return null
    }

    private fun containsCuvaTag(b: ByteArray): Boolean {
        val needle = "_cuva".toByteArray(Charsets.US_ASCII)
        val limit = minOf(b.size - needle.size, 262144)
        for (i in 0 until limit) {
            var hit = true
            for (j in needle.indices) if (b[i + j] != needle[j]) { hit = false; break }
            if (hit) return true
        }
        return false
    }
}
