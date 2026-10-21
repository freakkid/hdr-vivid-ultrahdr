package com.finn.hdr

/**
 * 极轻量 ISOBMFF（HEIC/HEIF/AVIF）结构探针。
 *
 * 目的只有一个：**在不引入 libheif、也不支持解码的前提下，把事实读出来**——
 * 这张图里有几个 item、主图多大、有没有辅助图（aux）、aux 的 URN 叫什么。
 *
 * 只做解析，不做解码。任何异常都不抛，收敛到 [Summary.error]。
 *
 * 两个渲染入口（共用同一份解析结果，避免两套逻辑漂移）：
 * - [brief]    4~8 行摘要，屏幕上一眼能判断，且短到能被粘贴出去
 * - [describe] 完整结构，用于详细排查
 */
object HeifInspector {

    data class Box(val type: String, val start: Int, val size: Int, val payloadStart: Int)

    /** 单个 item。size 来自 iloc，-1 表示未解析到。 */
    data class Item(val id: Long, val type: String, val name: String, val size: Long)

    data class Summary(
        val isIsobmff: Boolean = false,
        val topBoxes: List<String> = emptyList(),
        val brand: String = "",
        val compat: List<String> = emptyList(),
        val hasMeta: Boolean = false,
        val metaChildren: List<String> = emptyList(),
        val items: List<Item> = emptyList(),
        val auxUrns: List<String> = emptyList(),
        val props: List<String> = emptyList(),
        val error: String? = null,
    ) {
        val hasAux: Boolean get() = auxUrns.isNotEmpty()

        /** 增益图候选：ISO 21496-1 或厂商 hdrgainmap。 */
        val gainMapUrns: List<String>
            get() = auxUrns.filter {
                it.contains("21496") || it.contains("gainmap", true)
            }

        val likelyGainMap: Boolean get() = gainMapUrns.isNotEmpty()

        /** 主图通常就是体积最大的 item。 */
        val primaryItem: Item?
            get() = items.filter { it.size > 0 }.maxByOrNull { it.size }

        /** 除主图外体积最大的 item —— 增益图通常是这个。 */
        val largestSecondary: Item?
            get() {
                val p = primaryItem ?: return null
                return items.filter { it.id != p.id && it.size > 0 }.maxByOrNull { it.size }
            }
    }

    /**
     * 无符号 32 位读取。
     * 必须返回 Long：box 长度合法值可达 0xFFFF_FFFF，用 Int 会变成负数，
     * 后续 `pos + size` 溢出后与 limit 比较会误判为合法，进而负下标读取。
     */
    private fun u32(b: ByteArray, at: Int): Long =
        (((b[at].toLong() and 0xFF) shl 24) or ((b[at + 1].toLong() and 0xFF) shl 16) or
            ((b[at + 2].toLong() and 0xFF) shl 8) or (b[at + 3].toLong() and 0xFF)) and 0xFFFFFFFFL

    private fun fourcc(b: ByteArray, at: Int): String =
        if (at >= 0 && at + 4 <= b.size) String(b, at, 4, Charsets.US_ASCII) else "????"

    /** 遍历 [start, end) 内的同级 box。全程用 Long 参与长度运算。 */
    private fun boxes(b: ByteArray, start: Int, end: Int): List<Box> {
        val out = ArrayList<Box>()
        var pos = start
        val limit = minOf(end, b.size)
        while (pos >= 0 && pos + 8 <= limit) {
            var size = u32(b, pos)
            val type = fourcc(b, pos + 4)
            var payload = pos + 8
            if (size == 1L) {                      // 64 位长度
                if (pos + 16 > limit) break
                val hi = u32(b, pos + 8)
                val lo = u32(b, pos + 12)
                size = (hi shl 32) or lo
                payload = pos + 16
            } else if (size == 0L) {                // 延伸到末尾
                size = (limit - pos).toLong()
            }
            if (size < 8L || pos.toLong() + size > limit.toLong()) break
            out.add(Box(type, pos, size.toInt(), payload))
            pos += size.toInt()
        }
        return out
    }

    private fun child(b: ByteArray, box: Box, offset: Int): List<Box> =
        boxes(b, box.payloadStart + offset, box.start + box.size)

    private fun firstChild(b: ByteArray, box: Box, offset: Int, type: String): Box? =
        child(b, box, offset).firstOrNull { it.type == type }

    // =====================================================================
    // 解析
    // =====================================================================

    fun summarize(b: ByteArray): Summary {
        var err: String? = null
        try {
            val top = boxes(b, 0, b.size)
            if (top.isEmpty()) return Summary(error = "首 8 字节解析不出 box，可能不是 ISOBMFF")

            val ftyp = top.firstOrNull { it.type == "ftyp" }
            var brand = ""
            var compat = emptyList<String>()
            if (ftyp != null) {
                brand = fourcc(b, ftyp.payloadStart)
                val safeEnd = minOf(ftyp.start + ftyp.size, b.size)
                var p = ftyp.payloadStart + 8
                val list = ArrayList<String>()
                while (p + 4 <= safeEnd) { list.add(fourcc(b, p)); p += 4 }
                compat = list
            }

            val meta = top.firstOrNull { it.type == "meta" }
                ?: return Summary(
                    isIsobmff = true, topBoxes = top.map { it.type },
                    brand = brand, compat = compat,
                    error = "无 meta box，不是常规 HEIF 图片",
                )

            val metaChildren = child(b, meta, 4)   // meta 是 full box，前 4 字节 version+flags

            // ---- item 表（iinf → infe）
            val items = ArrayList<Item>()
            val iinf = metaChildren.firstOrNull { it.type == "iinf" }
            if (iinf != null && iinf.payloadStart < b.size) {
                try {
                    val ver = b[iinf.payloadStart].toInt() and 0xFF
                    val countOff = if (ver == 0) 4 + 2 else 4 + 4
                    val entries = child(b, iinf, countOff).filter { it.type == "infe" }
                    for (e in entries) {
                        val v = b[e.payloadStart].toInt() and 0xFF
                        var p = e.payloadStart + 4
                        val id: Long = if (v >= 3) { val x = u32(b, p); p += 4; x } else {
                            val x = ((b[p].toLong() and 0xFF) shl 8) or (b[p + 1].toLong() and 0xFF)
                            p += 2
                            x
                        }
                        if (v >= 2) p += 2              // protection_index
                        val type = fourcc(b, p); p += 4
                        val name = if (p < e.start + e.size) {
                            val len = b[p].toInt() and 0xFF
                            if (p + 1 + len <= e.start + e.size) String(b, p + 1, len, Charsets.UTF_8) else ""
                        } else ""
                        items.add(Item(id, type, name, -1))
                    }
                } catch (t: Throwable) {
                    err = "item 表解析中断：${t.javaClass.simpleName}"
                }
            }

            // ---- 属性（iprp → ipco），取 auxC 的 URN
            val auxUrns = ArrayList<String>()
            var props: List<String> = emptyList()
            val iprp = metaChildren.firstOrNull { it.type == "iprp" }
            if (iprp != null) {
                try {
                    val ipco = firstChild(b, iprp, 0, "ipco")
                    if (ipco != null) {
                        val list = child(b, ipco, 0)
                        props = list.map { it.type }
                        for (p in list.filter { it.type == "auxC" }) {
                            // auxC: fullbox(4) + 以 0 结尾的 URN
                            var q = p.payloadStart + 4
                            val endQ = minOf(p.start + p.size, b.size)
                            val startStr = q
                            while (q < endQ && b[q] != 0.toByte()) q++
                            if (q > startStr) {
                                auxUrns.add(String(b, startStr, q - startStr, Charsets.UTF_8))
                            }
                        }
                    }
                } catch (t: Throwable) {
                    err = (err ?: "") + " 属性解析中断：${t.javaClass.simpleName}"
                }
            }

            // ---- item 实际长度（iloc），回填 items
            val iloc = metaChildren.firstOrNull { it.type == "iloc" }
            if (iloc != null) {
                try {
                    val sizes = parseIloc(b, iloc)
                    for (i in items.indices) {
                        sizes[items[i].id]?.let { items[i] = items[i].copy(size = it) }
                    }
                } catch (t: Throwable) {
                    err = (err ?: "") + " iloc 解析中断：${t.javaClass.simpleName}"
                }
            }

            return Summary(
                isIsobmff = true,
                topBoxes = top.map { it.type },
                brand = brand,
                compat = compat,
                hasMeta = true,
                metaChildren = metaChildren.map { it.type },
                items = items,
                auxUrns = auxUrns,
                props = props,
                error = err,
            )
        } catch (t: Throwable) {
            return Summary(isIsobmff = true, error = "解析中断：${t.javaClass.simpleName} ${t.message ?: ""}")
        }
    }

    /**
     * 解析 iloc，返回 item id → 总字节数。
     *
     * 这是判断「增益图到底在不在」的唯一可靠依据：auxC 只说明声明了辅助图，
     * 数据可能在 mdat 里根本不存在。
     */
    private fun parseIloc(b: ByteArray, iloc: Box): Map<Long, Long> {
        val out = HashMap<Long, Long>()
        val limit = minOf(iloc.start + iloc.size, b.size)
        var p = iloc.payloadStart
        val ver = b[p].toInt() and 0xFF
        p += 4                                   // version + flags
        val b0 = b[p].toInt() and 0xFF
        val b1 = b[p + 1].toInt() and 0xFF
        p += 2
        val offsetSize = (b0 ushr 4) and 0x0F
        val lengthSize = b0 and 0x0F
        val baseOffsetSize = (b1 ushr 4) and 0x0F
        val count = if (ver < 2) {
            val c = ((b[p].toInt() and 0xFF) shl 8) or (b[p + 1].toInt() and 0xFF)
            p += 2; c
        } else {
            val c = u32(b, p).toInt()
            p += 4; c
        }
        var i = 0
        while (i < count && p < limit) {
            val id: Long = if (ver < 2) {
                val v = ((b[p].toLong() and 0xFF) shl 8) or (b[p + 1].toLong() and 0xFF)
                p += 2; v
            } else {
                val v = u32(b, p); p += 4; v
            }
            if (ver >= 1) p += 2                 // construction_method
            p += 2                               // data_reference_index
            p += baseOffsetSize
            if (p + 2 > limit) break
            val extentCount = ((b[p].toInt() and 0xFF) shl 8) or (b[p + 1].toInt() and 0xFF)
            p += 2
            var total = 0L
            var j = 0
            while (j < extentCount && p + offsetSize + lengthSize <= limit) {
                p += offsetSize
                total += readN(b, p, lengthSize.toLong())
                p += lengthSize
                j++
            }
            out[id] = total
            i++
        }
        return out
    }

    private fun readN(b: ByteArray, at: Int, len: Long): Long {
        var v = 0L
        var k = 0
        while (k < len) { v = (v shl 8) or (b[at + k].toLong() and 0xFF); k++ }
        return v
    }

    fun fmtSize(n: Long): String = when {
        n < 0 -> "?"
        n >= 1024 * 1024 -> String.format("%.1fMB", n / 1048576.0)
        n >= 1024 -> "${n / 1024}KB"
        else -> "${n}B"
    }

    // =====================================================================
    // 渲染
    // =====================================================================

    private fun tagOf(u: String): String = when {
        u.contains("21496") -> "  ← ISO 21496-1 增益图"
        u.contains("gainmap", true) -> "  ← 厂商 HDR 增益图"
        u.contains("depth", true) -> "  ← 深度图"
        u.contains("alpha", true) -> "  ← alpha"
        u.contains("thumbnail", true) -> "  ← 缩略图"
        else -> ""
    }

    /**
     * 短摘要。设计目标：**屏幕上一眼判断，且短到能粘贴出去**
     * （贴到微信也不会被长度限制截断）。排障先看这个，不够再看 [describe]。
     */
    fun brief(b: ByteArray): String {
        val s = summarize(b)
        val sb = StringBuilder()
        sb.append("── HEIC 摘要 ──\n")
        sb.append("品牌=").append(s.brand.ifEmpty { "?" })
        sb.append(" 顶层=").append(s.topBoxes.joinToString(","))
        sb.append(" item=").append(s.items.size).append('\n')

        if (s.auxUrns.isEmpty()) {
            sb.append("aux=0 → 没有辅助图，不是 HDR\n")
        } else {
            sb.append("aux=").append(s.auxUrns.size).append('\n')
            for (u in s.auxUrns) sb.append("  · ").append(u).append(tagOf(u)).append('\n')
        }

        s.primaryItem?.let {
            sb.append("主图≈").append(fmtSize(it.size))
                .append(" (#").append(it.id).append(' ').append(it.type).append(")\n")
        }
        s.largestSecondary?.let {
            sb.append("次大项≈").append(fmtSize(it.size))
                .append(" (#").append(it.id).append(' ').append(it.type).append(")")
            if (s.likelyGainMap) sb.append(" ← 很可能是增益图")
            sb.append('\n')
        }

        sb.append("结论：").append(
            when {
                s.likelyGainMap -> "有增益图（HDR 照片），值得做提取"
                !s.hasAux -> "无增益图，普通 SDR"
                else -> "有辅助图但非增益图"
            }
        ).append('\n')
        s.error?.let { sb.append("注意：").append(it).append('\n') }
        return sb.toString()
    }

    /** 完整结构。item 可能很多，限制条数避免撑爆界面。 */
    fun describe(b: ByteArray): String {
        val s = summarize(b)
        val sb = StringBuilder()
        sb.append("ISOBMFF 顶层: ").append(s.topBoxes.joinToString(" ")).append('\n')
        if (s.brand.isNotEmpty()) {
            sb.append("brand=").append(s.brand)
            if (s.compat.isNotEmpty()) sb.append(" compat=").append(s.compat.joinToString(","))
            sb.append('\n')
        }
        if (!s.hasMeta) {
            sb.append("无 meta box，不是常规 HEIF 图片\n")
            s.error?.let { sb.append(it).append('\n') }
            return sb.toString()
        }
        sb.append("meta 子 box: ").append(s.metaChildren.joinToString(" ")).append('\n')
        sb.append("item 数: ").append(s.items.size).append('\n')
        for (it in s.items.take(20)) {
            sb.append("  #").append(it.id).append(' ').append(it.type)
            if (it.name.isNotBlank()) sb.append(" name=").append(it.name)
            if (it.size >= 0) sb.append(' ').append(fmtSize(it.size))
            sb.append('\n')
        }
        if (s.items.size > 20) sb.append("  …（其余 ${s.items.size - 20} 个略）\n")
        if (s.props.isNotEmpty()) {
            sb.append("属性 box: ").append(s.props.joinToString(" ")).append('\n')
        }
        if (s.auxUrns.isEmpty()) {
            sb.append("aux URN: 无\n")
        } else {
            sb.append("aux URN:\n")
            for (u in s.auxUrns) sb.append("  ").append(u).append(tagOf(u)).append('\n')
        }
        s.error?.let { sb.append("注意：").append(it).append('\n') }
        return sb.toString()
    }
}
