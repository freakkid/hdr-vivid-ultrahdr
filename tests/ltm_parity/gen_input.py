"""LocalToneMapping 双实现对照：生成 Kotlin 与 Python 共用的合成输入。

背景：项目纪律是「容器/算法写得对不对，不能用写它的同一份代码验证」。
Python ltm.py 与 Kotlin LocalToneMapping.kt 是两套独立实现，本脚本生成
确定性合成 HDR（平滑渐变 + 硬边缘 + 细纹理 + 3.2x 高光块 + 纯黑区），
两侧同输入同参数跑 apply，由 compare.py 比对。

输出（tests/_tmp/ltm_parity/，小端）：
  input_<tag>.bin    int32 w, int32 h, float32 white, float32 params[8],
                     float32 hdr[w*h*3], float32 lum[w*h]
                    （params[8] = radius_frac, eps, range_compress, detail_gain,
                     black_point, highlight_start, shoulder_white, shoulder_span）

用法：python gen_input.py   （需要 numpy；tag = local / wechat）
"""
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "python"))
from uhdr import ltm  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "_tmp" / "ltm_parity"
OUT.mkdir(parents=True, exist_ok=True)
W, H = 512, 384

rng = np.random.default_rng(42)
yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
base = 0.02 + 0.9 * (xx / W) + 0.25 * np.sin(yy / 37.0)
step = (xx > W * 0.62).astype(np.float32) * 0.8            # 硬边缘：guided filter 保边
noise = rng.random((H, W), dtype=np.float32) * 0.06        # 细纹理：detail 增益
highlight = (((yy > H * 0.2) & (yy < H * 0.35) & (xx > W * 0.1) & (xx < W * 0.3))
             .astype(np.float32) * 3.2)                    # 3.2x 高光：compress_highlights
black = (yy > H * 0.75) & (xx > W * 0.75)                  # 纯黑区：lum=0 分支

lum = np.clip(base + step + noise + highlight, 0.0, None).astype(np.float32)
lum = np.where(black, 0.0, lum)
tint = np.stack([1.0, 0.93, 0.85], axis=-1).astype(np.float32)  # 三通道有色差
hdr = (lum[..., None] * tint + rng.random((H, W, 3), dtype=np.float32) * 0.004 * lum[..., None])
hdr = np.where(black[..., None], 0.0, hdr).astype(np.float32)

# Kotlin 侧 lum 由调用方传入，必须与 Python ltm.apply 内部公式一致
py_lum = (hdr * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)).sum(axis=2).astype(np.float32)

WHITE = 1.0
for tag, params in [("local", ltm.params_for_profile("local")),
                    ("wechat", ltm.params_for_profile("wechat"))]:
    path = OUT / f"input_{tag}.bin"
    with open(path, "wb") as f:
        f.write(struct.pack("<ii", W, H))
        f.write(struct.pack("<f", WHITE))
        f.write(struct.pack("<8f", params.radius_frac, params.eps, params.range_compress,
                            params.detail_gain, params.black_point, params.highlight_start,
                            params.shoulder_white, params.shoulder_span))
        f.write(hdr.tobytes())
        f.write(py_lum.tobytes())
    print(f"{tag}: {path}  hdr[{hdr.min():.3f},{hdr.max():.3f}] "
          f"lum[{py_lum.min():.5f},{py_lum.max():.3f}] zero_px={int((py_lum <= 1e-7).sum())}")
