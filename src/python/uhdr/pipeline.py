"""端到端导出管线：HDR 源 → Ultra HDR JPEG。

阶段：
  1. parse     解析源容器，取出 SDR 主图 / 增益图 / 元数据
  2. orient    归一化 EXIF Orientation（物理旋转，避免读取端二次旋转）
  3. transform 可选裁剪/旋转 + 预压缩到目标最长边（增益图同步）
  4. encode    主图与增益图分别重编码（严格保持比例关系）
  5. package   组装 XMP + MPF + 副图
  6. validate  结构性自检（不通过则拒绝输出为「HDR 可用」）
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import asdict, dataclass, field

from PIL import Image

from . import gainmap_ops, vivid_source, writer, xmp
from .validator import validate_bytes

DEFAULT_LONG_EDGE = 2048
DEFAULT_MAIN_QUALITY = 92
DEFAULT_GAINMAP_QUALITY = 85


@dataclass
class ExportOptions:
    long_edge: int = DEFAULT_LONG_EDGE
    main_quality: int = DEFAULT_MAIN_QUALITY
    gainmap_quality: int = DEFAULT_GAINMAP_QUALITY
    gain_map_max_log2: float = 2.0        # 对应 4x 峰值增益（约 2.0 log2）
    crop_box: tuple[int, int, int, int] | None = None
    rotate: int = 0


@dataclass
class ExportResult:
    src_path: str
    out_path: str
    ok: bool
    source_container: str
    steps: list[str] = field(default_factory=list)
    layout: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    size_before: int = 0
    size_after: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def _encode_main(img: Image.Image, quality: int, exif_bytes: bytes | None) -> bytes:
    buf = io.BytesIO()
    kwargs = dict(format="JPEG", quality=quality, subsampling=0, optimize=True)
    if exif_bytes:
        kwargs["exif"] = exif_bytes
    img.save(buf, **kwargs)
    return buf.getvalue()


def _encode_gainmap(gm: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    gm.convert("L").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _normalize_exif_orientation(img: Image.Image, gm: Image.Image, raw_exif: bytes | None):
    """物理应用 EXIF 方向并清除标记，防止读取端二次旋转导致主图/增益图错位。"""
    if not raw_exif:
        return img, gm, None, 0
    try:
        ex = Image.Exif()
        ex.load(raw_exif)
    except Exception:  # noqa: BLE001
        return img, gm, raw_exif, 0
    orientation = ex.get(0x0112, 1)
    applied = 0
    if orientation in (2, 4, 5, 7):          # 含镜像，需人工确认，避免误处理
        pass
    elif orientation == 3:
        img, gm, _ = gainmap_ops.rotate_pair(img, gm, 180)
        applied = 180
    elif orientation == 6:
        img, gm, _ = gainmap_ops.rotate_pair(img, gm, 90)
        applied = 90
    elif orientation == 8:
        img, gm, _ = gainmap_ops.rotate_pair(img, gm, 270)
        applied = 270
    if orientation != 1:
        ex[0x0112] = 1
        raw_exif = ex.tobytes()
    return img, gm, raw_exif, applied


def export(
    src_path: str,
    out_path: str | None = None,
    options: ExportOptions | None = None,
) -> ExportResult:
    opt = options or ExportOptions()
    with open(src_path, "rb") as f:
        src_bytes = f.read()
    steps: list[str] = []
    warnings: list[str] = []

    source = vivid_source.parse_file(src_path)
    steps.append(f"parse: container={source.container} has_gainmap={source.has_gainmap}")

    if not source.has_gainmap:
        raise ValueError("源文件没有增益图，无法导出 Ultra HDR；请走纯 SDR 导出分支")

    base = Image.open(io.BytesIO(source.primary_jpeg))
    base.load()
    gm = Image.open(io.BytesIO(source.gainmap_jpeg or b""))
    gm.load()
    steps.append(f"decode: base={base.size} gainmap={gm.size} mode_base={base.mode} mode_gm={gm.mode}")

    geo0 = gainmap_ops.assert_consistent(base, gm)
    steps.append(f"geometry(源): {geo0}")

    # EXIF：保留拍摄信息，但归一化方向
    raw_exif = None
    try:
        raw_exif = base.info.get("exif")
    except Exception:  # noqa: BLE001
        raw_exif = None
    base, gm, raw_exif, applied = _normalize_exif_orientation(base, gm, raw_exif)
    if applied:
        steps.append(f"orient: 物理旋转 {applied}°，并置 Orientation=1")

    if opt.crop_box:
        base, gm, plan = gainmap_ops.crop_pair(base, gm, opt.crop_box)
        steps.append(f"crop: {plan.op} base={plan.base_size} gm={plan.gainmap_size}")
        warnings.extend(plan.warnings)

    if opt.rotate:
        base, gm, plan = gainmap_ops.rotate_pair(base, gm, opt.rotate)
        steps.append(f"rotate: {plan.rotation}° base={plan.base_size} gm={plan.gainmap_size}")

    base, gm, plan = gainmap_ops.resize_pair(base, gm, opt.long_edge)
    steps.append(f"resize: base={plan.base_size} gm={plan.gainmap_size} k={plan.scale:.4f}")
    warnings.extend(plan.warnings)

    if base.mode not in ("RGB", "L"):
        base = base.convert("RGB")
    main_jpeg = _encode_main(base, opt.main_quality, raw_exif)
    gm_jpeg = _encode_gainmap(gm, opt.gainmap_quality)
    steps.append(f"encode: base={len(main_jpeg)}B q={opt.main_quality} | gainmap={len(gm_jpeg)}B q={opt.gainmap_quality}")

    meta = xmp.GainMapMeta(
        gain_map_min=0.0,
        gain_map_max=opt.gain_map_max_log2,
        gamma=1.0,
        hdr_capacity_min=0.0,
        hdr_capacity_max=opt.gain_map_max_log2,
    )
    uhdr, layout = writer.build_uhdr_jpeg(main_jpeg, gm_jpeg, meta)
    steps.append(f"package: primary_len={layout.primary_len} gm_offset={layout.gainmap_offset} total={layout.total_len}")

    out_path = out_path or os.path.splitext(src_path)[0] + ".ultrahdr.jpg"
    with open(out_path, "wb") as f:
        f.write(uhdr)

    rep = validate_bytes(uhdr, out_path)
    steps.append(f"validate: ok={rep.ok} errors={len([c for c in rep.checks if not c.ok and c.level == 'ERROR'])}")

    return ExportResult(
        src_path=src_path,
        out_path=out_path,
        ok=rep.ok,
        source_container=source.container,
        steps=steps,
        layout=asdict(layout),
        validation=rep.to_dict(),
        size_before=len(src_bytes),
        size_after=len(uhdr),
        warnings=warnings + source.notes,
    )
