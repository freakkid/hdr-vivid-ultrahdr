#!/usr/bin/env python3
"""Android 工程静态自检（无 SDK 环境下能做的最强校验）。

不编译、不联网，只做「引用完整性」检查，用来在缺少 Android SDK 的机器上
提前发现 90% 的低级错误：文件缺失、资源引用断裂、JNI 符号不匹配、
CMake 相对路径层数错误、wrapper jar 损坏等。

用法： python scripts/check_android_project.py
退出码：0 全通过；1 存在 ERROR
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AND = ROOT / "src" / "android"
APP = AND / "app" / "src" / "main"
JAVA = APP / "java" / "com" / "finn" / "hdr"

results: list[tuple[str, bool, str]] = []


def check(cid: str, ok: bool, detail: str = "") -> None:
    results.append((cid, ok, detail))


def main() -> int:
    # 1. 必需文件
    required = {
        "settings.gradle.kts": AND / "settings.gradle.kts",
        "build.gradle.kts": AND / "build.gradle.kts",
        "app/build.gradle.kts": AND / "app" / "build.gradle.kts",
        "gradle.properties": AND / "gradle.properties",
        "gradlew": AND / "gradlew",
        "gradlew.bat": AND / "gradlew.bat",
        "wrapper.jar": AND / "gradle" / "wrapper" / "gradle-wrapper.jar",
        "wrapper.properties": AND / "gradle" / "wrapper" / "gradle-wrapper.properties",
        "AndroidManifest.xml": APP / "AndroidManifest.xml",
        "file_paths.xml": APP / "res" / "xml" / "file_paths.xml",
        "strings.xml": APP / "res" / "values" / "strings.xml",
        "CMakeLists.txt": APP / "cpp" / "CMakeLists.txt",
        "uhdr_jni.cpp": APP / "cpp" / "uhdr_jni.cpp",
        "MainActivity.kt": JAVA / "MainActivity.kt",
    }
    for name, path in required.items():
        check(f"file.{name}", path.is_file(), str(path.relative_to(ROOT)))

    # 2. Wrapper jar 有效性
    jar = AND / "gradle" / "wrapper" / "gradle-wrapper.jar"
    try:
        with zipfile.ZipFile(jar) as z:
            names = z.namelist()
        ok = any("GradleWrapperMain" in n for n in names)
        check("wrapper.jar.valid", ok, f"{len(names)} entries, GradleWrapperMain={'有' if ok else '无'}")
    except Exception as exc:  # noqa: BLE001
        check("wrapper.jar.valid", False, f"损坏: {exc}")

    # 3. Manifest 资源引用
    manifest = (APP / "AndroidManifest.xml").read_text(encoding="utf-8")
    refs = re.findall(r'"@(xml|string|mipmap|drawable)/(\w+)"', manifest)
    missing = []
    for kind, name in refs:
        if kind == "xml":
            if not (APP / "res" / "xml" / f"{name}.xml").is_file():
                missing.append(f"@xml/{name}")
        elif kind == "string":
            if f'name="{name}"' not in (APP / "res" / "values" / "strings.xml").read_text(encoding="utf-8"):
                missing.append(f"@string/{name}")
    check("manifest.resources", not missing, "缺失: " + ", ".join(missing) if missing else "全部可解析")

    # 4. Manifest 里的 Activity/Provider 类是否存在于源码
    declared = re.findall(r'android:name="([^"]+)"', manifest)
    for cls in declared:
        if cls.startswith("android.") or cls.startswith("androidx."):
            continue
        simple = cls.lstrip(".").split(".")[-1]
        check(f"manifest.class[{simple}]", (JAVA / f"{simple}.kt").is_file(), f"{simple}.kt")

    # 5. Kotlin 文件：大括号/圆括号配平 + 关键类型引用
    kt_files = sorted(JAVA.glob("*.kt"))
    for f in kt_files:
        src = f.read_text(encoding="utf-8")
        # 去掉字符串与注释后统计括号（粗粒度但足够发现漏写右括号）
        stripped = re.sub(r'"""[\s\S]*?"""', '""', src)
        # 先剥字符字面量：`'"'` 这种会让下面的字符串正则跨行吞掉整段代码
        stripped = re.sub(r"'(?:\\.|[^'\\])'", "'_'", stripped)
        stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
        stripped = re.sub(r"//[^\n]*", "", stripped)
        stripped = re.sub(r"/\*[\s\S]*?\*/", "", stripped)
        balance = {c: stripped.count(c) for c in "{}()"}
        ok = balance["{"] == balance["}"] and balance["("] == balance[")"]
        check(f"kotlin.balanced[{f.name}]", ok, str(balance))

    # 烘焙的逐像素封顶必须真的落地：v0.1.14 曾出现「加了 maxLift 字段但两个分支都没用」
    # 的静默退化，App 端因此持续过曝，而 Python 侧验收是绿的。
    hb_src = (JAVA / "HdrBaker.kt").read_text(encoding="utf-8")
    check("bake.lift_cap_applied", hb_src.count("liftCap") >= 3,
          f"HdrBaker.kt 里 liftCap 出现 {hb_src.count('liftCap')} 次（应为 1 处定义 + 2 处使用）")

    declared_types = {
        "UltraHdrNative", "UltraHdrPackager", "HdrSourceParser", "GainMapGeometry",
        "UltraHdrExporter", "MetadataValidator", "ShareGuidance", "MainActivity",
        "HeifGridRewriter",
    }
    all_src = "\n".join(f.read_text(encoding="utf-8") for f in kt_files)
    for t in sorted(declared_types):
        used = re.search(rf"\b({t}\.(?:[A-Z]\w+|[a-z]\w+)|{t}\()", all_src) is not None
        defined = (JAVA / f"{t}.kt").is_file()
        if used:
            check(f"type.defined[{t}]", defined, "被引用且已定义" if defined else "被引用但未定义")

    # 6. JNI 符号与 Kotlin external 声明匹配
    kt_native = (JAVA / "UltraHdrNative.kt").read_text(encoding="utf-8")
    externals = set(re.findall(r"external fun (\w+)", kt_native))
    cpp = (APP / "cpp" / "uhdr_jni.cpp").read_text(encoding="utf-8")
    for fn in sorted(externals):
        symbol = f"Java_com_finn_hdr_UltraHdrNative_{fn}"
        check(f"jni.symbol[{fn}]", symbol in cpp, symbol)

    # 7. CMake 相对路径层数：cpp -> 项目根 应为 6 级
    cmake = (APP / "cpp" / "CMakeLists.txt").read_text(encoding="utf-8")
    m = re.search(r'CMAKE_CURRENT_SOURCE_DIR\}/((?:\.\./)+)third_party', cmake)
    if m:
        depth = m.group(1).count("../")
        cpp_dir = APP / "cpp"
        target = (cpp_dir / ("../" * depth) / "third_party").resolve()
        expected = (ROOT / "third_party").resolve()
        check("cmake.depth", target == expected, f"{depth} 级 → {target}")
    else:
        check("cmake.depth", False, "未找到 third_party 相对路径")

    # 8. Gradle 版本约束
    agp = re.search(r'com\.android\.application"\) version "([\d.]+)"', (AND / "build.gradle.kts").read_text(encoding="utf-8"))
    dist = re.search(r"gradle-([\d.]+)-bin\.zip", (AND / "gradle" / "wrapper" / "gradle-wrapper.properties").read_text(encoding="utf-8"))
    if agp and dist:
        check("gradle.agp_version", True, f"AGP {agp.group(1)} + Gradle {dist.group(1)}（AGP 8.5 需 Gradle ≥ 8.7）")
        check("gradle.agp_compat", float(dist.group(1).split(".")[0]) >= 8 and float(dist.group(1).split(".")[1]) >= 7,
              "Gradle 8.7 满足 AGP 8.5 要求")

    # 9. compileSdk / minSdk 合理性
    app_gradle = (AND / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    cs = re.search(r"compileSdk = (\d+)", app_gradle)
    ms = re.search(r"minSdk = (\d+)", app_gradle)
    if cs and ms:
        check("android.sdk_levels", int(cs.group(1)) >= 34 and int(ms.group(1)) >= 26,
              f"compileSdk={cs.group(1)} minSdk={ms.group(1)}")

    # ---- 输出 ----
    errors = [(i, d) for i, ok, d in results if not ok]
    for cid, ok, detail in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {cid}  {detail}")
    print()
    print(f"共 {len(results)} 项，通过 {len(results) - len(errors)} 项，失败 {len(errors)} 项")
    if errors:
        print("\n需修复：")
        for cid, detail in errors:
            print(f"  - {cid}: {detail}")
        return 1
    print("静态检查全通过。下一步：cd src/android && ./gradlew assembleDebug")
    return 0


if __name__ == "__main__":
    sys.exit(main())
