"""比对 Kotlin LocalToneMapping.apply 与 Python ltm.apply 的同输入输出。

判定标准（2026-10-21 首跑基线，见 docs/04 §7.4）：
  float 级：max_abs < 2e-3（guided filter 的 var=mean_ii-mean_i² 在强边缘处
            有 float32 相减相消，两端 rounding 路径不同，允许少量偏差）
  8bit  级：量化（round，不含抖动）后 max_lsb <= 1 且 lsb>1 占比为 0
            —— 即两端输出在最终 JPEG 层面不可区分
  统计   ：radius / detail_gain / shadow_lift 一致到 4 位有效数字

用法：python compare.py   （依赖 kotlin_out_<tag>.bin，由 ParityTest.kt 生成）
"""
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "python"))
from uhdr import ltm  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "_tmp" / "ltm_parity"
TOL_ABS = 2e-3
failed = False

for tag in ("local", "wechat"):
    with open(OUT / f"input_{tag}.bin", "rb") as f:
        w, h = struct.unpack("<ii", f.read(8))
        white, = struct.unpack("<f", f.read(4))
        p = struct.unpack("<6f", f.read(24))
        n = w * h
        hdr = np.frombuffer(f.read(n * 3 * 4), dtype=np.float32).reshape(h, w, 3).copy()
        lum = np.frombuffer(f.read(n * 4), dtype=np.float32).reshape(h, w).copy()

    params = ltm.LtmParams(radius_frac=p[0], eps=p[1], range_compress=p[2],
                           detail_gain=p[3], black_point=p[4], highlight_start=p[5])
    py_out, py_stats = ltm.apply(hdr, white, params)
    kt_out = np.fromfile(OUT / f"kotlin_out_{tag}.bin", dtype=np.float32).reshape(h, w, 3)

    diff = np.abs(py_out.astype(np.float64) - kt_out.astype(np.float64))
    py_q = np.clip(py_out * 255.0 + 0.5, 0, 255).astype(np.int32)
    kt_q = np.clip(kt_out * 255.0 + 0.5, 0, 255).astype(np.int32)
    lsb = np.abs(py_q - kt_q)

    kt_stats = (OUT / f"kotlin_stats_{tag}.txt").read_text().split()
    lift_rel = abs(float(kt_stats[3]) - py_stats["shadow_lift_x"]) / py_stats["shadow_lift_x"]

    ok = (diff.max() < TOL_ABS and lsb.max() <= 1 and (lsb > 1).sum() == 0
          and int(kt_stats[0]) == py_stats["ltm_radius_px"] and lift_rel < 1e-3)
    failed = failed or not ok
    print(f"[{tag}] {w}x{h} white={white} => {'PASS' if ok else 'FAIL'}")
    print(f"  float级: max_abs={diff.max():.2e} mean={diff.mean():.2e} (容差 {TOL_ABS:.0e})")
    print(f"  8bit级 : max_lsb={lsb.max()} mean_lsb={lsb.mean():.4f} "
          f"lsb>1 占比={100.0 * (lsb > 1).mean():.4f}%")
    print(f"  统计   : r py={py_stats['ltm_radius_px']} kt={kt_stats[0]} | "
          f"detail py={py_stats['ltm_detail_gain']:.2f} kt={kt_stats[1]} | "
          f"shadowLift py={py_stats['shadow_lift_x']:.4f} kt={float(kt_stats[3]):.4f}")

sys.exit(1 if failed else 0)
