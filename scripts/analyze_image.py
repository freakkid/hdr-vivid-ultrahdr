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


# ---- nclx / 传输特性语义表 ----
# 这些数字来自 ISO/IEC 23001-8 (CICP)。HDR 的判据不只有增益图：
# 主图本身编码成 PQ/HLG、或色域是 BT.2020，也说明它是 HDR 内容。
_TRANSFER = {
    1: "BT.709（SDR）", 2: "未指定", 4: "gamma 2.2（SDR）", 5: "gamma 2.8（SDR）",
    6: "BT.601（SDR）", 7: "SMPTE 240M（SDR）", 8: "线性",
    13: "sRGB（SDR）", 14: "BT.2020 10bit（SDR）", 15: "BT.2020 12bit（SDR）",
    16: "PQ / SMPTE ST 2084（HDR）",
    18: "HLG / ARIB STD-B67（HDR）",
}
_PRIMARIES = {
    1: "BT.709", 4: "BT.470M", 5: "BT.470BG", 6: "BT.601", 7: "SMPTE 240M",
    8: "通用胶片", 9: "BT.2020（广色域）",
    11: "SMPTE RP 431-2（DCI-P3）",
    12: "SMPTE EG 432-1（Display P3）",
}
_GAIN_URNS = {
    "urn:iso:std:iso:ts:21496:-1": "ISO 21496-1 增益图（标准）",
    "urn:com:apple:photo:2020:aux:hdrgainmap": "Apple HDR 增益图",
    "urn:com:huawei:photo:aux:hdrgainmap": "华为 HDR 增益图（推测 URN）",
    "urn:com:huawei:hdr:aux:gainmap": "华为 HDR 增益图（推测 URN，备选）",
}


def _stat_of(im) -> str:
    """给 aux 图一个数值画像，用来判断它像不像增益图。"""
    try:
        import numpy as np
        a = np.asarray(im, dtype=np.float32)
        lo, hi = float(a.min()), float(a.max())
        mean = float(a.mean())
        med = float(np.median(a))
        return f"min={lo:.0f} max={hi:.0f} mean={mean:.1f} median={med:.0f}"
    except Exception:  # noqa: BLE001
        return "(无 numpy，跳过统计)"


def analyze_heif(path: str, out_dir: str | None) -> int:
    try:
        import pillow_heif
    except ImportError:
        print("缺少 pillow-heif：pip install pillow-heif")
        print("  注意：阿里云 index 没有该包，用清华源")
        print("  pip install pillow-heif -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 2

    with open(path, "rb") as f:
        data = f.read()
    print(f"大小: {len(data):,} B")
    print("顶层 box:")
    for typ, off, size in top_level_boxes(data):
        print(f"  {typ}  @{off}  {size:,} B")

    pillow_heif.register_heif_opener()
    try:
        hf = pillow_heif.open_heif(path)
    except Exception as e:  # noqa: BLE001
        print(f"open_heif 失败: {type(e).__name__}: {e}")
        return 1

    im0 = hf[0]
    info = im0.info or {}
    print(f"主图: {im0.size[0]}x{im0.size[1]}  mode={im0.mode}  "
          f"bit_depth={info.get('bit_depth')}  chroma={info.get('chroma')}")

    # ---- 色彩元数据：第二条 HDR 判据
    nclx = info.get("nclx_profile") or {}
    tc = nclx.get("transfer_characteristics")
    cp = nclx.get("color_primaries")
    if nclx:
        print(f"色彩: primaries={cp} {_PRIMARIES.get(cp, '?')}  "
              f"transfer={tc} {_TRANSFER.get(tc, '?')}  "
              f"matrix={nclx.get('matrix_coefficients')}  "
              f"full_range={nclx.get('full_range_flag')}")

    # ---- 辅助图
    aux = info.get("aux") or {}
    aux_report = []
    if not aux:
        print("辅助图: 无")
    else:
        print(f"辅助图: {sum(len(v) for v in aux.values())} 张")
        for urn, ids in aux.items():
            desc = _GAIN_URNS.get(urn, "未登记 URN")
            print(f"  URN: {urn}  → ids {ids}   ({desc})")
            for iid in ids:
                try:
                    aim = im0.get_aux_image(iid).to_pillow()
                    print(f"    #{iid}  {aim.size}  mode={aim.mode}  {_stat_of(aim)}")
                    aux_report.append((urn, iid, aim))
                except Exception as e:  # noqa: BLE001
                    print(f"    #{iid}  取图失败: {type(e).__name__}: {e}")

    # ---- 结论
    print("\n" + "=" * 60)
    gain_hits = [u for u in aux if u in _GAIN_URNS]
    hdr_by_codec = tc in (16, 18) or cp == 9
    if gain_hits:
        for u in gain_hits:
            print(f"结论：**有增益图** —— {_GAIN_URNS[u]}")
            print(f"      URN: {u}")
        print("      → 可以做 HDR 烘焙（需按该 URN 的语义解释增益值）")
    elif hdr_by_codec:
        print("结论：**没有增益图，但主图本身是 HDR 编码**")
        if tc in (16, 18):
            print(f"      传输特性 = {_TRANSFER[tc]}，是 PQ/HLG 直接编码的 HDR")
        if cp == 9:
            print("      色域 = BT.2020（广色域）")
        print("      → 需要 PQ/HLG → 线性的逆变换，不是加增益图；")
        print("        这条路径我们尚未实现（见 HANDOVER §3.4 路线 A）")
    elif aux:
        print("结论：有辅助图，但没有已登记的增益图 URN —— 按上面 ids 逐个试探")
    else:
        print("结论：**普通 SDR HEIC**，无增益图、无 HDR 编码")
        print("      → 只需主图解码，不做增益图处理")

    # ---- 导出
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            base = im0.to_pillow().convert("RGB")
            bp = os.path.join(out_dir, f"{stem}_base.jpg")
            base.save(bp, quality=95, subsampling=0)
            print(f"\n已导出主图: {bp}  {base.size}")
        except Exception as e:  # noqa: BLE001
            print(f"导出主图失败: {type(e).__name__}: {e}")
        for urn, iid, aim in aux_report:
            safe = urn.replace(":", "_").replace("/", "_")[:50]
            ap = os.path.join(out_dir, f"{stem}_aux{iid}_{safe}.png")
            try:
                aim.save(ap)
                print(f"已导出 aux: {ap}  {aim.size} mode={aim.mode}")
            except Exception as e:  # noqa: BLE001
                print(f"导出 aux #{iid} 失败: {type(e).__name__}: {e}")
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
