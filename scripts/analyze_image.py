#!/usr/bin/env python3
"""统一图片分析器：判断真实格式、容器结构、以及是否带 HDR 增益图。

用途：手机端报「不是合法 JPEG」时，把文件拉到桌面用这个跑一遍，
一次拿到所有需要的事实，不用来回猜。

用法:
    python scripts/analyze_image.py <文件> [--extract 输出目录]

判据优先级与 src/android 的 HdrSourceParser 保持一致。
依赖: pillow, pillow-heif（仅 HEIC 分支需要）
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src", "python"))


def sniff(data: bytes) -> str:
    """按魔数判断真实格式——扩展名不可信。"""
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12].decode("ascii", "replace")
        return f"ISOBMFF ({brand})"
    if data[:4] == b"\x89PNG":
        return "PNG"
    if data[:4] == b"RIFF":
        return "RIFF/WebP"
    return "未知"


def top_level_boxes(data: bytes) -> list[tuple[str, int, int]]:
    """遍历 ISOBMFF 顶层 box，返回 [(类型, 偏移, 长度)]。"""
    out = []
    pos = 0
    n = len(data)
    while pos + 8 <= n:
        size = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8].decode("latin-1")
        if size == 1 and pos + 16 <= n:
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
        elif size == 0:
            size = n - pos
        if size < 8:
            break
        out.append((typ, pos, size))
        pos += size
    return out


def analyze_jpeg(path: str) -> int:
    from uhdr import jpegseg, vivid_source

    with open(path, "rb") as f:
        data = f.read()
    print(f"大小: {len(data):,} B")
    imgs = jpegseg.find_all_images(data)
    print(f"JPEG 图像数: {len(imgs)}  {imgs}")
    try:
        src = vivid_source.parse_hdr_source(data, path)
    except Exception as e:  # noqa: BLE001
        print(f"解析失败: {type(e).__name__}: {e}")
        return 1
    print(f"容器: {src.container}")
    print(f"增益图: {'有 ' + format(len(src.gainmap_jpeg), ',') + ' B' if src.gainmap_jpeg else '无'}")
    carrier = getattr(src, "metadata_carrier", None)
    if carrier is None:
        carrier = "ISO 21496-1" if getattr(src, "iso_meta", None) else ("XMP" if src.meta else "无")
    print(f"元数据载体: {carrier}")
    print(f"EXIF Make: {src.exif_make}")
    if src.meta:
        m = src.meta
        print(f"hdrgm: GainMapMin={m.gain_map_min:.4f} GainMapMax={m.gain_map_max:.4f} "
              f"Gamma={m.gamma:.4f} CapMax={m.hdr_capacity_max:.4f}")
    if src.iso_meta:
        i = src.iso_meta
        print(f"ISO  : min={i.min_content_boost:.3f} max={i.max_content_boost:.3f} "
              f"gamma={i.gamma:.3f} capMax={i.hdr_capacity_max:.3f}")
        print(f"       原始值 {i.raw_values}")
    for n in src.notes:
        print(f"· {n}")
    return 0


def analyze_heif(path: str, out_dir: str | None) -> int:
    try:
        import pillow_heif
    except ImportError:
        print("缺少 pillow-heif：pip install pillow-heif")
        return 2

    with open(path, "rb") as f:
        data = f.read()
    print(f"大小: {len(data):,} B")
    print("顶层 box:")
    for typ, off, size in top_level_boxes(data):
        print(f"  {typ}  @{off}  {size:,} B")

    pillow_heif.register_heif_opener()
    heif = pillow_heif.open_heif(path, convert_hdr_to_8bit=False)

    print(f"主图: {heif.size[0]}x{heif.size[1]}  mode={heif.mode}")
    info = getattr(heif, "info", {}) or {}
    print(f"info 键: {sorted(info.keys())}")

    aux = info.get("aux") or {}
    if not aux:
        print("辅助图: 无（该文件没有增益图 / 深度图 / 缩略图 aux）")
    else:
        print("辅助图 URN:")
        for urn, ids in aux.items():
            print(f"  {urn}  → item ids {ids}")

    # 已知的增益图 URN
    gain_urns = {
        "urn:iso:std:iso:ts:21496:-1": "ISO 21496-1 增益图（标准）",
        "urn:com:apple:photo:2020:aux:hdrgainmap": "Apple HDR 增益图",
        "urn:com:huawei:photo:aux:hdrgainmap": "华为 HDR 增益图",
        "urn:com:huawei:hdr:aux:gainmap": "华为 HDR 增益图（备选 URN）",
    }
    hit = [(u, d) for u, d in gain_urns.items() if u in aux]

    if not hit:
        print("\n结论：有 aux 但没有已登记的增益图 URN —— 需要按 ids 逐个试探解码。")
    else:
        for urn, desc in hit:
            print(f"\n结论：发现增益图 —— {desc}")
            print(f"  URN: {urn}")

    if out_dir and aux:
        os.makedirs(out_dir, exist_ok=True)
        for urn, ids in aux.items():
            for idx, iid in enumerate(ids):
                try:
                    img = heif.get_aux_image(iid) if hasattr(heif, "get_aux_image") else None
                except Exception as e:  # noqa: BLE001
                    print(f"  取 aux {iid} 失败: {e}")
                    continue
                if img is None:
                    print(f"  取 aux {iid} 返回 None（API 可能不支持）")
                    continue
                safe = urn.replace(":", "_").replace("/", "_")[:60]
                p = os.path.join(out_dir, f"aux_{safe}_{idx}.png")
                img.save(p)
                print(f"  已导出 {p}  {img.size}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="判断图片格式与 HDR 增益图")
    ap.add_argument("path")
    ap.add_argument("--extract", default=None, help="导出 aux 图到该目录")
    args = ap.parse_args()

    with open(args.path, "rb") as f:
        head = f.read(16)
    kind = sniff(head)
    print(f"文件: {os.path.basename(args.path)}")
    print(f"真实格式: {kind}  头部: {head.hex(' ')}")
    print("-" * 60)

    if kind == "JPEG":
        return analyze_jpeg(args.path)
    if kind.startswith("ISOBMFF"):
        return analyze_heif(args.path, args.extract)
    print("既不是 JPEG 也不是 HEIC/HEIF，暂不支持。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
