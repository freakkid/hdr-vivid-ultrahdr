package com.finn.hdr

import java.io.ByteArrayOutputStream

/**
 * HEIC 里 grid 派生图的读取与「最小 HEIF 重封」。
 *
 * 为什么要有它
 * ------------
 * 实测华为 HDR Vivid 的 HEIC：主图是 12 或 30 块 `hvc1` 拼的 grid，增益图是
 * **4 块拼的 grid**（既不是 `auxC` 辅助图，也不是单张 item）。要拿像素必须
 * 「拼 grid → 解 HEVC」，而系统 `ImageDecoder` 虽然会拼 grid，却不能指定
 * 「只解第 N 号 item」。
 *
 * 这里的做法：把「grid item + 它的 tile item + 相关属性(hvcC/ispe/colr…)」
 * 原样搬进一个最小 HEIF，再交给系统解码器 —— **完全不碰 HEVC 码流**，
 * 所以不需要 NDK，也不需要 libheif。
 *
 * 能力边界
 * --------
 * - 只重组容器，不做解码；能否出图取决于设备是否有 HEVC 解码器。
 * - 增益图的**参数**（华为放在 `mime` 项 / `it35` 项里）不在本类处理范围内。
 * - 桌面参考实现与回归测试：`src/python/uhdr/heif_grid.py`、`tests/test_heif_grid.py`。
 * - **尚未在真机 HEIC 上验证**：先用 [summary] 看结构对不对，再看重封图能否解出来。
 */
object HeifGridRewriter {

    data class GridInfo(
        val id: Int,
        val rows: Int,
        val cols: Int,
        val outW: Int,
        val outH: Int,
        val tileIds: List<Int>,
        val tileBytes: Long,
        val payloadBytes: Int,
        val tileType: String,
    ) {
        val isFourTile: Boolean get() = tileIds.size == 4 || rows * cols == 4
        val area: Long get() = outW.toLong() * outH
    }

    /** 结构摘要（屏幕用，控制在 10 行以内）。解析失败不抛，返回一行原因。 */
    fun summary(bytes: ByteArray): String = try {
        Model(bytes).summary()
    } catch (t: Throwable) {
        "grid: 解析失败（${t.javaClass.simpleName}）"
    }

    fun findGrids(bytes: ByteArray): List<GridInfo> =
        runCatching { Model(bytes).grids() }.getOrElse { emptyList() }

    /** 把指定 grid 重封成最小 HEIF；失败返回 null（原因看 [findGrids] 的块数与 [summary]）。 */
    fun rewrap(bytes: ByteArray, gridId: Int): ByteArray? =
        runCatching { Model(bytes).rewrap(gridId) }.getOrNull()

    /**
     * 找增益图并重封。候选规则：4 块 grid 优先，其次输出面积最小的 grid
     * （主图是 12/30 块，用块数即可分开）。
     */
    fun rewrapGainMapCandidate(bytes: ByteArray): Pair<ByteArray, GridInfo>? = try {
        val m = Model(bytes)
        val g = m.gainMapCandidate()
        if (g == null) null else m.rewrap(g.id) to g
    } catch (t: Throwable) {
        null
    }

    // =====================================================================
    // 解析
    // =====================================================================

    private class Model(private val b: ByteArray) {
        private val warnings = ArrayList<String>()
        private val items = LinkedHashMap<Int, Item>()
        private val refs = ArrayList<Triple<String, Int, List<Int>>>()
        private val props = ArrayList<Prop>()                        // 1-based
        private val assoc = HashMap<Int, MutableList<Pair<Int, Boolean>>>()
        private var pitm = -1
        private var idatStart = -1

        init {
            val top = boxes(b, 0, b.size)
            if (top.isEmpty()) throw IllegalArgumentException("不是 ISOBMFF")
            val meta = top.firstOrNull { it.type == "meta" }
                ?: throw IllegalArgumentException("无 meta box")
            val children = boxes(b, meta.payload + 4, meta.start + meta.size)

            children.firstOrNull { it.type == "idat" }?.let { idatStart = it.payload }
            parseIinf(children)
            parseIloc(children)
            parseIref(children)
            parseIprp(children)
            children.firstOrNull { it.type == "pitm" }?.let {
                val c = Cur(b, it.payload, it.start + it.size)
                val ver = c.u8()
                c.skip(3)
                pitm = if (ver >= 1) c.u32().toInt() else c.u16()
            }
        }

        private fun parseIinf(children: List<Box>) {
            val iinf = children.firstOrNull { it.type == "iinf" } ?: return
            val c = Cur(b, iinf.payload, iinf.start + iinf.size)
            val ver = c.u8()
            c.skip(3)
            if (ver == 0) c.u16() else c.u32()          // entry_count
            for (e in boxes(b, c.pos, iinf.start + iinf.size)) {
                if (e.type != "infe") continue
                val ec = Cur(b, e.payload, e.start + e.size)
                val v = ec.u8()
                val flags = (ec.u8() shl 16) or (ec.u8() shl 8) or ec.u8()
                val id = (if (v >= 3) ec.u32() else ec.u16().toLong()).toInt()
                if (v >= 2) ec.skip(2)                  // item_protection_index
                val type = fourcc(b, ec.pos)
                ec.skip(4)
                val item = Item(id, type)
                item.hidden = (flags and 1) != 0
                item.name = ec.cstr()
                if (type == "mime") item.ctype = ec.cstr()
                items[id] = item
            }
        }

        private fun parseIloc(children: List<Box>) {
            val iloc = children.firstOrNull { it.type == "iloc" } ?: return
            val c = Cur(b, iloc.payload, iloc.start + iloc.size)
            val ver = c.u8()
            c.skip(3)
            val b0 = c.u8()
            val b1 = c.u8()
            val offsetSize = (b0 ushr 4) and 0x0F
            val lengthSize = b0 and 0x0F
            val baseOffsetSize = (b1 ushr 4) and 0x0F
            val indexSize = if (ver == 1 || ver == 2) b1 and 0x0F else 0
            val count = if (ver < 2) c.u16() else c.u32().toInt()

            for (i in 0 until count) {
                if (c.left <= 0) break
                val id = (if (ver >= 2) c.u32() else c.u16().toLong()).toInt()
                val construction = if (ver == 1 || ver == 2) c.u16() and 0x0F else 0
                c.u16()                                     // data_reference_index
                val base = c.uid(baseOffsetSize)
                val extentCount = c.u16()
                val extents = ArrayList<LongArray>()
                var total = 0L
                for (k in 0 until extentCount) {
                    val off = c.uid(offsetSize)
                    val len = c.uid(lengthSize)
                    if (indexSize > 0) c.uid(indexSize)
                    var absolute = base + off
                    if (construction == 1 && idatStart >= 0) absolute += idatStart.toLong()
                    extents.add(longArrayOf(absolute, len))
                    total += len
                }
                val item = items.getOrPut(id) { Item(id, "????") }
                item.construction = construction
                item.extents = extents
                item.size = total
            }
        }

        private fun parseIref(children: List<Box>) {
            val iref = children.firstOrNull { it.type == "iref" } ?: return
            val ver = if (iref.payload < b.size) b[iref.payload].toInt() and 0xFF else 0
            val width = if (ver == 0) 2 else 4
            for (r in boxes(b, iref.payload + 4, iref.start + iref.size)) {
                val c = Cur(b, r.payload, r.start + r.size)
                if (c.left < width + 2) continue
                val from = c.uid(width).toInt()
                val n = c.u16()
                val to = ArrayList<Int>(n)
                for (k in 0 until n) to.add(c.uid(width).toInt())
                refs.add(Triple(r.type, from, to))
            }
        }

        private fun parseIprp(children: List<Box>) {
            val iprp = children.firstOrNull { it.type == "iprp" } ?: return
            val kids = boxes(b, iprp.payload, iprp.start + iprp.size)
            kids.firstOrNull { it.type == "ipco" }?.let { ipco ->
                for (p in boxes(b, ipco.payload, ipco.start + ipco.size)) {
                    props.add(Prop(p.type, b.copyOfRange(p.payload, p.start + p.size)))
                }
            }
            val ipma = kids.firstOrNull { it.type == "ipma" } ?: return
            val c = Cur(b, ipma.payload, ipma.start + ipma.size)
            val ver = c.u8()
            val flags = (c.u8() shl 16) or (c.u8() shl 8) or c.u8()
            val count = c.u32().toInt()
            val wide = (flags and 1) != 0
            for (e in 0 until count) {
                if (c.left <= 0) break
                val id = (if (ver >= 1) c.u32() else c.u16().toLong()).toInt()
                val n = c.u8()
                val list = ArrayList<Pair<Int, Boolean>>(n)
                for (k in 0 until n) {
                    val raw = if (wide) c.u16() else c.u8()
                    val idx = raw and if (wide) 0x7FFF else 0x7F
                    val ess = (raw and if (wide) 0x8000 else 0x80) != 0
                    list.add(idx to ess)
                }
                assoc[id] = list
            }
        }

        // -- 查询 ---------------------------------------------------------

        fun itemBytes(item: Item): ByteArray {
            val out = ByteArrayOutputStream()
            for (e in item.extents) {
                val off = e[0]
                val len = e[1]
                if (len <= 0 || len > Int.MAX_VALUE) continue
                if (off < 0 || off + len > b.size) {
                    warnings.add("#${item.id} 的 extent 越界")
                    continue
                }
                out.write(b, off.toInt(), len.toInt())
            }
            return out.toByteArray()
        }

        private fun refsOf(type: String, from: Int): List<Int> =
            refs.firstOrNull { it.first == type && it.second == from }?.third ?: emptyList()

        private fun propTypes(id: Int): List<String> =
            (assoc[id] ?: emptyList()).mapNotNull { (idx, _) ->
                if (idx in 1..props.size) props[idx - 1].type else null
            }

        private fun parseGrid(id: Int, payload: ByteArray): GridInfo? {
            if (payload.size < 10) {
                warnings.add("grid #$id 载荷只有 ${payload.size}B，无法解析")
                return null
            }
            val flags = ((payload[1].toInt() and 0xFF) shl 16) or
                ((payload[2].toInt() and 0xFF) shl 8) or (payload[3].toInt() and 0xFF)
            val rows = (payload[4].toInt() and 0xFF) + 1
            val cols = (payload[5].toInt() and 0xFF) + 1
            val outW: Int
            val outH: Int
            if ((flags and 1) != 0) {
                if (payload.size < 14) {
                    warnings.add("grid #$id 声明 32 位尺寸但载荷不足")
                    return null
                }
                outW = beInt(payload, 6)
                outH = beInt(payload, 10)
            } else {
                outW = ((payload[6].toInt() and 0xFF) shl 8) or (payload[7].toInt() and 0xFF)
                outH = ((payload[8].toInt() and 0xFF) shl 8) or (payload[9].toInt() and 0xFF)
            }
            val tiles = refsOf("dimg", id)
            if (tiles.size != rows * cols) {
                warnings.add("grid #$id 的 ${rows}x$cols 需要 ${rows * cols} 块，dimg 只给出 ${tiles.size} 块")
            }
            val tileType = items[tiles.firstOrNull() ?: -1]?.type ?: "?"
            var bytes = 0L
            for (t in tiles) items[t]?.let { bytes += it.size }
            return GridInfo(id, rows, cols, outW, outH, tiles, bytes, payload.size, tileType)
        }

        fun grids(): List<GridInfo> {
            val out = ArrayList<GridInfo>()
            for (it in items.values.filter { it.type == "grid" }.sortedBy { it.id }) {
                parseGrid(it.id, itemBytes(it))?.let { g -> out.add(g) }
            }
            return out
        }

        fun gainMapCandidate(): GridInfo? {
            val gs = grids()
            if (gs.isEmpty()) return null
            val four = gs.filter { it.tileIds.size == 4 }
            return (if (four.isNotEmpty()) four else gs).minByOrNull { it.area }
        }

        fun summary(): String {
            val sb = StringBuilder()
            val gs = grids()
            if (gs.isEmpty()) {
                sb.append("grid: 无（本图不是 grid 结构）")
            } else {
                val cand = gainMapCandidate()
                for (g in gs) {
                    sb.append("grid #").append(g.id).append(' ').append(g.rows).append('x').append(g.cols)
                        .append(" 输出 ").append(g.outW).append('x').append(g.outH)
                        .append(" 块=").append(g.tileIds.size)
                        .append("[").append(g.tileIds.joinToString(",") { "#$it" }).append("] ")
                        .append(fmtSize(g.tileBytes))
                    if (cand != null && g.id == cand.id) sb.append(" ← 增益图候选")
                    sb.append('\n')
                    sb.append("   tile ").append(g.tileType)
                        .append(" 属性=")
                        .append(g.tileIds.firstOrNull()?.let { propTypes(it).joinToString(",") } ?: "-")
                        .append('\n')
                }
            }
            for (m in items.values.filter { it.type == "mime" }) {
                sb.append("mime #").append(m.id).append(' ').append(m.ctype.ifEmpty { "?" })
                    .append(' ').append(fmtSize(m.size)).append('\n')
            }
            val others = items.values.filter { it.type != "hvc1" && it.type != "grid" && it.type != "mime" }
            if (others.isNotEmpty()) {
                sb.append("其余: ").append(
                    others.take(8).joinToString(", ") { "#${it.id}(${it.type},${fmtSize(it.size)})" }
                ).append('\n')
            }
            if (pitm >= 0) sb.append("pitm=#").append(pitm).append(' ').append(items[pitm]?.type ?: "?").append('\n')
            for (w in warnings) sb.append("⚠ ").append(w).append('\n')
            return sb.toString().trimEnd()
        }

        // -- 重封 ---------------------------------------------------------

        fun rewrap(gridId: Int): ByteArray {
            val g = grids().firstOrNull { it.id == gridId }
                ?: throw IllegalArgumentException("#$gridId 不是可解析的 grid")
            if (g.tileIds.isEmpty()) throw IllegalArgumentException("grid #$gridId 没有 dimg 引用")

            val ordered = ArrayList<Int>(g.tileIds.size + 1)
            ordered.add(gridId)
            ordered.addAll(g.tileIds)

            val payloads = HashMap<Int, ByteArray>(ordered.size)
            payloads[gridId] = itemBytes(items[gridId]!!)
            for (t in g.tileIds) {
                val it = items[t] ?: throw IllegalArgumentException("grid 引用了不存在的 #$t")
                payloads[t] = itemBytes(it)
            }

            // 属性按源 ipma 复制，去重后重建 ipco/ipma
            val propList = ArrayList<Prop>()
            val propIndex = HashMap<Prop, Int>()
            val assocNew = HashMap<Int, MutableList<Pair<Int, Boolean>>>()
            for (id in ordered) {
                val list = ArrayList<Pair<Int, Boolean>>()
                for ((idx, ess) in assoc[id] ?: emptyList()) {
                    if (idx !in 1..props.size) continue
                    val p = props[idx - 1]
                    val n = propIndex[p] ?: run {
                        propList.add(p)
                        propList.size.also { propIndex[p] = it }
                    }
                    list.add(n to ess)
                }
                assocNew[id] = list
            }
            val hasIspe = assocNew[gridId]!!.any { (i, _) -> propList[i - 1].type == "ispe" }
            if (!hasIspe) {
                propList.add(
                    Prop("ispe", fullBox("ispe", 0, 0, be32(g.outW.toLong()) + be32(g.outH.toLong())))
                )
                assocNew[gridId]!!.add(propList.size to true)
            }

            val wide = ordered.any { it > 0xFFFF }
            val ftyp = box(
                "ftyp",
                "heic".toByteArray(Charsets.US_ASCII) + be32(0) +
                    "mif1".toByteArray(Charsets.US_ASCII) + "heic".toByteArray(Charsets.US_ASCII),
            )

            fun buildMeta(offsets: IntArray): ByteArray {
                val hdlr = fullBox(
                    "hdlr", 0, 0,
                    be32(0) + "pict".toByteArray(Charsets.US_ASCII) + ByteArray(12) + byteArrayOf(0),
                )
                val pitmB = fullBox(
                    "pitm", if (wide) 1 else 0, 0,
                    if (wide) be32(gridId.toLong()) else be16(gridId),
                )
                val iinfB = fullBox("iinf", if (wide) 1 else 0, 0, infeEntries(ordered, wide))
                val irefB = fullBox("iref", if (wide) 1 else 0, 0, dimgBox(gridId, g.tileIds, wide))
                val iprpB = iprp(propList, assocNew, wide)
                val ilocB = buildIloc(ordered, payloads, offsets, wide)
                return box("meta", ByteArray(4) + hdlr + pitmB + iinfB + irefB + iprpB + ilocB)
            }

            val probe = buildMeta(IntArray(ordered.size))
            var pos = ftyp.size + probe.size + 8   // mdat 载荷起点
            val offsets = IntArray(ordered.size) { i ->
                val v = pos
                pos += payloads[ordered[i]]!!.size
                v
            }
            val meta = buildMeta(offsets)
            require(meta.size == probe.size) { "meta 长度随偏移变化，重封中止" }

            val out = ByteArrayOutputStream()
            out.write(ftyp)
            out.write(meta)
            val mdat = ByteArrayOutputStream()
            for (id in ordered) mdat.write(payloads[id]!!)
            out.write(box("mdat", mdat.toByteArray()))
            return out.toByteArray()
        }

        private fun infeEntries(ids: List<Int>, wide: Boolean): ByteArray {
            val out = ByteArrayOutputStream()
            for ((n, id) in ids.withIndex()) {
                val it = items[id]
                val type = (it?.type ?: "????").toByteArray(Charsets.US_ASCII)
                val name = (it?.name ?: "").toByteArray(Charsets.UTF_8)
                val hidden = it?.hidden == true && n != 0
                val body = ByteArrayOutputStream()
                body.write(if (wide) be32(id.toLong()) else be16(id))
                body.write(ByteArray(2))                    // protection_index
                body.write(type, 0, minOf(4, type.size))
                if (type.size < 4) body.write(ByteArray(4 - type.size))
                body.write(name.size and 0xFF)
                body.write(name)
                out.write(fullBox("infe", if (wide) 3 else 2, if (hidden) 1 else 0, body.toByteArray()))
            }
            val head = ByteArrayOutputStream()
            if (wide) head.write(be32(ids.size.toLong())) else head.write(be16(ids.size))
            head.write(out.toByteArray())
            return head.toByteArray()
        }

        private fun dimgBox(from: Int, to: List<Int>, wide: Boolean): ByteArray {
            val out = ByteArrayOutputStream()
            out.write(if (wide) be32(from.toLong()) else be16(from))
            out.write(be16(to.size))
            for (id in to) out.write(if (wide) be32(id.toLong()) else be16(id))
            return box("dimg", out.toByteArray())
        }

        private fun iprp(
            propList: List<Prop>,
            assocNew: Map<Int, List<Pair<Int, Boolean>>>,
            wide: Boolean,
        ): ByteArray {
            val ipco = ByteArrayOutputStream()
            for (p in propList) ipco.write(box(p.type, p.payload))
            val wideIdx = propList.size > 127
            val body = ByteArrayOutputStream()
            body.write(be32(assocNew.size.toLong()))
            for ((id, list) in assocNew) {
                body.write(if (wide) be32(id.toLong()) else be16(id))
                body.write(list.size and 0xFF)
                for ((idx, ess) in list) {
                    if (wide || wideIdx) {
                        body.write(be16(idx or (if (ess) 0x8000 else 0)))
                    } else {
                        body.write((idx and 0x7F) or (if (ess) 0x80 else 0))
                    }
                }
            }
            val ipma = fullBox("ipma", if (wide) 1 else 0, if (wide || wideIdx) 1 else 0, body.toByteArray())
            return box("iprp", box("ipco", ipco.toByteArray()) + ipma)
        }

        private fun buildIloc(
            ordered: List<Int>,
            payloads: Map<Int, ByteArray>,
            offsets: IntArray,
            wide: Boolean,
        ): ByteArray {
            val body = ByteArrayOutputStream()
            body.write(byteArrayOf(0x44, 0x00))         // offset_size=4 length_size=4 base_offset=0
            if (wide) body.write(be32(ordered.size.toLong())) else body.write(be16(ordered.size))
            for ((i, id) in ordered.withIndex()) {
                if (wide) {
                    body.write(be32(id.toLong()))
                    body.write(be16(0))                 // construction_method
                } else {
                    body.write(be16(id))
                }
                body.write(be16(0))                     // data_reference_index
                body.write(be16(1))                     // extent_count
                body.write(be32(offsets[i].toLong()))
                body.write(be32(payloads[id]!!.size.toLong()))
            }
            return fullBox("iloc", if (wide) 2 else 0, 0, body.toByteArray())
        }
    }
}

// =========================================================================
// 基础读写（文件级 private，供 HeifGridRewriter.Model 使用）
// =========================================================================

private class Box(val type: String, val start: Int, val size: Int, val payload: Int)

private class Item(val id: Int, val type: String) {
    var name: String = ""
    var ctype: String = ""          // 仅 mime 项
    var hidden: Boolean = false
    var construction: Int = 0       // 0=文件偏移 1=idat 相对
    var extents: MutableList<LongArray> = ArrayList()   // [绝对偏移, 长度]
    var size: Long = 0
}

private class Prop(val type: String, val payload: ByteArray) {
    override fun equals(other: Any?): Boolean =
        other is Prop && other.type == type && other.payload.contentEquals(payload)

    override fun hashCode(): Int = type.hashCode() * 31 + payload.contentHashCode()
}

private fun readU16(b: ByteArray, at: Int): Int =
    if (at >= 0 && at + 2 <= b.size) {
        ((b[at].toInt() and 0xFF) shl 8) or (b[at + 1].toInt() and 0xFF)
    } else 0

private fun readU32(b: ByteArray, at: Int): Long =
    if (at >= 0 && at + 4 <= b.size) {
        ((b[at].toLong() and 0xFF) shl 24) or ((b[at + 1].toLong() and 0xFF) shl 16) or
            ((b[at + 2].toLong() and 0xFF) shl 8) or (b[at + 3].toLong() and 0xFF)
    } else 0L

private fun beInt(b: ByteArray, at: Int): Int =
    ((b[at].toInt() and 0xFF) shl 24) or ((b[at + 1].toInt() and 0xFF) shl 16) or
        ((b[at + 2].toInt() and 0xFF) shl 8) or (b[at + 3].toInt() and 0xFF)

private fun fourcc(b: ByteArray, at: Int): String =
    if (at >= 0 && at + 4 <= b.size) String(b, at, 4, Charsets.US_ASCII) else "????"

private fun boxes(b: ByteArray, start: Int, end: Int): List<Box> {
    val out = ArrayList<Box>()
    var pos = start
    val limit = minOf(end, b.size)
    while (pos + 8 <= limit) {
        var size = readU32(b, pos)
        val type = fourcc(b, pos + 4)
        var payload = pos + 8
        if (size == 1L) {
            if (pos + 16 > limit) break
            size = (readU32(b, pos + 8) shl 32) or readU32(b, pos + 12)
            payload = pos + 16
        } else if (size == 0L) {
            size = (limit - pos).toLong()
        }
        if (size < 8L || pos.toLong() + size > limit.toLong()) break
        out.add(Box(type, pos, size.toInt(), payload))
        pos += size.toInt()
    }
    return out
}

/** 大端游标；越界返回 0 而不是抛异常（畸形文件要能走完并留下告警）。 */
private class Cur(private val b: ByteArray, var pos: Int, val end: Int) {
    fun u8(): Int = if (pos < end && pos < b.size) b[pos++].toInt() and 0xFF else 0

    fun u16(): Int {
        val v = readU16(b, pos)
        pos = minOf(pos + 2, end)
        return v
    }

    fun u32(): Long {
        val v = readU32(b, pos)
        pos = minOf(pos + 4, end)
        return v
    }

    /** 宽度 0 是合法的（如 base_offset_size=0），此时不能挪游标。 */
    fun uid(width: Int): Long {
        if (width <= 0) return 0L
        var v = 0L
        var k = 0
        while (k < width) {
            val at = pos + k
            v = (v shl 8) or (if (at < end && at < b.size) (b[at].toLong() and 0xFF) else 0L)
            k++
        }
        pos = minOf(pos + width, end)
        return v
    }

    fun skip(n: Int) {
        pos = minOf(pos + maxOf(n, 0), end)
    }

    fun cstr(): String {
        val start = pos
        while (pos < end && pos < b.size && b[pos] != 0.toByte()) pos++
        val s = if (pos > start) String(b, start, pos - start, Charsets.UTF_8) else ""
        if (pos < end) pos++
        return s
    }

    val left: Int get() = maxOf(0, end - pos)
}

private fun be16(v: Int): ByteArray = byteArrayOf((v ushr 8).toByte(), v.toByte())

private fun be32(v: Long): ByteArray = byteArrayOf(
    (v ushr 24).toByte(), (v ushr 16).toByte(), (v ushr 8).toByte(), v.toByte(),
)

private fun box(type: String, payload: ByteArray): ByteArray {
    val out = ByteArray(8 + payload.size)
    val size = out.size
    out[0] = (size ushr 24).toByte()
    out[1] = (size ushr 16).toByte()
    out[2] = (size ushr 8).toByte()
    out[3] = size.toByte()
    val t = type.toByteArray(Charsets.US_ASCII)
    for (i in 0 until 4) out[4 + i] = if (i < t.size) t[i] else 0x20
    System.arraycopy(payload, 0, out, 8, payload.size)
    return out
}

private fun fullBox(type: String, version: Int, flags: Int, payload: ByteArray): ByteArray =
    box(
        type,
        byteArrayOf(
            (version and 0xFF).toByte(),
            ((flags ushr 16) and 0xFF).toByte(),
            ((flags ushr 8) and 0xFF).toByte(),
            (flags and 0xFF).toByte(),
        ) + payload,
    )

private fun fmtSize(n: Long): String = when {
    n < 0 -> "?"
    n >= 1024 * 1024 -> String.format("%.1fMB", n / 1048576.0)
    n >= 1024 -> "${n / 1024}KB"
    else -> "${n}B"
}
