#!/usr/bin/env python3
"""Python / Kotlin 双实现的一致性检查（不需要编译器、不需要设备）。

为什么需要：真正坑人的不是「两边算法写得不一样」，而是**一边改了、另一边只加字段没使用**
（v0.1.14 的 `maxLift`、以及 strength 只乘增益却没作用到增量上，都是这样漏掉的）。
单测只覆盖 Python，所以必须有一层「常量与关键表达式是否同步」的静态对账。

检查项（任一不一致 → 非零退出）：
- `blend_start/blend_end`（0.35 / 0.90）
- `max_lift`（1.25）与 faithful 档的 1.05
- 「strength 作用在增量上」的表达式两边都在
- faithful 的 LTM 参数（0.97 / 1.08 / 0.70）
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_BAKE = os.path.join(ROOT, "src", "python", "uhdr", "bake.py")
PY_LTM = os.path.join(ROOT, "src", "python", "uhdr", "ltm.py")
KT_BAKE = os.path.join(ROOT, "src", "android", "app", "src", "main", "java", "com", "finn", "hdr", "HdrBaker.kt")
KT_LTM = os.path.join(ROOT, "src", "android", "app", "src", "main", "java", "com", "finn", "hdr", "LocalToneMapping.kt")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def num(text: str, pattern: str) -> float | None:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def main() -> int:
    py, kt = read(PY_BAKE), read(KT_BAKE)
    pyl, ktl = read(PY_LTM), read(KT_LTM)

    pairs = [
        ("blend_start", num(py, r"blend_start:\s*float\s*=\s*([\d.]+)"),
         num(kt, r"blendStart:\s*Float\s*=\s*([\d.]+)f")),
        ("blend_end", num(py, r"blend_end:\s*float\s*=\s*([\d.]+)"),
         num(kt, r"blendEnd:\s*Float\s*=\s*([\d.]+)f")),
        ("max_lift", num(py, r"max_lift:\s*float\s*=\s*([\d.]+)"),
         num(kt, r"maxLift:\s*Float\s*=\s*([\d.]+)f")),
        ("faithful 的 LTM range_compress", num(pyl, r"range_compress=0\.9?([\d.]+)"),
         num(ktl, r"rangeCompress\s*=\s*0\.9?([\d.]+)f")),
    ]
    for name, a, b in pairs:
        check(name, a is not None and b is not None and abs(a - b) < 1e-6, f"py={a} kt={b}")

    # faithful 档的 maxLift 必须钉在 1.05（这是「嫌亮」时的逃生阀）
    check("faithful cap=1.05（py）", bool(re.search(r"min\(opt\.max_lift,\s*1\.05\)", py)),
          "bake.py 缺 min(opt.max_lift, 1.05)")
    check("faithful cap=1.05（kt）", bool(re.search(r"minOf\(opt\.maxLift,\s*1\.05f\)", kt)),
          "HdrBaker.kt 缺 minOf(opt.maxLift, 1.05f)")

    # strength 必须作用在「增量」上，否则色调映射会把差异压平（v0.1.18 的真 bug）
    check("strength 作用于增量（py）", bool(re.search(r"\(blended\s*-\s*sdr_lum\)\s*\*\s*amount", py)),
          "bake.py 缺 (blended - sdr_lum) * amount")
    check("strength 作用于增量（kt）", bool(re.search(r"\(blended\s*-\s*sdrLum\[i\]\)\s*\*\s*amount", kt)),
          "HdrBaker.kt 缺 (blended - sdrLum[i]) * amount")

    # ---- 目标模式分派（display = 底图锚定 + HDR 观感曲线）：两端必须同源 ----
    # 只要有一端漏改，「Python 出增强图、App 出灰图」这种最坏情况就会静默发生。
    check("targetModeForProfile 存在（py）", "def target_mode_for_profile" in py, "bake.py 缺该函数")
    check("targetModeForProfile 存在（kt）", "fun targetModeForProfile" in kt, "HdrBaker.kt 缺该函数")
    py_family = re.search(r'return "sdr" if profile in \((.*?)\)', py)
    kt_family = re.search(r'profile == "off" \|\| profile == "faithful"', kt)
    check("targetModeForProfile 归类一致",
          bool(py_family) and bool(kt_family)
          and {s.strip().strip('"\'') for s in py_family.group(1).split(",")} == {"off", "faithful"},
          f"py={py_family.group(1) if py_family else None} kt={'off/faithful' if kt_family else None}")

    # ---- 增益驱动剂量：函数与常量逐项对账 ----
    for name, py_tok, kt_tok in [
        ("hdr_content_scale", "def hdr_content_scale(", "fun hdrContentScale("),
        ("hdr_lift_stops", "def hdr_lift_stops(", "fun hdrLiftStops("),
        ("hdr_toe_stops", "def hdr_toe_stops(", "fun hdrToeStops("),
        ("apply_hdr_curve", "def _apply_hdr_curve(", "fun applyHdrCurve("),
    ]:
        check(f"{name} 存在", py_tok in py and kt_tok in kt,
              f"py={py_tok in py} kt={kt_tok in kt}")

    curve_pairs = [
        ("toe_max", r'"toe_max":\s*([\d.]+)', r"TOE_MAX\s*=\s*([\d.]+)f"),
        ("toe_end", r'"toe_end":\s*([\d.]+)', r"TOE_END\s*=\s*([\d.]+)f"),
        ("lift_max", r'"lift_max":\s*([\d.]+)', r"LIFT_MAX\s*=\s*([\d.]+)f"),
        ("lift_eps", r'"lift_eps":\s*([\d.]+)', r"LIFT_EPS\s*=\s*([\d.]+)f"),
        ("sat_max", r'"sat_max":\s*([\d.]+)', r"SAT_MAX\s*=\s*([\d.]+)f"),
        ("ref_excess", r'"ref_excess":\s*([\d.]+)', r"REF_EXCESS\s*=\s*([\d.]+)f"),
        ("headroom_ceiling", r'"headroom_ceiling":\s*([\d.]+)',
         r"HEADROOM_CEILING\s*=\s*([\d.]+)f"),
        ("headroom_span", r'"headroom_span":\s*([\d.]+)', r"HEADROOM_SPAN\s*=\s*([\d.]+)f"),
        ("ceil_knee", r"CURVE_CEIL_KNEE\s*=\s*([\d.]+)", r"CURVE_CEIL_KNEE\s*=\s*([\d.]+)f"),
        ("ceil", r"CURVE_CEIL\s*=\s*([\d.]+)", r"CURVE_CEIL\s*=\s*([\d.]+)f"),
    ]
    for name, pat_py, pat_kt in curve_pairs:
        a, b = num(py, pat_py), num(kt, pat_kt)
        check(f"剂量常量 {name}", a is not None and b is not None and abs(a - b) < 1e-6,
              f"py={a} kt={b}")

    # 剂量必须「按增益」算：两端都得用 HDR 亮度减白点，而不是按码值查表
    check("lift 由增益推出（py）",
          bool(re.search(r"ex\s*=\s*np\.maximum\(lum\s*-\s*1\.0,\s*0\.0\)", py)),
          "bake.py 的 lift 没用 excess = max(HDR-1,0)")
    check("lift 由增益推出（kt）",
          bool(re.search(r"val ex = \(lum\[i\] - 1f\)", kt)),
          "HdrBaker.kt 的 lift 没用 excess = max(HDR-1,0)")
    check("lift 受头顶空间约束（py）", "headroom_ceiling" in py and "headroom_span" in py,
          "bake.py 缺头顶空间闸门")
    check("lift 受头顶空间约束（kt）", "HEADROOM_CEILING" in kt and "HEADROOM_SPAN" in kt,
          "HdrBaker.kt 缺头顶空间闸门")
    check("曲线用精确 sRGB 转换（kt）",
          "fun linToSrgbExact(" in kt and "fun srgbToLinExact(" in kt,
          "HdrBaker.kt 曲线仍在用 LUT（会把像素对照推到 mean>1.0）")

    # faithful 的 LTM 参数：0.97 / 1.08 / 0.70 两个文件都要有
    for name, token_py, token_kt in [
        ("faithful detail_gain 1.08", r"detail_gain=1\.08", r"detailGain\s*=\s*1\.08f"),
        ("faithful highlight_start 0.70", r"highlight_start=0\.70", r"highlightStart\s*=\s*0\.70f"),
    ]:
        check(name, bool(re.search(token_py, pyl)) and bool(re.search(token_kt, ktl)),
              f"py={bool(re.search(token_py, pyl))} kt={bool(re.search(token_kt, ktl))}")

    failed = 0
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   [{detail}]"))
        failed += 0 if ok else 1
    print(f"\n{len(results) - failed}/{len(results)} 一致")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
