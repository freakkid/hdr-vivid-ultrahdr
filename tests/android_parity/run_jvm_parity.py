#!/usr/bin/env python3
"""Kotlin 四模块（UltraHdrPackager / MetadataValidator / HeifInspector /
HdrSourceParser）在本机 JVM 上与 Python 实现的真实执行对照。

docs/07-testing-strategy.md §1 指出：这四个 Kotlin 模块此前只做过括号配平、
从未被真实执行过——而封装器与自检器恰恰决定用户手里那个文件能不能被系统
认成 HDR。本 runner 用 kotlinc 把**真实的** Kotlin 源（只加一个
android.util.Base64 桩）编成 jar，在 JVM 上真实执行，并与 Python 侧
（synthetic / vivid_source / validator / xmp / mpf / heif_grid）互检：

  模块 1  UltraHdrPackager + MetadataValidator 闭环：
          合成样本拆出 primary/gainmap → Kotlin pack → Kotlin MetadataValidator
          自检 → Python validator 互检 Kotlin 产物 → 结构（XMP/MPF/偏移）断言
          → Kotlin/Python 双端回解析比对元数据 → 纯 SDR 必须被拒。
  模块 2  HeifInspector：合成 ISOBMFF（复用 test_heif_grid 的生成方式），
          断言 box 树 / iinf 类型 / iloc 长度表 / iref 关系与 Python heif_grid 一致。
  模块 3  HdrSourceParser：合成 CUVA 样本双端解析；out/visual/samples/ 下有
          真实样本时再各解析一遍，比对 GainMapMax/Min、offset、容器判定
          （容差 1e-3）；样本不存在则该部分 SKIP（不计 PASS）。

用法： python tests/android_parity/run_jvm_parity.py [--rebuild]
退出码：0 通过 / 2 缺 kotlinc 或 JDK（SKIP）/ 3 编译或断言失败（FAIL）。
执行用例数为 0 也视为 FAIL。
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src", "python"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from uhdr import jpegseg, mpf as uhdr_mpf, validator as uhdr_validator  # noqa: E402
from uhdr import vivid_source, xmp as uhdr_xmp                         # noqa: E402
from uhdr.heif_grid import HeifGrids                                   # noqa: E402
from uhdr.synthetic import make_cuva_sample                            # noqa: E402
import test_heif_grid                                                  # noqa: E402

HDR_DIR = os.path.join(ROOT, "src", "android", "app", "src", "main", "java", "com", "finn", "hdr")
JVM_DIR = os.path.join(ROOT, "tests", "android_parity", "jvm")
JAR = os.path.join(ROOT, "out", "kotlin_parity", "jvm_parity.jar")
SAMPLES_DIR = os.path.join(ROOT, "out", "visual", "samples")
MAX_SAMPLES = 3

# 真实生产 Kotlin 源 + 唯一的 import 级桩（android.util.Base64）+ JVM 入口
SRC = [
    os.path.join(HDR_DIR, "UltraHdrPackager.kt"),
    os.path.join(HDR_DIR, "MetadataValidator.kt"),
    os.path.join(HDR_DIR, "HeifInspector.kt"),
    os.path.join(HDR_DIR, "HdrSourceParser.kt"),
    os.path.join(HDR_DIR, "IsoGainMap.kt"),
    os.path.join(HDR_DIR, "UltraHdrNative.kt"),   # 真实源；probe 在 JVM 上抛
                                                  # ExceptionInInitializerError，
                                                  # 由 HdrSourceParser 的
                                                  # catch(Throwable) 吞掉
    os.path.join(JVM_DIR, "android", "util", "Base64.kt"),
    os.path.join(JVM_DIR, "JvmParityMain.kt"),
]

# 封装元数据（hdrgm log2 域），Kotlin/Python 两端共用同一组值
PACK_META = {
    "gmMin": 0.0,
    "gmMax": 1.0,
    "gamma": 1.0,
    "offSdr": 1.0 / 64.0,
    "offHdr": 1.0 / 64.0,
    "capMin": 0.0,
    "capMax": 1.0,
}
META_KEYS = ["gmMin", "gmMax", "gamma", "offSdr", "offHdr", "capMin", "capMax"]
# Python xmp.GainMapMeta 字段名 ↔ PACK_META 键
PY_META_FIELDS = {
    "gain_map_min": "gmMin",
    "gain_map_max": "gmMax",
    "gamma": "gamma",
    "offset_sdr": "offSdr",
    "offset_hdr": "offHdr",
    "hdr_capacity_min": "capMin",
    "hdr_capacity_max": "capMax",
}
# 增益图 XMP 里的 hdrgm 属性名 ↔ PACK_META 键
XMP_HDRA_FIELDS = {
    "GainMapMin": "gmMin",
    "GainMapMax": "gmMax",
    "Gamma": "gamma",
    "OffsetSDR": "offSdr",
    "OffsetHDR": "offHdr",
    "HDRCapacityMin": "capMin",
    "HDRCapacityMax": "capMax",
}
# Kotlin META 输出字段 ↔ PACK_META 键
KT_META_FIELDS = {
    "min": "gmMin",
    "max": "gmMax",
    "gamma": "gamma",
    "offSdr": "offSdr",
    "offHdr": "offHdr",
    "capMin": "capMin",
    "capMax": "capMax",
}

# MetadataValidator 在合法 Ultra HDR 文件上必须全绿的检查项
EXPECTED_CHECKS = [
    "two_images", "xmp_present", "xmp_primary_item", "xmp_gainmap_item",
    "primary_version", "mpf_present", "mpf_points_to_soi",
    "gainmap_xmp_present", "aspect_consistent", "gm_not_larger",
]

CONTAINER_PY2KT = {
    "cuva": "CUVA",
    "iso_ultrahdr": "ISO_ULTRA_HDR",
    "plain_jpeg": "PLAIN_JPEG",
    "unknown_mpo": "UNKNOWN_MPO",
}


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


def find_java() -> str:
    for c in [
        os.path.join(os.environ.get("JAVA_HOME", ""), "bin", "java.exe"),
        os.path.expanduser("~/.workbuddy/binaries/jdk/17/bin/java.exe"),
        "java",
    ]:
        if not c:
            continue
        if os.sep in c or "/" in c:
            if os.path.isfile(c):
                return c
        elif shutil.which(c):
            return c
    return "java"


def build(kotlinc: str, rebuild: bool) -> bool:
    if os.path.isfile(JAR) and not rebuild and \
            os.path.getmtime(JAR) > max(os.path.getmtime(p) for p in SRC):
        return True
    os.makedirs(os.path.dirname(JAR), exist_ok=True)
    env = dict(os.environ)
    env.setdefault("JAVA_HOME", os.path.expanduser("~/.workbuddy/binaries/jdk/17"))
    env["PATH"] = os.path.join(env["JAVA_HOME"], "bin") + os.pathsep + env.get("PATH", "")
    print(f"[build] kotlinc → {JAR}")
    r = subprocess.run([kotlinc, *SRC, "-include-runtime", "-d", JAR],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        print("[build] 失败：\n" + (r.stderr or r.stdout)[-2000:])
        return False
    return True


def run_kt(java: str, mode: str, *args: str) -> list[str]:
    """调用 JVM 入口；非零退出抛 RuntimeError（调用方计为该用例 FAIL）。"""
    r = subprocess.run([java, "-Dfile.encoding=UTF-8", "-jar", JAR, mode, *args],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"JVM {mode} 退出码 {r.returncode}: {(r.stderr or r.stdout)[-400:]}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def parse_kv(line: str) -> dict[str, str]:
    """'LAYOUT\\ta=1\\tb=2' → {'a':'1','b':'2'}"""
    out: dict[str, str] = {}
    for kv in line.split("\t")[1:]:
        k, _, v = kv.partition("=")
        out[k] = v
    return out


def parse_checks(lines: list[str]) -> tuple[dict[str, tuple[int, str]], int]:
    checks: dict[str, tuple[int, str]] = {}
    report = -1
    for ln in lines:
        parts = ln.split("\t")
        if parts[0] == "CHECK" and len(parts) >= 4:
            checks[parts[1]] = (int(parts[2]), parts[3])
        elif parts[0] == "REPORT":
            report = int(parse_kv(ln).get("ok", "0"))
    return checks, report


def parse_meta(line: str) -> dict[str, float] | None:
    rest = line.split("\t")[1:]
    if rest and rest[0] == "none":
        return None
    out: dict[str, float] = {}
    for kv in rest:
        k, _, v = kv.partition("=")
        out[k] = float(v)
    return out


def parse_report(lines: list[str]) -> dict:
    """解析 parse 模式的行式输出：CONTAINER/MAKE/GAINMAP/NEEDSBOOST/ISO 为裸值，
    META 为 k=v 列表或 none，NOTE 为自由文本。"""
    d: dict = {"META": None, "NOTES": []}
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        if parts[0] == "META":
            if parts[1] != "none":
                d["META"] = parse_meta(ln)
        elif parts[0] == "NOTE":
            d["NOTES"].append("\t".join(parts[1:]))
        elif len(parts) == 2:
            d[parts[0]] = parts[1]
    return d


def find_seg(data: bytes, start: int, marker: int, sig: bytes):
    for seg in jpegseg.iter_header_segments(data, start):
        if seg.marker == marker and data[seg.payload_start:seg.payload_start + len(sig)] == sig:
            return seg
    return None


class Case:
    """单模块用例收集器：断言计数 + 失败清单。"""

    def __init__(self, name: str):
        self.name = name
        self.cases = 0
        self.asserts = 0
        self.fails: list[str] = []

    def check(self, name: str, ok: bool, extra: str = "") -> bool:
        self.asserts += 1
        if not ok:
            self.fails.append(name + (f"  [{extra}]" if extra else ""))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
              (f"  [{extra}]" if extra and not ok else ""))
        return ok

    def close(self) -> bool:
        return not self.fails


# ---------------------------------------------------------------------------
# 模块 1：UltraHdrPackager + MetadataValidator 闭环
# ---------------------------------------------------------------------------

def case_pack_validate(java: str, tmp: str) -> Case:
    c = Case("UltraHdrPackager+MetadataValidator")

    # 1) Python 合成样本拆出 primary / gainmap（不依赖真实照片）
    cuva_path = os.path.join(tmp, "cuva.jpg")
    with open(cuva_path, "wb") as f:
        f.write(make_cuva_sample(base_long_edge=512))
    src = vivid_source.parse_file(cuva_path)
    c.cases += 1
    c.check("synthetic: CUVA 样本可被 Python 解析且含增益图",
            src.container == "cuva" and src.has_gainmap, src.container)
    primary_p = os.path.join(tmp, "primary.jpg")
    gain_p = os.path.join(tmp, "gain.jpg")
    with open(primary_p, "wb") as f:
        f.write(src.primary_jpeg)
    with open(gain_p, "wb") as f:
        f.write(src.gainmap_jpeg)

    # 2) Kotlin UltraHdrPackager.pack 真实执行
    out_p = os.path.join(tmp, "packed.jpg")
    meta_args = [repr(PACK_META[k]) for k in META_KEYS]
    try:
        lines = run_kt(java, "pack", primary_p, gain_p, out_p, *meta_args)
    except RuntimeError as e:
        c.check("Kotlin UltraHdrPackager.pack 执行", False, str(e))
        return c
    c.cases += 1
    layout = {}
    for ln in lines:
        if ln.startswith("LAYOUT\t"):
            layout = {k: int(v) for k, v in parse_kv(ln).items()}
    with open(out_p, "rb") as f:
        packed = f.read()
    ok = bool(layout) and len(packed) == layout.get("totalLen", -1)
    c.check("Kotlin pack 写出文件且 totalLen 自洽", ok,
            f"layout={layout} 实际={len(packed)}")

    # 3) Python 侧独立验证字节结构（不信任 Kotlin 自述）
    SOI = b"\xff\xd8"
    c.check("primaryLen 指向增益图 SOI",
            packed[layout["primaryLen"]:layout["primaryLen"] + 2] == SOI)
    c.check("primaryLen + gainMapLen == totalLen",
            layout["primaryLen"] + layout["gainMapLen"] == layout["totalLen"])

    seg2 = find_seg(packed, 0, 0xFFE2, uhdr_mpf.MPF_SIG)
    c.check("存在 MPF APP2 段", seg2 is not None)
    if seg2 is not None:
        mp = uhdr_mpf.parse_mpf(packed[seg2.payload_start:seg2.payload_end],
                                tiff_base_abs=seg2.payload_start + 4)
        e = mp.entries[1]
        abs_off = mp.tiff_base_abs + e.offset
        c.check("MPF entry1 offset 与 Kotlin layout 一致",
                e.offset == layout["gainMapOffset"], f"{e.offset} vs {layout['gainMapOffset']}")
        c.check("MPF entry1 size 与 Kotlin layout 一致",
                e.size == layout["gainMapLen"], f"{e.size} vs {layout['gainMapLen']}")
        c.check("MPF 偏移绝对位置指向 SOI", packed[abs_off:abs_off + 2] == SOI,
                f"abs={abs_off}")

    seg1 = find_seg(packed, 0, 0xFFE1, uhdr_xmp.XMP_APP1_SIG)
    c.check("存在 XMP APP1 段", seg1 is not None)
    if seg1 is not None:
        xml = packed[seg1.payload_start:seg1.payload_end][len(uhdr_xmp.XMP_APP1_SIG):].decode("utf-8", "replace")
        directory, _ = uhdr_xmp.parse_xmp(xml)
        pri = directory.by_semantic(uhdr_xmp.SEMANTIC_PRIMARY)
        gm = directory.by_semantic(uhdr_xmp.SEMANTIC_GAINMAP)
        c.check("XMP Primary 项 length == primaryLen",
                pri is not None and pri.length == layout["primaryLen"])
        c.check("XMP GainMap 项 offset/length 与 layout 一致",
                gm is not None and gm.offset == layout["gainMapOffset"]
                and gm.length == layout["gainMapLen"])

    # 4) Kotlin MetadataValidator 自检（真实执行，逐项断言）
    checks, report_ok = parse_checks(lines)
    c.cases += 1
    c.check("Kotlin MetadataValidator REPORT 全绿", report_ok == 1, f"report={report_ok}")
    for cid in EXPECTED_CHECKS:
        got = checks.get(cid)
        c.check(f"MetadataValidator[{cid}]", got is not None and got[0] == 1,
                "" if got is None else got[1])

    # 5) Python validator 互检 Kotlin 产物（跨实现）
    c.cases += 1
    rep = uhdr_validator.validate_bytes(packed, "kt_packed.jpg")
    c.check("Python uhdr.validator 校验 Kotlin 产物全绿", rep.ok,
            "; ".join(x.id for x in rep.checks if not x.ok and x.level == "ERROR"))

    # 6) 增益图自身 XMP 的 hdrgm 参数与传入 meta 一致（%.6f 舍入内）
    gm_xml = None
    for seg in jpegseg.iter_header_segments(packed, layout["primaryLen"]):
        if seg.marker == 0xFFE1 and \
                packed[seg.payload_start:seg.payload_start + len(uhdr_xmp.XMP_APP1_SIG)] == uhdr_xmp.XMP_APP1_SIG:
            gm_xml = packed[seg.payload_start + len(uhdr_xmp.XMP_APP1_SIG):seg.payload_end].decode("utf-8", "replace")
            break
    c.check("增益图携带 XMP APP1", gm_xml is not None)
    if gm_xml:
        _, gm_raw = uhdr_xmp.parse_xmp(gm_xml)
        for attr, mk in XMP_HDRA_FIELDS.items():
            want = PACK_META[mk]
            got = float(gm_raw.get(attr, "nan"))
            c.check(f"增益图 XMP hdrgm:{attr} == {want:.6f}", abs(got - want) <= 1e-6, f"got={got}")

    # 7) 双端回解析 packed：Kotlin HdrSourceParser vs Python vivid_source
    kt = parse_report(run_kt(java, "parse", out_p))
    c.check("Kotlin 回解析判定 ISO_ULTRA_HDR", kt.get("CONTAINER") == "ISO_ULTRA_HDR",
            str(kt.get("CONTAINER")))
    kt_meta = kt.get("META")
    c.check("Kotlin 回解析拿到增益图元数据", kt_meta is not None)
    if kt_meta:
        for kk, mk in KT_META_FIELDS.items():
            want = PACK_META[mk]
            c.check(f"Kotlin 回解析 {kk} == {want:.6f}",
                    abs(kt_meta[kk] - want) <= 1e-3, f"got={kt_meta[kk]}")

    py2 = vivid_source.parse_file(out_p)
    c.check("Python 回解析判定 iso_ultrahdr", py2.container == "iso_ultrahdr", py2.container)
    c.check("Python 回解析拿到元数据", py2.meta is not None)
    if py2.meta is not None:
        for pf, mk in PY_META_FIELDS.items():
            want = PACK_META[mk]
            got = getattr(py2.meta, pf)
            c.check(f"Python 回解析 {pf} == {want:.6f}",
                    abs(got - want) <= 1e-3, f"got={got}")

    # 8) 反向：纯 SDR 必须被 MetadataValidator 拒绝（不能「看起来像 HDR」）
    c.cases += 1
    neg_lines = run_kt(java, "validate", primary_p)
    neg_checks, neg_ok = parse_checks(neg_lines)
    c.check("纯 SDR 被 MetadataValidator 拒绝（REPORT ok=0）", neg_ok == 0)
    c.check("纯 SDR 缺第二张图（two_images FAIL）",
            neg_checks.get("two_images", (1, ""))[0] == 0)
    return c


# ---------------------------------------------------------------------------
# 模块 2：HeifInspector
# ---------------------------------------------------------------------------

def case_heif(java: str, tmp: str) -> Case:
    c = Case("HeifInspector")
    source = test_heif_grid.build_source()
    expected = {iid: (typ, len(payload)) for iid, typ, _n, _ct, _h, payload in test_heif_grid._source_items()}
    path = os.path.join(tmp, "in.heic")
    with open(path, "wb") as f:
        f.write(source)

    c.cases += 1
    lines = run_kt(java, "heif", path)
    top, brand, compat, props = "", "", [], []
    items: dict[int, tuple[str, int]] = {}
    refs: list[str] = []
    flags: dict[str, str] = {}
    primary = secondary = None
    for ln in lines:
        parts = ln.split("\t")
        if parts[0] == "TOP":
            top = parts[1]
        elif parts[0] == "BRAND":
            brand = parts[1]
        elif parts[0] == "COMPAT":
            compat = parts[1].split(",")
        elif parts[0] == "ITEM" and len(parts) >= 5:
            items[int(parts[1])] = (parts[2], int(parts[4]))
        elif parts[0] == "REF":
            refs.append(parts[1])
        elif parts[0] == "PRIMARY":
            primary = (int(parts[1]), int(parts[2]))
        elif parts[0] == "SECONDARY":
            secondary = (int(parts[1]), int(parts[2]))
        elif parts[0] in ("TMAPITEM", "DIMGREF"):
            flags[parts[0]] = parts[1]

    c.check("顶层 box 树 == ftyp,meta,mdat", top == "ftyp,meta,mdat", top)
    c.check("brand == heic", brand == "heic", brand)
    c.check("compat 含 mif1/heic", "mif1" in compat and "heic" in compat, str(compat))
    c.check("item 数与 iinf 一致", len(items) == len(expected),
            f"{len(items)} vs {len(expected)}")
    bad_type = {i: (items.get(i), expected[i]) for i in expected if items.get(i, ("?", -2))[0] != expected[i][0]}
    c.check("每个 item 的 infe 类型与 Python 一致", not bad_type, str(bad_type))
    bad_size = {i: (items.get(i, ("?", -2))[1], expected[i][1])
                for i in expected if items.get(i, ("?", -2))[1] != expected[i][1]}
    c.check("iloc 长度表与 Python 载荷字节数一致", not bad_size, str(bad_size))
    # iref 逐条严格比对（2026-10-25 起生效）：HeifInspector 的 iref 起点多跳 4 字节
    # bug 已修复——真机华为/Apple HEIC 字节级实证（cdsc 载荷恰 6B=from(2)+count(2)+to(2)）
    # reference box 不带 version/flags，from_item_ID 紧跟 8 字节 box 头；
    # ID 宽度由 iref 自身 FullBox version 决定（与 Python heif_grid._parse_iref 同口径）。
    want_refs = [
        "dimg #42 -> #43,#44,#45,#46",
        "dimg #50 -> " + ",".join(f"#{i}" for i in range(51, 63)),
        "cdsc #47 -> #50",
    ]
    c.check("iref 逐条一致（from/count/to 全对，无 +4 错位）", refs == want_refs,
            f"kt={refs}")
    print(f"  INFO  Kotlin iref={refs}")
    c.check("tmap 派生项被识别（item #49）", flags.get("TMAPITEM") == "1")
    c.check("存在 dimg 引用", flags.get("DIMGREF") == "1")
    c.check("主图（最大 item）== #47", primary == (47, 600), str(primary))
    c.check("次大 item == #48（it35）", secondary == (48, 237), str(secondary))

    # Python heif_grid 对同一文件的独立解析（交叉印证期望值不是自证）
    heif = HeifGrids(source)
    py_ok = all(heif.items[i].item_type == expected[i][0] for i in expected if i in heif.items)
    c.check("Python heif_grid 印证类型表", py_ok)
    return c


# ---------------------------------------------------------------------------
# 模块 3：HdrSourceParser
# ---------------------------------------------------------------------------

def _compare_parse(c: Case, path: str, py: vivid_source.HdrSource, kt: dict) -> None:
    want_container = CONTAINER_PY2KT.get(py.container)
    c.check(f"容器判定一致（{os.path.basename(path)}）",
            kt.get("CONTAINER") == want_container,
            f"kt={kt.get('CONTAINER')} py={py.container}")
    c.check(f"exif_make 一致（{os.path.basename(path)}）",
            (kt.get("MAKE") or "") == (py.exif_make or ""),
            f"kt={kt.get('MAKE')} py={py.exif_make}")
    has_gm = kt.get("GAINMAP", "-2") not in ("-1", "-2")
    c.check(f"增益图有无一致（{os.path.basename(path)}）", has_gm == py.has_gainmap,
            f"kt={kt.get('GAINMAP')} py={py.has_gainmap}")
    kt_meta = kt.get("META")
    if py.meta is None:
        c.check(f"元数据均缺席（{os.path.basename(path)}）", kt_meta is None,
                f"kt={kt_meta}")
        return
    c.check(f"元数据均存在（{os.path.basename(path)}）", kt_meta is not None)
    if kt_meta is None:
        return
    for kk, pf in (("min", "gain_map_min"), ("max", "gain_map_max"), ("gamma", "gamma"),
                   ("offSdr", "offset_sdr"), ("offHdr", "offset_hdr"),
                   ("capMin", "hdr_capacity_min"), ("capMax", "hdr_capacity_max")):
        a, b = kt_meta[kk], getattr(py.meta, pf)
        c.check(f"meta[{kk}] 双端一致 ≤1e-3（{os.path.basename(path)}）",
                abs(a - b) <= 1e-3, f"kt={a} py={b}")


def case_parser(java: str, tmp: str) -> tuple[Case, int, int]:
    """返回 (case, n_samples, n_skipped_samples)。合成 CUVA 用例无条件执行。"""
    c = Case("HdrSourceParser")

    # 1) 合成 CUVA 样本：双端解析必须同判 CUVA / HUAWEI / needsBoost
    cuva_path = os.path.join(tmp, "cuva.jpg")
    if not os.path.isfile(cuva_path):
        with open(cuva_path, "wb") as f:
            f.write(make_cuva_sample(base_long_edge=512))
    c.cases += 1
    kt = parse_report(run_kt(java, "parse", cuva_path))
    py = vivid_source.parse_file(cuva_path)
    c.check("Kotlin 判定 CUVA", kt.get("CONTAINER") == "CUVA", str(kt.get("CONTAINER")))
    c.check("Python 同判 cuva", py.container == "cuva", py.container)
    c.check("EXIF Make=HUAWEI", kt.get("MAKE") == "HUAWEI" and py.exif_make == "HUAWEI",
            f"kt={kt.get('MAKE')} py={py.exif_make}")
    c.check("CUVA 需重算增益（needsBoostRecompute）", kt.get("NEEDSBOOST") == "1")
    c.check("CUVA 不产生 hdrgm 元数据", kt.get("META") is None and py.meta is None)

    # 2) 真实样本（gitignore，可能不存在；不存在则该部分 SKIP 不计 PASS）
    n_samples = n_skip = 0
    samples = sorted(glob.glob(os.path.join(SAMPLES_DIR, "*.jpg")))[:MAX_SAMPLES]
    if not samples:
        n_skip = 1
        print("  SKIP  out/visual/samples 无真实样本（先跑 verify_bake.py 拉样例）")
        return c, n_samples, n_skip
    for path in samples:
        c.cases += 1
        n_samples += 1
        try:
            kt_lines = run_kt(java, "parse", path)
        except RuntimeError as e:
            c.check(f"Kotlin 解析真实样本 {os.path.basename(path)}", False, str(e))
            continue
        ktd = parse_report(kt_lines)
        pyp = vivid_source.parse_file(path)
        _compare_parse(c, path, pyp, ktd)
    return c, n_samples, n_skip


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Kotlin 四模块 JVM 真实执行对照")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    kotlinc = find_kotlinc()
    if not kotlinc:
        print("没有 kotlinc：设 KOTLINC=<path>（SKIP）")
        return 2
    java = find_java()
    if not java:
        print("没有 java/JDK17（SKIP）")
        return 2
    if not build(kotlinc, args.rebuild):
        return 3

    results: list[tuple[str, Case]] = []
    with tempfile.TemporaryDirectory() as tmp:
        print("\n== 模块 1: UltraHdrPackager + MetadataValidator 闭环 ==")
        c1 = case_pack_validate(java, tmp)
        results.append(("UltraHdrPackager+MetadataValidator", c1))

        print("\n== 模块 2: HeifInspector ==")
        c2 = case_heif(java, tmp)
        results.append(("HeifInspector", c2))

        print("\n== 模块 3: HdrSourceParser ==")
        c3, n_samples, n_skip = case_parser(java, tmp)
        results.append(("HdrSourceParser", c3))

    print("\n==== 汇总 ====")
    total_cases = 0
    total_asserts = 0
    any_fail = False
    for name, c in results:
        total_cases += c.cases
        total_asserts += c.asserts
        ok = c.close()
        any_fail |= not ok
        print(f"{name:<40} 用例 {c.cases}  断言 {c.asserts}  "
              f"{'FAIL' if not ok else 'PASS'}"
              + (f"（真实样本 {n_samples}，SKIP 部分 {n_skip}）" if name == "HdrSourceParser" else ""))
    for name, c in results:
        for f in c.fails:
            print(f"  FAIL {name}: {f}")

    if total_cases == 0:
        print("执行用例数为 0 → FAIL")
        return 3
    if any_fail:
        return 3
    print(f"\n结论: 全部通过（{total_cases} 用例 / {total_asserts} 断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
