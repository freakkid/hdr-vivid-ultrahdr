"""HEIC(ISOBMFF) 网格增益图的解析与「最小 HEIF 重封」。

为什么需要它
------------
华为 HDR Vivid 的 HEIC 里，增益图既不是 ``auxC`` 辅助图，也不是单张图，
而是 ``grid`` 派生项 + 4 块 ``hvc1`` tile（主图则是 12 或 30 块）。
要拿到像素必须「拼 grid → 解 HEVC」，成本远高于 JPEG 里的增益图。

而系统解码器（Android ``ImageDecoder``）能解整张 HEIC——它内部会拼 grid——
却不能指定「只解第 N 号 item」。于是最省事的取法是：

    把 grid item + 它的 tile item + 相关属性(hvcC/ispe/colr…) 原样搬进一个
    最小 HEIF(ftyp + meta + mdat)，再交给系统解码器。

这样**完全不碰 HEVC 码流**（参数集与 NAL 原样搬运），既不需要 NDK，也不需要
libheif。本模块是该思路的桌面参考实现，App 侧同名逻辑见
``src/android/.../HeifGridRewriter.kt``。

验证状态
--------
- 已用合成文件验证：box 序列化、iloc 偏移、dimg 顺序、属性关联、重封后再解析。
- **未**在真机华为 HEIC 上验证（原件只在手机/容器内）。先跑 CLI 看结构：

    PYTHONPATH=src/python python -m uhdr.heif_grid <file.heic>
    PYTHONPATH=src/python python -m uhdr.heif_grid <file.heic> --rewrap 42 -o gm.heic
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# box 基础
# ---------------------------------------------------------------------------


def box(typ: str, payload: bytes) -> bytes:
    """普通 box：4 字节长度 + 4 字节类型 + 载荷。"""
    return (8 + len(payload)).to_bytes(4, "big") + typ.encode("latin-1") + payload


def full_box(typ: str, version: int, flags: int, payload: bytes) -> bytes:
    """FullBox：长度 + 类型 + version(1) + flags(3) + 载荷。"""
    return box(typ, bytes([version & 0xFF]) + (flags & 0xFFFFFF).to_bytes(3, "big") + payload)


@dataclass
class Box:
    typ: str
    start: int
    size: int
    payload: int  # 载荷起始（已跳过头部）


def boxes(buf: bytes, start: int, end: int) -> list[Box]:
    """遍历 [start, end) 内的同级 box。长度一律按无符号处理，畸形即停。"""
    out: list[Box] = []
    pos = start
    limit = min(end, len(buf))
    while pos + 8 <= limit:
        size = int.from_bytes(buf[pos : pos + 4], "big")
        typ = buf[pos + 4 : pos + 8].decode("latin-1")
        header = 8
        if size == 1:  # 64 位长度
            if pos + 16 > limit:
                break
            size = int.from_bytes(buf[pos + 8 : pos + 16], "big")
            header = 16
        elif size == 0:  # 延伸到末尾
            size = limit - pos
        if size < header or pos + size > limit:
            break
        out.append(Box(typ, pos, size, pos + header))
        pos += size
    return out


def _cstr(buf: bytes, pos: int, end: int) -> tuple[str, int]:
    """读以 0 结尾的字符串，返回 (文本, 下一个位置)。"""
    start = pos
    while pos < end and buf[pos] != 0:
        pos += 1
    text = buf[start:pos].decode("utf-8", "replace")
    return text, min(pos + 1, end)


class _Cur:
    """带边界检查的小端…不，是**大端**游标。畸形数据返回 0 而不是抛异常。"""

    def __init__(self, buf: bytes, pos: int, end: int):
        self.buf = buf
        self.pos = pos
        self.end = end

    def take(self, n: int) -> bytes:
        # n<=0 是合法的（如 base_offset_size=0 时读 0 字节），此时**不能**把游标推到末尾
        if n <= 0:
            return b""
        if self.pos + n > self.end:
            self.pos = self.end
            return b"\x00" * n
        out = self.buf[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "big")

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def uid(self, width: int) -> int:
        return int.from_bytes(self.take(width), "big")

    @property
    def left(self) -> int:
        return max(0, self.end - self.pos)


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------


@dataclass
class Item:
    item_id: int
    item_type: str
    name: str = ""
    content_type: str = ""  # 仅 mime 项有
    hidden: bool = False
    construction: int = 0  # 0=文件偏移 1=idat 相对
    extents: list[tuple[int, int]] = field(default_factory=list)  # (绝对偏移, 长度)
    size: int = 0


@dataclass
class Ref:
    typ: str
    from_id: int
    to_ids: list[int]


@dataclass
class Grid:
    item_id: int
    rows: int
    cols: int
    out_w: int
    out_h: int
    tile_ids: list[int]
    payload: bytes = b""

    @property
    def tile_count(self) -> int:
        return len(self.tile_ids)

    @property
    def is_4_tile(self) -> bool:
        return self.tile_count == 4 or (self.rows * self.cols == 4)

    @property
    def area(self) -> int:
        return max(0, self.out_w) * max(0, self.out_h)


class HeifGrids:
    """一份 HEIC 的只读结构视图（item 表 / iloc / iref / 属性关联）。"""

    def __init__(self, data: bytes):
        self.data = data
        self.warnings: list[str] = []
        self.top = boxes(data, 0, len(data))
        if not self.top:
            raise ValueError("首 8 字节解析不出 box，不是 ISOBMFF")

        ftyp = next((b for b in self.top if b.typ == "ftyp"), None)
        self.brand = ""
        self.compat: list[str] = []
        if ftyp is not None:
            self.brand = data[ftyp.payload : ftyp.payload + 4].decode("latin-1")
            p = ftyp.payload + 8
            end = ftyp.start + ftyp.size
            while p + 4 <= end:
                self.compat.append(data[p : p + 4].decode("latin-1"))
                p += 4

        self.items: dict[int, Item] = {}
        self.refs: list[Ref] = []
        self.properties: list[tuple[str, bytes]] = []  # 1-based（下标 0 对应 1 号属性）
        self.assoc: dict[int, list[tuple[int, bool]]] = {}
        self.pitm: int | None = None
        self.idat: tuple[int, int] | None = None

        meta = next((b for b in self.top if b.typ == "meta"), None)
        if meta is None:
            self.warnings.append("无 meta box，不是常规 HEIF 图片")
            return

        children = boxes(data, meta.payload + 4, meta.start + meta.size)  # meta 是 FullBox
        self._parse_idat(children)
        self._parse_iinf(children)
        self._parse_iloc(children)
        self._parse_iref(children)
        self._parse_iprp(children)
        self._parse_pitm(children)

    # -- 解析 ---------------------------------------------------------------

    def _parse_idat(self, children: list[Box]) -> None:
        for b in children:
            if b.typ == "idat":
                self.idat = (b.payload, b.start + b.size)
                return

    def _parse_iinf(self, children: list[Box]) -> None:
        iinf = next((b for b in children if b.typ == "iinf"), None)
        if iinf is None:
            return
        cur = _Cur(self.data, iinf.payload, iinf.start + iinf.size)
        ver = cur.u8()
        cur.take(3)
        count = cur.u16() if ver == 0 else cur.u32()
        start = cur.pos
        entries = boxes(self.data, start, iinf.start + iinf.size)
        if count and len(entries) != count:
            self.warnings.append(f"iinf 声明 {count} 项，实读到 {len(entries)} 项")
        for e in entries:
            if e.typ != "infe":
                continue
            c = _Cur(self.data, e.payload, e.start + e.size)
            v = c.u8()
            flags = int.from_bytes(c.take(3), "big")
            iid = c.u32() if v >= 3 else c.u16()
            if v >= 2:
                c.take(2)  # item_protection_index
            typ = c.take(4).decode("latin-1")
            name, p = _cstr(self.data, c.pos, e.start + e.size)
            item = Item(iid, typ, name, hidden=bool(flags & 1))
            if typ == "mime":
                ctype, _ = _cstr(self.data, p, e.start + e.size)
                item.content_type = ctype
            self.items[iid] = item

    def _parse_iloc(self, children: list[Box]) -> None:
        iloc = next((b for b in children if b.typ == "iloc"), None)
        if iloc is None:
            return
        cur = _Cur(self.data, iloc.payload, iloc.start + iloc.size)
        ver = cur.u8()
        cur.take(3)
        b0, b1 = cur.u8(), cur.u8()
        offset_size = (b0 >> 4) & 0x0F
        length_size = b0 & 0x0F
        base_offset_size = (b1 >> 4) & 0x0F
        index_size = b1 & 0x0F if ver in (1, 2) else 0
        count = cur.u16() if ver < 2 else cur.u32()

        for _ in range(count):
            iid = cur.u32() if ver >= 2 else cur.u16()
            construction = 0
            if ver in (1, 2):
                construction = cur.u16() & 0x0F
            cur.u16()  # data_reference_index
            base = cur.uid(base_offset_size)
            extent_count = cur.u16()
            extents: list[tuple[int, int]] = []
            total = 0
            for _ in range(extent_count):
                off = cur.uid(offset_size)
                length = cur.uid(length_size)
                if index_size:
                    cur.uid(index_size)
                absolute = base + off
                if construction == 1 and self.idat is not None:
                    absolute += self.idat[0]
                extents.append((absolute, length))
                total += length
            item = self.items.get(iid)
            if item is None:
                item = Item(iid, "????")
                self.items[iid] = item
            item.construction = construction
            item.extents = extents
            item.size = total

    def _parse_iref(self, children: list[Box]) -> None:
        iref = next((b for b in children if b.typ == "iref"), None)
        if iref is None:
            return
        ver = self.data[iref.payload] if iref.payload < len(self.data) else 0
        width = 2 if ver == 0 else 4
        for r in boxes(self.data, iref.payload + 4, iref.start + iref.size):
            c = _Cur(self.data, r.payload, r.start + r.size)
            if c.left < width + 2:
                continue
            from_id = c.uid(width)
            n = c.u16()
            tos = [c.uid(width) for _ in range(n)]
            self.refs.append(Ref(r.typ, from_id, tos))

    def _parse_iprp(self, children: list[Box]) -> None:
        iprp = next((b for b in children if b.typ == "iprp"), None)
        if iprp is None:
            return
        kids = boxes(self.data, iprp.payload, iprp.start + iprp.size)
        ipco = next((b for b in kids if b.typ == "ipco"), None)
        if ipco is not None:
            for p in boxes(self.data, ipco.payload, ipco.start + ipco.size):
                self.properties.append((p.typ, self.data[p.payload : p.start + p.size]))
        ipma = next((b for b in kids if b.typ == "ipma"), None)
        if ipma is None:
            return
        cur = _Cur(self.data, ipma.payload, ipma.start + ipma.size)
        ver = cur.u8()
        flags = int.from_bytes(cur.take(3), "big")
        count = cur.u32()
        wide = bool(flags & 1)
        for _ in range(count):
            iid = cur.u32() if ver >= 1 else cur.u16()
            n = cur.u8()
            entries: list[tuple[int, bool]] = []
            for _ in range(n):
                raw = cur.u16() if wide else cur.u8()
                idx = raw & (0x7FFF if wide else 0x7F)
                ess = bool(raw & (0x8000 if wide else 0x80))
                entries.append((idx, ess))
            self.assoc[iid] = entries

    def _parse_pitm(self, children: list[Box]) -> None:
        pitm = next((b for b in children if b.typ == "pitm"), None)
        if pitm is None:
            return
        cur = _Cur(self.data, pitm.payload, pitm.start + pitm.size)
        ver = cur.u8()
        cur.take(3)
        self.pitm = cur.u32() if ver >= 1 else cur.u16()

    # -- 查询 ---------------------------------------------------------------

    def item_bytes(self, item: Item) -> bytes:
        out = bytearray()
        for off, length in item.extents:
            if length <= 0:
                continue
            if off < 0 or off + length > len(self.data):
                self.warnings.append(f"item #{item.item_id} 的 extent 越界（off={off} len={length}）")
                continue
            out += self.data[off : off + length]
        return bytes(out)

    def refs_of(self, typ: str, from_id: int) -> list[int]:
        for r in self.refs:
            if r.typ == typ and r.from_id == from_id:
                return list(r.to_ids)
        return []

    def properties_of(self, item_id: int) -> list[tuple[str, bytes]]:
        out = []
        for idx, _ess in self.assoc.get(item_id, []):
            if 1 <= idx <= len(self.properties):
                out.append(self.properties[idx - 1])
        return out

    def describe_props(self, item_id: int) -> str:
        names = [t for t, _ in self.properties_of(item_id)]
        return ",".join(names) if names else "-"

    def grids(self) -> list[Grid]:
        out: list[Grid] = []
        for it in sorted(self.items.values(), key=lambda x: x.item_id):
            if it.item_type != "grid":
                continue
            payload = self.item_bytes(it)
            g = self._parse_grid(it.item_id, payload)
            if g is not None:
                out.append(g)
        return out

    def _parse_grid(self, item_id: int, payload: bytes) -> Grid | None:
        if len(payload) < 8:
            self.warnings.append(f"grid #{item_id} 载荷只有 {len(payload)} B，无法解析")
            return None
        ver = payload[0]
        flags = int.from_bytes(payload[1:4], "big")
        rows = payload[4] + 1
        cols = payload[5] + 1
        if flags & 1:  # 32 位输出尺寸（HEIF Amendment 1）
            if len(payload) < 16:
                self.warnings.append(f"grid #{item_id} 声明 32 位尺寸但载荷不足")
                return None
            out_w = int.from_bytes(payload[6:10], "big")
            out_h = int.from_bytes(payload[10:14], "big")
        else:
            out_w = int.from_bytes(payload[6:8], "big")
            out_h = int.from_bytes(payload[8:10], "big")
        tiles = self.refs_of("dimg", item_id)
        if len(tiles) != rows * cols:
            self.warnings.append(
                f"grid #{item_id} 的 {rows}x{cols} 需要 {rows * cols} 块，"
                f"但 dimg 指向 {len(tiles)} 块"
            )
        return Grid(item_id, rows, cols, out_w, out_h, tiles, payload)

    def grid(self, item_id: int) -> Grid:
        for g in self.grids():
            if g.item_id == item_id:
                return g
        raise ValueError(f"#{item_id} 不是可解析的 grid item")

    def gain_map_candidate(self) -> Grid | None:
        """增益图候选：4 块 grid 优先，其次面积最小的 grid。

        实测华为样张的增益图固定 4 块；主图是 12/30 块，用块数即可分开。
        """
        grids = self.grids()
        if not grids:
            return None
        four = [g for g in grids if g.tile_count == 4]
        pool = four or grids
        return min(pool, key=lambda g: g.area)

    # -- 重封 ---------------------------------------------------------------

    def rewrap(self, grid_id: int, brand: str = "heic") -> bytes:
        """把 grid + 它的 tile 重封成只含这张图的最小 HEIF。

        关键点：tile 的码流、hvcC 参数集、grid 载荷全部原样搬运，
        只重算 iloc 偏移与 ipma 关联。系统解码器会自己拼 2x2。
        """
        g = self.grid(grid_id)
        if not g.tile_ids:
            raise ValueError(f"grid #{grid_id} 没有 dimg 引用，无法重封")

        missing = [i for i in g.tile_ids if i not in self.items]
        if missing:
            raise ValueError(f"grid #{grid_id} 引用了不存在的 item：{missing}")

        ordered = [g.item_id] + g.tile_ids
        payloads: dict[int, bytes] = {}
        for iid in ordered:
            payloads[iid] = g.payload if iid == g.item_id else self.item_bytes(self.items[iid])

        # ---- 属性：按源 ipma 逐项复制，去重后重建 ipco/ipma
        props: list[tuple[str, bytes]] = []
        prop_index: dict[tuple[str, bytes], int] = {}
        assoc_new: dict[int, list[tuple[int, bool]]] = {}
        for iid in ordered:
            assoc_new[iid] = []
            for idx, ess in self.assoc.get(iid, []):
                if not (1 <= idx <= len(self.properties)):
                    continue
                key = self.properties[idx - 1]
                if key not in prop_index:
                    props.append(key)
                    prop_index[key] = len(props)  # 1-based
                assoc_new[iid].append((prop_index[key], ess))

        # grid 必须有 ispe（部分写入器省略），否则解码器可能拿不到输出尺寸
        if not any(t == "ispe" for t, _ in self.properties_of(g.item_id)):
            ispe = full_box("ispe", 0, 0, g.out_w.to_bytes(4, "big") + g.out_h.to_bytes(4, "big"))
            props.append(("ispe", ispe))
            assoc_new[g.item_id].append((len(props), True))

        wide_ids = any(i > 0xFFFF for i in ordered)
        ftyp = box("ftyp", brand.encode("latin-1") + (0).to_bytes(4, "big") + b"mif1" + b"heic")

        tile_layout: list[tuple[int, int]] = [(i, len(payloads[i])) for i in g.tile_ids]

        def build_meta(offsets: list[int]) -> bytes:
            hdlr = full_box("hdlr", 0, 0, (0).to_bytes(4, "big") + b"pict" + (0).to_bytes(12, "big") + b"\x00")
            pitm = full_box("pitm", 1 if wide_ids else 0, 0,
                            g.item_id.to_bytes(4 if wide_ids else 2, "big"))
            # iinf 的 version 决定 count 宽度（0=16 位，1=32 位），与 infe 的 version 无关
            iinf = full_box("iinf", 1 if wide_ids else 0, 0,
                            _infe_entries(ordered, self.items, wide_ids))
            iref = full_box("iref", 1 if wide_ids else 0, 0,
                            _dimg_box(g.item_id, g.tile_ids, wide_ids))
            iprp = _iprp(props, assoc_new, wide_ids)
            entries = [(i, len(payloads[i])) for i in ordered]
            iloc = _iloc(entries, offsets, wide_ids)
            return box("meta", (0).to_bytes(4, "big") + hdlr + pitm + iinf + iref + iprp + iloc)

        probe = build_meta([0] * len(ordered))
        base = len(ftyp) + len(probe) + 8  # mdat 载荷起点
        offsets: list[int] = []
        pos = base
        for i in ordered:
            offsets.append(pos)
            pos += len(payloads[i])
        meta = build_meta(offsets)
        # 偏移字段宽度固定，长度不应变化；变了说明 iloc 编码出了问题，宁可报错也不产出坏文件
        if len(meta) != len(probe):
            raise RuntimeError("meta 长度随偏移变化（iloc 字段宽度不稳定），重封中止")

        mdat_payload = b"".join(payloads[i] for i in ordered)
        return ftyp + meta + box("mdat", mdat_payload)

    # -- 人类可读 -----------------------------------------------------------

    def summary(self) -> str:
        lines = [
            f"品牌={self.brand or '?'} compat={','.join(self.compat) or '-'} "
            f"顶层={','.join(b.typ for b in self.top)} item={len(self.items)}"
        ]
        if self.warnings:
            for w in self.warnings:
                lines.append(f"⚠ {w}")

        grids = self.grids()
        if grids:
            cand = self.gain_map_candidate()
            for g in grids:
                mark = "  ← 增益图候选" if cand is not None and g.item_id == cand.item_id else ""
                tiles = ",".join(f"#{i}" for i in g.tile_ids)
                lines.append(
                    f"grid #{g.item_id} {g.rows}x{g.cols} 输出 {g.out_w}x{g.out_h} "
                    f"块={g.tile_count}[{tiles}] 载荷={len(g.payload)}B{mark}"
                )
                for i in g.tile_ids:
                    it = self.items.get(i)
                    if it is not None:
                        lines.append(
                            f"    tile #{i} {it.item_type} {it.size}B 属性={self.describe_props(i)}"
                        )
        else:
            lines.append("grid: 无")

        mimes = [it for it in self.items.values() if it.item_type == "mime"]
        for it in mimes:
            lines.append(f"mime 项 #{it.item_id} content_type={it.content_type or '?'} "
                         f"{_fmt_size(it.size)}")
        others = [it for it in self.items.values()
                  if it.item_type not in ("hvc1", "grid", "mime", "infe")]
        if others:
            sample = ", ".join(f"#{i.item_id}({i.item_type},{_fmt_size(i.size)})" for i in others[:8])
            lines.append(f"其余类型: {sample}")

        if self.pitm is not None:
            it = self.items.get(self.pitm)
            lines.append(f"pitm=#{self.pitm} {it.item_type if it else '?'}")
        lines.append(f"iref: {len(self.refs)} 条")
        for r in self.refs[:6]:
            lines.append(f"    {r.typ} #{r.from_id} -> {','.join(f'#{x}' for x in r.to_ids)}")
        return "\n".join(lines)


def _fmt_size(n: int) -> str:
    if n < 0:
        return "?"
    if n >= 1024 * 1024:
        return f"{n / 1048576.0:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


def _infe_entries(ids: list[int], src: dict[int, Item], wide: bool) -> bytes:
    ver = 3 if wide else 2
    parts = []
    for iid in ids:
        it = src.get(iid)
        typ = (it.item_type if it else "????").encode("latin-1")[:4]
        name = (it.name if it else "").encode("utf-8")
        flags = 1 if (it and it.hidden and iid != ids[0]) else 0
        body = (iid.to_bytes(4 if wide else 2, "big") + (0).to_bytes(2, "big")
                + typ + len(name).to_bytes(1, "big") + name)
        parts.append(full_box("infe", ver, flags, body))
    return (len(ids).to_bytes(4 if wide else 2, "big")) + b"".join(parts)


def _dimg_box(from_id: int, to_ids: list[int], wide: bool) -> bytes:
    w = 4 if wide else 2
    return box("dimg", from_id.to_bytes(w, "big") + len(to_ids).to_bytes(2, "big")
               + b"".join(i.to_bytes(w, "big") for i in to_ids))


def _iprp(props: list[tuple[str, bytes]], assoc: dict[int, list[tuple[int, bool]]],
          wide: bool) -> bytes:
    # props 里存的是「属性载荷」，必须重新套上 box 头；直接拼载荷会得到一堆无法解析的废字节
    ipco = box("ipco", b"".join(box(t, p) for t, p in props))
    wide_idx = len(props) > 127
    entries = []
    for iid, lst in assoc.items():
        body = iid.to_bytes(4 if wide else 2, "big") + len(lst).to_bytes(1, "big")
        for idx, ess in lst:
            if wide_idx or wide:
                body += ((idx | (0x8000 if ess else 0))).to_bytes(2, "big")
            else:
                body += bytes([(idx & 0x7F) | (0x80 if ess else 0)])
        entries.append(body)
    ipma = full_box("ipma", 1 if wide else 0, 1 if wide_idx or wide else 0,
                    len(assoc).to_bytes(4, "big") + b"".join(entries))
    return box("iprp", ipco + ipma)


def _iloc(entries: list[tuple[int, int]], offsets: list[int], wide: bool) -> bytes:
    ver = 2 if wide else 0
    w = 4 if wide else 2
    body = bytes([0x44, 0x00])  # offset_size=4, length_size=4, base_offset_size=0
    body += len(entries).to_bytes(4 if wide else 2, "big")
    for (iid, length), off in zip(entries, offsets):
        if wide:
            body += iid.to_bytes(4, "big")
            body += (0).to_bytes(2, "big")  # construction_method
        else:
            body += iid.to_bytes(2, "big")
        body += (0).to_bytes(2, "big")      # data_reference_index
        body += (1).to_bytes(2, "big")      # extent_count
        body += off.to_bytes(4, "big") + length.to_bytes(4, "big")
    return full_box("iloc", ver, 0, body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _hex_dump(b: bytes, n: int = 64) -> str:
    head = b[:n]
    return " ".join(f"{x:02X}" for x in head) + (f" …（共 {len(b)}B）" if len(b) > n else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m uhdr.heif_grid",
        description="HEIC 结构诊断 + grid 增益图最小重封（纯标准库）",
    )
    ap.add_argument("file", help="HEIC/HEIF 文件")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结构")
    ap.add_argument("--dump", metavar="ID:PATH", action="append", default=[],
                    help="把某个 item 的载荷写到文件（可多次）")
    ap.add_argument("--hex", metavar="ID", type=int, default=None,
                    help="打印某个 item 载荷的前 64 字节")
    ap.add_argument("--rewrap", metavar="GRID_ID", type=int, default=None,
                    help="把指定 grid 重封成最小 HEIF（配合 -o）")
    ap.add_argument("-o", "--out", help="--rewrap 的输出文件")
    args = ap.parse_args(argv)

    with open(args.file, "rb") as f:
        data = f.read()

    try:
        heif = HeifGrids(data)
    except ValueError as e:
        print(f"不是 ISOBMFF：{e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "brand": heif.brand,
            "compat": heif.compat,
            "pitm": heif.pitm,
            "items": [
                {"id": i.item_id, "type": i.item_type, "name": i.name,
                 "content_type": i.content_type, "size": i.size, "hidden": i.hidden,
                 "props": [t for t, _ in heif.properties_of(i.item_id)]}
                for i in sorted(heif.items.values(), key=lambda x: x.item_id)
            ],
            "refs": [{"type": r.typ, "from": r.from_id, "to": r.to_ids} for r in heif.refs],
            "grids": [
                {"id": g.item_id, "rows": g.rows, "cols": g.cols,
                 "out": [g.out_w, g.out_h], "tiles": g.tile_ids}
                for g in heif.grids()
            ],
            "warnings": heif.warnings,
        }, ensure_ascii=False, indent=2))
        return 0

    print(heif.summary())

    for spec in args.dump:
        if ":" not in spec:
            print(f"--dump 需要 ID:PATH，收到 {spec}", file=sys.stderr)
            return 2
        sid, path = spec.split(":", 1)
        iid = int(sid, 0)
        it = heif.items.get(iid)
        if it is None:
            print(f"没有 #{iid}", file=sys.stderr)
            return 2
        blob = heif.item_bytes(it)
        with open(path, "wb") as f:
            f.write(blob)
        print(f"已导出 #{iid}（{it.item_type}, {len(blob)}B）→ {path}")

    if args.hex is not None:
        it = heif.items.get(args.hex)
        if it is None:
            print(f"没有 #{args.hex}", file=sys.stderr)
            return 2
        print(f"#{args.hex} {it.item_type} {it.size}B: {_hex_dump(heif.item_bytes(it))}")

    if args.rewrap is not None:
        if not args.out:
            print("--rewrap 需要 -o 指定输出文件", file=sys.stderr)
            return 2
        try:
            blob = heif.rewrap(args.rewrap)
        except (ValueError, RuntimeError) as e:
            print(f"重封失败：{e}", file=sys.stderr)
            return 3
        with open(args.out, "wb") as f:
            f.write(blob)
        print(f"已重封 grid #{args.rewrap} → {args.out}（{len(blob)}B）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
