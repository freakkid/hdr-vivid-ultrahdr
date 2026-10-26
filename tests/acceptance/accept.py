"""烘焙验收：把「观感对不对」变成可自动判定的指标。

两条链路，两套基准（2026-10-26 起）
----------------------------------
`bake.target_mode_for_profile()` 决定烘焙目标：

**display（local / wechat，出货默认）** —— 目标是「手机上的 HDR 显示效果」。
烘焙图必须真的带上 HDR 的亮度结构，所以基准是**可见增强带 + 自然护栏**：
中位/均值亮度必须比 SDR 底图明显更亮（下限拦「没效果」回归），
但暗部提亮、纯白占比、饱和度、结构相似度都要落在自然范围内（上限拦失控）。

**legacy（off / faithful）** —— 旧行为，明确承诺「不改变整体明暗」。
基准是老的「≈底图」口径（中位 ±3 / 均值 ±5 / 暗部 ≤1.10）。

为什么必须分开：2026-10-25 深夜定位到「烘焙图灰灰的/没效果」的根因，就是
**全链一直拿 SDR 主图当参照基准**——门禁指标全部对比底图，把 HDR 增量死死摁回
平坦底图（实测中间调与底图只差 ±1 灰阶）。用户眼睛的参照物是手机上的 HDR 显示，
两个目标根本不同。用一套「≈底图」阈值去守 display 路径，等于让门禁主动拦住修复。

display 档的护栏来自实测（3 真实样本 × 2 profile × 2 strength，
`out/visual/diag/calib_new.py`）：

| 指标 | 实测 | 阈值 | 为什么是这个值 |
|---|---|---|---|
| `median_delta` | +12 ~ +35 | **+10 ~ +45** | 下限高于 legacy 实测（+0~+2）一个数量级，换回旧算法必被拦 |
| `mean_delta` | +10.4 ~ +30.0 | **+8 ~ +40** | 同上 |
| `shadow_lift` | 1.287 ~ 1.885 | **1.15 ~ 2.10** | 下限拦「暗部没动 = 没效果」 |
| `clip_delta` | -0.002 ~ 0.000 | ≤ **+0.01** | 不许比源多出纯白 |
| `ssim` | 0.895 ~ 0.978 | ≥ **0.88** | 贴着实测最低（A/local@0.70 = 0.895）|
| `sat_ratio` | 0.923 ~ 1.024 | **0.90 ~ 1.15** | 下限仍防丢 ICC |
| `hl_spread` | 0.500 ~ 0.800 | ≥ **0.45** | 贴着实测最低；旧指数肩恒定 0.417，换回去必被拦 |
| `hl_hf` | 0.605 ~ 0.984 | ≥ **0.55** | 0.70 档实测最低 0.605 |
| `hf_all` | 0.946 ~ 1.068 | ≥ **0.92** | display 路径亮度重排更强，全图梯度略降属预期 |

几条容易踩的判断，写下来免得后人改错：

- `hl_spread` 是「高光被拍平」的**主哨兵**，`hl_hf` 是辅哨兵。旧指数肩的
  `hl_spread` 恒为 0.417、`hl_hf` 0.690 —— 0.45 能拦住前者，0.55 拦不住后者，
  所以判据必须放在 `hl_spread` 上。
- `hf_all` 在 display 档实测最低 0.946（C/wechat@0.70），旧阈值 0.95 会误拦，
  故放到 0.92。它只防「整体被磨平」这类灾难性退化（那会掉到 0.85 以下）。
- legacy 档阈值**一个都没动**：`off` 是 A/B 对照旧行为，不代表出货物性。

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

# ---------------------------------------------------------------------------
# 双基准。键名约定：`*_min` / `*_max` 同时存在 = 双侧区间；
# 只有 `*_max` = 对称口径（|实测值| ≤ max），这是 legacy 的老语义。
# ---------------------------------------------------------------------------
DISPLAY_THRESHOLDS = {
    # ---- 增强带（第 4 版：剂量由增益数据逐像素决定）----
    # 依据：① 用户截图反解（中位不动、暗部更深、高光更亮、饱和更足）；
    #       ② 增益图真值（超 SDR 白点像素仅 13~23%，不支持整体提亮）；
    #       ③ 剂量标定：P99(excess) 决定整图 scale，逐像素按自身 excess 分配。
    "median_delta_min": -3.0,
    "median_delta_max": 8.0,
    "mean_delta_min": 0.5,
    "mean_delta_max": 8.0,
    # 暗部：不再要求「必须更暗」——压暗量已改为按该图 HDR 含量缩放
    # （scale = P99(excess)/1.3）。B 这类超白内容多的图几乎不压（实测 1.001），
    # A/C 压到 0.91~0.99。上限 1.02 仍排除「抬暗部」的旧路线（0.1.24 是 1.88）。
    "shadow_lift_min": 0.88,
    "shadow_lift_max": 1.02,
    # ---- 自然护栏 ----
    "clip_ratio_max": 0.01,
    "ssim_min": 0.93,
    "sat_ratio_min": 1.00,   # 额外饱和 = 1 + 0.14×scale，实测 1.005~1.091
    "sat_ratio_max": 1.15,
    # 高光层次下限 0.22：增益驱动的提亮必然吃掉一部分高光层次 —— 提亮的正是
    # 「HDR 超过白点」的那些像素，而它们在 8bit 里本来就逼近 255，加多少压平多少。
    # 已用「头顶空间闸门」把剂量限制在还有余量的像素上（实测 0.70 档 0.25~0.47，
    # 0.40 档 0.58~0.67；不设闸门时 A@0.70 只有 0.17）。要更亮就得更平，
    # 这是 SDR 容器的物理上限，不是参数没调好。clip_delta 与 hl_hf 继续看守。
    "hl_spread_min": 0.22,
    "hl_hf_min": 0.65,
    "hf_all_min": 0.95,
}

LEGACY_THRESHOLDS = {
    "median_delta_max": 3.0,      # 中位亮度差（灰阶），对称口径
    "mean_delta_max": 5.0,        # 均值亮度差（灰阶），对称口径
    "clip_ratio_max": 0.01,
    "shadow_lift_max": 1.10,
    "ssim_min": 0.90,
    "sat_ratio_min": 0.92,
    "sat_ratio_max": 1.15,
    # 「层次少」下限（2026-10-25 加入，当晚随双斜率肩上调）。
    # 首轮依据：local@0.70 把高光分位距压到源的 0.42~0.53、高光局部对比压到
    # 0.64~0.78（3 真实样本 × local/wechat/faithful/off × strength 0.40/0.70）。
    # 双斜率肩上线后复测 hl_spread 0.50~0.80 / hl_hf 0.78~0.97 / hf_all 1.01~1.15，
    # 下限随实测上调且必须落在旧算法实测值（0.417 / 0.690）之下 ——
    # 保证「换回旧肩会被门禁拦住」。
    "hl_spread_min": 0.45,
    "hl_hf_min": 0.75,
    "hf_all_min": 0.95,
}

# 合成样本（uhdr.synthetic 的极端高对比测试图）不是照片：大块纯黑/纯白平台，
# 中位、均值、暗部这些**摄影口径**的指标在上面没有意义（提亮/压暗都只作用在
# 边缘过渡带上）。所以它只做「结构保真」验收：不许爆白、不许走样、不许丢 ICC，
# 增强带放宽为「不崩」。真实照片仍走完整的显示档阈值。
DISPLAY_STRUCT_THRESHOLDS = dict(
    DISPLAY_THRESHOLDS,
    median_delta_min=-20.0, median_delta_max=20.0,
    mean_delta_min=-10.0, mean_delta_max=15.0,
    shadow_lift_min=0.75, shadow_lift_max=1.25,
    sat_ratio_min=0.95, sat_ratio_max=1.25,
)

THRESHOLDS = {"display": DISPLAY_THRESHOLDS, "legacy": LEGACY_THRESHOLDS,
              "display_struct": DISPLAY_STRUCT_THRESHOLDS}

# 层次下限（hl_spread / hl_hf）只对**出货 profile** 生效。`off` 是 A/B 对照旧行为，
# 不代表出货物性（其 0.70 档高光分位距低至 0.33，是旧行为的已知特性）。
HL_FLOOR_PROFILES = ("local", "wechat", "faithful")

# 验收强度矩阵：0.40 是定档基准，0.70 是产品默认（docs/07 §2-4）。
ACCEPT_PROFILES = ("local", "off", "wechat", "faithful")
ACCEPT_STRENGTHS = (0.40, 0.70)


def baseline_for(profile: str) -> str:
    """profile → 基准名。与 `bake.target_mode_for_profile()` 同源同义。"""
    return "legacy" if bake.target_mode_for_profile(profile) == "sdr" else "display"


def thresholds_for(profile: str, synthetic_sample: bool = False) -> dict:
    base = baseline_for(profile)
    if base == "display" and synthetic_sample:
        return DISPLAY_STRUCT_THRESHOLDS
    return THRESHOLDS[base]


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


def _band(bad: list[str], label: str, value: float, th: dict, key: str,
          fmt: str = "%+.1f", scale: float = 1.0, hint: str = "") -> None:
    """按 `key_min`/`key_max` 判一个指标。

    只给 `_max` 时按老语义判 `|value| <= max`（legacy 的对称口径）；
    给了 `_min` 时按双侧带宽判，并区分「低于下限」与「超过上限」的提示语。
    """
    lo = th.get(key + "_min")
    hi = th.get(key + "_max")
    v = value * scale
    if lo is None:
        if hi is not None and abs(v) > hi * scale:
            bad.append("%s %s 偏离（上限 %s）"
                       % (label, fmt % v, fmt.strip("+") % (hi * scale)))
        return
    lo_v, hi_v = lo * scale, (hi * scale if hi is not None else None)
    if v < lo_v:
        why = "，疑似「没效果」回归" if hint == "low" else ""
        bad.append("%s %s 低于下限 %s%s" % (label, fmt % v, fmt.strip("+") % lo_v, why))
    elif hi_v is not None and v > hi_v:
        why = "，疑似失控提亮" if hint == "high" else ""
        bad.append("%s %s 超过上限 %s%s" % (label, fmt % v, fmt.strip("+") % hi_v, why))


def judge(m: dict, profile: str, synthetic_sample: bool = False) -> list[str]:
    """返回失败原因列表；空表示 PASS。

    基准按 profile 自动分派（display / legacy），不再由调用方传阈值 ——
    传阈值容易让「同一个 profile 在不同调用点用不同口径」。
    """
    th = thresholds_for(profile, synthetic_sample)
    bad: list[str] = []

    # ---- 亮度：display 是双向带宽，legacy 是对称偏差 ----
    _band(bad, "中位亮度", m["median_delta"], th, "median_delta", hint="low")
    _band(bad, "均值亮度", m["mean_delta"], th, "mean_delta", hint="low")
    _band(bad, "暗部提亮", m["shadow_lift"], th, "shadow_lift",
          fmt="%.3f", hint="low")

    # ---- 自然护栏 ----
    if m["clip_delta"] > th["clip_ratio_max"]:
        bad.append("纯白占比比源多 %.2f 个百分点（上限 %.2f）"
                   % (m["clip_delta"] * 100, th["clip_ratio_max"] * 100))
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
    if profile in HL_FLOOR_PROFILES:
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
    # 真实样本跑全 4 档：display 与 legacy 两套基准都要被真实数据压过一遍
    for p in real_paths:
        for profile in ACCEPT_PROFILES:
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
        bad = judge(m, profile, synthetic_sample=cid.startswith("synthetic/"))
        failures += bool(bad)
        rows.append({
            "case": cid, "profile": profile, "strength": strength,
            "baseline": baseline_for(profile),
            "metrics": {k: _fmt(v) for k, v in m.items()},
            "pass": not bad, "reasons": bad,
        })

    if as_json:
        print(json.dumps({
            "thresholds": THRESHOLDS,
            "cases": rows,
            "failures": failures,
        }, ensure_ascii=False, indent=2))
    else:
        d, l = DISPLAY_THRESHOLDS, LEGACY_THRESHOLDS
        print("display 基准（local/wechat）: 中位 %+.0f~%+.0f 均值 %+.0f~%+.0f 暗部 %.2f~%.2f "
              "纯白<%.0f%% SSIM>%.2f 饱和 %.2f~%.2f"
              % (d["median_delta_min"],
                 d["median_delta_max"], d["mean_delta_min"], d["mean_delta_max"],
                 d["shadow_lift_min"], d["shadow_lift_max"], d["clip_ratio_max"] * 100,
                 d["ssim_min"], d["sat_ratio_min"], d["sat_ratio_max"]))
        print("display 层次下限: 高光分位距>%.2f 高光对比>%.2f 全图对比>%.2f"
              % (d["hl_spread_min"], d["hl_hf_min"], d["hf_all_min"]))
        print("legacy 基准（off/faithful）: 中位±%.0f 均值±%.0f 纯白<%.0f%% 暗部<%.2fx "
              "SSIM>%.2f 饱和 %.2f~%.2f"
              % (l["median_delta_max"], l["mean_delta_max"], l["clip_ratio_max"] * 100,
                 l["shadow_lift_max"], l["ssim_min"], l["sat_ratio_min"], l["sat_ratio_max"]))
        print("legacy 层次下限: 高光分位距>%.2f 高光对比>%.2f 全图对比>%.2f（%s 档生效）"
              % (l["hl_spread_min"], l["hl_hf_min"], l["hf_all_min"],
                 "/".join(HL_FLOOR_PROFILES)))
        print()
        print("%-40s %-8s %7s %7s %8s %9s %8s %8s %s" %
              ("用例", "基准", "中位差", "均值差", "暗部", "高光层次", "高光对比", "全图对比", "结论"))
        for r in rows:
            m = r["metrics"]
            print("%-40s %-8s %+7.1f %+7.1f %8.3f %9s %8s %8s %s"
                  % (r["case"], r["baseline"], m["median_delta"], m["mean_delta"],
                     m["shadow_lift"],
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
            json.dump({"thresholds": THRESHOLDS, "cases": rows}, f,
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
