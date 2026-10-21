"""烘焙画质复核：回答 docs/04 §7.3 的悬案——「高频能量降 36% 但目视更好」，
是指标对绝对亮度敏感的假象，还是 range_compress 压掉了真实细节？

两种用法：

1) 真实/合成源体检（默认，信息性输出，不做硬判定）：
     python tests/bake_quality_check.py <源.jpg> [--strength 0.70]
   指标（都把烘焙图等亮度归一到与源同均值，剔除整体提亮干扰）：
     - SSIM（结构相似度）：0.9x 是「故意改写影调」的预期表现，不作细节判据
     - 等亮度高频能量比：拉普拉斯均值之比，≈1 表示高频细节保留
     - 局部对比（块 std 中位数，block=4 偏细节 / 16 偏通透感）
   并排 off / local / wechat 三档，重点看 local 相对 off 是否有细节增益。

2) 受控合成实验（判决性，有退出码）：
     python tests/bake_quality_check.py --selftest
   构造「恒定增益 + chirp 细节图」：增益图全 128（gain 恒定），主图是中灰
   调上的频率扫描正弦 + 硬边缘。恒定增益下 off 档等价于全局缩放（细节不被
   压缩），因此 local 档的细块局部对比若显著高于 off 档，才证明 detail_gain
   按设计在放大细节；否则说明 LTM 没有可测量的细节增益，其价值仅在影调
   分布（暗部提升/高光收敛）。
   退出码：0 = detail_gain 生效；1 = 未生效（需重新标定）。
"""
from __future__ import annotations

import argparse
import io
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "python"))

from uhdr import bake, vivid_source, xmp  # noqa: E402

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def luma(a: np.ndarray) -> np.ndarray:
    return (a.astype(np.float32) * _LUMA).sum(axis=2)


def _box_mean(a: np.ndarray, r: int) -> np.ndarray:
    """(2r+1)^2 均匀窗均值（可分离滑动窗口，O(1)/像素，边界 clamp）。"""
    h, w = a.shape
    win = 2 * r + 1
    tmp = np.zeros((h, w), np.float32)
    for y in range(h):
        s = 0.0
        for k in range(-r, r + 1):
            s += a[y, min(max(k, 0), w - 1)]
        for x in range(w):
            tmp[y, x] = s / win
            s += a[y, min(x + r + 1, w - 1)] - a[y, min(max(x - r, 0), w - 1)]
    out = np.zeros((h, w), np.float32)
    for x in range(w):
        s = 0.0
        for k in range(-r, r + 1):
            s += tmp[min(max(k, 0), h - 1), x]
        for y in range(h):
            out[y, x] = s / win
            s += tmp[min(y + r + 1, h - 1), x] - tmp[min(max(y - r, 0), h - 1), x]
    return out


def ssim_map(x: np.ndarray, y: np.ndarray, r: int = 5) -> np.ndarray:
    """亮度通道 SSIM 图（0-255 尺度，C1=(0.01L)^2，C2=(0.03L2)^2）。"""
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    mx, my = _box_mean(x, r), _box_mean(y, r)
    mxx, myy = _box_mean(x * x, r), _box_mean(y * y, r)
    mxy = _box_mean(x * y, r)
    vx = np.maximum(mxx - mx * mx, 0)
    vy = np.maximum(myy - my * my, 0)
    vxy = mxy - mx * my
    return ((2 * mx * my + c1) * (2 * vxy + c2)) / ((mxx + myy + c1) * (vx + vy + c2))


def local_contrast_median(y: np.ndarray, block: int) -> float:
    """块内亮度标准差的中位数。block=16 偏全局通透感，block=4 偏细节。"""
    h, w = y.shape
    st = [float(y[by:by + block, bx:bx + block].std())
          for by in range(0, h - block + 1, block)
          for bx in range(0, w - block + 1, block)]
    return float(np.median(st)) if st else 0.0


def hf_energy(y: np.ndarray) -> float:
    """朴素高频能量：亮度拉普拉斯响应均值。"""
    lap = (-4 * y + np.roll(y, 1, 0) + np.roll(y, -1, 0)
           + np.roll(y, 1, 1) + np.roll(y, -1, 1))
    return float(np.abs(lap).mean())


def _ratio(a: float, b: float) -> float:
    return a / max(b, 1e-6)


def bake_to_luma(source, profile: str, strength: float, ref_size) -> tuple[np.ndarray, dict]:
    data, stats = bake.bake(source, bake.BakeOptions(profile=profile, strength=strength))
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.size != ref_size:
        img = img.resize(ref_size, Image.Resampling.LANCZOS)
    return luma(np.asarray(img, np.float32)), stats


def check_source(src_path: str, strength: float) -> None:
    """模式 1：真实/合成源三档体检（信息性）。"""
    source = vivid_source.parse_file(src_path)
    primary = Image.open(io.BytesIO(source.primary_jpeg)).convert("RGB")
    ya = luma(np.asarray(primary, np.float32))

    print(f"源: {src_path}  strength={strength}  源均值亮度={ya.mean():.1f}")
    hdr = (f"  {'档位':<7} {'均值亮度':>8} {'归一化系数':>10} {'SSIM':>7} "
           f"{'等亮度HF比':>10} {'局部对比16':>11} {'局部对比4':>10}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    hf_src = hf_energy(ya)
    lc16_src, lc4_src = local_contrast_median(ya, 16), local_contrast_median(ya, 4)
    res = {}
    for profile in ("off", "local", "wechat"):
        yb, _ = bake_to_luma(source, profile, strength, primary.size)
        scale = _ratio(float(ya.mean()), float(yb.mean()))
        yb_eq = np.clip(yb * scale, 0, 255)
        row = {
            "mean": float(yb.mean()),
            "scale": scale,
            "ssim": float(ssim_map(ya, yb_eq).mean()),
            "hf": _ratio(hf_energy(yb_eq), hf_src),
            "lc16": _ratio(local_contrast_median(yb_eq, 16), lc16_src),
            "lc4": _ratio(local_contrast_median(yb_eq, 4), lc4_src),
        }
        res[profile] = row
        print(f"  {profile:<7} {row['mean']:>8.1f} {row['scale']:>10.3f} {row['ssim']:>7.4f} "
              f"{row['hf']:>10.3f} {row['lc16']:>11.3f} {row['lc4']:>10.3f}")

    d_lc4 = res["local"]["lc4"] - res["off"]["lc4"]
    d_hf = res["local"]["hf"] - res["off"]["hf"]
    print(f"\n  local 相对 off：细块局部对比 {d_lc4:+.3f}，等亮度高频能量 {d_hf:+.3f}")
    if d_lc4 < -0.02:
        print("  注意：local 的细节向指标低于 off——LTM 未产生可测量的细节增益，")
        print("        其价值在影调分布（暗部提升/高光收敛），detail_gain 标定存疑")
        print("        （对应 docs/04 §6.4 P1-3）。")


def selftest() -> int:
    """模式 2：受控合成实验——恒定增益 chirp 图，判决 detail_gain 是否生效。"""
    w, h = 768, 512
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # 频率扫描正弦（周期 2→24 px）+ 右半硬边缘，围绕中灰 0.45（sRGB）
    freq = 1.0 / (2.0 + 22.0 * xx / w)
    phase = np.cumsum(freq, axis=1) * 2.0 * np.pi
    chirp = 0.45 + 0.10 * np.sin(phase)
    edge = np.where(xx > w * 0.8, 0.12, 0.0)
    srgb = np.clip(chirp + edge, 0, 1)
    rgb = np.stack([srgb] * 3, axis=-1)          # 灰度图案扩三通道
    primary = Image.fromarray((rgb * 255).astype(np.uint8), "RGB")

    buf = io.BytesIO()
    primary.save(buf, format="JPEG", quality=95, subsampling=0)
    gm = Image.new("L", (w, h), 128)  # recovery=0.5 → gain 恒定
    gbuf = io.BytesIO()
    gm.save(gbuf, format="JPEG", quality=95)

    source = vivid_source.HdrSource(
        path="<selftest>", container="iso_ultrahdr",
        primary_jpeg=buf.getvalue(), gainmap_jpeg=gbuf.getvalue(),
        meta=xmp.GainMapMeta(gain_map_min=0.0, gain_map_max=2.0),
    )
    # 参考亮度：主图 JPEG 解码回来（与烘焙输出同经 JPEG，编码差异相互抵消）
    ya = luma(np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"), np.float32))

    rows = {}
    for profile in ("off", "local"):
        yb, _ = bake_to_luma(source, profile, 0.70, (w, h))
        scale = _ratio(float(ya.mean()), float(yb.mean()))
        yb_eq = np.clip(yb * scale, 0, 255)
        rows[profile] = {
            "hf": _ratio(hf_energy(yb_eq), hf_energy(ya)),
            "lc4": _ratio(local_contrast_median(yb_eq, 4), local_contrast_median(ya, 4)),
        }
    print("受控合成实验（恒定增益 chirp，等亮度归一化，相对主图 JPEG 的比值）：")
    for p, r in rows.items():
        print(f"  {p:<6} 等亮度HF比={r['hf']:.3f}  细块局部对比比={r['lc4']:.3f}")

    gain = rows["local"]["lc4"] / max(rows["off"]["lc4"], 1e-6)
    print(f"  local/off 细块局部对比增益 = {gain:.3f}")
    if gain >= 1.05:
        print("=> detail_gain 生效：恒定增益下 LTM 确实放大了细节层")
        return 0
    print("=> detail_gain 未产生可测量细节增益（阈值 1.05），需重新标定")
    print("   （docs/04 §6.4 P1-3；机制：eps=4e-3 远小于真实纹理的 log2 方差（~1），")
    print("    guided filter 退化为恒等，detail≈0，range_compress 压掉了纹理本身。")
    print("    跑 --eps-sweep 验证更大 eps 能否恢复细节增益）")
    return 1


def eps_sweep(src_path: str | None = None) -> None:
    """模式 3：扫 guided filter 的 eps。

    无 src_path：合成受控实验（恒定增益 chirp + 干净台阶），验证机制猜想；
    带 src_path：真实样张扫描，额外报告真实强边缘的振铃能量（halo 代理）。
    """
    from uhdr import ltm

    if src_path:
        _eps_sweep_real(src_path)
        return

    w, h = 768, 512
    xx = np.mgrid[0:h, 0:w][1].astype(np.float32)
    freq = 1.0 / (2.0 + 22.0 * xx / w)
    chirp = 0.45 + 0.10 * np.sin(np.cumsum(freq, axis=1) * 2.0 * np.pi)
    srgb = np.clip(chirp + np.where(xx > w * 0.8, 0.12, 0.0), 0, 1)
    primary = Image.fromarray((np.stack([srgb] * 3, axis=-1) * 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    primary.save(buf, format="JPEG", quality=95, subsampling=0)
    gm_buf = io.BytesIO()
    Image.new("L", (w, h), 128).save(gm_buf, format="JPEG", quality=95)
    source = vivid_source.HdrSource(
        path="<sweep>", container="iso_ultrahdr",
        primary_jpeg=buf.getvalue(), gainmap_jpeg=gm_buf.getvalue(),
        meta=xmp.GainMapMeta(gain_map_min=0.0, gain_map_max=2.0),
    )
    ya = luma(np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"), np.float32))
    lc4_src = local_contrast_median(ya, 4)

    # off 档基线（lc4 在 chirp 图上测）
    yb_off, _ = bake_to_luma(source, "off", 0.70, (w, h))
    off_scale = _ratio(float(ya.mean()), float(yb_off.mean()))
    yb_off_eq = np.clip(yb_off * off_scale, 0, 255)
    off_lc4 = _ratio(local_contrast_median(yb_off_eq, 4), lc4_src)

    # 边缘行为用干净图（平坦 + 硬台阶，无 chirp 纹波干扰）
    flat = np.clip(0.45 + np.where(xx > w * 0.8, 0.12, 0.0), 0, 1)
    flat_img = Image.fromarray((np.stack([flat] * 3, axis=-1) * 255).astype(np.uint8), "RGB")
    fbuf = io.BytesIO()
    flat_img.save(fbuf, format="JPEG", quality=95, subsampling=0)
    fgm = io.BytesIO()
    Image.new("L", (w, h), 128).save(fgm, format="JPEG", quality=95)
    fsrc = vivid_source.HdrSource(
        path="<sweep-edge>", container="iso_ultrahdr",
        primary_jpeg=fbuf.getvalue(), gainmap_jpeg=fgm.getvalue(),
        meta=xmp.GainMapMeta(gain_map_min=0.0, gain_map_max=2.0),
    )

    def edge_metrics(y: np.ndarray) -> tuple[float, float]:
        """10-90% 过渡宽度（px）与亮侧过冲量（0-255，halo 签名）。"""
        row = y[h // 2]
        x0 = int(w * 0.8)
        lo = float(row[x0 - 60:x0 - 30].mean())
        hi = float(row[x0 + 30:x0 + 60].mean())
        if hi <= lo:
            return 0.0, 0.0
        xs = np.arange(x0 - 60, x0 + 60)
        prof = row[x0 - 60:x0 + 60]
        i_lo = int(np.argmax(prof >= lo + 0.1 * (hi - lo))) if (prof >= lo + 0.1 * (hi - lo)).any() else 0
        i_hi = int(np.argmax(prof >= lo + 0.9 * (hi - lo))) if (prof >= lo + 0.9 * (hi - lo)).any() else len(prof) - 1
        width = float(xs[i_hi] - xs[i_lo])
        overshoot = float(max(prof[i_hi:].max() - hi, 0.0))
        return width, overshoot

    yb_off_flat, _ = bake_to_luma(fsrc, "off", 0.70, (w, h))
    off_flat_eq = np.clip(yb_off_flat * _ratio(float(ya.mean()), float(yb_off_flat.mean())), 0, 255)
    off_ew, off_os = edge_metrics(off_flat_eq)

    orig = ltm.params_for_profile
    print(f"off 基线: lc4={off_lc4:.3f}  边缘宽度={off_ew:.1f}px 过冲={off_os:.1f}")
    print(f"  {'eps':>8} {'lc4':>7} {'local/off':>10} {'边缘宽度':>9} {'过冲':>7}")
    for eps in (0.004, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0):
        ltm.params_for_profile = (
            lambda p, _e=eps: ltm.LtmParams(eps=_e) if p == "local" else orig(p))
        try:
            yb, _ = bake_to_luma(source, "local", 0.70, (w, h))
            yf, _ = bake_to_luma(fsrc, "local", 0.70, (w, h))
        finally:
            ltm.params_for_profile = orig
        yb_eq = np.clip(yb * _ratio(float(ya.mean()), float(yb.mean())), 0, 255)
        yf_eq = np.clip(yf * _ratio(float(ya.mean()), float(yf.mean())), 0, 255)
        lc4 = _ratio(local_contrast_median(yb_eq, 4), lc4_src)
        ew, os_ = edge_metrics(yf_eq)
        print(f"  {eps:>8.3f} {lc4:>7.3f} {lc4 / off_lc4:>10.3f} {ew:>8.1f}px {os_:>7.1f}")
    print("\n解读：lc4/off > 1.05 = detail_gain 生效；过冲明显 > off 基线 = 边缘 halo。")
    print("两者同时满足的 eps 才是可用标定值。")


def _eps_sweep_real(src_path: str) -> None:
    """真实样张 eps 扫描：细节指标 + 真实强边缘振铃能量（halo 代理）。

    振铃代理：取源图 |梯度| 前 0.1% 的像素为强边缘掩码（±3px 膨胀），
    统计这些位置亮度二阶差分的中位数。off 档在边缘处近似单调过渡（二阶差分≈0），
    LTM 若在边缘产生过冲/振铃，该值会显著上升——比合成台阶更接近真实风险。
    """
    from uhdr import ltm

    source = vivid_source.parse_file(src_path)
    primary = Image.open(io.BytesIO(source.primary_jpeg)).convert("RGB")
    ya = luma(np.asarray(primary, np.float32))
    h, w = ya.shape
    lc4_src, lc16_src = local_contrast_median(ya, 4), local_contrast_median(ya, 16)
    hf_src = hf_energy(ya)

    # 强边缘掩码：源图梯度前 0.1%，膨胀 ±3px
    gx = np.abs(np.diff(ya, axis=1))
    gy = np.abs(np.diff(ya, axis=0))
    g = np.zeros_like(ya)
    g[:, 1:] = np.maximum(g[:, 1:], gx)
    g[1:, :] = np.maximum(g[1:, :], gy)
    mask = g >= np.percentile(g, 99.9)
    dilated = mask.copy()
    for s in range(-3, 4):
        dilated |= np.roll(mask, s, axis=0)
        dilated |= np.roll(mask, s, axis=1)
    mask = dilated

    def edge_ring(y: np.ndarray) -> float:
        """强边缘处亮度二阶差分中位数（off≈0；振铃/过冲会抬升）。"""
        d2x = np.abs(y[:, 2:] - 2 * y[:, 1:-1] + y[:, :-2])
        d2y = np.abs(y[2:, :] - 2 * y[1:-1, :] + y[:-2, :])
        d2 = np.zeros_like(y)
        d2[:, 1:-1] = np.maximum(d2[:, 1:-1], d2x)
        d2[1:-1, :] = np.maximum(d2[1:-1, :], d2y)
        return float(np.median(d2[mask])) if mask.any() else 0.0

    def bake_eq(profile: str) -> np.ndarray:
        yb, _ = bake_to_luma(source, profile, 0.70, primary.size)
        return np.clip(yb * _ratio(float(ya.mean()), float(yb.mean())), 0, 255)

    off = bake_eq("off")
    off_ring = edge_ring(off)
    print(f"源: {src_path}（{w}x{h}）  off 基线: lc4={_ratio(local_contrast_median(off, 4), lc4_src):.3f}  "
          f"边缘振铃={off_ring:.2f}")
    print(f"  {'eps':>8} {'lc4':>7} {'lc16':>7} {'HF比':>7} {'振铃':>7} {'振铃/off':>9}")

    orig = ltm.params_for_profile
    for eps in (0.004, 0.02, 0.05, 0.1, 0.2, 0.5):
        ltm.params_for_profile = (
            lambda p, _e=eps: ltm.LtmParams(eps=_e) if p == "local" else orig(p))
        try:
            yb = bake_eq("local")
        finally:
            ltm.params_for_profile = orig
        ring = edge_ring(yb)
        print(f"  {eps:>8.3f} {_ratio(local_contrast_median(yb, 4), lc4_src):>7.3f} "
              f"{_ratio(local_contrast_median(yb, 16), lc16_src):>7.3f} "
              f"{_ratio(hf_energy(yb), hf_src):>7.3f} {ring:>7.2f} {ring / max(off_ring, 1e-6):>9.2f}")
    print("\n解读：lc4/HF比 越高细节保留越好；振铃/off > ~1.3 需警惕可见 halo。")
    print("（默认值 eps=4e-3 未改动；本表只用于标定决策，见 docs/04 §8）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", help="HDR 源文件；省略时需 --selftest")
    ap.add_argument("--strength", type=float, default=0.70)
    ap.add_argument("--selftest", action="store_true", help="跑受控合成实验")
    ap.add_argument("--eps-sweep", action="store_true",
                    help="扫 guided filter 的 eps（带 src 时用真实样张）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.eps_sweep:
        eps_sweep(args.src)
        return 0
    if not args.src:
        ap.error("需提供源文件，或使用 --selftest")
    check_source(args.src, args.strength)
    return 0


if __name__ == "__main__":
    sys.exit(main())
