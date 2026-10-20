package com.finn.hdr

import android.util.Base64
import java.io.ByteArrayOutputStream

/**
 * 纯 Kotlin 的 Ultra HDR 容器封装器（不依赖 NDK / libultrahdr）。
 *
 * 定位：路线 B（复用源增益图 + 直接封装）的**无 NDK 实现**，用于：
 *   1. 真机快速验证「结构是否被系统/看图器识别」
 *   2. 在没有 C++ 工具链的机器上也能编出可安装的 APK
 *
 * 局限：只做封装，不做增益图重算。路线 A（线性光重算）仍必须走 native。
 * 与 Python 参考实现（src/python/uhdr/writer.py）逻辑一致，两者互为交叉校验。
 */
object UltraHdrPackager {

    private const val XMP_SIG = "http://ns.adobe.com/xap/1.0/\u0000"

    data class Meta(
        val version: String = "1.0",
        val gainMapMin: Double = 0.0,
        val gainMapMax: Double = 2.0,
        val gamma: Double = 1.0,
        val offsetSdr: Double = 1.0 / 64.0,
        val offsetHdr: Double = 1.0 / 64.0,
        val hdrCapacityMin: Double = 0.0,
        val hdrCapacityMax: Double = 2.0,
    ) {
        fun validate(): List<String> = buildList {
            if (gamma <= 0) add("Gamma 必须 > 0")
            if (gainMapMax < 0) add("GainMapMax 必须 >= 0")
            if (hdrCapacityMax < hdrCapacityMin) add("HDRCapacityMax < HDRCapacityMin")
        }
    }

    data class Layout(
        val primaryLen: Int,
        val gainMapLen: Int,
        val gainMapOffset: Int,
        val totalLen: Int,
    )

    fun pack(primaryJpeg: ByteArray, gainMapJpeg: ByteArray, meta: Meta = Meta()): Pair<ByteArray, Layout> {
        meta.validate().let { require(it.isEmpty()) { "元数据非法: ${it.joinToString()}" } }
        require(primaryJpeg.size > 4 && primaryJpeg[0] == 0xFF.toByte() && primaryJpeg[1] == 0xD8.toByte()) { "主图不是 JPEG" }
        require(gainMapJpeg.size > 4 && gainMapJpeg[0] == 0xFF.toByte() && gainMapJpeg[1] == 0xD8.toByte()) { "增益图不是 JPEG" }

        val rest = primaryJpeg.copyOfRange(2, primaryJpeg.size)     // 去掉主图 SOI
        val gm = withGainMapXmp(gainMapJpeg, meta)                  // 增益图自带完整 hdrgm

        var primaryLen = 2 + rest.size + 1024
        var gmOffset = 0
        var app1 = ByteArray(0)
        var app2 = ByteArray(0)
        var converged = false

        for (round in 0 until 12) {
            val primaryXmp = buildPrimaryXmp(primaryLen, gm.size, gmOffset, meta.version)
            app1 = app1Xmp(primaryXmp)
            app2 = app2Mpf(primaryLen, 0, gm.size, gmOffset)
            val newPrimary = 2 + app1.size + app2.size + rest.size
            val newOffset = newPrimary - (2 + app1.size + 8)        // 8 = APP2 头(4) + "MPF\0"(4)
            if (newPrimary == primaryLen && newOffset == gmOffset) {
                converged = true
                break
            }
            primaryLen = newPrimary
            gmOffset = newOffset
        }
        check(converged) { "布局迭代未收敛" }

        val out = ByteArrayOutputStream(primaryLen + gm.size)
        out.write(byteArrayOf(0xFF.toByte(), 0xD8.toByte()))
        out.write(app1)
        out.write(app2)
        out.write(rest)
        out.write(gm)
        val bytes = out.toByteArray()

        val tiffAbs = 2 + app1.size + 8
        check(primaryLen + 1 < bytes.size) { "布局异常：primaryLen 越界（$primaryLen / ${bytes.size}）" }
        check(bytes[primaryLen] == 0xFF.toByte() && bytes[primaryLen + 1] == 0xD8.toByte()) { "增益图起点错位" }
        check(tiffAbs + gmOffset + 1 < bytes.size) { "MPF 偏移越界" }
        check(bytes[tiffAbs + gmOffset] == 0xFF.toByte() && bytes[tiffAbs + gmOffset + 1] == 0xD8.toByte()) { "MPF 偏移未指向 SOI" }

        return bytes to Layout(primaryLen, gm.size, gmOffset, bytes.size)
    }

    // ---------------- XMP ----------------

    private fun withGainMapXmp(gm: ByteArray, meta: Meta): ByteArray {
        val seg = app1Xmp(buildGainMapXmp(meta))
        return gm.copyOfRange(0, 2) + seg + gm.copyOfRange(2, gm.size)
    }

    private fun buildPrimaryXmp(primaryLen: Int, gmLen: Int, gmOffset: Int, version: String): String {
        val dir = """
      <Container:Directory>
        <rdf:Seq>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Length="$primaryLen" Item:Data="${itemData(0, primaryLen)}"/>
          </rdf:li>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="image/jpeg" Item:Semantic="GainMap" Item:Length="$gmLen" Item:Data="${itemData(gmOffset, gmLen)}"/>
          </rdf:li>
        </rdf:Seq>
      </Container:Directory>
""".trimEnd()
        return doc("hdrgm:Version=\"$version\"", dir)
    }

    private fun buildGainMapXmp(m: Meta): String {
        val attrs = listOf(
            "hdrgm:Version=\"${m.version}\"",
            "hdrgm:GainMapMin=\"${fmt(m.gainMapMin)}\"",
            "hdrgm:GainMapMax=\"${fmt(m.gainMapMax)}\"",
            "hdrgm:Gamma=\"${fmt(m.gamma)}\"",
            "hdrgm:OffsetSDR=\"${fmt(m.offsetSdr)}\"",
            "hdrgm:OffsetHDR=\"${fmt(m.offsetHdr)}\"",
            "hdrgm:HDRCapacityMin=\"${fmt(m.hdrCapacityMin)}\"",
            "hdrgm:HDRCapacityMax=\"${fmt(m.hdrCapacityMax)}\"",
            "hdrgm:BaseRenditionIsHDR=\"False\"",
        ).joinToString(" ")
        return doc(attrs, "")
    }

    private fun doc(attrs: String, body: String): String = buildString {
        append("<?xpacket begin=\"\uFEFF\" id=\"W5M0MpCehiHzreSzNTczkc9d\"?>\n")
        append("<x:xmpmeta xmlns:x=\"adobe:ns:meta/\">\n")
        append("  <rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\">\n")
        append("    <rdf:Description rdf:about=\"\"\n")
        append("        xmlns:Container=\"http://ns.google.com/photos/1.0/container/\"\n")
        append("        xmlns:Item=\"http://ns.google.com/photos/1.0/container/item/\"\n")
        append("        xmlns:hdrgm=\"http://ns.adobe.com/hdr-gain-map/1.0/\" $attrs>\n")
        if (body.isNotEmpty()) append(body).append("\n")
        append("    </rdf:Description>\n  </rdf:RDF>\n</x:xmpmeta>\n<?xpacket end=\"w\"?>")
    }

    private fun itemData(offset: Int, length: Int): String {
        val raw = ByteArray(8)
        writeU32(raw, 0, offset)
        writeU32(raw, 4, length)
        return Base64.encodeToString(raw, Base64.NO_WRAP)
    }

    private fun fmt(v: Double) = String.format(java.util.Locale.US, "%.6f", v)

    private fun app1Xmp(xml: String): ByteArray {
        val payload = XMP_SIG.toByteArray(Charsets.UTF_8) + xml.toByteArray(Charsets.UTF_8)
        require(payload.size + 2 <= 0xFFFF) { "XMP 段超长" }
        return byteArrayOf(0xFF.toByte(), 0xE1.toByte()) + u16(payload.size + 2) + payload
    }

    // ---------------- MPF ----------------

    private fun app2Mpf(primaryLen: Int, primaryOff: Int, gmLen: Int, gmOffset: Int): ByteArray {
        val entryBytes = 32
        val dataStart = 8 + 2 + 4 * 12 + 4          // 62
        val tiff = ByteArrayOutputStream()
        tiff.write(byteArrayOf(0x4D, 0x4D, 0x00, 0x2A))     // "MM\0*"
        tiff.write(u32(8))
        tiff.write(u16(4))
        tiff.write(u16(0xB000)); tiff.write(u16(7)); tiff.write(u32(4)); tiff.write("0100".toByteArray())
        tiff.write(u16(0xB001)); tiff.write(u16(4)); tiff.write(u32(1)); tiff.write(u32(2))
        tiff.write(u16(0xB002)); tiff.write(u16(7)); tiff.write(u32(entryBytes)); tiff.write(u32(dataStart))
        tiff.write(u16(0xB003)); tiff.write(u16(7)); tiff.write(u32(33)); tiff.write(u32(dataStart + entryBytes))
        tiff.write(u32(0))

        tiff.write(u32(0x20000000))      // entry0: attribute = Baseline MP Primary
        tiff.write(u32(primaryLen))
        tiff.write(u32(primaryOff))
        tiff.write(u16(0)); tiff.write(u16(0))

        tiff.write(u32(0))               // entry1: 增益图
        tiff.write(u32(gmLen))
        tiff.write(u32(gmOffset))
        tiff.write(u16(0)); tiff.write(u16(0))

        tiff.write(ByteArray(33))        // ImageUID

        val payload = "MPF\u0000".toByteArray(Charsets.US_ASCII) + tiff.toByteArray()
        return byteArrayOf(0xFF.toByte(), 0xE2.toByte()) + u16(payload.size + 2) + payload
    }

    // ---------------- 辅助 ----------------

    private fun u16(v: Int): ByteArray = byteArrayOf((v shr 8).toByte(), v.toByte())

    private fun u32(v: Int): ByteArray =
        byteArrayOf((v shr 24).toByte(), (v shr 16).toByte(), (v shr 8).toByte(), v.toByte())

    private fun writeU32(buf: ByteArray, at: Int, v: Int) {
        buf[at] = (v shr 24).toByte()
        buf[at + 1] = (v shr 16).toByte()
        buf[at + 2] = (v shr 8).toByte()
        buf[at + 3] = v.toByte()
    }
}
