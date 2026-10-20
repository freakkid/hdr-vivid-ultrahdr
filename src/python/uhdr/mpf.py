"""MPF（Multi-Picture Format, CIPA DC-007）APP2 段的构造与解析。

Ultra HDR 依赖 MPF 让主图索引到增益图副图。约定：
  - TIFF 头从段净荷 "MPF\\0" 之后的第一个字节开始
  - MPEntry 的 offset 相对于该 TIFF 头起点
  - 第 0 项（Primary）按业界惯例写 offset = 0，读取端不使用该值；
    副图（GainMap）写真实相对偏移
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MPF_SIG = b"MPF\x00"

TAG_MPF_VERSION = 0xB000
TAG_NUMBER_OF_IMAGES = 0xB001
TAG_MP_ENTRY = 0xB002
TAG_IMAGE_UID = 0xB003

TYPE_BYTE = 1
TYPE_ASCII = 2
TYPE_SHORT = 3
TYPE_LONG = 4
TYPE_UNDEFINED = 7

# 第 0 项标记为 "Baseline MP Primary Image"：MP Type Code = 1（bit29-31）
ATTR_PRIMARY = 0x20000000


@dataclass
class MpEntry:
    attribute: int
    size: int
    offset: int
    dep1: int = 0
    dep2: int = 0

    def to_bytes(self) -> bytes:
        return struct.pack(">IIIHH", self.attribute, self.size, self.offset, self.dep1, self.dep2)


@dataclass
class MpDirectory:
    entries: list[MpEntry]
    tiff_base_abs: int = 0   # TIFF 头在文件中的绝对偏移（解析时填充）

    @property
    def count(self) -> int:
        return len(self.entries)


def build_mpf(entries: list[MpEntry], image_uid: bytes = b"") -> bytes:
    """返回 APP2 段净荷（含 "MPF\\0"），调用方负责补 marker 与长度。"""
    if not entries:
        raise ValueError("MPF 至少需要 1 个 entry")
    uid = (image_uid or b"")[:32].ljust(33, b"\x00")

    n_entries = 4
    ifd_start = 8
    ifd_size = 2 + n_entries * 12 + 4
    data_start = ifd_start + ifd_size

    mp_entry_bytes = b"".join(e.to_bytes() for e in entries)
    mp_entry_off = data_start
    uid_off = data_start + len(mp_entry_bytes)

    out = bytearray()
    out += b"MM\x00\x2a"                    # TIFF header（大端）
    out += struct.pack(">I", ifd_start)     # IFD0 偏移

    out += struct.pack(">H", n_entries)
    out += struct.pack(">HHI", TAG_MPF_VERSION, TYPE_UNDEFINED, 4) + b"0100"
    out += struct.pack(">HHI", TAG_NUMBER_OF_IMAGES, TYPE_LONG, 1) + struct.pack(">I", len(entries))
    out += struct.pack(">HHI", TAG_MP_ENTRY, TYPE_UNDEFINED, len(mp_entry_bytes)) + struct.pack(">I", mp_entry_off)
    out += struct.pack(">HHI", TAG_IMAGE_UID, TYPE_UNDEFINED, len(uid)) + struct.pack(">I", uid_off)
    out += struct.pack(">I", 0)             # 无下一个 IFD

    assert len(out) == data_start, (len(out), data_start)
    out += mp_entry_bytes
    out += uid
    return MPF_SIG + bytes(out)


def parse_mpf(payload: bytes, tiff_base_abs: int = 0) -> MpDirectory:
    if not payload.startswith(MPF_SIG):
        raise ValueError("不是 MPF APP2 段（缺少 MPF\\0 标识）")
    tiff = payload[len(MPF_SIG):]
    if tiff[:4] != b"MM\x00\x2a":
        raise ValueError("仅支持大端 MPF（MM）")
    ifd_off = struct.unpack(">I", tiff[4:8])[0]
    count = struct.unpack(">H", tiff[ifd_off:ifd_off + 2])[0]

    def read_values(tag: int, typ: int, cnt: int, raw_value: bytes) -> bytes:
        size_map = {1: 1, 2: 1, 3: 2, 4: 4, 7: 1}
        total = size_map.get(typ, 1) * cnt
        if total <= 4:
            return raw_value[:total]
        off = struct.unpack(">I", raw_value)[0]
        return tiff[off:off + total]

    entries: list[MpEntry] = []
    for i in range(count):
        base = ifd_off + 2 + i * 12
        tag, typ, cnt = struct.unpack(">HHI", tiff[base:base + 8])
        raw = tiff[base + 8:base + 12]
        if tag == TAG_MP_ENTRY:
            blob = read_values(tag, typ, cnt, raw)
            for k in range(len(blob) // 16):
                a, s, o, d1, d2 = struct.unpack(">IIIHH", blob[k * 16:(k + 1) * 16])
                entries.append(MpEntry(a, s, o, d1, d2))
    return MpDirectory(entries=entries, tiff_base_abs=tiff_base_abs)


def wrap_app2(payload: bytes) -> bytes:
    if len(payload) + 2 > 0xFFFF:
        raise ValueError("APP2 段净荷超长（>65533），需要改用 APP11 大段扩展")
    return b"\xff\xe2" + struct.pack(">H", len(payload) + 2) + payload
