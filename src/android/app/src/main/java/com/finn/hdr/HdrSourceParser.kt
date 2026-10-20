package com.finn.hdr

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * HDR 源图解析：ISO Ultra HDR（XMP 或 ISO 21496-1 两种载体）与华为 CUVA。
 *
 * 判定优先级（高 → 低），不允许靠猜：
 *  1. 主图带 APP2 `urn:iso:std:iso:ts:21496:-1`  → ISO Ultra HDR（ISO 载体）
 *  2. libultrahdr probe 成功                     → ISO Ultra HDR
 *  3. 主图带 XMP `<Container:Directory>`          → ISO Ultra HDR（XMP 载体）
 *  4. 副图首段为 APP5(0xFFE5)                     → CUVA
 *  5. EXIF Make=HUAWEI 且含 `_cuva`（弱证据）      → CUVA，需标注置信度低
 *
 * 实测教训：华为产出的文件可以只有 ISO 21496-1、完全没有 XMP，同时 EXIF 里
 * 仍保留 `_cuva` 字样。若把「含 _cuva」当强证据，就会把 ISO Ultra HDR 误判成
 * CUVA，用户侧表现为「传输把增益图弄丢了」——实际文件完好，是解析器认错。
 */
object HdrSourceParser {

    private const val APP1 = 0xE1
    private const val APP2 = 0xE2
    private const val APP5 = 0xE5
    private const val SOS = 0xDA
    private const val EOI = 0xD9

    private val XMP_SIG = "http://ns.adobe.com/xap/1.0/\u0000".toByteArray(Charsets.US_ASCII)

    enum class Container { CUVA, ISO_ULTRA_HDR, PLAIN_JPEG, UNKNOWN_MPO }

    data class HdrSource(
        val container: Container,
        val sdrJpeg: ByteArray,
        val gainMapJpeg: ByteArray?,
        val meta: UltraHdrNative.GainMapMeta?,
        val exifMake: String?,
        val needsBoostRecompute: Boolean,
        val notes: MutableList<String> = mutableListOf(),
        val isoMeta: IsoGainMap.Meta? = null,
    ) {
        val hasGainMap: Boolean get() = gainMapJpeg != null
        val metadataCarrier: String
            get() = when {
                isoMeta != null -> "ISO 21496-1"
                meta != null -> "XMP"
                else -> "无"
            }
    }

    private data class Range(val start: Int, val end: Int)

    private class PrimaryScan {
        var xmpStart = -1
        var xmpEnd = -1
        var isoStart = -1
        var isoEnd = -1
        var hasMpf = false
    }

    fun parse(bytes: ByteArray): HdrSource {
        val notes = mutableListOf<String>()
        val ranges = findImages(bytes)
        if (ranges.isEmpty()) throw IllegalArgumentException("不是合法 JPEG")

        val sdr = bytes.copyOfRange(ranges[0].start, ranges[0].end)
        val make = parseExifMake(bytes, ranges[0])
        val scan = scanPrimary(bytes, ranges[0])

        if (ranges.size < 2) {
            notes += "单图 JPEG，无增益图：只能输出纯 SDR"
            return HdrSource(Container.PLAIN_JPEG, sdr, null, null, make, false, notes)
        }

        val second = ranges[1]
        val gmRaw = bytes.copyOfRange(second.start, second.end)

        // ---- 证据 1：ISO 21496-1 APP2（最强，优先于 probe 与 _cuva）----
        if (scan.isoStart >= 0) {
            var iso = findIsoInGainMap(bytes, second)?.let { r ->
                IsoGainMap.parse(bytes, r.start, r.end)
            }
            if (iso != null) {
                notes += "完整增益参数取自增益图 APP2（原始值: " +
                    iso.rawValues.joinToString(",") { String.format(java.util.Locale.US, "%.4f", it) } + "）"
            } else {
                iso = IsoGainMap.parse(bytes, scan.isoStart, scan.isoEnd)
            }
            if (iso == null) {
                iso = IsoGainMap.Meta(notes = mutableListOf("仅发现 version 块，参数回退默认值"))
            }
            notes += iso.notes

            val lg = iso.toLog2()
            val meta = UltraHdrNative.GainMapMeta(
                minContentBoost = (lg["GainMapMin"] ?: 0.0).toFloat(),
                maxContentBoost = (lg["GainMapMax"] ?: 2.0).toFloat(),
                gamma = (lg["Gamma"] ?: 1.0).toFloat(),
                offsetSdr = (lg["OffsetSDR"] ?: 1.0 / 64).toFloat(),
                offsetHdr = (lg["OffsetHDR"] ?: 1.0 / 64).toFloat(),
                hdrCapacityMin = (lg["HDRCapacityMin"] ?: 0.0).toFloat(),
                hdrCapacityMax = (lg["HDRCapacityMax"] ?: 2.0).toFloat(),
            )
            notes += "增益参数由 ISO 21496-1 换算为 hdrgm（log2 域）"
            return HdrSource(Container.ISO_ULTRA_HDR, sdr, gmRaw, meta, make,
                needsBoostRecompute = false, notes = notes, isoMeta = iso)
        }

        // ---- 证据 2：libultrahdr probe（native 不可用时静默跳过）----
        val (probeOk, probeMeta) = try {
            UltraHdrNative.probe(bytes)
        } catch (t: Throwable) {
            notes += "native probe 不可用（${t.javaClass.simpleName}）"
            false to null
        }
        if (probeOk) {
            notes += "libultrahdr probe 成功：按 ISO Ultra HDR 处理"
            return HdrSource(Container.ISO_ULTRA_HDR, sdr, gmRaw, probeMeta, make,
                needsBoostRecompute = false, notes = notes)
        }

        // ---- 证据 3：XMP GContainer ----
        if (scan.xmpStart >= 0 && scan.hasMpf) {
            val txt = String(bytes, scan.xmpStart + XMP_SIG.size, scan.xmpEnd - scan.xmpStart - XMP_SIG.size)
            if (txt.contains("Semantic=\"GainMap\"") || txt.contains("Semantic='GainMap'")) {
                notes += "主图 XMP 含 GContainer 增益图项"
                return HdrSource(Container.ISO_ULTRA_HDR, sdr, gmRaw, probeMeta, make,
                    needsBoostRecompute = false, notes = notes)
            }
        }

        // ---- 证据 4：CUVA 的 APP5 副图 ----
        val firstMarker = firstAppMarker(bytes, second.start)
        if (firstMarker == APP5) {
            notes += "副图首段为 APP5(0xFFE5)，判定为 CUVA / 华为 HDR Vivid"
            notes += "CUVA 增益图语义与 hdrgm 不等价，建议走「反解 HDR→重算增益图」"
            return HdrSource(Container.CUVA, sdr, stripApp5(bytes, second), null, make,
                needsBoostRecompute = true, notes = notes)
        }

        // ---- 证据 5：厂商标记（弱证据，放最后）----
        if (make?.uppercase()?.startsWith("HUAWEI") == true && containsCuvaTag(bytes)) {
            notes += "仅凭 EXIF Make=HUAWEI 与 _cuva 标记判定 CUVA（未见 APP5，置信度低）"
            return HdrSource(Container.CUVA, sdr, stripApp5(bytes, second), null, make,
                needsBoostRecompute = true, notes = notes)
        }

        notes += "存在副图但缺少 ISO/XMP/APP5 证据，按未知 MPO 处理（不生成 HDR 元数据）"
        return HdrSource(Container.UNKNOWN_MPO, sdr, stripApp5(bytes, second), null, make,
            needsBoostRecompute = false, notes = notes)
    }

    // ---------------- 段扫描 ----------------

    private fun scanPrimary(b: ByteArray, r: Range): PrimaryScan {
        val s = PrimaryScan()
        var pos = r.start + 2
        while (pos + 3 < r.end) {
            if (b[pos] != 0xFF.toByte()) break
            val m = b[pos + 1].toInt() and 0xFF
            if (m == 0xD8) { pos += 2; continue }
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            if (len < 2) break
            val segEnd = pos + 2 + len
            if (segEnd > r.end) break
            val pStart = pos + 4
            if (m == APP1 && startsWith(b, pStart, segEnd, XMP_SIG)) {
                s.xmpStart = pStart; s.xmpEnd = segEnd
            } else if (m == APP2) {
                if (IsoGainMap.startsWithSig(b, pStart)) {
                    s.isoStart = pStart; s.isoEnd = segEnd
                } else if (startsWith(b, pStart, segEnd, "MPF\u0000".toByteArray(Charsets.US_ASCII))) {
                    s.hasMpf = true
                }
            }
            if (m == SOS) break
            pos = segEnd
        }
        return s
    }

    private fun findIsoInGainMap(b: ByteArray, r: Range): Range? {
        var pos = r.start + 2
        while (pos + 3 < r.end) {
            if (b[pos] != 0xFF.toByte()) break
            val m = b[pos + 1].toInt() and 0xFF
            if (m == 0xD8) { pos += 2; continue }
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            if (len < 2) break
            val segEnd = pos + 2 + len
            if (segEnd > r.end) break
            if (m == APP2 && IsoGainMap.startsWithSig(b, pos + 4)) return Range(pos + 4, segEnd)
            if (m == SOS) break
            pos = segEnd
        }
        return null
    }

    private fun startsWith(b: ByteArray, at: Int, limit: Int, sig: ByteArray): Boolean {
        if (at + sig.size > limit) return false
        for (i in sig.indices) if (b[at + i] != sig[i]) return false
        return true
    }

    // ---------------- 基础 JPEG 结构 ----------------

    private fun findImages(b: ByteArray): List<Range> {
        val out = mutableListOf<Range>()
        var pos = 0
        while (pos + 1 < b.size) {
            if (b[pos] == 0xFF.toByte() && b[pos + 1] == 0xD8.toByte()) {
                val end = imageEnd(b, pos) ?: break
                out += Range(pos, end)
                pos = end
            } else pos++
        }
        return out
    }

    private fun imageEnd(b: ByteArray, start: Int): Int? {
        var pos = start + 2
        // 边界必须是 pos+1：EOI(FFD9) 可能正好位于文件最后两个字节，
        // 用 pos+3 会提前退出并返回 null，导致副图被静默丢弃（真机踩过的坑）。
        while (pos + 1 < b.size) {
            if (b[pos] != 0xFF.toByte()) { pos++; continue }
            val m = b[pos + 1].toInt() and 0xFF
            if (m == EOI) return pos + 2
            if (pos + 3 >= b.size) return null
            val len = ((b[pos + 2].toInt() and 0xFF) shl 8) or (b[pos + 3].toInt() and 0xFF)
            if (len < 2) return null
            if (m == SOS) {
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

    private fun stripApp5(b: ByteArray, r: Range): ByteArray {
        val out = ByteArrayOutputStream(r.end - r.start)
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
        return b.copyOfRange(r.start, r.end)
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
        // slice()：position 对齐 TIFF 起点，索引即 TIFF 内相对偏移
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
