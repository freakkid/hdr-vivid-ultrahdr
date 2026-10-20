"""HDR 源图解析：华为 HDR Vivid（CUVA 变体）与 ISO Ultra HDR。

已核实的格式差异（来源见 docs/技术方案.md 附录）：
  - 华为 CUVA / HDR Vivid 静图：JPEG MPO 容器，副图以 APP5(0xFFE5) 起始，
    并在 EXIF 中带厂商标记（Make=HUAWEI）与 "_cuva" 标题标记
  - ISO Ultra HDR：副图由 APP2(0xFFE2) MPF 索引 + 主图 XMP GContainer 描述
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import jpegseg, mpf, xmp

CUVA_SIG = b"_cuva"
APP5 = 0xFFE5


@dataclass
class HdrSource:
    path: str
    container: str                     # cuva / iso_ultrahdr / plain_jpeg
    primary_jpeg: bytes
    gainmap_jpeg: bytes | None
    meta: xmp.GainMapMeta | None
    exif_make: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def has_gainmap(self) -> bool:
        return self.gainmap_jpeg is not None

    def describe(self) -> str:
        gm = "无" if not self.gainmap_jpeg else f"{len(self.gainmap_jpeg)} bytes"
        return (f"<HdrSource {self.container}> 主图={len(self.primary_jpeg)}B "
                f"增益图={gm} Make={self.exif_make} 备注={self.notes}")


def _parse_exif_make(data: bytes) -> str | None:
    """从首个 Exif APP1 段读 Make（0x010F）。"""
    for seg in jpegseg.iter_header_segments(data, 0):
        if seg.marker != 0xFFE1:
            continue
        payload = data[seg.payload_start:seg.payload_end]
        if not payload.startswith(b"Exif\x00\x00"):
            continue
        tiff = payload[6:]
        if len(tiff) < 8:
            return None
        little = tiff[:2] == b"II"
        endian = "<" if little else ">"
        ifd_off = int.from_bytes(tiff[4:8], "little" if little else "big")
        try:
            count = int.from_bytes(tiff[ifd_off:ifd_off + 2], endian)
            for i in range(count):
                base = ifd_off + 2 + i * 12
                tag = int.from_bytes(tiff[base:base + 2], endian)
                if tag != 0x010F:
                    continue
                cnt = int.from_bytes(tiff[base + 4:base + 8], endian)
                voff = int.from_bytes(tiff[base + 8:base + 12], endian)
                return tiff[voff:voff + cnt].decode("ascii", errors="ignore").strip("\x00 ")
        except Exception:  # noqa: BLE001
            return None
    return None


def _has_cuva_marker(data: bytes, limit: int = 262144) -> bool:
    head = data[:limit]
    if CUVA_SIG in head:
        return True
    return CUVA_SIG.decode().encode("utf-16-le") in head


def _strip_private_segments(jpeg: bytes) -> bytes:
    """剥掉增益图自带的 APP5 私有段，输出干净的单图 JPEG。"""
    end = jpegseg.find_image_end(jpeg, 0)
    out = bytearray(jpeg[0:2])
    for seg in jpegseg.iter_header_segments(jpeg, 0):
        if seg.marker == APP5:
            continue
        if seg.marker == jpegseg.SOS:
            out += jpeg[seg.start:end]
            return bytes(out)
        out += jpeg[seg.start:seg.end]
    return jpeg


def parse_hdr_source(data: bytes, path: str = "<memory>") -> HdrSource:
    images = jpegseg.find_all_images(data)
    notes: list[str] = []

    if not images:
        raise ValueError("不是合法的 JPEG 文件")

    primary = data[images[0][0]:images[0][1]]
    exif_make = _parse_exif_make(primary if primary[:2] == b"\xff\xd8" else data)

    if len(images) == 1:
        return HdrSource(path, "plain_jpeg", primary, None, None, exif_make,
                         ["单图 JPEG，无增益图：只能输出纯 SDR"])

    second_start, second_end = images[1]
    gainmap_raw = data[second_start:second_end]

    # 1) ISO Ultra HDR：主图有 GContainer XMP + MPF，且副图被 MPF 索引
    xmp_seg = None
    mpf_seg = None
    for seg in jpegseg.iter_header_segments(data, 0):
        if seg.marker == 0xFFE1 and data[seg.payload_start:seg.payload_start + len(xmp.XMP_APP1_SIG)] == xmp.XMP_APP1_SIG:
            xmp_seg = seg
        if seg.marker == 0xFFE2 and data[seg.payload_start:seg.payload_start + 4] == mpf.MPF_SIG:
            mpf_seg = seg

    if xmp_seg and mpf_seg:
        xml_text = data[xmp_seg.payload_start + len(xmp.XMP_APP1_SIG):xmp_seg.payload_end].decode("utf-8", "replace")
        directory, hdrgm_raw = xmp.parse_xmp(xml_text)
        if directory.by_semantic(xmp.SEMANTIC_GAINMAP):
            meta = xmp.meta_from_dict(hdrgm_raw) if hdrgm_raw else None
            if meta is None:
                notes.append("ISO Ultra HDR 源缺少 hdrgm 参数，回退默认值")
            return HdrSource(path, "iso_ultrahdr", primary, gainmap_raw, meta, exif_make, notes)

    # 2) CUVA：副图首段为 APP5
    second_segs = jpegseg.header_segments_of(data, second_start, second_end)
    if second_segs and second_segs[0].marker == APP5:
        notes.append("检测到 APP5 副图，判定为 CUVA / 华为 HDR Vivid 容器")
        notes.append("CUVA 增益图语义与 hdrgm 不完全等价，建议走「反解 HDR→重算增益图」路径")
        return HdrSource(path, "cuva", primary, _strip_private_segments(gainmap_raw), None,
                         exif_make, notes)

    # 3) 启发式兜底：厂商标记
    if (exif_make or "").upper().startswith("HUAWEI") or _has_cuva_marker(data):
        notes.append("通过 EXIF/字节标记判定为 CUVA，但副图缺少 APP5 标记")
        return HdrSource(path, "cuva", primary, _strip_private_segments(gainmap_raw), None,
                         exif_make, notes)

    notes.append("存在第二张图但未识别出 HDR 容器类型，按未知副图处理")
    return HdrSource(path, "unknown_mpo", primary, _strip_private_segments(gainmap_raw), None,
                     exif_make, notes)


def parse_file(path: str) -> HdrSource:
    with open(path, "rb") as f:
        return parse_hdr_source(f.read(), path)
