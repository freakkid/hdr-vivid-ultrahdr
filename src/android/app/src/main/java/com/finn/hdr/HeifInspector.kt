package com.finn.hdr

/**
 * 极轻量 ISOBMFF（HEIC/HEIF/AVIF）结构探针。
 *
 * 目的只有一个：**在不引入 libheif、也不支持解码的前提下，把事实读出来**——
 * 这张图里到底有几个 item、主图是什么编码、有没有辅助图（aux）、aux 的 URN 叫什么。
 * 有了 URN 就能判断它是不是增益图，进而决定要不要投入做 HEIC 解码。
 *
 * 只做解析，不做解码。任何异常都不抛，尽量把已读到的信息返回。
 */
object HeifInspector {

    data class Box(val type: String, val start: Int, val size: Int, val payloadStart: Int)

    private fun u32(b: ByteArray, at: Int): Int =
        ((b[at].toInt() and 0xFF) shl 24) or ((b[at + 1].toInt() and 0xFF) shl 16) or
            ((b[at + 2].toInt() and 0xFF) shl 8) or (b[at + 3].toInt() and 0xFF)

    private fun fourcc(b: ByteArray, at: Int): String =
        if (at + 4 <= b.size) String(b, at, 4, Charsets.US_ASCII) else "????"

    /** 遍历 [start, end) 内的同级 box。 */
    private fun boxes(b: ByteArray, start: Int, end: Int): List<Box> {
        val out = ArrayList<Box>()
        var pos = start
        val limit = minOf(end, b.size)
        while (pos + 8 <= limit) {
            var size = u32(b, pos)
            val type = fourcc(b, pos + 4)
            var payload = pos + 8
            if (size == 1) {                       // 64 位长度
                if (pos + 16 > limit) break
                val hi = u32(b, pos + 8).toLong() and 0xFFFFFFFFL
                val lo = u32(b, pos + 12).toLong() and 0xFFFFFFFFL
                size = ((hi shl 32) or lo).toInt()
                payload = pos + 16
            } else if (size == 0) {                // 延伸到末尾
                size = limit - pos
            }
            if (size < 8 || pos + size > limit) break
            out.add(Box(type, pos, size, payload))
            pos += size
        }
        return out
    }

    private fun child(b: ByteArray, box: Box, offset: Int): List<Box> =
        boxes(b, box.payloadStart + offset, box.start + box.size)

    private fun firstChild(b: ByteArray, box: Box, offset: Int, type: String): Box? =
        child(b, box, offset).firstOrNull { it.type == type }

    /** 极简报告：品牌 / item 列表 / aux URN。 */
    fun describe(b: ByteArray): String {
        val sb = StringBuilder()
        try {
            val top = boxes(b, 0, b.size)
            sb.append("ISOBMFF 顶层:").append(top.joinToString(" ") { it.type })
            sb.append('\n')

            val ftyp = top.firstOrNull { it.type == "ftyp" }
            if (ftyp != null) {
                sb.append("brand=").append(fourcc(b, ftyp.payloadStart))
                sb.append(" compat=")
                var p = ftyp.payloadStart + 8
                val safeEnd = minOf(ftyp.start + ftyp.size, b.size)
                val list = ArrayList<String>()
                while (p + 4 <= safeEnd) { list.add(fourcc(b, p)); p += 4 }
                sb.append(list.joinToString(",")).append('\n')
            }

            val meta = top.firstOrNull { it.type == "meta" }
            if (meta == null) {
                sb.append("无 meta box —— 不是常规 HEIF 图片\n")
                return sb.toString()
            }

            // meta 是 full box，前 4 字节是 version+flags
            val metaChildren = child(b, meta, 4)
            sb.append("meta 子 box: ").append(metaChildren.joinToString(" ") { it.type }).append('\n')

            // ---- item 列表（iinf → infe）
            val iinf = metaChildren.firstOrNull { it.type == "iinf" }
            if (iinf != null) {
                val ver = b[iinf.payloadStart].toInt() and 0xFF
                val countOff = if (ver == 0) 4 + 2 else 4 + 4
                val entries = child(b, iinf, countOff).filter { it.type == "infe" }
                sb.append("item 数: ").append(entries.size).append('\n')
                for (e in entries) {
                    val v = b[e.payloadStart].toInt() and 0xFF
                    var p = e.payloadStart + 4
                    val id = if (v >= 3) { val x = u32(b, p); p += 4; x } else {
                        val x = ((b[p].toInt() and 0xFF) shl 8) or (b[p + 1].toInt() and 0xFF); p += 2; x
                    }
                    if (v >= 2) p += 2                        // protection_index
                    val type = fourcc(b, p); p += 4
                    val name = if (p < e.start + e.size) {
                        val len = b[p].toInt() and 0xFF
                        if (p + 1 + len <= e.start + e.size) String(b, p + 1, len, Charsets.UTF_8) else ""
                    } else ""
                    sb.append("  #").append(id).append(' ').append(type)
                    if (name.isNotBlank()) sb.append(" \"").append(name).append('"')
                    sb.append('\n')
                }
            }

            // ---- 辅助图类型（iprp → ipco → auxC）
            val iprp = metaChildren.firstOrNull { it.type == "iprp" }
            val auxUrns = ArrayList<String>()
            if (iprp != null) {
                val ipco = firstChild(b, iprp, 0, "ipco")
                if (ipco != null) {
                    val props = child(b, ipco, 0)
                    sb.append("属性 box: ").append(props.joinToString(" ") { it.type }).append('\n')
                    for (p in props.filter { it.type == "auxC" }) {
                        // auxC: fullbox(4) + 以 0 结尾的 URN 字符串
                        var q = p.payloadStart + 4
                        val endQ = minOf(p.start + p.size, b.size)
                        val startStr = q
                        while (q < endQ && b[q] != 0.toByte()) q++
                        if (q > startStr) {
                            auxUrns.add(String(b, startStr, q - startStr, Charsets.UTF_8))
                        }
                    }
                }
            }
            if (auxUrns.isEmpty()) {
                sb.append("aux URN: 无（没有辅助图，通常也就没有增益图）\n")
            } else {
                sb.append("aux URN:\n")
                for (u in auxUrns) {
                    sb.append("  ").append(u)
                    val known = when {
                        u.contains("21496") -> "  ← ISO 21496-1 增益图"
                        u.contains("hdrgainmap", true) -> "  ← 厂商 HDR 增益图"
                        u.contains("depth", true) -> "  ← 深度图"
                        u.contains("alpha", true) -> "  ← alpha"
                        u.contains("thumbnail", true) -> "  ← 缩略图"
                        else -> ""
                    }
                    sb.append(known).append('\n')
                }
            }
        } catch (t: Throwable) {
            sb.append("\n结构解析中断：").append(t.javaClass.simpleName)
                .append(' ').append(t.message ?: "").append('\n')
        }
        return sb.toString()
    }
}
