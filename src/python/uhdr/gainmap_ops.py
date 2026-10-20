"""主图 / 增益图同步几何变换。

核心约束：增益图分辨率通常低于主图（常见 1/2、1/4、1/8），任何几何操作
都必须用「归一化坐标 → 增益图坐标」映射，而不是各自独立缩放 —— 否则
两者视场错位，HDR 合成会出现亮部光晕错位或暗部块状伪影。

本模块不做像素级的 HDR 重建，只保证几何与元数据一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

RESAMPLE = Image.Resampling.LANCZOS


@dataclass
class GainMapGeometry:
    base_w: int
    base_h: int
    gm_w: int
    gm_h: int

    @property
    def scale_x(self) -> float:
        return self.gm_w / self.base_w

    @property
    def scale_y(self) -> float:
        return self.gm_h / self.base_h

    def is_consistent(self, tol: float = 0.02) -> bool:
        return abs(self.scale_x - self.scale_y) <= tol

    @classmethod
    def of(cls, base: Image.Image, gm: Image.Image) -> "GainMapGeometry":
        return cls(base.width, base.height, gm.width, gm.height)

    def __str__(self) -> str:
        return (f"base={self.base_w}x{self.base_h} gm={self.gm_w}x{self.gm_h} "
                f"scale=({self.scale_x:.4f},{self.scale_y:.4f})")


@dataclass
class TransformPlan:
    """记录一次几何变换在两张图上的实际参数，用于审计与回归断言。"""

    op: str
    base_size: tuple[int, int]
    gainmap_size: tuple[int, int]
    scale: float = 1.0
    crop_box: tuple[int, int, int, int] | None = None
    rotation: int = 0
    mirror: bool = False
    warnings: list[str] = field(default_factory=list)


def assert_consistent(base: Image.Image, gm: Image.Image, tol: float = 0.02) -> GainMapGeometry:
    geo = GainMapGeometry.of(base, gm)
    if not geo.is_consistent(tol):
        raise ValueError(
            f"主图与增益图比例不一致（{geo}），几何变换会错位。"
            f"宽高比例差 {abs(geo.scale_x - geo.scale_y):.4f} > 容差 {tol}"
        )
    return geo


def target_size_for_long_edge(w: int, h: int, long_edge: int) -> tuple[int, int]:
    """按最长边等比缩放；只缩不放。"""
    cur = max(w, h)
    if cur <= long_edge:
        return w, h
    k = long_edge / cur
    return max(1, round(w * k)), max(1, round(h * k))


def scale_gainmap_for(gm_w: int, gm_h: int, new_base: tuple[int, int], old_base: tuple[int, int]) -> tuple[int, int]:
    """按主图缩放比例推算增益图尺寸，保持两者的相对分辨率关系。"""
    kx = new_base[0] / old_base[0]
    ky = new_base[1] / old_base[1]
    return max(1, round(gm_w * kx)), max(1, round(gm_h * ky))


def resize_pair(base: Image.Image, gm: Image.Image, long_edge: int) -> tuple[Image.Image, Image.Image, TransformPlan]:
    """预压缩：主图最长边缩到 long_edge，增益图同步缩放。"""
    assert base.size == (base.width, base.height)
    geo = assert_consistent(base, gm)
    old_size = base.size
    new_size = target_size_for_long_edge(*old_size, long_edge)

    if new_size == old_size:
        plan = TransformPlan("resize_noop", old_size, gm.size, 1.0)
        plan.warnings.append("主图最长边已 <= 目标值，未执行缩放（避免无意义重编码）")
        return base, gm, plan

    base2 = base.resize(new_size, RESAMPLE)
    gm2_size = scale_gainmap_for(gm.width, gm.height, new_size, old_size)
    gm2 = gm.resize(gm2_size, RESAMPLE)

    plan = TransformPlan("resize", new_size, gm2_size, new_size[0] / old_size[0])
    geo2 = GainMapGeometry.of(base2, gm2)
    if not geo2.is_consistent():
        plan.warnings.append(f"缩放后比例漂移：{geo2}（原 {geo}）")
    return base2, gm2, plan


def crop_pair(base: Image.Image, gm: Image.Image, box: tuple[int, int, int, int]) -> tuple[Image.Image, Image.Image, TransformPlan]:
    """裁剪：box 为主图坐标系，映射到增益图坐标。"""
    geo = assert_consistent(base, gm)
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= base.width and 0 <= y0 < y1 <= base.height):
        raise ValueError(f"裁剪框越界: {box} vs 主图 {base.size}")

    gm_box = (
        round(x0 * geo.scale_x),
        round(y0 * geo.scale_y),
        round(x1 * geo.scale_x),
        round(y1 * geo.scale_y),
    )
    gm_box = (
        max(0, gm_box[0]),
        max(0, gm_box[1]),
        min(gm.width, max(gm_box[0] + 1, gm_box[2])),
        min(gm.height, max(gm_box[1] + 1, gm_box[3])),
    )
    base2 = base.crop(box)
    gm2 = gm.crop(gm_box)
    plan = TransformPlan("crop", base2.size, gm2.size, crop_box=box)

    geo2 = GainMapGeometry.of(base2, gm2)
    if not geo2.is_consistent(0.03):
        plan.warnings.append(f"裁剪后比例漂移：{geo2}")
    return base2, gm2, plan


def rotate_pair(base: Image.Image, gm: Image.Image, degrees: int) -> tuple[Image.Image, Image.Image, TransformPlan]:
    """旋转：仅支持 90 的整数倍（保持像素网格与比例关系）。"""
    if degrees % 90 != 0:
        raise ValueError(f"只支持 90 度整数倍旋转，收到 {degrees}")
    d = degrees % 360
    if d == 0:
        return base, gm, TransformPlan("rotate_noop", base.size, gm.size)
    base2 = base.rotate(d, expand=True)
    gm2 = gm.rotate(d, expand=True)
    plan = TransformPlan("rotate", base2.size, gm2.size, rotation=d)
    GainMapGeometry.of(base2, gm2)
    return base2, gm2, plan


def mirror_pair(base: Image.Image, gm: Image.Image, horizontal: bool = True) -> tuple[Image.Image, Image.Image, TransformPlan]:
    op = Image.Transpose.FLIP_LEFT_RIGHT if horizontal else Image.Transpose.FLIP_TOP_BOTTOM
    base2 = base.transpose(op)
    gm2 = gm.transpose(op)
    return base2, gm2, TransformPlan("mirror", base2.size, gm2.size, mirror=horizontal)
