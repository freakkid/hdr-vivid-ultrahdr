#!/usr/bin/env python3
"""Kotlin(`HeifGridRewriter.kt`) ↔ Python(`heif_grid.py`) 的 grid 重封对照。

`heif_grid` 有 16 项 Python 单测，但那些只证明 Python 自洽。App 跑的是 Kotlin，
所以这里在本机用 kotlinc 编出**真实的** Kotlin 实现，对同一个合成 HEIC 各自重封，
再把两个产物都用 Python 解析回来，比对语义（item 集合 / tile 载荷 / grid 参数 / dimg 顺序）。

用法：python tests/android_parity/run_grid_parity.py [--rebuild]
退出码：0 一致 / 1 不一致 / 2 环境缺失（无 kotlinc）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src", "python"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from uhdr.heif_grid import HeifGrids      # noqa: E402

JAR = os.path.join(ROOT, "out", "kotlin_parity", "heif.jar")
SRC = [
    os.path.join(ROOT, "src", "android", "app", "src", "main", "java", "com", "finn", "hdr", "HeifGridRewriter.kt"),
    os.path.join(ROOT, "tools", "kotlin_parity", "HeifMain.kt"),
]


def find_kotlinc() -> str | None:
    from shutil import which
    for c in [
        os.environ.get("KOTLINC", ""),
        os.path.expanduser("~/.workbuddy/binaries/kotlinc/kotlinc/bin/kotlinc.bat"),
        os.path.expanduser("~/.workbuddy/binaries/kotlinc/kotlinc/bin/kotlinc"),
        "kotlinc",
    ]:
        if not c:
            continue
        if os.sep in c or "/" in c:
            if os.path.isfile(c):
                return c
        elif which(c):
            return c
    return None


def build(kotlinc: str, rebuild: bool) -> bool:
    if os.path.isfile(JAR) and not rebuild and os.path.getmtime(JAR) > max(map(os.path.getmtime, SRC)):
        return True
    os.makedirs(os.path.dirname(JAR), exist_ok=True)
    env = dict(os.environ)
    env.setdefault("JAVA_HOME", os.path.expanduser("~/.workbuddy/binaries/jdk/17"))
    env["PATH"] = os.path.join(env["JAVA_HOME"], "bin") + os.pathsep + env.get("PATH", "")
    print(f"[build] kotlinc → {JAR}")
    r = subprocess.run([kotlinc, *SRC, "-include-runtime", "-d", JAR],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        print("[build] 失败：\n" + (r.stderr or r.stdout)[-1500:])
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    kotlinc = find_kotlinc()
    if not kotlinc:
        print("没有 kotlinc：设 KOTLINC=<path>")
        return 2
    if not build(kotlinc, args.rebuild):
        return 2

    # 合成源：2x2 增益图 grid（#42）+ 3x4 主图 grid（#50）
    import test_heif_grid as t
    source = t.build_source()

    java = os.path.join(os.path.expanduser("~/.workbuddy/binaries/jdk/17"), "bin", "java.exe")
    if not os.path.isfile(java):
        java = "java"

    with tempfile.TemporaryDirectory() as tmp:
        src_p = os.path.join(tmp, "in.heic")
        kt_p = os.path.join(tmp, "kt.heic")
        open(src_p, "wb").write(source)

        # JVM 17 在 Windows 上默认按 locale(GBK) 输出中文，必须强制 UTF-8，否则父进程解码失败
        r = subprocess.run([java, "-Dfile.encoding=UTF-8", "-jar", JAR, src_p],
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            print("Kotlin summary 失败：", (r.stderr or r.stdout)[-500:])
            return 1
        summary = r.stdout
        ok_summary = "grid #42 2x2" in summary
        print(f"{'PASS' if ok_summary else 'FAIL'}  Kotlin summary 认出 2x2 增益图")
        if not ok_summary:
            print(summary)

        r2 = subprocess.run([java, "-Dfile.encoding=UTF-8", "-jar", JAR, src_p, "--rewrap", "42", kt_p],
                            capture_output=True, text=True, encoding="utf-8")
        if r2.returncode != 0 or not os.path.isfile(kt_p):
            print("Kotlin rewrap 失败：", (r2.stderr or r2.stdout)[-500:])
            return 1
        kt_blob = open(kt_p, "rb").read()

    py_blob = HeifGrids(source).rewrap(42)

    def digest(blob: bytes) -> dict:
        h = HeifGrids(blob)
        g = h.grids()[0]
        items = {i: (h.items[i].item_type, h.item_bytes(h.items[i]))
                 for i in sorted(h.items)}
        props = {i: [t for t, _ in h.properties_of(i)] for i in sorted(h.items)}
        return {
            "pitm": h.pitm,
            "grid": (g.item_id, g.rows, g.cols, g.out_w, g.out_h, tuple(g.tile_ids)),
            "items": items,
            "props": props,
            "grid_payload": h.item_bytes(h.items[g.item_id]),
        }

    kd, pd = digest(kt_blob), digest(py_blob)
    checks = [
        ("pitm 指向 grid", kd["pitm"] == pd["pitm"]),
        ("grid 参数一致（行列/输出/块序）", kd["grid"] == pd["grid"]),
        ("item 集合一致", set(kd["items"]) == set(pd["items"])),
        ("grid 载荷逐字节一致", kd["grid_payload"] == pd["grid_payload"]),
        ("tile 载荷逐字节一致",
         all(kd["items"][i] == pd["items"][i] for i in kd["items"] if kd["items"][i][0] == "hvc1")),
        ("属性集合一致（含 hvcC/ispe）",
         all(kd["props"].get(i) == pd["props"].get(i) for i in kd["props"])),
    ]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"\nKotlin {len(kt_blob)}B / Python {len(py_blob)}B")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
