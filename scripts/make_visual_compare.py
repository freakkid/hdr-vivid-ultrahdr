#!/usr/bin/env python3
"""生成 LTM 三档目视对比材料（out/visual/）。

为什么需要这个脚本：`out/visual/` 在 .gitignore 里（含真实照片，不入库），
所以每台机器都得自己重做一遍。此前是临时命令拼出来的，换机后无法复现，
这里固化成一个入口。

用法:
    python scripts/make_visual_compare.py <源图> [-o out/visual]

产出:
    source_sdr.jpg        源 SDR 主图（对照组）
    py_off.jpg            仅全局映射
    py_local.jpg          全局 + 局部色调映射（默认档）
    py_wechat.jpg         克制版，为平台二次压缩留余量
    00_compare.jpg        2x2 拼图，带体积/暗部/高光标注
    00_preview.jpg        缩略版，便于直接打开
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore", message=".*malformed MPO.*")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src", "python"))

from uhdr import bake, vivid_source  # noqa: E402


def _metrics(im) -> dict:
    """暗部/中间调/高光均值，口径与 docs/04 §7 一致（按百分位切分）。"""
    import numpy as np

    g = np.asarray(im.convert("L"), dtype=np.float32)
    return {
        "shadow": float(g[g < np.percentile(g, 20)].mean()),
        "mid": float(g[(g >= np.percentile(g, 40)) & (g <= np.percentile(g, 60))].mean()),
        "high": float(g[g > np.percentile(g, 95)].mean()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 LTM 三档目视对比材料")
    ap.add_argument("src", help="源图路径（需含增益图）")
    ap.add_argument("-o", "--out", default="out/visual", help="输出目录")
    ap.add_argument("--long-edge", type=int, default=2048)
    ap.add_argument("--strength", type=float, default=0.70, help="增益强度，默认 0.70")
    args = ap.parse_args()

    from PIL import Image, ImageDraw, ImageFont

    os.makedirs(args.out, exist_ok=True)

    src = vivid_source.parse_file(args.src)
    print(f"源图: {src.describe()}")
    if not src.has_gainmap:
        print("源图没有增益图，无法烘焙")
        return 1

    # 对照组：源 SDR 主图（MPO 基图）
    base = Image.open(args.src)
    base.load()
    base.convert("RGB").save(os.path.join(args.out, "source_sdr.jpg"), quality=95, subsampling=0)

    panels = [("源 SDR 主图", os.path.join(args.out, "source_sdr.jpg"))]
    for profile in ("off", "local", "wechat"):
        path = os.path.join(args.out, f"py_{profile}.jpg")
        data, stats = bake.bake(
            src, bake.BakeOptions(long_edge=args.long_edge, strength=args.strength, profile=profile)
        )
        with open(path, "wb") as f:
            f.write(data)
        print(
            f"  {profile:7s} {len(data) / 1024:6.0f}KB  峰值 {stats.hdr_peak:.2f}x  "
            f"裁切 {stats.clipped_ratio * 100:.2f}%"
        )
        panels.append((f"profile={profile}", path))

    # 统一宽度后测指标 + 拼 2x2
    W = 1400
    rows = []
    print(f"\n{'面板':>16} {'体积KB':>7} {'暗部':>6} {'中间调':>7} {'高光':>6}")
    print("-" * 46)
    for name, path in panels:
        im = Image.open(path).convert("RGB")
        im = im.resize((W, int(W * im.height / im.width)), Image.Resampling.LANCZOS)
        m = _metrics(im)
        kb = os.path.getsize(path) // 1024
        rows.append((name, im, kb, m))
        print(f"{name:>16} {kb:>7} {m['shadow']:>6.1f} {m['mid']:>7.1f} {m['high']:>6.1f}")

    try:
        font = ImageFont.load_default(size=28)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()

    bar, gap = 46, 10
    w, h = rows[0][1].size
    canvas = Image.new("RGB", (w * 2 + gap, (h + bar) * 2 + gap), (18, 18, 18))
    draw = ImageDraw.Draw(canvas)
    for i, (name, im, kb, m) in enumerate(rows):
        x = (i % 2) * (w + gap)
        y = (i // 2) * (h + bar + gap)
        canvas.paste(im, (x, y + bar))
        draw.text(
            (x + 10, y + 9),
            f"{name}   {kb}KB   暗部 {m['shadow']:.0f}  高光 {m['high']:.0f}",
            fill=(255, 255, 255),
            font=font,
        )

    canvas.save(os.path.join(args.out, "00_compare.jpg"), quality=90, subsampling=0)
    canvas.resize((1500, int(1500 * canvas.height / canvas.width)), Image.Resampling.LANCZOS).save(
        os.path.join(args.out, "00_preview.jpg"), quality=88, subsampling=0
    )

    print(f"\n对比图: {os.path.join(args.out, '00_compare.jpg')}")
    print(f"预览  : {os.path.join(args.out, '00_preview.jpg')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
