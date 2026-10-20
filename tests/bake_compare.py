"""烘焙效果量化对比 + 左右拼图。

用法（在 src/python 目录下）：
    python ../../tests/bake_compare.py <源.jpg> <out_dir>

产出：
    <out_dir>/bake_compare.jpg   三联对比（原 SDR 主图 | 全局映射 | 局部映射）
    stdout                       指标表
"""
from __future__ import annotations

import io
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "python"))

from uhdr import bake, jpegseg, vivid_source  # noqa: E402

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def luma(a: np.ndarray) -> np.ndarray:
    return (a.astype(np.float32) * _LUMA).sum(axis=2)


def local_contrast(a: np.ndarray, block: int = 16) -> float:
    """块内亮度标准差的中位数 —— 衡量「局部通透感」，不受整体亮度影响。"""
    y = luma(a)
    h, w = y.shape
    st = []
    for by in range(0, h - block + 1, block):
        for bx in range(0, w - block + 1, block):
            st.append(float(y[by:by + block, bx:bx + block].std()))
    return float(np.median(st)) if st else 0.0


def shadow_mean(a: np.ndarray) -> float:
    y = luma(a)
    thr = np.percentile(y, 10)
    return float(y[y <= thr].mean())


def highlight_clip(a: np.ndarray) -> float:
    return float((np.asarray(a) >= 252).all(axis=2).mean())


def flatness(a: np.ndarray) -> float:
    """平滑区的平均绝对二阶差分：越低越平顺（可粗略反映 banding / 噪点）。"""
    y = luma(a)
    d2 = np.abs(np.diff(y, n=2, axis=1))
    # 只看低梯度区（二阶差分本身很小的区域）
    mask = d2 < np.percentile(d2, 50)
    return float(d2[mask].mean()) if mask.any() else 0.0


def has_hdr_meta(data: bytes) -> tuple[bool, str]:
    """确认输出是「纯 JPEG」—— 不含 XMP / MPF / 增益图。"""
    segs = list(jpegseg.iter_header_segments(data, 0))
    kinds = []
    for s in segs:
        if s.marker == 0xFFE1:
            kinds.append("APP1")
        elif s.marker == 0xFFE2:
            kinds.append("APP2")
    blob = data[:65536]
    hit = b"hdrgm" in blob or b"GContainer" in blob or b"MPF" in blob
    return hit, ",".join(kinds) or "none"


def main() -> int:
    src_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "out"
    os.makedirs(out_dir, exist_ok=True)

    src = vivid_source.parse_file(src_path)
    print(src.describe())

    base_img = Image.open(io.BytesIO(src.primary_jpeg)).convert("RGB")

    results = {}
    for profile in ("off", "local", "wechat"):
        opt = bake.BakeOptions(profile=profile)
        data, stats = bake.bake(src, opt)
        path = os.path.join(out_dir, f"baked_{profile}.jpg")
        with open(path, "wb") as f:
            f.write(data)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        results[profile] = (img, data, stats, path)

    # 基准：源 SDR 主图缩放到同一尺寸
    ref = base_img.resize(results["local"][0].size, Image.Resampling.LANCZOS)

    rows = [
        ("源 SDR 主图", ref),
        ("全局映射 off", results["off"][0]),
        ("局部映射 local", results["local"][0]),
        ("微信档 wechat", results["wechat"][0]),
    ]

    print(f"\n{'档位':<16}{'尺寸':<14}{'体积KB':>8}{'局部对比':>10}{'暗部亮度':>10}{'高光裁切':>10}{'平滑度':>9}")
    print("-" * 78)
    for name, img in rows:
        a = np.asarray(img)
        sz = "—" if name.startswith("源") else f"{len(results[{'全局映射 off':'off','局部映射 local':'local','微信档 wechat':'wechat'}.get(name,'')][1])/1024:.0f}"
        print(f"{name:<16}{img.width}x{img.height:<9}{sz:>8}"
              f"{local_contrast(a):>10.2f}{shadow_mean(a):>10.1f}"
              f"{highlight_clip(a)*100:>9.2f}%{flatness(a):>9.3f}")

    lc_ref = local_contrast(np.asarray(ref))
    lc_off = local_contrast(np.asarray(results["off"][0]))
    lc_loc = local_contrast(np.asarray(results["local"][0]))
    print(f"\n局部对比相对源图：off x{lc_off/lc_ref:.2f}  local x{lc_loc/lc_ref:.2f}")

    hit, kinds = has_hdr_meta(results["local"][1])
    print(f"输出元数据检查：HDR 标记={'存在（异常）' if hit else '无（符合预期）'}  段={kinds}")

    # 三联拼图（每张缩到高 900）
    thumbs = []
    for _, img in rows[:3]:
        k = 900 / img.height
        thumbs.append(img.resize((max(1, int(img.width * k)), 900), Image.Resampling.LANCZOS))
    gap = 10
    W = sum(t.width for t in thumbs) + gap * (len(thumbs) - 1)
    canvas = Image.new("RGB", (W, 900), (20, 20, 22))
    x = 0
    for t in thumbs:
        canvas.paste(t, (x, 0))
        x += t.width + gap
    cmp_path = os.path.join(out_dir, "bake_compare.jpg")
    canvas.save(cmp_path, quality=92, subsampling=0)
    print(f"\n对比图: {cmp_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
