"""JPEG 段级解析：定位主图 / 副图边界，提取 APPn 段。

只做结构解析，不做像素解码；用于 CUVA（华为 HDR Vivid）与 ISO Ultra HDR
两类多图 JPEG 的容器分析。纯标准库实现，便于在服务端 / CI 中做离线校验。
"""
from __future__ import annotations

from dataclasses import dataclass

SOI = 0xFFD8
EOI = 0xFFD9
SOS = 0xFFDA


class JpegParseError(ValueError):
    """JPEG 结构异常。"""


@dataclass(frozen=True)
class Segment:
    marker: int          # 0xFFE1 / 0xFFE2 / 0xFFDA ...
    start: int           # marker 首字节在文件中的绝对偏移
    end: int             # 段结束位置（不含）
    payload_start: int   # 段净荷起始
    payload_end: int     # 段净荷结束

    @property
    def marker_name(self) -> str:
        if 0xFFE0 <= self.marker <= 0xFFEF:
            return f"APP{self.marker - 0xFFE0}"
        return f"0x{self.marker:04X}"


def _read_segment_length(data: bytes, pos: int) -> int:
    if pos + 4 > len(data):
        raise JpegParseError(f"段头越界 @ {pos}")
    return int.from_bytes(data[pos + 2:pos + 4], "big")


def iter_header_segments(data: bytes, start: int = 0):
    """遍历 SOS 之前的段（含 SOS 段头），不进入熵编码数据。"""
    if data[start:start + 2] != b"\xff\xd8":
        raise JpegParseError(f"偏移 {start} 处不是 SOI")
    pos = start + 2
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            raise JpegParseError(f"段边界错位 @ {pos}")
        marker = 0xFF00 | data[pos + 1]
        # 填充字节：marker 前允许任意多个 0xFF（JPEG 规范允许，部分相机/编码器会写）。
        # 只能前进 1 字节——前进 2 会跳过真正的 marker 起始位，把后续长度读错。
        if marker == 0xFFFF:
            pos += 1
            continue
        if marker == EOI:
            return
        if marker == 0xFF01 or 0xFFD0 <= marker <= 0xFFD7:
            pos += 2
            continue
        seglen = _read_segment_length(data, pos)
        seg_end = pos + 2 + seglen
        if seglen < 2 or seg_end > len(data):
            raise JpegParseError(f"段长度非法 @ {pos}: {seglen}")
        yield Segment(
            marker=marker,
            start=pos,
            end=seg_end,
            payload_start=pos + 4,
            payload_end=seg_end,
        )
        if marker == SOS:
            return
        pos = seg_end


def find_image_end(data: bytes, start: int = 0) -> int:
    """返回从 start 开始的完整 JPEG（含 EOI）的结束偏移。"""
    pos = start + 2
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:  # 填充字节，前进 1 字节重新找 marker
            pos += 1
            continue
        if marker == 0xDA:  # SOS：跳过熵编码数据
            seglen = _read_segment_length(data, pos)
            pos += 2 + seglen
            while pos + 1 < len(data):
                if data[pos] == 0xFF:
                    nxt = data[pos + 1]
                    if nxt != 0x00 and not (0xD0 <= nxt <= 0xD7):
                        break
                    pos += 2
                    continue
                pos += 1
            continue
        if marker == 0xD9:
            return pos + 2
        if marker == 0x00 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        seglen = _read_segment_length(data, pos)
        pos += 2 + seglen
    raise JpegParseError("未找到 EOI，文件可能被截断")


def find_all_images(data: bytes) -> list[tuple[int, int]]:
    """扫描文件中所有 JPEG 图像（MPO 容器里每张图一个 SOI/EOI 对）。"""
    images: list[tuple[int, int]] = []
    pos = 0
    while True:
        pos = data.find(b"\xff\xd8\xff", pos)
        if pos < 0:
            break
        try:
            end = find_image_end(data, pos)
        except JpegParseError:
            break
        images.append((pos, end))
        pos = end
    return images


def header_segments_of(data: bytes, start: int, end: int) -> list[Segment]:
    """返回单张图的头部段列表（在 [start, end) 内截断）。"""
    segs = []
    for seg in iter_header_segments(data, start):
        if seg.end > end:
            break
        segs.append(seg)
    return segs


def find_segment(data: bytes, start: int, marker: int) -> Segment | None:
    for seg in iter_header_segments(data, start):
        if seg.marker == marker:
            return seg
    return None


def strip_header_segments(data: bytes, start: int, markers: set[int]) -> bytes:
    """去掉指定 marker 的段，返回新的单图 JPEG 字节流。"""
    end = find_image_end(data, start)
    out = bytearray(data[start:start + 2])
    for seg in iter_header_segments(data, start):
        if seg.marker in markers:
            continue
        if seg.marker == SOS:
            out += data[seg.start:end]
            return bytes(out)
        out += data[seg.start:seg.end]
    raise JpegParseError("未遇到 SOS")
