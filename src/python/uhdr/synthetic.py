"""生成「CUVA / 华为 HDR Vivid 风格」的合成测试源。

用途：在没有真实样张的机器上验证容器解析与导出链路。
限制：合成源只模拟「容器结构 + 几何关系」，不模拟真实 Vivid 的
BT.2020 / PQ 色彩语义，因此不能用于评估色彩准确性。
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

from . import jpegseg
from .vivid_source import APP5


def _gradient(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w, 4):
            r = int(30 + 200 * x / max(1, w - 1))
            g = int(40 + 180 * y / max(1, h - 1))
            b = int(60 + 120 * (1 - x / max(1, w - 1)))
            for k in range(4):
                if x + k < w:
                    px[x + k, y] = (r, g, b)
    d = ImageDraw.Draw(img)
    # 高光斑：模拟需要增益图补偿的亮部
    d.ellipse([w * 0.55, h * 0.15, w * 0.85, h * 0.45], fill=(255, 250, 235))
    d.rectangle([w * 0.08, h * 0.62, w * 0.42, h * 0.9], fill=(18, 18, 22))
    return img


def _gainmap(w: int, h: int) -> Image.Image:
    """灰度增益图：高光区高增益，暗部接近 0。"""
    gm = Image.new("L", (w, h), 0)
    px = gm.load()
    for y in range(h):
        for x in range(w):
            dx = (x / max(1, w - 1) - 0.7) ** 2
            dy = (y / max(1, h - 1) - 0.3) ** 2
            v = int(255 * max(0.0, 1.0 - (dx + dy) * 6))
            px[x, y] = min(255, max(0, v))
    return gm


def make_cuva_sample(base_long_edge: int = 3000, gainmap_div: int = 4) -> bytes:
    """返回一个 CUVA 风格的 JPEG 多图容器字节流。"""
    if base_long_edge >= 3000:
        bw, bh = base_long_edge, int(base_long_edge * 3 / 4)
    else:
        bw, bh = base_long_edge, int(base_long_edge * 3 / 4)
    base = _gradient(bw, bh)

    exif = Image.Exif()
    exif[0x010F] = "HUAWEI"          # Make
    exif[0x0110] = "HUAWEI P60"      # Model
    exif[0x010E] = "_cuva"           # ImageDescription：CUVA 标记
    exif[0x0112] = 1                 # Orientation：正向

    buf = io.BytesIO()
    base.save(buf, "JPEG", quality=95, subsampling=0, exif=exif.tobytes())
    primary = buf.getvalue()

    gm = _gainmap(bw // gainmap_div, bh // gainmap_div)
    gbuf = io.BytesIO()
    gm.save(gbuf, "JPEG", quality=90)
    gm_jpeg = gbuf.getvalue()

    # 副图：SOI + APP5(CUVA 私有标记) + 其余段
    gm_payload = b"CUVA\x00" + bytes([1, 1])
    app5 = b"\xff\xe5" + (len(gm_payload) + 2).to_bytes(2, "big") + gm_payload
    gm_with_marker = gm_jpeg[:2] + app5 + gm_jpeg[2:]

    return primary + gm_with_marker


def write_cuva_sample(path: str, base_long_edge: int = 3000) -> None:
    data = make_cuva_sample(base_long_edge)
    with open(path, "wb") as f:
        f.write(data)


def describe_container(data: bytes) -> list[str]:
    lines = []
    for start, end in jpegseg.find_all_images(data):
        segs = jpegseg.header_segments_of(data, start, end)
        names = ",".join(s.marker_name for s in segs[:6])
        lines.append(f"  image @{start}..{end} ({end - start}B) 段: {names}")
    return lines
