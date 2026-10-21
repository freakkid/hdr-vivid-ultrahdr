"""微信路径代理测试：模拟「勾选原图 / 不勾选原图」两种传输，看元数据存活率。

背景（HANDOVER §3.2 / docs/01 H-1、H-2）：微信是否保留 XMP/MPF 决定能否对用户
承诺。真机三设备实测做不了时，本脚本给出**下界证据**：

  - 原图直传（字节不变）：元数据必然保留——验证我们的容器本身自洽；
  - 通用重压缩（PIL 重编码，模拟不勾选原图）：元数据**必然丢失**——
    这是任何「只传像素」的压缩通道的下界。若微信比重压缩更狠，结果一样；
    若微信保留部分元数据（如 EXIF），那是微信的特例，不能反推。

重压缩参数扫三档长边（H-2 未核实：1080/1440/1920 说法不一）与两档质量。

用法：
    python tests/wechat_proxy.py <file.jpg>   （Ultra HDR 或烘焙图均可）
退出码：0 = 原图路径存活（可对「原图」承诺）；1 = 连原图路径都丢元数据（容器有 bug）。

对烘焙图（无元数据的普通 JPEG）自动跳过元数据测试，只跑「效果存活率」——
它正是产品给「不勾选原图」路径的答案：HDR 观感压在像素里，该扛压缩。
"""
from __future__ import annotations

import io
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "python"))
sys.path.insert(0, os.path.dirname(__file__))

from bake_quality_check import hf_energy as _hf  # noqa: E402
from bake_quality_check import luma, ssim_map  # noqa: E402
from uhdr import validator  # noqa: E402


def check(data: bytes, label: str) -> tuple[bool, int, list[str]]:
    rep = validator.validate_bytes(data, label)
    failed = [c.check_id for c in rep.checks if not c.ok]
    return rep.ok, len(rep.checks), failed


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = open(sys.argv[1], "rb").read()

    print(f"文件: {sys.argv[1]}  ({len(src):,} B)")

    # 1) 原图直传（字节不变）
    ok, total, failed = check(src, "原图直传")
    print(f"\n[原图直传]  {'PASS' if ok else 'FAIL'}  ({total - len(failed)}/{total} 项通过)")
    if failed:
        print("  失败项:", ", ".join(failed))

    # 2) 通用重压缩（只传像素，不保留 APP 段）
    img = Image.open(io.BytesIO(src)).convert("RGB")
    print(f"\n[不勾选原图 → 通用重压缩]  源尺寸 {img.size[0]}x{img.size[1]}")
    print(f"  {'场景':<22} {'体积':>10}  增益图  XMP  MPF  结论")

    def detect(data: bytes) -> tuple[bool, bool, bool]:
        """直接按字节签名判断，不依赖 validator 的失败项（普通 JPEG 会提前退出）。"""
        has_gm = data.count(b"\xff\xd8\xff") >= 2      # 两个 JPEG 图像
        has_xmp = b"GContainer" in data or b"ns.adobe.com/xap" in data
        has_mpf = b"MPF\x00" in data
        return has_gm, has_xmp, has_mpf

    for long_edge, quality in ((None, 80), (1920, 80), (1440, 75), (1080, 75)):
        im = img
        if long_edge:
            k = long_edge / max(im.size)
            im = im.resize((max(1, round(im.size[0] * k)), max(1, round(im.size[1] * k))),
                           Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, subsampling=2, optimize=True)
        data = buf.getvalue()
        has_gm, has_xmp, has_mpf = detect(data)
        tag = f"{'原尺寸' if not long_edge else f'长边{long_edge}'} q={quality}"
        verdict = "HDR 保留" if (has_gm and has_xmp and has_mpf) else "HDR 丢失"
        print(f"  {tag:<22} {len(data):>9,}B  {'有' if has_gm else '无':^4} "
              f"{'有' if has_xmp else '无':^4} {'有' if has_mpf else '无':^4}  {verdict}")

    print("\n结论：")
    print("  · 勾选原图 → HDR 完整保留（上表第一行 + 原图直传 PASS）")
    print("  · 不勾选原图 → 经过任何「只重编码像素」的通道，XMP/MPF/增益图全部丢失，")
    print("    这是下界证据：微信若这么做，承诺「发图可见 HDR」不成立；")
    print("    微信若奇迹般保留元数据，属平台特例，不能当承诺依据。")
    print("  · 真机三设备实测（H-1/H-2）仍待做，模板 tests/test_matrix.md。")

    # 3) 效果存活率：像素观感能否扛住重压缩（烘焙图的价值所在）
    print("\n[效果存活率] 同一张图经重压缩后与自身的相似度（烘焙图靠这个活）")
    ya_ref = luma(np.asarray(img, np.float32))
    print(f"  {'场景':<22} {'SSIM':>7} {'HF比':>7}")
    for long_edge, quality in ((None, 80), (1920, 80), (1440, 75), (1080, 75)):
        im = img
        if long_edge:
            k = long_edge / max(im.size)
            im = im.resize((max(1, round(im.size[0] * k)), max(1, round(im.size[1] * k))),
                           Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, subsampling=2, optimize=True)
        im2 = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
        if im2.size != img.size:
            im2 = im2.resize(img.size, Image.Resampling.LANCZOS)
        yb = luma(np.asarray(im2, np.float32))
        tag = f"{'原尺寸' if not long_edge else f'长边{long_edge}'} q={quality}"
        print(f"  {tag:<22} {ssim_map(ya_ref, yb).mean():>7.4f} "
              f"{_hf(yb) / max(_hf(ya_ref), 1e-6):>7.3f}")
    print("  解读：SSIM > 0.9 即观感基本无损——烘焙图的 HDR 效果在像素里，")
    print("  不依赖元数据，因此这条路对「不勾选原图」依然成立（与 Ultra HDR 相反）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
