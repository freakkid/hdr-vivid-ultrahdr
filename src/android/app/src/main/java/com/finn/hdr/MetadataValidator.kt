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

    private val XMP_SIG = (TAG_XMP + "\u0000").toByteArray(Charsets.US_ASCII)
    private val MPF_SIG = (TAG_MPF + "\u0000").toByteArray(Charsets.US_ASCII)

    fun validate(jpeg: ByteArray): Report {
        val checks = mutableListOf<Check>()

        val images = scanImages(jpeg)
        checks += Check("two_images", images.size >= 2, "检测到 ${images.size} 个 JPEG 图像")

        val segs = headerSegments(jpeg, 0, images.firstOrNull()?.second ?: jpeg.size)
        // 必须按段内签名挑，不能取「第一个 APP1」：相机文件通常 EXIF 在 XMP 之前，
        // 取第一个 APP1 会把完全正确的文件判成「缺 XMP」。APP2 同理（ISO 21496-1 也是 APP2）。
        val xmpSeg = segs.firstOrNull { it.marker == 0xE1 && hasSig(jpeg, it, XMP_SIG) }
        val mpfSeg = segs.firstOrNull { it.marker == 0xE2 && hasSig(jpeg, it, MPF_SIG) }

        val xmpText = xmpSeg?.let {
            String(jpeg, it.payloadStart + XMP_SIG.size, it.segEnd - it.payloadStart - XMP_SIG.size)
        }
        checks += Check("xmp_present", xmpText?.contains("Container:Directory") == true, "XMP 含 Container:Directory")

        val hasPrimary = xmpText?.let { Regex("Item:Semantic=\"Primary\"").containsMatchIn(it) } == true
        val hasGainMap = xmpText?.let { Regex("Item:Semantic=\"GainMap\"").containsMatchIn(it) } == true
        checks += Check("xmp_primary_item", hasPrimary)
        checks += Check("xmp_gainmap_item", hasGainMap)

        // 设计约定：主图只放 hdrgm:Version 信号位，完整参数写在增益图自身 XMP。
        // 若在这里要求主图含 GainMapMax/Gamma，必然误报失败。
        val primaryVersionOk = xmpText?.contains("hdrgm:Version") == true
        checks += Check("primary_version", primaryVersionOk, "主图含 hdrgm:Version 信号位")

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
            // 增益图自身必须携带完整 hdrgm 参数（规范把参数放在增益图 XMP 里）
            val gmXmp = xmpTextAt(jpeg, abs)
            val required = listOf("GainMapMin", "GainMapMax", "Gamma",
                "OffsetSDR", "OffsetHDR", "HDRCapacityMin", "HDRCapacityMax")
            val missing = if (gmXmp == null) required else required.filter { !gmXmp.contains("hdrgm:$it") }
            checks += Check("gainmap_xmp_present", missing.isEmpty(),
                when {
                    gmXmp == null -> "增益图缺少 XMP APP1"
                    missing.isEmpty() -> "7 个 hdrgm 字段齐全"
                    else -> "缺失字段: ${missing.joinToString(",")}"
                })

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

    /** 段净荷是否以指定签名开头（用于区分 APP1 的 EXIF / XMP、APP2 的 MPF / ISO）。 */
    private fun hasSig(b: ByteArray, seg: Seg, sig: ByteArray): Boolean {
        if (seg.segEnd - seg.payloadStart < sig.size) return false
        for (i in sig.indices) if (b[seg.payloadStart + i] != sig[i]) return false
        return true
    }

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
        while (pos + 1 < b.size) {
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
        // 同 HdrSourceParser：EOI 可能在文件最后 2 字节，边界必须用 pos+1
        while (pos + 1 < b.size) {
            if (b[pos] != 0xFF.toByte()) { pos++; continue }
            val m = b[pos + 1].toInt() and 0xFF
            if (m == 0xD9) return pos + 2
            if (pos + 3 >= b.size) return null
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

    /** 从指定图像起点的头部段里提取 XMP 文本（找不到返回 null）。 */
    private fun xmpTextAt(b: ByteArray, start: Int): String? {
        val sig = "http://ns.adobe.com/xap/1.0/\u0000".toByteArray(Charsets.US_ASCII)
        var pos = start + 2
        while (pos + 3 < b.size) {
            if (b[pos] != 0xFF.toByte()) return null
            val m = b[pos + 1].toInt() and 0xFF
            if (m == 0xD8) { pos += 2; continue }
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            if (len < 2) return null
            val segEnd = pos + 2 + len
            if (segEnd > b.size) return null
            if (m == 0xE1 && segEnd - (pos + 4) > sig.size) {
                var hit = true
                for (i in sig.indices) if (b[pos + 4 + i] != sig[i]) { hit = false; break }
                if (hit) {
                    return String(b, pos + 4 + sig.size, segEnd - pos - 4 - sig.size, Charsets.UTF_8)
                }
            }
            if (m == 0xDA) return null
            pos = segEnd
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
