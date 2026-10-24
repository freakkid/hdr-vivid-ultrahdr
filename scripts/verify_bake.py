#!/usr/bin/env python3
"""一条命令自检烘焙效果 —— 不需要人眼看图，也不用手动传样例。

做四件事（任一失败即非零退出）：

1. **拉样例**：从手机 `Download/` 把 `HDR_SAMPLE_*.jpg` 拉到 `out/visual/samples/`
   （该目录已被 gitignore，真实照片不会入库）。
2. **静态自检**：`check_android_project.py`（含 `bake.lift_cap_applied` —— 防止
   「只加字段没落地」再次发生）。
3. **回归**：`python -m unittest discover -s tests`。
4. **观感验收**：`tests/acceptance/accept.py` 的 6 个客观指标，跑真机样例 + 合成样例。

用法::

    python scripts/verify_bake.py                 # 自动从手机拉样例
    python scripts/verify_bake.py --no-pull        # 只用已拉过的样例
    python scripts/verify_bake.py --samples a.jpg b.jpg

退出码：0 全过 / 1 有失败 / 2 环境或样例缺失。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(ROOT, "out", "visual", "samples")
REMOTE_DIR = "/storage/media/100/local/files/Docs/Download"
HDC_CANDIDATES = [
    os.environ.get("HDC", ""),
    r"C:/DevEco/command-line-tools/sdk/default/openharmony/toolchains/hdc.exe",
    "hdc",
]


def _env() -> dict:
    env = dict(os.environ)
    sep = ";" if os.name == "nt" else ":"
    env["PYTHONPATH"] = os.path.join(ROOT, "src", "python") + sep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def find_hdc() -> str | None:
    for c in HDC_CANDIDATES:
        if not c:
            continue
        if os.path.sep in c or "/" in c:
            if os.path.isfile(c):
                return c
        else:
            from shutil import which
            if which(c):
                return c
    return None


def pull_samples(hdc: str) -> list[str]:
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    try:
        out = subprocess.run([hdc, "shell", f"ls {REMOTE_DIR}"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[pull] 无法执行 hdc：{e}")
        return []
    if out.returncode != 0:
        print(f"[pull] hdc shell 失败：{out.stderr.strip()[:200]}")
        return []

    names = [n.strip() for n in out.stdout.splitlines()
             if n.strip().startswith("HDR_SAMPLE_") and n.strip().lower().endswith((".jpg", ".jpeg"))]
    if not names:
        print(f"[pull] 手机 {REMOTE_DIR} 里没有 HDR_SAMPLE_*.jpg")
        return []

    local: list[str] = []
    for name in names:
        dst = os.path.join(SAMPLES_DIR, name)
        r = subprocess.run([hdc, "file", "recv", f"{REMOTE_DIR}/{name}", dst],
                           capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 1024:
            local.append(dst)
            print(f"[pull] {name}  {os.path.getsize(dst) / 1048576:.1f} MB")
        else:
            print(f"[pull] {name} 失败：{(r.stderr or r.stdout).strip()[:160]}")
    return local


def existing_samples() -> list[str]:
    if not os.path.isdir(SAMPLES_DIR):
        return []
    return [os.path.join(SAMPLES_DIR, n) for n in sorted(os.listdir(SAMPLES_DIR))
            if n.lower().endswith((".jpg", ".jpeg"))]


def run_step(title: str, cmd: list[str]) -> bool:
    print("\n" + "=" * 72)
    print(f"== {title}")
    print("=" * 72)
    r = subprocess.run(cmd, cwd=ROOT, env=_env())
    ok = r.returncode == 0
    print(f"-- {title}: {'OK' if ok else 'FAILED'}（exit={r.returncode}）")
    return ok


def run_acceptance(samples: list[str], strength: float) -> bool:
    print("\n" + "=" * 72)
    print("== 观感验收（6 个客观指标）")
    print("=" * 72)
    sys.path.insert(0, os.path.join(ROOT, "src", "python"))
    sys.path.insert(0, os.path.join(ROOT, "tests", "acceptance"))
    os.environ.update(_env())
    import accept  # noqa: E402  tests/acceptance/accept.py

    if strength != 0.40:
        # 验收脚本内部硬编码 0.40（与 docs/04 定档一致）；这里只提示，不改语义
        print(f"[warn] 验收固定用 strength=0.40，忽略传入的 {strength:.2f}")
    rc = accept.run(samples, as_json=False, update=False)
    print(f"-- 观感验收: {'OK' if rc == 0 else 'FAILED'}")
    return rc == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="一条命令验证烘焙效果")
    ap.add_argument("--samples", nargs="*", default=None,
                    help="直接用这些样例（不拉手机）")
    ap.add_argument("--no-pull", action="store_true", help="不访问手机，只用本地已有样例")
    ap.add_argument("--strength", type=float, default=0.40, help="仅提示用，验收固定 0.40")
    args = ap.parse_args()

    print(f"仓库: {ROOT}")

    if args.samples:
        samples = [os.path.abspath(p) for p in args.samples]
        missing = [p for p in samples if not os.path.isfile(p)]
        if missing:
            print(f"样例不存在：{missing}")
            return 2
    elif args.no_pull:
        samples = existing_samples()
        print(f"本地样例: {len(samples)} 个（{SAMPLES_DIR}）")
    else:
        hdc = find_hdc()
        if not hdc:
            print("找不到 hdc，且未指定 --samples / --no-pull")
            return 2
        samples = pull_samples(hdc)
        if not samples:
            samples = existing_samples()
            if samples:
                print(f"[pull] 沿用本地已有样例 {len(samples)} 个")
            else:
                print("没有可用样例：请把 HDR_SAMPLE_*.jpg 放到手机 Download/，或传 --samples")
                return 2

    print(f"真机样例: {len(samples)} 个")
    for p in samples:
        print(f"  · {os.path.basename(p)}  {os.path.getsize(p) / 1048576:.1f} MB")

    results: list[tuple[str, bool]] = []
    results.append(("静态自检",
                    run_step("静态自检 check_android_project.py",
                             [sys.executable, os.path.join(ROOT, "scripts", "check_android_project.py")])))
    results.append(("回归测试",
                    run_step("回归测试 unittest discover",
                             [sys.executable, "-m", "unittest", "discover", "-s", os.path.join(ROOT, "tests")])))
    results.append(("观感验收", run_acceptance(samples, args.strength)))

    print("\n" + "=" * 72)
    for name, ok in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in results if not ok]
    print("=" * 72)
    if failed:
        print(f"结论：未通过（{', '.join(failed)}）")
        return 1
    print("结论：全部通过。渲染结果已由客观指标验收，不需要人工看图。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
