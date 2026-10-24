#!/usr/bin/env python3
"""一键发版：构建签名 APK → 生成发布说明 → 建 GitHub Release。

为什么需要这个脚本
==================
两个会直接毁掉分发的坑：

1. **签名必须固定**。项目此前只发 ``app-debug.apk``，而 debug keystore 是每台机器
   各自随机生成的。本项目有两台开发机（见 HANDOVER §1.3），一旦换机器构建，签名
   就变了 —— 用户无法覆盖安装，Obtainium / F-Droid 的自动更新全部失效，且**只能
   卸载重装**。所以对外发布前必须用一份仓库外的固定 keystore 签 release 包。
2. **versionCode 必须递增**。Obtainium 靠 versionCode 判断有没有新版本，忘了递增
   就是「发了但没人收到更新」。``--bump`` 帮你做这件事。

一次性准备（只做一次，做完立刻备份）
====================================
::

    KEYDIR="$HOME/android-keys"          # 仓库外；两台机器各存一份，用私有云/U 盘同步
    mkdir -p "$KEYDIR"
    keytool -genkeypair -v \\
      -keystore "$KEYDIR/hdr-release.jks" \\
      -alias hdr -keyalg RSA -keysize 2048 -validity 10000 \\
      -storepass '你的口令' -keypass '你的口令' \\
      -dname "CN=HDR Tools, OU=Personal, O=Personal, L=CN, ST=CN, C=CN"

然后写 ``src/android/keystore.properties``（已在 .gitignore 里）::

    storeFile=/Users/you/android-keys/hdr-release.jks
    storePassword=你的口令
    keyAlias=hdr
    keyPassword=你的口令

.. warning::
   **keystore 丢了 = 永远无法更新已发布的应用**，只能换包名重新发、用户全部流失。
   至少要备份三份，其中一份离线（U 盘/纸质口令）。

用法
====
::

    python scripts/release.py --dry-run          # 自检 + 预览发布说明，不构建不发布
    python scripts/release.py                    # 构建 + 建 Release
    python scripts/release.py --bump             # 先把 patch 号 +1 再发（推荐日常用）
    python scripts/release.py --allow-unsigned   # 还没有 keystore 时发 debug 包（仅限试水）
    python scripts/release.py --notes notes.md   # 自定义说明，脚本自动追加安装尾注

退出码：0 成功 / 1 检查失败 / 2 参数或环境缺失 / 3 构建失败
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID_DIR = os.path.join(ROOT, "src", "android")
GRADLE_FILE = os.path.join(ANDROID_DIR, "app", "build.gradle.kts")
KEYSTORE_PROPS = os.path.join(ANDROID_DIR, "keystore.properties")
APK_OUT = os.path.join(ANDROID_DIR, "app", "build", "outputs", "apk")

RE_VERSION_CODE = re.compile(r"versionCode\s*=\s*(\d+)")
RE_VERSION_NAME = re.compile(r'versionName\s*=\s*"([^"]+)"')
RE_CONVENTIONAL = re.compile(r"^(\w+)(?:\([^)]*\))?!?:\s*(.+)$")

# Conventional Commits 前缀 → 中文小标题（顺序即输出顺序）
GROUPS = [
    ("feat", "新增"),
    ("fix", "修复"),
    ("perf", "性能"),
    ("refactor", "重构"),
    ("docs", "文档"),
    ("test", "测试"),
]
OTHER_TITLE = "其他"

# 首个 Release 不列开发历史：本项目此前没有 tag，把全部 commit 铺出来对用户毫无意义。
# 第一次见到这个仓库的人只需要知道「这是什么、怎么装」，所以改用一页简介。
FIRST_RELEASE_INTRO = """把 **华为 HDR Vivid 静图** 转换成 **Ultra HDR 式 JPEG**（SDR 主图 + 增益图 + XMP + MPF）。

- 普通设备当 SDR 看：不发灰、不过曝
- 支持 Ultra HDR 的设备：读增益图还原 HDR
- 手机本地处理，不联网、不上传任何图片"""


# ---------------------------------------------------------------- 基础工具


def _fix_stdio() -> None:
    """Windows 控制台默认 cp936，中文输出会炸。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def run(cmd, cwd=None, check=True, capture=False):
    p = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if check and p.returncode != 0:
        if capture and p.stdout:
            print(p.stdout, file=sys.stderr)
        raise SystemExit(f"命令失败（{p.returncode}）: {' '.join(cmd)}")
    return p


def git(*args, check=True) -> str:
    return run(["git", *args], cwd=ROOT, check=check, capture=True).stdout.strip()


def fail(msg: str, hint: str = "") -> None:
    print(f"[x] {msg}", file=sys.stderr)
    if hint:
        print(f"    {hint}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------- 版本


def read_version() -> tuple[int, str]:
    with open(GRADLE_FILE, encoding="utf-8") as f:
        text = f.read()
    m_code = RE_VERSION_CODE.search(text)
    m_name = RE_VERSION_NAME.search(text)
    if not m_code or not m_name:
        fail(f"无法从 {GRADLE_FILE} 读到 versionCode/versionName")
    return int(m_code.group(1)), m_name.group(1)


def bump_version() -> tuple[int, str]:
    """patch +1、versionCode +1，并提交。不 push（交给 scripts/ship.sh）。"""
    code, name = read_version()
    parts = name.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        fail(f'versionName "{name}" 不是 x.y.z 形式，无法自动递增')
    new_name = ".".join(parts)

    with open(GRADLE_FILE, encoding="utf-8") as f:
        text = f.read()
    text = RE_VERSION_CODE.sub(f"versionCode = {code + 1}", text, count=1)
    text = RE_VERSION_NAME.sub(f'versionName = "{new_name}"', text, count=1)
    with open(GRADLE_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    git("add", GRADLE_FILE)
    git("commit", "-m", f"chore(release): v{new_name}")
    print(f"[+] 版本 {name}({code}) → {new_name}({code + 1})，已提交")
    return code + 1, new_name


# ---------------------------------------------------------------- 发布说明


def repo_web_url() -> str:
    url = git("remote", "get-url", "origin")
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    url = re.sub(r"\.git$", "", url)
    return url


def last_tag() -> str | None:
    tags = [t for t in git("tag", "--list", "v*", "--sort=-v:refname").splitlines() if t.strip()]
    return tags[0] if tags else None


def collect_commits(since: str | None) -> list[str]:
    rng = f"{since}..HEAD" if since else "HEAD"
    out = git("log", "--no-merges", "--pretty=format:%s", rng, check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def render_notes(
    version: str, code: int, commits: list[str], extra: str, first_release: bool = False
) -> str:
    header = f"## v{version}" + ("（首个公开版本）" if first_release else " 更新")
    lines = [header, ""]

    if first_release:
        lines += [FIRST_RELEASE_INTRO, ""]
        if extra.strip():
            lines += [extra.strip(), ""]
        return "\n".join(lines)

    buckets: dict[str, list[str]] = {title: [] for _, title in GROUPS}
    buckets[OTHER_TITLE] = []
    mapping = {prefix: title for prefix, title in GROUPS}

    if extra.strip():
        lines += [extra.strip(), ""]

    for subject in commits:
        if subject.startswith("chore(release)"):
            continue
        m = RE_CONVENTIONAL.match(subject)
        if m:
            prefix, body = m.group(1).lower(), m.group(2).strip()
            if prefix == "chore":
                continue  # 杂项不进用户可见的 changelog
            buckets[mapping.get(prefix, OTHER_TITLE)].append(body)
        else:
            buckets[OTHER_TITLE].append(subject)

    has_items = False
    for title in [t for _, t in GROUPS] + [OTHER_TITLE]:
        items = buckets[title]
        if not items:
            continue
        has_items = True
        lines.append(f"### {title}")
        lines += [f"- {item}" for item in items]
        lines.append("")

    if not has_items:
        lines += ["本次为稳定性发布，无面向用户的变更。", ""]

    return "\n".join(lines)


def install_footer(version: str, code: int, sha256: str | None) -> str:
    lines = [
        "## 安装",
        "",
        "**推荐：Obtainium（自动更新）**",
        "",
        f"1. 装 [Obtainium](https://github.com/ImranR98/Obtainium/releases)",
        f"2. 添加应用 → 填入本仓库地址 `{repo_web_url()}`",
        "3. 之后每次发版会自动提示更新，不用手动找回这里",
        "",
        "**手动安装**：下载下方 APK，允许对应的「安装未知应用」权限。",
        "",
        "## 版本",
        "",
        f"- versionCode: `{code}`",
        f"- versionName: `{version}`",
    ]
    if sha256:
        lines.append(f"- SHA-256: `{sha256}`")
    lines += [
        "",
        "> 应用完全本地处理，不联网、不上传任何图片。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 构建与校验


def build(allow_unsigned: bool) -> str:
    task = "assembleDebug" if allow_unsigned else "assembleRelease"
    gradlew = ["cmd", "/c", "gradlew.bat"] if os.name == "nt" else ["./gradlew"]
    print(f"[.] 构建 {task} …")
    p = run([*gradlew, task], cwd=ANDROID_DIR, check=False)
    if p.returncode != 0:
        raise SystemExit(3)
    return task


def find_apk(allow_unsigned: bool) -> str:
    sub = "debug" if allow_unsigned else "release"
    name = "app-debug.apk" if allow_unsigned else "app-release.apk"
    path = os.path.join(APK_OUT, sub, name)
    if not os.path.exists(path):
        alt = os.path.join(APK_OUT, sub, "app-release-unsigned.apk")
        if os.path.exists(alt):
            fail(
                "产物是 unsigned 的，说明 keystore.properties 没生效或不存在",
                "先按脚本顶部说明生成 keystore；只想试水就加 --allow-unsigned",
            )
        fail(f"找不到 APK: {path}", "确认构建是否真的产出了 apk 目录")
    return path


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def print_cert(path: str) -> None:
    """打印签名证书指纹，用来确认是不是那把固定的 key。"""
    if not shutil.which("keytool"):
        print("[!] 没找到 keytool，跳过签名指纹检查")
        return
    p = run(["keytool", "-printcert", "-jarfile", path], check=False, capture=True)
    if p.returncode != 0:
        print("[!] 读不出证书指纹（可能只有 v2 签名），跳过")
        return
    for line in (p.stdout or "").splitlines():
        if "SHA256" in line or "所有者" in line or "Owner" in line:
            print(f"    {line.strip()}")


# ---------------------------------------------------------------- 发布


def check_preconditions(args) -> None:
    if not os.path.exists(GRADLE_FILE):
        fail(f"找不到 {GRADLE_FILE}")

    if not args.allow_dirty:
        # 只看已跟踪文件：未跟踪的临时产物不该拦住发版
        dirty = git("status", "--porcelain", "--untracked-files=no")
        if dirty:
            fail(
                "有未提交的已跟踪文件改动，先提交（scripts/ship.sh）再发版",
                "确实要带脏数据发版就加 --allow-dirty",
            )
        untracked = git("ls-files", "--others", "--exclude-standard", check=False)
        if untracked.strip():
            print("[!] 有未跟踪文件，不会进仓库，仅提示：")
            for line in untracked.splitlines()[:5]:
                print(f"    {line}")

    if not args.allow_unsigned and not os.path.exists(KEYSTORE_PROPS):
        fail(
            f"缺少 {KEYSTORE_PROPS}，release 包会是无签名的",
            "按 scripts/release.py 顶部说明生成 keystore（一次性，需备份）；"
            "只想先发一版试水就加 --allow-unsigned",
        )


def check_tag_available(tag: str) -> None:
    if git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False):
        fail(f"本地已存在 tag {tag}", "加 --bump 自动递增版本号，或手动改 build.gradle.kts")
    if git("ls-remote", "--tags", "origin", tag, check=False):
        fail(f"远端已存在 tag {tag}", "加 --bump 自动递增版本号")


def check_pushed() -> None:
    """Release 的 tag 会指向 HEAD；HEAD 不在远端时 gh 建不出 release。"""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    p = run(
        ["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}"],
        cwd=ROOT,
        check=False,
        capture=True,
    )
    if p.returncode != 0:
        fail(
            f"HEAD 还没推送到 origin/{branch}",
            '先 python scripts/ship.sh "说明" 或 git push，再回来发版',
        )


def check_gh() -> None:
    if not shutil.which("gh"):
        fail(
            "没装 GitHub CLI（gh）",
            "macOS: brew install gh ｜ Windows: winget install --id GitHub.cli",
        )
    p = run(["gh", "auth", "status"], check=False, capture=True)
    if p.returncode != 0:
        fail("gh 未登录", "跑一次 gh auth login")


def create_release(tag: str, apk: str, notes_file: str) -> None:
    target = f"hdr-vivid-ultrahdr-{tag.lstrip('v')}.apk"
    tmpdir = tempfile.mkdtemp(prefix="hdr-release-")
    staged = os.path.join(tmpdir, target)
    shutil.copy2(apk, staged)

    print(f"[.] 上传 {target} …")
    p = run(
        [
            "gh", "release", "create", tag, staged,
            "--title", tag,
            "--notes-file", notes_file,
            "--latest",
            "--target", git("rev-parse", "HEAD"),
        ],
        check=False,
    )
    if p.returncode != 0:
        fail("gh release create 失败", "看上面的报错；常见是 tag 已存在或网络问题")

    print(f"[+] 已发布 {tag}")
    print(f"    {repo_web_url()}/releases/tag/{tag}")


def print_next_steps(version: str) -> None:
    url = f"{repo_web_url()}/releases"
    print(
        "\n后续（推广动作，零成本）:\n"
        f"  · 酷安发帖标题： [开源] HDR Vivid → Ultra HDR 转换，解决发图发灰\n"
        f"  · 帖内链接：{url}\n"
        "  · 配图：发灰原图 / 转换后正常 的对比（3 秒能看懂的那种）\n"
        "  · 知乎搜「HDR Vivid 照片发灰」相关问题，答完放这条链接\n"
    )


# ---------------------------------------------------------------- 入口


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="构建签名 APK 并发布 GitHub Release",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true", help="只自检并预览发布说明，不构建不发布")
    ap.add_argument("--build", action="store_true", help="dry-run 时也执行构建")
    ap.add_argument("--bump", action="store_true", help="先把 versionName 的 patch 号与 versionCode +1")
    ap.add_argument("--allow-unsigned", action="store_true", help="发 debug 包（还没有 keystore 时用）")
    ap.add_argument("--allow-dirty", action="store_true", help="允许工作区有未提交改动")
    ap.add_argument("--notes", metavar="FILE", help="自定义发布说明（Markdown），自动追加安装尾注")
    return ap.parse_args()


def main() -> int:
    _fix_stdio()
    args = parse_args()

    check_preconditions(args)

    if args.bump:
        bump_version()

    code, version = read_version()
    tag = f"v{version}"
    print(f"[.] 待发布 {tag} (versionCode {code})")

    # 所有检查都在构建前做完 —— 构建要几分钟，不能让它在最后一步才失败
    if not args.dry_run:
        check_pushed()
        check_tag_available(tag)
        check_gh()

    apk = None
    if args.dry_run and not args.build:
        print("[.] dry-run：跳过构建")
    else:
        build(args.allow_unsigned)
        apk = find_apk(args.allow_unsigned)
        size_mb = os.path.getsize(apk) / 1024 / 1024
        print(f"[+] 产物 {os.path.basename(apk)}  {size_mb:.2f} MB")
        print_cert(apk)

        if not args.allow_unsigned and size_mb < 0.5:
            fail(f"APK 只有 {size_mb:.2f} MB，明显不对")

    extra = ""
    if args.notes:
        with open(args.notes, encoding="utf-8") as f:
            extra = f.read()

    since = last_tag()
    first_release = since is None
    if first_release:
        print("[.] 未发现历史 tag，按「首个公开版本」生成说明")
    commits = [] if first_release else collect_commits(since)
    body = render_notes(version, code, commits, extra, first_release)

    digest = sha256_of(apk) if apk else None
    body += "\n" + install_footer(version, code, digest)

    notes_path = os.path.join(tempfile.gettempdir(), f"hdr-release-{version}.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(body)

    if args.dry_run:
        print(f"\n----- 发布说明预览（{notes_path}）-----\n")
        print(body)
        print("\n[dry-run 结束，未构建未发布]")
        return 0

    create_release(tag, apk, notes_path)
    print_next_steps(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
