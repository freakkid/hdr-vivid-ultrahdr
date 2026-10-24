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
