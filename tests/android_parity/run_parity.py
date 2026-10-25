#!/usr/bin/env python3
"""Kotlin(`HdrBaker.kt`) ↔ Python(`bake.py`) 逐像素对照 —— 在本机执行，不需要手机。

为什么要它：单测只覆盖 Python；App 跑的是 Kotlin。历史上出现过「Python 验收全绿、
App 端照样过曝」（`maxLift` 只加字段没落地）。只有真把 Kotlin 跑起来、逐点比对，
才能在没有设备时确认两端算法一致。

做法：
1. 用 kotlinc 把**真实的** `HdrBaker.kt` + `LocalToneMapping.kt` 与两个桩
   （`tools/kotlin_parity/` 下的 `android.graphics.Bitmap`、`UltraHdrNative`）编成 jar；
2. 取真实 HDR 样例，解出 base / gain，统一缩放到同一尺寸后写成 **PNG**（无损）；
3. Kotlin 与 Python 分别烘焙同一份 PNG；Python 用 `bake(..., return_pixels=True)`
   跳过 JPEG 编码；
4. 逐像素比对（均值差 / P99 / 最大差）并给出阈值判定。

用法：
    python tests/android_parity/run_parity.py                 # 用 out/visual/samples
    python tests/android_parity/run_parity.py --rebuild       # 强制重编 jar
    python tests/android_parity/run_parity.py --samples a.jpg
退出码（三态，docs/07 §2-1）：0 一致 / 1 数值超阈值或比对失败（FAIL） /
2 工具缺失（无 kotlinc 或样例，SKIP）/ 3 编译失败或一个用例都没比对上（FAIL）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src", "python"))

from uhdr import bake as bake_mod          # noqa: E402
from uhdr import vivid_source              # noqa: E402

JAR = os.path.join(ROOT, "out", "kotlin_parity", "parity.jar")
SAMPLES_DIR = os.path.join(ROOT, "out", "visual", "samples")
SRC = [
    os.path.join(ROOT, "src", "android", "app", "src", "main", "java", "com", "finn", "hdr", "HdrBaker.kt"),
    os.path.join(ROOT, "src", "android", "app", "src", "main", "java", "com", "finn", "hdr", "LocalToneMapping.kt"),
    os.path.join(ROOT, "tools", "kotlin_parity", "android", "graphics", "Bitmap.kt"),
    os.path.join(ROOT, "tools", "kotlin_parity", "UltraHdrNativeStub.kt"),
    os.path.join(ROOT, "tools", "kotlin_parity", "Main.kt"),
]

# 阈值：算法一致 + 浮点/查表差异允许 1~2 级
MEAN_MAX = 1.0
P99_MAX = 4
MAX_MAX = 12


def find_kotlinc() -> str | None:
    cand = [
        os.environ.get("KOTLINC", ""),
        os.path.expanduser("~/.workbuddy/binaries/kotlinc/kotlinc/bin/kotlinc.bat"),
        os.path.expanduser("~/.workbuddy/binaries/kotlinc/kotlinc/bin/kotlinc"),
        "kotlinc",
    ]
    for i, c in enumerate(cand):
        if not c:
            continue
        if os.sep in c or "/" in c:
            if os.path.isfile(c):
                return c
        elif shutil.which(c):
            return c
        if i == 0 and os.environ.get("KOTLINC"):
            # 显式指定的 KOTLINC 无效时不静默回落：否则门禁永远用另一个编译器，
            # 用户以为在验证的环境和实际跑的不是同一个
            print(f"[warn] KOTLINC={c} 不存在，且不再回落默认路径 → 按工具缺失处理")
            return None
    return None


def find_java() -> str:
    cand = [
        os.path.join(os.environ.get("JAVA_HOME", ""), "bin", "java.exe"),
        os.path.expanduser("~/.workbuddy/binaries/jdk/17/bin/java.exe"),
        "java",
    ]
    for c in cand:
        if c and (os.path.isfile(c) or (os.sep not in c and shutil.which(c))):
            return c
    return "java"


def _src_fingerprint() -> str:
    """参与编译的源码树 sha256（文件名+内容依次喂入）。

    jar 复用不能只看 mtime：git checkout / 换分支后 mtime 可能比 jar 新也可能旧，
    曾有对着旧 Kotlin 比对的风险（docs/07 §2-7）。哈希不一致就强制重编。
    """
    h = hashlib.sha256()
    for p in SRC:
        with open(p, "rb") as f:
            h.update(os.path.basename(p).encode("utf-8"))
            h.update(b"\0")
            h.update(f.read())
    return h.hexdigest()


SIDECAR = JAR + ".sha256"


def _read_sidecar() -> str:
    try:
        with open(SIDECAR, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def build_jar(kotlinc: str, rebuild: bool) -> bool:
    if os.path.isfile(JAR) and not rebuild:
        want = _src_fingerprint()
        if _read_sidecar() == want:
            return True
        print("[build] 源码树哈希与侧车不符（git checkout / 换机器），强制重编")
    os.makedirs(os.path.dirname(JAR), exist_ok=True)
    print(f"[build] kotlinc → {JAR}")
    env = dict(os.environ)
    env.setdefault("JAVA_HOME", os.path.expanduser("~/.workbuddy/binaries/jdk/17"))
    env["PATH"] = os.path.join(env["JAVA_HOME"], "bin") + os.pathsep + env.get("PATH", "")
    r = subprocess.run([kotlinc, *SRC, "-include-runtime", "-d", JAR],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        print("[build] 失败：\n" + (r.stderr or r.stdout)[-2000:])
        return False
    with open(SIDECAR, "w", encoding="utf-8") as f:
        f.write(_src_fingerprint())
    return True


def run_case(java: str, path: str, profile: str, strength: float, long_edge: int) -> tuple[dict, bool]:
    src = vivid_source.parse_file(path)
    if not src.gainmap_jpeg or src.meta is None:
        return {"case": os.path.basename(path), "skip": "无增益图或元数据"}, True
    meta = src.meta

    # 与两端实现一致：先把源图从自身 ICC（多为 Display P3）转到 sRGB 再喂进去，
    # 否则对照的是「双方都没做色彩管理」的旧行为
    base = bake_mod.to_srgb(Image.open(io.BytesIO(src.primary_jpeg)))
    gain = Image.open(io.BytesIO(src.gainmap_jpeg)).convert("RGB")
    cur = max(base.size)
    if cur > long_edge:
        k = long_edge / cur
        size = (max(1, round(base.width * k)), max(1, round(base.height * k)))
        base = base.resize(size, Image.Resampling.LANCZOS)
    gain = gain.resize(base.size, Image.Resampling.LANCZOS)

    with tempfile.TemporaryDirectory() as tmp:
        base_p, gain_p, out_p = (os.path.join(tmp, n) for n in ("b.png", "g.png", "o.png"))
        base.save(base_p, "PNG")
        gain.save(gain_p, "PNG")

        cmd = [java, "-jar", JAR, base_p, gain_p, out_p, profile, repr(strength),
               repr(meta.gain_map_min), repr(meta.gain_map_max), repr(meta.gamma),
               repr(meta.offset_sdr), repr(meta.offset_hdr), str(long_edge)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0 or not os.path.isfile(out_p):
            return {"case": os.path.basename(path), "error": (r.stderr or r.stdout)[-400:]}, False

        kt = np.asarray(Image.open(out_p).convert("RGB")).astype(np.int16)
        kt_stats = r.stdout.strip()

    # Python 侧：喂同一份 PNG，跳过 JPEG 编码
    fake = vivid_source.HdrSource(
        path=path, container=src.container,
        primary_jpeg=_png_bytes(base),
        gainmap_jpeg=_png_bytes(gain),
        meta=meta, exif_make=src.exif_make, iso_meta=src.iso_meta,
    )
    py_img, py_stats = bake_mod.bake(
        fake, bake_mod.BakeOptions(long_edge=long_edge, profile=profile, strength=strength),
        return_pixels=True,
    )
    py = np.asarray(py_img.convert("RGB")).astype(np.int16)

    if py.shape != kt.shape:
        return {"case": os.path.basename(path),
                "error": f"尺寸不一致 kt={kt.shape} py={py.shape}"}, False

    diff = np.abs(kt - py)
    m = {
        "case": os.path.basename(path),
        "profile": profile,
        "mean": float(diff.mean()),
        "p99": float(np.percentile(diff, 99)),
        "max": int(diff.max()),
        "kt": kt_stats,
        "py_mean_lift": getattr(py_stats, "mean_lift", None),
    }
    ok = m["mean"] <= MEAN_MAX and m["p99"] <= P99_MAX and m["max"] <= MAX_MAX
    return m, ok


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description="Kotlin/Python 烘焙逐像素对照")
    ap.add_argument("--samples", nargs="*", default=None)
    ap.add_argument("--profiles", nargs="*", default=["local", "off", "faithful"])
    ap.add_argument("--strength", type=float, default=0.70)
    ap.add_argument("--long-edge", type=int, default=512)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    kotlinc = find_kotlinc()
    if not kotlinc:
        print("没有 kotlinc：装一个，或设 KOTLINC=<path>")
        return 2
    if not build_jar(kotlinc, args.rebuild):
        return 3

    samples = args.samples or ([os.path.join(SAMPLES_DIR, n) for n in sorted(os.listdir(SAMPLES_DIR))
                                if n.lower().endswith(".jpg")] if os.path.isdir(SAMPLES_DIR) else [])
    samples = [s for s in samples if os.path.isfile(s)]
    if not samples:
        print("没有样例：先跑 python scripts/verify_bake.py 拉样例，或传 --samples")
        return 2

    java = find_java()
    print(f"样例 {len(samples)} 个，profiles={args.profiles}，strength={args.strength}")
    print(f"{'用例':<38}{'均值差':>8}{'P99':>6}{'最大':>6}  结论")
    fails = 0
    executed = 0
    for path in samples:
        for profile in args.profiles:
            m, ok = run_case(java, path, profile, args.strength, args.long_edge)
            name = f"{m['case']}/{profile}"
            if "skip" in m:
                print(f"{name:<38}{'-':>8}{'-':>6}{'-':>6}  SKIP（{m['skip']}）")
                continue
            executed += 1
            if "error" in m:
                print(f"{name:<38}{'-':>8}{'-':>6}{'-':>6}  ERROR {m['error'][:60]}")
                fails += 1
                continue
            print(f"{name:<38}{m['mean']:>8.2f}{m['p99']:>6.0f}{m['max']:>6d}  {'PASS' if ok else 'FAIL'}")
            if not ok:
                print(f"      kt: {m['kt']}")
            fails += 0 if ok else 1

    print(f"\n阈值: 均值≤{MEAN_MAX} P99≤{P99_MAX} 最大≤{MAX_MAX}")
    if executed == 0:
        # 一个用例都没比对上（全是 SKIP）却报「全部一致」就是假绿（docs/07 §2-2）
        print("结论: 用例数为 0 —— 没有完成任何逐像素对照，判 FAIL")
        return 3
    print("结论:", "全部一致" if fails == 0 else f"{fails} 例超阈值")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
