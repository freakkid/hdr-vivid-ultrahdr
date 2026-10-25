"""烘焙验收：把「观感是否接近手机上的 HDR」变成可自动判定的指标。

为什么需要它
------------
人工看图和口头确认不可回归，也无法在 CI 里拦住退化。这里把用户提的验收目标
翻译成可计算指标，跑完直接给 PASS / FAIL（含 2026-10-25 加入的「层次」下限）。

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
    # ---- 「层次少」下限（2026-10-25 加入，当晚随双斜率肩上调）----
    # 首轮依据：用户对 0.1.22 的反馈「层次少」量化 —— local@0.70 把高光分位距压到
    # 源的 0.42~0.53、高光局部对比压到 0.64~0.78（3 真实样本 × local/wechat/faithful
    # × strength 0.40/0.70 实测，见 README「层次实测」）。
    # 2026-10-25 晚：根因定位为「指数软膝把 1.5~2.4× 白点全部拍平在 [0.95, 0.96]」，
    # LTM 换双斜率肩（SDR 带线性压到 0.93 让出空间、HDR 带小斜率延伸到 0.96、
    # ceiling 放开到 1.0）后复测（3 真实样本 × local）：
    #   hl_spread 0.50~0.80（0.70 档最低 0.50；旧指数肩为恒定 0.417）
    #   hl_hf     0.78~0.97（0.70 档最低 0.78；旧 0.690）
    #   hf_all    1.01~1.15
    # 下限随实测上调，且必须落在旧算法实测值（0.417 / 0.690）之下 ——
    # 保证「换回旧肩会被门禁拦住」。钉在出货档实测最低值附近，只守不调；
    # 再次调参提升后仍须上调，不许反向放宽。
    "hl_spread_min": 0.45,   # 高光区（源 L≥217）P95−P50 分位距之比 out/src
    "hl_hf_min": 0.75,       # 高光区梯度能量之比 out/src（0.70 档实测最低 0.78）
    "hf_all_min": 0.95,      # 全图梯度能量之比 out/src（实测最低 1.01；防整体磨平）
}

# 层次下限只对出货 profile 生效；off 是 A/B 对照旧行为，不代表出货物性。
# 验收强度矩阵：0.40 是定档基准，0.70 是产品默认（docs/07 §2-4）。
ACCEPT_PROFILES = ("local", "off", "wechat", "faithful")
ACCEPT_STRENGTHS = (0.40, 0.70)
HL_FLOOR_PROFILES = ("local", "wechat", "faithful")


def _saturation(img: Image.Image) -> float:
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32)
    return float(hsv[..., 1].mean())


def _hf_energy(l: np.ndarray, mask: np.ndarray | None = None) -> float:
    """梯度能量：|dx|+|dy| 的均值；mask 限定两端都落在 mask 内的差分。"""
    gx = np.abs(np.diff(l, axis=1))
    gy = np.abs(np.diff(l, axis=0))
    if mask is not None:
        m = mask.astype(bool)
        gx = gx[m[:, :-1] & m[:, 1:]]
        gy = gy[m[:-1, :] & m[1:, :]]
    n = gx.size + gy.size
    return float((gx.sum() + gy.sum()) / max(n, 1))


def _hl_metrics(s: np.ndarray, o: np.ndarray) -> tuple[float | None, float | None]:
    """高光层次指标（分位距比、梯度能量比）。

    源高光区太小（<100 像素）或本身就是平坦平台（P95−P50 < 4 灰阶，如合成样本）
    时返回 None —— 该用例不判这两项，避免除零噪声。以**源**的分位距做判据，
    源有层次而输出被压平才是要拦的退化。
    """
    hl = s >= 217
    if int(hl.sum()) < 100:
        return None, None

    def spread(v: np.ndarray) -> float:
        return float(np.percentile(v, 95) - np.percentile(v, 50))

    s_spread = spread(s[hl])
    if s_spread < 4.0:
        return None, None
    return spread(o[hl]) / max(s_spread, 1e-6), \
        _hf_energy(o, hl) / max(_hf_energy(s, hl), 1e-6)


def measure(sdr: Image.Image, out: Image.Image) -> dict:
    s = np.asarray(sdr.convert("L"), dtype=np.float32)
    o = np.asarray(out.convert("L"), dtype=np.float32)
    shadow = s < 64
    s_shadow = float(s[shadow].mean()) if shadow.any() else 1.0
    o_shadow = float(o[shadow].mean()) if shadow.any() else 1.0
    s_clip = float((s >= 254).mean())
    o_clip = float((o >= 254).mean())
    hl_spread, hl_hf = _hl_metrics(s, o)
    return {
        "median_delta": float(np.median(o) - np.median(s)),
        "mean_delta": float(o.mean() - s.mean()),
        # 源图本身的高光就可能纯白（灯、泡沫），所以看「比源多多少」而不是绝对值
        "clip_ratio": o_clip,
        "clip_delta": o_clip - s_clip,
        "shadow_lift": float(o_shadow / max(s_shadow, 1e-6)),
        "ssim": float(ssim_map(s, o).mean()),
        "sat_ratio": float(_saturation(out) / max(_saturation(sdr), 1e-6)),
        # 层次（源高光区退化时为 None，judge 跳过）
        "hf_all": _hf_energy(o) / max(_hf_energy(s), 1e-6),
        "hl_spread": hl_spread,
        "hl_hf": hl_hf,
    }


def judge(m: dict, th: dict, apply_hl: bool = True) -> list[str]:
    """返回失败原因列表；空表示 PASS。apply_hl=False 时跳过高光层次下限（off 档）。"""
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
    if m.get("hf_all") is not None and m["hf_all"] < th["hf_all_min"]:
        bad.append("全图局部对比 x%.2f（下限 %.2f，疑似整体被磨平）"
                   % (m["hf_all"], th["hf_all_min"]))
    if apply_hl:
        if m.get("hl_spread") is not None and m["hl_spread"] < th["hl_spread_min"]:
            bad.append("高光层次 x%.2f（下限 %.2f，高光被压平 = 用户说的「层次少」）"
                       % (m["hl_spread"], th["hl_spread_min"]))
        if m.get("hl_hf") is not None and m["hl_hf"] < th["hl_hf_min"]:
            bad.append("高光局部对比 x%.2f（下限 %.2f）"
                       % (m["hl_hf"], th["hl_hf_min"]))
    return bad


def _sdr_baseline(img: Image.Image) -> Image.Image:
    """独立的 sRGB 归一基准，不复用 bake.to_srgb（docs/07 §2-5「自证」）。

    直接用 ImageCms 读源图自身 ICC → sRGB，与被测实现零共享：若 bake.to_srgb
    被改坏（静默丢 ICC、转换出错退回原图），对照基准不会跟着一起变灰。
    """
    icc = img.info.get("icc_profile")
    if not icc:
        return img.convert("RGB")
    from PIL import ImageCms

    src_prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    desc = ImageCms.getProfileDescription(src_prof).lower()
    if "srgb" in desc and "p3" not in desc and "2020" not in desc:
        return img.convert("RGB")
    return ImageCms.profileToProfile(
        img.convert("RGB"), src_prof, ImageCms.createProfile("sRGB"), outputMode="RGB"
    )


def bake_and_measure(src: vivid_source.HdrSource,
                     profile: str, strength: float,
                     long_edge: int = 1200) -> tuple[Image.Image, Image.Image, dict]:
    opt = bake.BakeOptions(long_edge=long_edge, profile=profile, strength=strength)
    data, _stats = bake.bake(src, opt)
    out = Image.open(io.BytesIO(data)).convert("RGB")
    # 与烘焙端口径一致：源图先转 sRGB（否则 P3 图会被当成「更灰」的对照基准），
    # 但基准用独立的 ImageCms 链路，不经过被测的 bake.to_srgb
    sdr = _sdr_baseline(Image.open(io.BytesIO(src.primary_jpeg)))
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
    for profile in ACCEPT_PROFILES:
        for strength in ACCEPT_STRENGTHS:
            cases.append(("synthetic/%s@%.2f" % (profile, strength), synth_path, profile, strength))
    for p in real_paths:
        for profile in ("local", "off"):
            for strength in ACCEPT_STRENGTHS:
                cases.append(("real:%s/%s@%.2f" % (os.path.basename(p), profile, strength),
                              p, profile, strength))

    def _fmt(v, nd=4):
        return "-" if v is None else round(v, nd)

    rows = []
    failures = 0
    for cid, path, profile, strength in cases:
        src = vivid_source.parse_file(path)
        sdr, _out, m = bake_and_measure(src, profile, strength)
        bad = judge(m, DEFAULT_THRESHOLDS, apply_hl=profile in HL_FLOOR_PROFILES)
        failures += bool(bad)
        rows.append({
            "case": cid, "profile": profile, "strength": strength,
            "metrics": {k: _fmt(v) for k, v in m.items()},
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
        print("验收阈值: 中位±%.0f 均值±%.0f 纯白<%.0f%% 暗部<%.2fx SSIM>%.2f 饱和 %.2f~%.2f"
              % (th["median_delta_max"], th["mean_delta_max"], th["clip_ratio_max"] * 100,
                 th["shadow_lift_max"], th["ssim_min"], th["sat_ratio_min"], th["sat_ratio_max"]))
        print("层次下限: 高光分位距>%.2f 高光对比>%.2f 全图对比>%.2f（%s 档生效）"
              % (th["hl_spread_min"], th["hl_hf_min"], th["hf_all_min"],
                 "/".join(HL_FLOOR_PROFILES)))
        print()
        print("%-38s %7s %7s %7s %8s %8s %8s %s" %
              ("用例", "中位差", "均值差", "纯白%", "高光层次", "高光对比", "全图对比", "结论"))
        for r in rows:
            m = r["metrics"]
            print("%-38s %+7.1f %+7.1f %6.2f%% %8s %8s %8s %s"
                  % (r["case"], m["median_delta"], m["mean_delta"],
                     m["clip_ratio"] * 100,
                     _fmt(m["hl_spread"], 2) if m["hl_spread"] != "-" else "-",
                     _fmt(m["hl_hf"], 2) if m["hl_hf"] != "-" else "-",
                     _fmt(m["hf_all"], 2),
                     "PASS" if r["pass"] else "FAIL"))
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
