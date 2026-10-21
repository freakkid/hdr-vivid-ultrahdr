#!/usr/bin/env python3
"""通过 MTP 把文件推到已连接的手机（Windows，走 Shell COM）。

为什么需要它：MTP 设备不分配盘符，Git Bash / adb / 资源管理器路径都不好用；
鸿蒙（HDC）与安卓（ADB）在这里是同一套 MTP，所以这条通道两边通用。

用法:
    python scripts/push_to_phone.py <本地文件> [--device Mate] [--dest 我的手机/Download]

依赖: pip install pywin32
"""
from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import win32com.client as win32
except ImportError:
    sys.exit("缺少 pywin32：pip install pywin32")

SSF_DRIVES = 17
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400


def find_phone(shell, hint: str):
    """在「此电脑」下按名称找设备（MTP 设备没有盘符，只能这样找）。"""
    items = shell.NameSpace(SSF_DRIVES).Items()
    for i in range(items.Count):
        it = items.Item(i)
        try:
            if it.IsFolder and hint.lower() in it.Name.lower():
                return it
        except Exception:
            continue
    return None


def folder_by_path(root, parts: list[str]):
    """逐级进入 MTP 目录；parts 为空表示根。"""
    cur = root
    for p in parts:
        items = cur.Items()
        nxt = None
        for i in range(items.Count):
            it = items.Item(i)
            if it.IsFolder and it.Name.strip().lower() == p.strip().lower():
                nxt = it.GetFolder
                break
        if nxt is None:
            names = [items.Item(i).Name for i in range(min(items.Count, 30))]
            raise FileNotFoundError(f"目录 {p!r} 不存在，当前层有：{names}")
        cur = nxt
    return cur


def listing(folder) -> dict[str, int]:
    """返回 {文件名: 大小}。MTP 下 size 可能为 0 或 None。"""
    out = {}
    items = folder.Items()
    for i in range(items.Count):
        it = items.Item(i)
        try:
            if it.IsFolder:
                continue
            size = folder.GetDetailsOf(it, 1) or ""
            out[it.Name] = int(str(size).replace(",", "").strip() or 0)
        except Exception:
            out[it.Name] = 0
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="MTP 推送文件到手机")
    ap.add_argument("local", help="本地文件路径")
    ap.add_argument("--device", default="Mate", help="设备名关键词，默认 Mate")
    ap.add_argument("--dest", default="我的手机/Download", help="手机内目标目录")
    ap.add_argument("--timeout", type=int, default=90, help="等待写入完成的秒数")
    args = ap.parse_args()

    src = os.path.abspath(args.local)
    if not os.path.isfile(src):
        sys.exit(f"本地文件不存在：{src}")
    name = os.path.basename(src)
    size = os.path.getsize(src)

    shell = win32.Dispatch("Shell.Application")
    phone = find_phone(shell, args.device)
    if phone is None:
        sys.exit(f"未找到名称含 {args.device!r} 的 MTP 设备。请确认手机 USB 用途选了「传输文件」。")

    parts = [p for p in args.dest.replace("\\", "/").split("/") if p]
    dest = folder_by_path(phone.GetFolder, parts)
    print(f"设备: {phone.Name}")
    print(f"目标: {args.dest}")
    print(f"推送: {name}  ({size / 1048576:.2f} MB)")

    before = listing(dest)
    if name in before:
        print("目标已有同名文件，将被覆盖（MTP 会自动处理重名）")

    dest.CopyHere(src, FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI)

    # CopyHere 是异步的，轮询等待出现
    deadline = time.time() + args.timeout
    last = None
    while time.time() < deadline:
        time.sleep(2)
        now = listing(dest)
        got = now.get(name)
        if got and got >= size * 0.99:
            print(f"完成：{name} = {got:,} B")
            return 0
        if got != last:
            last = got
            print(f"  …写入中 {got or 0:,} B")
    now = listing(dest)
    if name in now:
        print(f"已出现但大小未确认：{name} = {now[name]:,} B（源 {size:,} B）")
        return 0
    print("超时未完成。可能被系统拒绝，可手动在资源管理器里拖入。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
