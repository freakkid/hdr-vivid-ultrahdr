"""烘焙验收：把「观感是否接近手机上的 HDR」变成可自动判定的指标。

为什么需要它
------------
人工看图和口头确认不可回归，也无法在 CI 里拦住退化。这里把用户提的验收目标
翻译成 5 个可计算指标，跑完直接给 PASS / FAIL。

验收口径（依据见 README.md）
--------------------------
手机显示 HDR 时，屏幕参考白仍对应 SDR 的 1.0 —— 也就是说**中间调的观感与 SDR 主图一致**，
只有超过 1.0 的部分显示为「更亮的白」。因此在 SDR 上复现时：

  1. 中位/均值亮度必须贴近源 SDR 主图（不能整体提亮）
  2. 高光不能被裁成一片纯白
  3. 暗部不能被 LTM 大幅提亮
  4. 结构不能走样（SSIM）
  5. 饱和度不能因为缺少色域转换而失控

用法
----
  python tests/acceptance/accept.py                        # 合成样本，CI 用
  python tests/acceptance/accept.py --real <a.jpg> [b.jpg]  # 追加真实样本（不入库）
  python tests/acceptance/accept.py --json                  # 机器可读
  python tests/acceptance/accept.py --update-baseline       # 记录本次指标作为基准
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "src", "python"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from uhdr import bake, synthetic, vivid_source  # noqa: E402

try:
    from bake_quality_check import ssim_map  # noqa: E402
except Exception:  # pragma: no cover - 缺依赖时退化为 numpy 版
    def ssim_map(x, y, r=5):
        """简易 SSIM（局部均值/方差），够用于结构判定。"""
        def blur(a):
            k = 2 * r + 1
            c = np.cumsum(np.cumsum(a, axis=0), axis=1)
            c = np.pad(c, ((1, 0), (1, 0)))
            s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
            return s / (k * k)
        mx, my = blur(x), blur(y)
        vx = np.maximum(blur(x * x) - mx * mx, 0)
        vy = np.maximum(blur(y * y) - my * my, 0)
        cxy = blur(x * y) - mx * my
        c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
        return ((2 * mx * my + c1) * (2 * cxy + c2)) / \
               ((mx * mx + my * my + c1) * (vx + vy + c2))

DEFAULT_THRESHOLDS = {
    "median_delta_max": 3.0,      # 中位亮度差（灰阶）
    "mean_delta_max": 5.0,        # 均值亮度差（灰阶）
    "clip_ratio_max": 0.01,       # 近纯白像素占比
    "shadow_lift_max": 1.10,      # 暗部（源 L<64）平均亮度提升倍数
    "ssim_min": 0.90,             # 与源 SDR 的结构相似度
    "sat_ratio_max": 1.15,        # 平均饱和度增幅上限
    # 饱和度**下限**：v0.1.21 就是因为这里没有下限，丢了 Display P3 的 ICC 后
    # 饱和度掉到 0.873 却仍然全绿，发出去才发现「烘焙图比原图灰」。
    "sat_ratio_min": 0.92,
}


def _saturation(img: Image.Image) -> float:
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32)
    return float(hsv[..., 1].mean())


def measure(sdr: Image.Image, out: Image.Image) -> dict:
    s = np.asarray(sdr.convert("L"), dtype=np.float32)
    o = np.asarray(out.convert("L"), dtype=np.float32)
    shadow = s < 64
    s_shadow = float(s[shadow].mean()) if shadow.any() else 1.0
    o_shadow = float(o[shadow].mean()) if shadow.any() else 1.0
    s_clip = float((s >= 254).mean())
    o_clip = float((o >= 254).mean())
    return {
        "median_delta": float(np.median(o) - np.median(s)),
        "mean_delta": float(o.mean() - s.mean()),
        # 源图本身的高光就可能纯白（灯、泡沫），所以看「比源多多少」而不是绝对值
        "clip_ratio": o_clip,
        "clip_delta": o_clip - s_clip,
        "shadow_lift": float(o_shadow / max(s_shadow, 1e-6)),
        "ssim": float(ssim_map(s, o).mean()),
        "sat_ratio": float(_saturation(out) / max(_saturation(sdr), 1e-6)),
    }


def judge(m: dict, th: dict) -> list[str]:
    """返回失败原因列表；空表示 PASS。"""
    bad = []
    if abs(m["median_delta"]) > th["median_delta_max"]:
        bad.append("中位亮度偏离 %+.1f（上限 %.1f）" % (m["median_delta"], th["median_delta_max"]))
    if abs(m["mean_delta"]) > th["mean_delta_max"]:
        bad.append("均值亮度偏离 %+.1f（上限 %.1f）" % (m["mean_delta"], th["mean_delta_max"]))
    if m["clip_delta"] > th["clip_ratio_max"]:
        bad.append("纯白占比比源多 %.2f 个百分点（上限 %.2f）"
                   % (m["clip_delta"] * 100, th["clip_ratio_max"] * 100))
    if m["shadow_lift"] > th["shadow_lift_max"]:
        bad.append("暗部提亮 %.2fx（上限 %.2fx）" % (m["shadow_lift"], th["shadow_lift_max"]))
    if m["ssim"] < th["ssim_min"]:
        bad.append("SSIM %.3f（下限 %.2f）" % (m["ssim"], th["ssim_min"]))
    if m["sat_ratio"] > th["sat_ratio_max"]:
        bad.append("饱和度 x%.2f（上限 %.2f）" % (m["sat_ratio"], th["sat_ratio_max"]))
    if m["sat_ratio"] < th["sat_ratio_min"]:
        bad.append("饱和度 x%.2f（下限 %.2f，疑似丢失 ICC/色彩管理）"
                   % (m["sat_ratio"], th["sat_ratio_min"]))
    return bad


def bake_and_measure(src: vivid_source.HdrSource,
                     profile: str, strength: float,
                     long_edge: int = 1200) -> tuple[Image.Image, Image.Image, dict]:
    opt = bake.BakeOptions(long_edge=long_edge, profile=profile, strength=strength)
    data, _stats = bake.bake(src, opt)
    out = Image.open(io.BytesIO(data)).convert("RGB")
    # 与烘焙端一致：源图先转 sRGB（否则 P3 图会被当成「更灰」的对照基准）
    sdr = bake.to_srgb(Image.open(io.BytesIO(src.primary_jpeg)))
    if sdr.size != out.size:
        sdr = sdr.resize(out.size, Image.Resampling.LANCZOS)
    return sdr, out, measure(sdr, out)


def run(real_paths: list[str], as_json: bool, update: bool) -> int:
    cases = []
    # 合成样本：不依赖任何真实照片，CI 可跑
    tmp_dir = os.path.join(HERE, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    synth_path = os.path.join(tmp_dir, "synth_cuva.jpg")
    synthetic.write_cuva_sample(synth_path, base_long_edge=1200)
    for profile in ("local", "off", "wechat", "faithful"):
        cases.append(("synthetic/" + profile, synth_path, profile, 0.40))
    for p in real_paths:
        for profile in ("local", "off"):
            cases.append(("real:" + os.path.basename(p) + "/" + profile, p, profile, 0.40))

    rows = []
    failures = 0
    for cid, path, profile, strength in cases:
        src = vivid_source.parse_file(path)
        sdr, _out, m = bake_and_measure(src, profile, strength)
        bad = judge(m, DEFAULT_THRESHOLDS)
        failures += bool(bad)
        rows.append({
            "case": cid, "profile": profile, "strength": strength,
            "metrics": {k: round(v, 4) for k, v in m.items()},
            "pass": not bad, "reasons": bad,
        })

    if as_json:
        print(json.dumps({
            "thresholds": DEFAULT_THRESHOLDS,
            "cases": rows,
            "failures": failures,
        }, ensure_ascii=False, indent=2))
    else:
        th = DEFAULT_THRESHOLDS
        print("验收阈值: 中位±%.0f 均值±%.0f 纯白<%.0f%% 暗部<%.2fx SSIM>%.2f 饱和<%.2f"
              % (th["median_delta_max"], th["mean_delta_max"], th["clip_ratio_max"] * 100,
                 th["shadow_lift_max"], th["ssim_min"], th["sat_ratio_max"]))
        print()
        print("%-34s %7s %7s %7s %7s %7s %6s" %
              ("用例", "中位差", "均值差", "纯白%", "暗部", "SSIM", "饱和"))
        for r in rows:
            m = r["metrics"]
            print("%-34s %+7.1f %+7.1f %6.2f%% %6.2fx %7.3f %6.2f %s"
                  % (r["case"], m["median_delta"], m["mean_delta"],
                     m["clip_ratio"] * 100, m["shadow_lift"], m["ssim"],
                     m["sat_ratio"], "PASS" if r["pass"] else "FAIL"))
            for reason in r["reasons"]:
                print("      · " + reason)
        print()
        print("结论: %d/%d 通过" % (len(rows) - failures, len(rows)))

    baseline = os.path.join(HERE, "baseline.json")
    if update:
        with open(baseline, "w", encoding="utf-8") as f:
            json.dump({"thresholds": DEFAULT_THRESHOLDS, "cases": rows}, f,
                      ensure_ascii=False, indent=2)
        print("已写入基准:", baseline)

    return 0 if failures == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="烘焙观感验收")
    ap.add_argument("--real", nargs="*", default=[],
                    help="真实 HDR 源（ISO Ultra HDR JPEG），可多个；不入库")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()
    return run(args.real, args.json, args.update_baseline)


if __name__ == "__main__":
    sys.exit(main())
