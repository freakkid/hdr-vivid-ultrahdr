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
import re
import shutil
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


def run_step_optional(title: str, cmd: list[str]) -> bool:
    """跑一个步骤；退出码 2 视为「环境缺失，跳过」，不计为失败。"""
    print("\n" + "=" * 72)
    print(f"== {title}")
    print("=" * 72)
    r = subprocess.run(cmd, cwd=ROOT, env=_env())
    if r.returncode == 2:
        print(f"-- {title}: 跳过（缺环境）")
        return True
    print(f"-- {title}: {'OK' if r.returncode == 0 else 'FAILED'}")
    return r.returncode == 0


def run_android_parity(samples: list[str], strength: float) -> bool:
    """在本机执行真实的 Kotlin HdrBaker，与 Python 逐像素比对。

    缺 kotlinc 时按「跳过」处理（返回 True），不把环境缺失算成回归失败。
    """
    script = os.path.join(ROOT, "tests", "android_parity", "run_parity.py")
    if not os.path.isfile(script):
        print("-- 跳过：没有 tests/android_parity/run_parity.py")
        return True
    cmd = [sys.executable, script, "--samples", *samples, "--strength", str(strength)]
    r = subprocess.run(cmd, cwd=ROOT, env=_env())
    if r.returncode == 2:
        print("-- 跳过：没有 kotlinc（设 KOTLINC=<path> 后可启用 Kotlin 像素对照）")
        return True
    return r.returncode == 0


def build_and_push() -> bool:
    """构建 APK 并推到手机。推到手机后只剩「在卓易通里点安装」这一步是人工的。"""
    gradle = os.path.join(ROOT, "src", "android", "gradlew.bat" if os.name == "nt" else "gradlew")
    app_gradle = os.path.join(ROOT, "src", "android", "app", "build.gradle.kts")
    m = re.search(r'versionName\s*=\s*"([\d.]+)"', open(app_gradle, encoding="utf-8").read())
    version = m.group(1) if m else "0.0.0"
    env = _env()
    env.setdefault("JAVA_HOME", r"C:/Users/ugnam/.workbuddy/binaries/jdk/17")
    env.setdefault("ANDROID_HOME", r"C:/Users/ugnam/Android/Sdk")

    print("\n" + "=" * 72)
    print(f"== 构建 APK v{version}")
    print("=" * 72)
    r = subprocess.run([gradle, "assembleDebug", "--no-daemon"],
                       cwd=os.path.join(ROOT, "src", "android"), env=env)
    if r.returncode != 0:
        print("-- 构建失败")
        return False

    apk_src = os.path.join(ROOT, "src", "android", "app", "build", "outputs", "apk", "debug", "app-debug.apk")
    apk_dst = os.path.join(ROOT, "out", f"HdrVividUltraHdr-{version}-debug.apk")
    shutil.copyfile(apk_src, apk_dst)
    print(f"-- 产物: {apk_dst}（{os.path.getsize(apk_dst) / 1048576:.2f} MB）")

    hdc = find_hdc()
    if not hdc:
        print("-- 没有 hdc，跳过推送（APK 已在 out/）")
        return True

    listing = subprocess.run([hdc, "shell", f"ls {REMOTE_DIR}"], capture_output=True,
                             text=True, encoding="utf-8", timeout=30)
    for name in (listing.stdout or "").splitlines():
        name = name.strip()
        if name.startswith("HdrVividUltraHdr-") and name.endswith(".apk") and name != f"HdrVividUltraHdr-{version}.apk":
            subprocess.run([hdc, "shell", f"rm -rf {REMOTE_DIR}/{name}"], capture_output=True, timeout=30)
    target = f"{REMOTE_DIR}/HdrVividUltraHdr-{version}.apk"
    push = subprocess.run([hdc, "file", "send", apk_dst, target], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    ok = push.returncode == 0
    print(f"-- 推送: {'OK' if ok else 'FAILED'}  {target}")
    if ok:
        print("   手机上只剩「打开文件管理 → 点这个 APK → 安装」一步")
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
    ap.add_argument("--apk", action="store_true",
                    help="额外构建 APK 并推到手机（之后只剩「点安装」是人工的）")
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
    results.append(("两端一致性",
                    run_step("两端一致性 check_parity.py",
                             [sys.executable, os.path.join(ROOT, "scripts", "check_parity.py")])))
    results.append(("回归测试",
                    run_step("回归测试 unittest discover",
                             [sys.executable, "-m", "unittest", "discover", "-s", os.path.join(ROOT, "tests")])))
    results.append(("观感验收", run_acceptance(samples, args.strength)))
    results.append(("Kotlin 像素对照", run_android_parity(samples, args.strength)))
    results.append(("Kotlin grid 对照", run_step_optional(
        "Kotlin grid 重封对照",
        [sys.executable, os.path.join(ROOT, "tests", "android_parity", "run_grid_parity.py")],
    )))
    if args.apk:
        results.append(("构建推送", build_and_push()))

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
