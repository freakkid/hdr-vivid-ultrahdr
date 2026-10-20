"""Ultra HDR 导出文件自检器。

设计原则：
  1. 只依赖字节流结构 + JPEG SOF 段，不依赖图片解码器（可在无 GUI 环境跑）
  2. 每条检查有稳定 id，便于在 CI / 灰度上报中统计
  3. 区分 ERROR（HDR 链路已断，等同于降级为 SDR）与 WARN（有风险但可显示）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import jpegseg, mpf, xmp

SOI = b"\xff\xd8"
SOF_MARKERS = {0xFFC0, 0xFFC1, 0xFFC2, 0xFFC3, 0xFFC5, 0xFFC6, 0xFFC7,
               0xFFC9, 0xFFCA, 0xFFCB, 0xFFCD, 0xFFCE, 0xFFCF}


@dataclass
class Check:
    check_id: str
    ok: bool
    level: str          # ERROR / WARN / INFO
    detail: str = ""


@dataclass
class Report:
    path: str
    checks: list[Check] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not [c for c in self.checks if not c.ok and c.level == "ERROR"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.level == "WARN"]

    def add(self, check_id: str, ok: bool, level: str = "ERROR", detail: str = "") -> None:
        self.checks.append(Check(check_id, ok, level, detail))

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "facts": self.facts,
            "checks": [
                {"id": c.check_id, "ok": c.ok, "level": c.level, "detail": c.detail}
                for c in self.checks
            ],
        }

    def pretty(self) -> str:
        lines = [f"[{'PASS' if self.ok else 'FAIL'}] {self.path}"]
        if self.facts:
            lines.append("  事实: " + ", ".join(f"{k}={v}" for k, v in self.facts.items()))
        for c in self.checks:
            mark = "OK  " if c.ok else ("WARN" if c.level == "WARN" else "ERR ")
            lines.append(f"  [{mark}] {c.check_id}: {c.detail}")
        return "\n".join(lines)


def jpeg_size(data: bytes, start: int = 0) -> tuple[int, int] | None:
    """从 SOF 段读宽高。结构损坏时返回 None，不抛异常。"""
    try:
        segs = list(jpegseg.iter_header_segments(data, start))
    except jpegseg.JpegParseError:
        return None
    for seg in segs:
        if seg.marker in SOF_MARKERS:
            p = seg.payload_start
            if p + 5 > seg.payload_end:
                return None
            h = int.from_bytes(data[p + 1:p + 3], "big")
            w = int.from_bytes(data[p + 3:p + 5], "big")
            return w, h
    return None


def _jpeg_ends_cleanly(data: bytes, start: int, end: int) -> bool:
    return data[start:start + 2] == SOI and data[end - 2:end] == b"\xff\xd9"


def validate_bytes(data: bytes, path: str = "<memory>", *, ratio_tol: float = 0.02) -> Report:
    """自检入口。任何内部异常都转成失败项，保证调用方（App/CI）不会崩溃。"""
    try:
        return _validate_bytes(data, path, ratio_tol=ratio_tol)
    except Exception as exc:  # noqa: BLE001
        rep = Report(path=path)
        rep.add("internal.validator_crashed", False, detail=f"自检器内部异常: {exc!r}")
        return rep


def _validate_bytes(data: bytes, path: str = "<memory>", *, ratio_tol: float = 0.02) -> Report:
    rep = Report(path=path)

    # 1. 基本完整性
    rep.add("file.soi_eoi", data.startswith(SOI) and data.endswith(b"\xff\xd9"),
            detail="文件首尾为 SOI/EOI")

    images = jpegseg.find_all_images(data)
    rep.add("container.two_images", len(images) >= 2, "WARN",
            detail=f"检测到 {len(images)} 个 JPEG 图像（Ultra HDR 需要 >= 2）")

    # 2. 主图 XMP
    seg1 = jpegseg.find_segment(data, 0, 0xFFE1)
    has_xmp = bool(seg1 and data[seg1.payload_start:seg1.payload_start + len(xmp.XMP_APP1_SIG)] == xmp.XMP_APP1_SIG)
    rep.add("meta.xmp_present", has_xmp, detail="存在 XMP APP1 段")

    directory = xmp.ContainerDirectory()
    hdrgm_raw: dict[str, str] = {}
    if has_xmp:
        payload = data[seg1.payload_start:seg1.payload_end]
        xml_text = payload[len(xmp.XMP_APP1_SIG):].decode("utf-8", errors="replace")
        try:
            directory, hdrgm_raw = xmp.parse_xmp(xml_text)
            rep.add("meta.xmp_parsed", True, detail=f"解析到 {len(directory.items)} 个媒体项")
        except Exception as exc:  # noqa: BLE001
            rep.add("meta.xmp_parsed", False, detail=f"XMP 解析失败: {exc}")
    else:
        rep.add("meta.xmp_parsed", False, detail="无 XMP 段")

    primary_item = directory.by_semantic(xmp.SEMANTIC_PRIMARY)
    gm_item = directory.by_semantic(xmp.SEMANTIC_GAINMAP)
    rep.add("meta.primary_item", primary_item is not None, detail="XMP 含 Semantic=Primary 项")
    rep.add("meta.gainmap_item", gm_item is not None, detail="XMP 含 Semantic=GainMap 项")

    # 3. 主图侧：只要求 hdrgm:Version 信号位（完整参数在增益图 XMP，见第 5 步）
    if hdrgm_raw.get("Version"):
        rep.add("meta.primary_version", True, detail=f'hdrgm:Version="{hdrgm_raw["Version"]}"')
    else:
        rep.add("meta.primary_version", False, detail="主图 XMP 缺少 hdrgm:Version 信号位")

    # 4. MPF
    seg2 = None
    for seg in jpegseg.iter_header_segments(data, 0):
        if seg.marker == 0xFFE2 and data[seg.payload_start:seg.payload_start + 4] == mpf.MPF_SIG:
            seg2 = seg
            break
    mp_dir = None
    if seg2:
        try:
            mp_dir = mpf.parse_mpf(data[seg2.payload_start:seg2.payload_end], tiff_base_abs=seg2.payload_start + 4)
            rep.add("meta.mpf_present", True, detail=f"MPF 含 {mp_dir.count} 个 entry")
        except Exception as exc:  # noqa: BLE001
            rep.add("meta.mpf_present", False, detail=f"MPF 解析失败: {exc}")
    else:
        rep.add("meta.mpf_present", False, detail="缺少 MPF APP2 段")

    # 5. 偏移一致性：MPF 指向的副图必须是合法 JPEG，且与 XMP 记录一致
    if mp_dir and len(mp_dir.entries) >= 2 and gm_item:
        e = mp_dir.entries[1]
        abs_off = mp_dir.tiff_base_abs + e.offset
        in_range = 0 <= abs_off < len(data) - 1
        at_soi = in_range and data[abs_off:abs_off + 2] == SOI
        rep.add("offset.mpf_points_to_soi", at_soi,
                detail=f"MPF 偏移 {e.offset}（绝对 {abs_off}）指向 {'SOI' if at_soi else '非 SOI'}")
        rep.add("offset.mpf_length_match", e.size == gm_item.length,
                detail=f"MPF size={e.size} vs XMP length={gm_item.length}")
        rep.add("offset.xmp_mpf_agree", e.offset == gm_item.offset,
                detail=f"MPF offset={e.offset} vs XMP offset={gm_item.offset}")

        if at_soi:
            # 5.1 增益图自身必须携带完整 hdrgm 参数（规范把参数放在增益图 XMP 里）
            gm_hdrgm_raw: dict[str, str] = {}
            try:
                for seg in jpegseg.iter_header_segments(data, abs_off):
                    if seg.marker == 0xFFE1 and data[seg.payload_start:seg.payload_start + len(xmp.XMP_APP1_SIG)] == xmp.XMP_APP1_SIG:
                        txt = data[seg.payload_start + len(xmp.XMP_APP1_SIG):seg.payload_end].decode("utf-8", "replace")
                        _, gm_hdrgm_raw = xmp.parse_xmp(txt)
                        break
            except jpegseg.JpegParseError:
                pass

            rep.add("meta.gainmap_xmp_present", bool(gm_hdrgm_raw),
                    detail=f"增益图 XMP 字段数={len(gm_hdrgm_raw)}")

            effective = gm_hdrgm_raw or hdrgm_raw
            eff_meta = xmp.meta_from_dict(effective)
            errs = eff_meta.validate()
            has_params = bool(effective.get("GainMapMax") or effective.get("HDRCapacityMax"))
            if not has_params:
                errs.append("缺少 GainMapMax / HDRCapacityMax 等重建参数")
            src = "增益图" if gm_hdrgm_raw else "主图"
            rep.add("meta.hdrgm_valid", not errs,
                    detail="; ".join(errs) or f"hdrgm 参数自洽（来源：{src}）")
            rep.facts["hdrgm_source"] = "gainmap" if gm_hdrgm_raw else "primary"
            rep.facts["hdrgm"] = {
                "Version": effective.get("Version"),
                "GainMapMax": effective.get("GainMapMax"),
                "HDRCapacityMax": effective.get("HDRCapacityMax"),
                "Gamma": effective.get("Gamma"),
            }

            # 两个字段都是 log2 域，实践中应一致。写成线性 boost（如 4.0）会让
            # 读取端认为容量是 16x，属于隐蔽的语义错误，这里作为门禁拦下。
            delta = abs(eff_meta.hdr_capacity_max - eff_meta.gain_map_max)
            rep.add("meta.capacity_matches_gainmap", delta <= 0.01, "WARN",
                    detail=f"GainMapMax={eff_meta.gain_map_max:.4f} "
                           f"HDRCapacityMax={eff_meta.hdr_capacity_max:.4f} 差={delta:.4f}（应 <0.01）")

            gm_end = None
            try:
                gm_end = jpegseg.find_image_end(data, abs_off)
            except jpegseg.JpegParseError as exc:
                rep.add("offset.gainmap_complete", False,
                        detail=f"增益图无法解析到 EOI（可能被截断）: {exc}")
            if gm_end is not None:
                rep.add("offset.gainmap_complete", _jpeg_ends_cleanly(data, abs_off, gm_end),
                        detail=f"增益图范围 [{abs_off}, {gm_end})")
                rep.add("offset.no_trailing_garbage", gm_end == len(data),
                        "WARN", detail=f"文件尾部剩余 {len(data) - gm_end} 字节")

            base_size = jpeg_size(data, 0)
            gm_size = jpeg_size(data, abs_off)
            if base_size and gm_size:
                bw, bh = base_size
                gw, gh = gm_size
                rep.facts["primary_size"] = f"{bw}x{bh}"
                rep.facts["gainmap_size"] = f"{gw}x{gh}"
                ratio_w = gw / bw
                ratio_h = gh / bh
                consistent = abs(ratio_w - ratio_h) <= ratio_tol
                rep.add("geometry.aspect_consistent", consistent,
                        detail=f"宽比={ratio_w:.4f} 高比={ratio_h:.4f}（容差 {ratio_tol}）")
                rep.add("geometry.gainmap_not_larger", gw <= bw and gh <= bh, "WARN",
                        detail="增益图尺寸不应大于主图")
                rep.facts["gainmap_scale"] = round(ratio_w, 4)

        if primary_item:
            # 主图长度应等于增益图的绝对起点（主图范围 = [0, 增益图起点)）
            rep.add("offset.primary_len_match", primary_item.length == abs_off,
                    detail=f"XMP primary length={primary_item.length} vs 增益图绝对起点={abs_off}")
            rep.add("offset.primary_offset_zero", primary_item.offset == 0, "INFO",
                    detail=f"XMP primary offset={primary_item.offset}（惯例为 0）")
    return rep


def validate_file(path: str) -> Report:
    with open(path, "rb") as f:
        return validate_bytes(f.read(), path)
