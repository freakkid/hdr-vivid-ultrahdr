package com.finn.hdr

import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.abs

/**
 * 导出后自检：确认「元数据确实写进去了」而不是「看起来像 HDR」。
 *
 * 一旦这里 ERROR，说明文件退化为纯 SDR，UI 必须降级提示，
 * 不能继续对用户宣称「已保留 HDR」。
 */
object MetadataValidator {

    data class Check(val id: String, val ok: Boolean, val detail: String = "")

    data class Report(val checks: List<Check>) {
        val errors: List<Check> get() = checks.filter { !it.ok }
        val ok: Boolean get() = errors.isEmpty()
        fun brief(): String = if (ok) "全部通过" else errors.joinToString("; ") { "${it.id}: ${it.detail}" }
    }

    private const val TAG_XMP = "http://ns.adobe.com/xap/1.0/"
    private const val TAG_MPF = "MPF"

    fun validate(jpeg: ByteArray): Report {
        val checks = mutableListOf<Check>()

        val images = scanImages(jpeg)
        checks += Check("two_images", images.size >= 2, "检测到 ${images.size} 个 JPEG 图像")

        val segs = headerSegments(jpeg, 0, images.firstOrNull()?.second ?: jpeg.size)
        val xmpSeg = segs.firstOrNull { it.marker == 0xE1 }
        val mpfSeg = segs.firstOrNull { it.marker == 0xE2 }

        val xmpText = xmpSeg?.let {
            String(jpeg, it.payloadStart + TAG_XMP.length + 1, it.segEnd - it.payloadStart - TAG_XMP.length - 1)
        }
        checks += Check("xmp_present", xmpText?.contains("Container:Directory") == true, "XMP 含 Container:Directory")

        val hasPrimary = xmpText?.let { Regex("Item:Semantic=\"Primary\"").containsMatchIn(it) } == true
        val hasGainMap = xmpText?.let { Regex("Item:Semantic=\"GainMap\"").containsMatchIn(it) } == true
        checks += Check("xmp_primary_item", hasPrimary)
        checks += Check("xmp_gainmap_item", hasGainMap)

        val hdrgmOk = xmpText?.let {
            it.contains("hdrgm:Version") && it.contains("hdrgm:GainMapMax") && it.contains("hdrgm:Gamma")
        } == true
        checks += Check("xmp_hdrgm", hdrgmOk, "含 hdrgm:Version/GainMapMax/Gamma")

        if (mpfSeg == null) {
            checks += Check("mpf_present", false, "缺少 MPF APP2")
            return Report(checks)
        }
        val entries = parseMpf(jpeg, mpfSeg.payloadStart, mpfSeg.segEnd)
        checks += Check("mpf_present", entries.size >= 2, "MPF entry 数=${entries.size}")
        if (entries.size < 2) return Report(checks)

        val tiffBase = mpfSeg.payloadStart + 4
        val e = entries[1]
        val abs = tiffBase + e.offset
        val atSoi = abs > 0 && abs + 1 < jpeg.size &&
            jpeg[abs] == 0xFF.toByte() && jpeg[abs + 1] == 0xD8.toByte()
        checks += Check("mpf_points_to_soi", atSoi, "增益图绝对偏移=$abs")

        if (atSoi) {
            val baseSz = sofSize(jpeg, 0)
            val gmSz = sofSize(jpeg, abs)
            if (baseSz != null && gmSz != null) {
                val rw = gmSz.first.toDouble() / baseSz.first
                val rh = gmSz.second.toDouble() / baseSz.second
                checks += Check("aspect_consistent", abs(rw - rh) <= 0.02,
                    "宽比=%.4f 高比=%.4f".format(rw, rh))
                checks += Check("gm_not_larger", gmSz.first <= baseSz.first && gmSz.second <= baseSz.second,
                    "主图=${baseSz.first}x${baseSz.second} 增益图=${gmSz.first}x${gmSz.second}")
            }
        }
        return Report(checks)
    }

    // ---------------- 结构扫描 ----------------

    private data class Seg(val marker: Int, val payloadStart: Int, val segEnd: Int)

    private fun headerSegments(b: ByteArray, start: Int, end: Int): List<Seg> {
        val out = mutableListOf<Seg>()
        var pos = start + 2
        while (pos + 3 < end) {
            if (b[pos] != 0xFF.toByte()) break
            val m = b[pos + 1].toInt() and 0xFF
            if (m == 0xD8) { pos += 2; continue }
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            val segEnd = pos + 2 + len
            if (len < 2 || segEnd > end) break
            if (m in 0xE0..0xEF) out += Seg(m, pos + 4, segEnd)
            if (m == 0xDA) break
            pos = segEnd
        }
        return out
    }

    private fun scanImages(b: ByteArray): List<Pair<Int, Int>> {
        val out = mutableListOf<Pair<Int, Int>>()
        var pos = 0
        while (pos + 2 < b.size) {
            if (b[pos] == 0xFF.toByte() && b[pos + 1] == 0xD8.toByte()) {
                val end = imageEnd(b, pos) ?: break
                out += pos to end
                pos = end
            } else pos++
        }
        return out
    }

    private fun imageEnd(b: ByteArray, start: Int): Int? {
        var pos = start + 2
        while (pos + 3 < b.size) {
            if (b[pos] != 0xFF.toByte()) { pos++; continue }
            val m = b[pos + 1].toInt() and 0xFF
            if (m == 0xD9) return pos + 2
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            if (len < 2) return null
            if (m == 0xDA) {
                pos += 2 + len
                while (pos + 1 < b.size) {
                    if (b[pos] == 0xFF.toByte()) {
                        val n = b[pos + 1].toInt() and 0xFF
                        if (n != 0x00 && n !in 0xD0..0xD7) break
                        pos += 2; continue
                    }
                    pos++
                }
                continue
            }
            pos += 2 + len
        }
        return null
    }

    private data class MpEntry(val offset: Int, val size: Int)

    private fun parseMpf(b: ByteArray, payloadStart: Int, segEnd: Int): List<MpEntry> {
        if (payloadStart + 8 > segEnd) return emptyList()
        val tiff = payloadStart + 4
        if (b[tiff] != 'M'.code.toByte()) return emptyList()
        // slice()：position 对齐 TIFF 起点，索引即 TIFF 内相对偏移
        val buf = ByteBuffer.wrap(b, tiff, segEnd - tiff).slice().order(ByteOrder.BIG_ENDIAN)
        val ifdOff = buf.getInt(4)
        if (ifdOff <= 0 || ifdOff + 2 > buf.limit()) return emptyList()
        val count = buf.getShort(ifdOff).toInt() and 0xFFFF
        for (i in 0 until count) {
            val base = ifdOff + 2 + i * 12
            if (base + 12 > buf.limit()) return emptyList()
            if ((buf.getShort(base).toInt() and 0xFFFF) != 0xB002) continue
            val n = buf.getInt(base + 4)
            val off = buf.getInt(base + 8)
            val list = mutableListOf<MpEntry>()
            for (k in 0 until n / 16) {
                val p = off + k * 16
                if (p + 16 > buf.limit()) break
                list += MpEntry(buf.getInt(p + 8), buf.getInt(p + 4))
            }
            return list
        }
        return emptyList()
    }

    private fun sofSize(b: ByteArray, start: Int): Pair<Int, Int>? {
        var pos = start + 2
        while (pos + 9 < b.size) {
            if (b[pos] != 0xFF.toByte()) return null
            val m = b[pos + 1].toInt() and 0xFF
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            if (m in 0xC0..0xCF && m != 0xC4 && m != 0xC8 && m != 0xCC) {
                val h = ((b[pos + 5].toInt() and 0xFF) shl 8) or (b[pos + 6].toInt() and 0xFF)
                val w = ((b[pos + 7].toInt() and 0xFF) shl 8) or (b[pos + 8].toInt() and 0xFF)
                return w to h
            }
            if (m == 0xDA) return null
            pos += 2 + len
        }
        return null
    }
}
