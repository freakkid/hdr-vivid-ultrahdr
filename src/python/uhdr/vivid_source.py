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
        # int.from_bytes 只接受 "little"/"big"，此前传的是 "<"/">" 会抛 ValueError，
        # 被下面的 except 吞掉 → 对所有文件都返回 None（函数整体失效）
        little = tiff[:2] == b"II"
        order = "little" if little else "big"
        ifd_off = int.from_bytes(tiff[4:8], order)
        try:
            if ifd_off + 2 > len(tiff):
                return None
            count = int.from_bytes(tiff[ifd_off:ifd_off + 2], order)
            for i in range(count):
                base = ifd_off + 2 + i * 12
                if base + 12 > len(tiff):
                    return None
                tag = int.from_bytes(tiff[base:base + 2], order)
                if tag != 0x010F:
                    continue
                cnt = int.from_bytes(tiff[base + 4:base + 8], order)
                voff = int.from_bytes(tiff[base + 8:base + 12], order)
                if voff + cnt > len(tiff):
                    return None
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


def _has_real_gain(raw: dict | None) -> bool:
    """主图 XMP 是否带真实增益参数（而非只有 Version 占位）。"""
    if not raw:
        return False
    return any(k in raw for k in ("GainMapMax", "GainMapMin", "HDRCapacityMax"))


def _meta_from_gainmap_xmp(data: bytes, start: int, end: int):
    """从增益图自身的 XMP APP1 里取 hdrgm 参数；没有则返回 None。"""
    for seg in jpegseg.header_segments_of(data, start, end):
        if seg.marker != 0xFFE1:
            continue
        payload = data[seg.payload_start:seg.payload_end]
        if not payload.startswith(xmp.XMP_APP1_SIG):
            continue
        txt = payload[len(xmp.XMP_APP1_SIG):].decode("utf-8", "replace")
        _dir, raw = xmp.parse_xmp(txt)
        if _has_real_gain(raw):
            return xmp.meta_from_dict(raw)
    return None


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
            # 只有主图 XMP 里**确实带增益参数**才允许覆盖 ISO 值。
            # 我们自己导出的文件主图 XMP 只写 hdrgm:Version（完整参数在增益图侧），
            # 此前用 `if raw:` 判空，会拿全默认值(2.0)覆盖掉刚解出的 2.148。
            has_real_gain = _has_real_gain(raw)
            if has_real_gain:
                meta = xmp.meta_from_dict(raw)
                notes.append("主图 XMP 含增益参数，优先采用 XMP")
            elif raw:
                notes.append("主图 XMP 仅有版本信息，增益参数仍取自 ISO 21496-1")
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
            # hdrgm 的**完整参数应优先取自增益图自身的 XMP**；
            # 主图 XMP 按设计只写 hdrgm:Version（占位）。
            # 此前直接用主图 XMP 的 raw 覆盖，得到的是全默认值(max=2.0)，
            # 导致「导出 → 再解析」时 4.43x 静默退化为 4.00x。
            meta = _meta_from_gainmap_xmp(data, gm_start, gm_end)
            if meta is not None:
                notes.append("hdrgm 参数取自增益图自身 XMP")
            elif _has_real_gain(raw):
                meta = xmp.meta_from_dict(raw)
                notes.append("增益图无 hdrgm，改用主图 XMP 参数")
            else:
                notes.append("增益图与主图 XMP 均无完整 hdrgm，回退默认值")
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
