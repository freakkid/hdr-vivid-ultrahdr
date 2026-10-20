"""HDR 源图解析：ISO Ultra HDR（XMP 或 ISO 21496-1 两种载体）与华为 CUVA。

判定优先级（高 → 低），**不允许靠猜**：
  1. 主图带 APP2 `urn:iso:std:iso:ts:21496:-1`  → ISO Ultra HDR（ISO 载体）
  2. 主图带 XMP `<Container:Directory>`        → ISO Ultra HDR（XMP 载体）
  3. 副图首段为 APP5(0xFFE5)                    → CUVA / 华为 HDR Vivid
  4. EXIF Make=HUAWEI 且含 `_cuva` 标记          → CUVA（仅在前三条都不成立时）
  5. 其余含副图的容器                            → unknown_mpo（不猜，不生成 hdrgm）

实测教训：华为设备产出的文件可以**只有 ISO 21496-1、完全没有 XMP**，同时 EXIF 里
仍保留 `_cuva` 字样。若把「含 `_cuva`」当成强证据，就会把 ISO Ultra HDR 误判为 CUVA，
表现为「增益图丢失」——其实文件完好，是解析器认错了。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import iso21496, jpegseg, mpf, xmp

CUVA_SIG = b"_cuva"
APP5 = 0xFFE5


@dataclass
class HdrSource:
    path: str
    container: str                     # cuva / iso_ultrahdr / plain_jpeg / unknown_mpo
    primary_jpeg: bytes
    gainmap_jpeg: bytes | None
    meta: xmp.GainMapMeta | None
    exif_make: str | None = None
    iso_meta: iso21496.IsoGainMapMeta | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def has_gainmap(self) -> bool:
        return self.gainmap_jpeg is not None

    def describe(self) -> str:
        gm = "无" if not self.gainmap_jpeg else f"{len(self.gainmap_jpeg):,} bytes"
        carrier = "ISO 21496-1" if self.iso_meta else ("XMP" if self.meta else "无")
        return (f"<HdrSource {self.container}> 主图={len(self.primary_jpeg):,}B "
                f"增益图={gm} 元数据={carrier} Make={self.exif_make}")


def _parse_exif_make(data: bytes) -> str | None:
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
    return CUVA_SIG in head or CUVA_SIG.decode().encode("utf-16-le") in head


def _strip_private_segments(jpeg: bytes) -> bytes:
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


@dataclass
class _ScanResult:
    xmp_seg: jpegseg.Segment | None = None
    iso_seg: jpegseg.Segment | None = None
    mpf_seg: jpegseg.Segment | None = None


def _scan_primary(data: bytes, start: int, end: int) -> _ScanResult:
    r = _ScanResult()
    for seg in jpegseg.header_segments_of(data, start, end):
        if seg.marker == 0xFFE1:
            payload = data[seg.payload_start:seg.payload_end]
            if payload.startswith(xmp.XMP_APP1_SIG):
                r.xmp_seg = seg
        elif seg.marker == 0xFFE2:
            payload = data[seg.payload_start:seg.payload_end]
            if iso21496.is_iso_app2(payload):
                r.iso_seg = seg
            elif payload.startswith(mpf.MPF_SIG):
                r.mpf_seg = seg
    return r


def _find_iso_in_gainmap(data: bytes, start: int, end: int) -> bytes | None:
    for seg in jpegseg.header_segments_of(data, start, end):
        if seg.marker == 0xFFE2:
            payload = data[seg.payload_start:seg.payload_end]
            if iso21496.is_iso_app2(payload):
                return payload
    return None


def _iso_to_hdrgm(iso: iso21496.IsoGainMapMeta) -> xmp.GainMapMeta:
    import math

    def lg(v: float) -> float:
        return math.log2(v) if v > 0 else 0.0

    return xmp.GainMapMeta(
        gain_map_min=lg(iso.min_content_boost),
        gain_map_max=lg(iso.max_content_boost),
        gamma=iso.gamma,
        offset_sdr=iso.offset_sdr,
        offset_hdr=iso.offset_hdr,
        hdr_capacity_min=lg(iso.hdr_capacity_min),
        hdr_capacity_max=lg(iso.hdr_capacity_max),
    )


def parse_hdr_source(data: bytes, path: str = "<memory>") -> HdrSource:
    images = jpegseg.find_all_images(data)
    notes: list[str] = []
    if not images:
        raise ValueError("不是合法的 JPEG 文件")

    primary = data[images[0][0]:images[0][1]]
    exif_make = _parse_exif_make(primary)
    scan = _scan_primary(data, images[0][0], images[0][1])

    if len(images) == 1:
        return HdrSource(path, "plain_jpeg", primary, None, None, exif_make,
                         notes=["单图 JPEG，无增益图：只能输出纯 SDR"])

    gm_start, gm_end = images[1]
    gainmap_raw = data[gm_start:gm_end]

    # ---- 证据 1：ISO 21496-1 APP2 ----
    if scan.iso_seg is not None:
        main_iso = iso21496.parse_app2(data[scan.iso_seg.payload_start:scan.iso_seg.payload_end])
        gm_iso_payload = _find_iso_in_gainmap(data, gm_start, gm_end)
        iso_meta = None
        if gm_iso_payload:
            iso_meta = iso21496.parse_app2(gm_iso_payload)
            if iso_meta:
                notes.append(f"完整增益参数取自增益图 APP2（原始值: "
                             + ", ".join(f"{v:g}" for v in iso_meta.raw_values) + "）")
        if iso_meta is None:
            iso_meta = main_iso
        if iso_meta is None:
            iso_meta = iso21496.IsoGainMapMeta(notes=["仅发现 version 块，参数回退默认值"])
        notes.extend(iso_meta.notes)

        # 同时若还有 XMP，则以 XMP 为准（可读性更好、语义更明确）
        meta = None
        if scan.xmp_seg is not None:
            txt = data[scan.xmp_seg.payload_start + len(xmp.XMP_APP1_SIG):scan.xmp_seg.payload_end].decode("utf-8", "replace")
            _dir, raw = xmp.parse_xmp(txt)
            if raw:
                meta = xmp.meta_from_dict(raw)
                notes.append("同时存在 XMP，hdrgm 参数优先采用 XMP")
        if meta is None:
            meta = _iso_to_hdrgm(iso_meta)
            notes.append("增益参数由 ISO 21496-1 换算为 hdrgm（log2 域）")

        return HdrSource(path, "iso_ultrahdr", primary, gainmap_raw, meta, exif_make,
                         iso_meta=iso_meta, notes=notes)

    # ---- 证据 2：XMP GContainer ----
    if scan.xmp_seg is not None and scan.mpf_seg is not None:
        txt = data[scan.xmp_seg.payload_start + len(xmp.XMP_APP1_SIG):scan.xmp_seg.payload_end].decode("utf-8", "replace")
        directory, raw = xmp.parse_xmp(txt)
        if directory.by_semantic(xmp.SEMANTIC_GAINMAP):
            meta = xmp.meta_from_dict(raw) if raw else None
            if meta is None:
                notes.append("XMP 缺少 hdrgm 参数，回退默认值")
            return HdrSource(path, "iso_ultrahdr", primary, gainmap_raw, meta, exif_make,
                             notes=notes)

    # ---- 证据 3：CUVA 的 APP5 副图 ----
    second_segs = jpegseg.header_segments_of(data, gm_start, gm_end)
    if second_segs and second_segs[0].marker == APP5:
        notes.append("副图首段为 APP5(0xFFE5)，判定为 CUVA / 华为 HDR Vivid")
        notes.append("CUVA 增益图语义与 hdrgm 不等价，建议走「反解 HDR→重算增益图」")
        return HdrSource(path, "cuva", primary, _strip_private_segments(gainmap_raw), None,
                         exif_make, notes=notes)

    # ---- 证据 4：厂商标记（弱证据，放最后）----
    if (exif_make or "").upper().startswith("HUAWEI") and _has_cuva_marker(data):
        notes.append("仅凭 EXIF Make=HUAWEI 与 _cuva 标记判定 CUVA（结构上未见 APP5，置信度低）")
        return HdrSource(path, "cuva", primary, _strip_private_segments(gainmap_raw), None,
                         exif_make, notes=notes)

    notes.append("存在副图但缺少 ISO/XMP/APP5 证据，按未知 MPO 处理（不生成 HDR 元数据）")
    return HdrSource(path, "unknown_mpo", primary, _strip_private_segments(gainmap_raw), None,
                     exif_make, notes=notes)


def parse_file(path: str) -> HdrSource:
    with open(path, "rb") as f:
        return parse_hdr_source(f.read(), path)
