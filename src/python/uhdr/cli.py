"""命令行入口。

用法：
  python -m uhdr.cli demo                          # 合成源 → 导出 → 自检
  python -m uhdr.cli inspect  <src.jpg>            # 只做容器诊断
  python -m uhdr.cli export   <src.jpg> -o out.jpg --long-edge 2048
  python -m uhdr.cli validate <file.jpg>
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import bake, pipeline, synthetic, validator, vivid_source


def cmd_demo(args: argparse.Namespace) -> int:
    root = args.root
    os.makedirs(os.path.join(root, "samples"), exist_ok=True)
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    src = os.path.join(root, "samples", "sample_cuva_3000x2250.jpg")
    synthetic.write_cuva_sample(src, base_long_edge=3000)
    print(f"[1/3] 合成 CUVA 风格源文件: {src} ({os.path.getsize(src)} B)")
    for line in synthetic.describe_container(open(src, "rb").read()):
        print(line)

    out = os.path.join(root, "out", "sample.ultrahdr.jpg")
    result = pipeline.export(src, out, pipeline.ExportOptions(long_edge=args.long_edge))
    print(f"[2/3] 导出: {out} ({result.size_after} B)")
    for s in result.steps:
        print("      ·", s)

    print("[3/3] 自检:")
    print(validator.validate_file(out).pretty())
    if result.warnings:
        print("注意事项:")
        for w in result.warnings:
            print("      !", w)

    report_path = os.path.join(root, "out", "demo_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(result.to_json())
    print(f"\n完整报告: {report_path}")
    return 0 if result.ok else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    with open(args.src, "rb") as f:
        data = f.read()
    src = vivid_source.parse_hdr_source(data, args.src)
    print(src.describe())
    for line in synthetic.describe_container(data):
        print(line)
    rep = validator.validate_bytes(data, args.src)
    print(rep.pretty())
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    opt = pipeline.ExportOptions(
        long_edge=args.long_edge,
        main_quality=args.quality,
        gainmap_quality=args.gainmap_quality,
        gain_map_max_log2=args.gain_map_max,
    )
    res = pipeline.export(args.src, args.out, opt)
    print(res.to_json())
    if not res.semantics_verified:
        # 结构自检通过 ≠ 增益语义正确。必须让调用方看到，否则「看着像 HDR 但不对」。
        print("⚠ 语义未验证：源图未提供 hdrgm/ISO 增益参数，已按默认值封装；"
              "结果不可当作已校准的 HDR。加 --require-verified 可让此情况直接失败。",
              file=sys.stderr)
        if args.require_verified:
            return 3
    return 0 if res.ok else 2


def cmd_bake(args: argparse.Namespace) -> int:
    """把 HDR 源烘焙成普通 SDR JPEG（不依赖接收端 HDR 能力）。"""
    opt = bake.BakeOptions(
        long_edge=args.long_edge,
        profile=args.profile,
        strength=args.strength,
        quality=args.quality,
        anchor=args.anchor,          # 此前解析了却没传，--anchor peak 静默无效
    )
    src = vivid_source.parse_file(args.src)
    print(src.describe())
    data, stats = bake.bake(src, opt)
    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    print(f"[bake:{stats.profile}] {out}  {len(data):,} B")
    print(f"  主图 {stats.base_size[0]}x{stats.base_size[1]}  增益图 {stats.gainmap_size[0]}x{stats.gainmap_size[1]}")
    print(f"  白点 {stats.white_point:.3f}  峰值 {stats.hdr_peak:.3f}  裁切率 {stats.clipped_ratio*100:.2f}%")
    if stats.ltm:
        print(f"  LTM r={stats.ltm['ltm_radius_px']}px  detail={stats.ltm['ltm_detail_gain']}  "
              f"compress={stats.ltm['ltm_range_compress']}  暗部提亮 x{stats.ltm['shadow_lift_x']:.2f}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    rep = validator.validate_file(args.file)
    print(rep.pretty())
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    return 0 if rep.ok else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="uhdr", description="Ultra HDR 参考工具链")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="端到端演示（合成源）")
    d.add_argument("--root", default=".")
    d.add_argument("--long-edge", type=int, default=2048)
    d.set_defaults(func=cmd_demo)

    i = sub.add_parser("inspect", help="容器结构诊断")
    i.add_argument("src")
    i.set_defaults(func=cmd_inspect)

    e = sub.add_parser("export", help="导出 Ultra HDR JPEG")
    e.add_argument("src")
    e.add_argument("-o", "--out", default=None)
    e.add_argument("--long-edge", type=int, default=2048)
    e.add_argument("--quality", type=int, default=92)
    e.add_argument("--gainmap-quality", type=int, default=85)
    e.add_argument("--gain-map-max", type=float, default=2.0)
    e.add_argument("--require-verified", action="store_true",
                   help="增益语义未经源参数验证时以退出码 3 失败（CI 门禁用）")
    e.set_defaults(func=cmd_export)

    b = sub.add_parser("bake", help="烘焙成普通 JPEG（HDR 效果压进 SDR，微信可见）")
    b.add_argument("src")
    b.add_argument("-o", "--out", required=True)
    b.add_argument("--long-edge", type=int, default=2048)
    b.add_argument("--strength", type=float, default=0.70, help="增益强度 0..1（0.70 为目视定档）")
    b.add_argument("--quality", type=int, default=95)
    b.add_argument("--profile", default="local", choices=["off", "local", "wechat"],
                   help="off=仅全局映射 / local=加局部色调映射 / wechat=为二次压缩留余量")
    b.add_argument("--anchor", default="sdr-white", choices=["sdr-white", "peak"],
                   help="白点锚定方式")
    b.set_defaults(func=cmd_bake)

    v = sub.add_parser("validate", help="文件自检")
    v.add_argument("file")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_validate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
