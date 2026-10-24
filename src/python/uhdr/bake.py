"""把 HDR（SDR 主图 + 增益图）**烘焙**成一张普通 SDR JPEG。

与 Ultra HDR 封装的区别：
  - Ultra HDR：保留增益图，让支持 HDR 的设备自行还原，不支持时显示 SDR
  - 烘焙（本模块）：在这里就把 HDR 亮度压回 SDR 范围，输出**任何设备、任何平台
    （含微信）都能直接看到增强效果**的普通 JPEG

代价：HDR 信息被永久压缩到 8 bit，不可逆。收益：不依赖接收端能力。

算法（遵循 ISO 21496-1 / hdrgm 的恢复公式）：

    1. sRGB → 线性
    2. recovery = 增益图像素 / 255
       log_boost = recovery × (log2(max_boost) − log2(min_boost)) + log2(min_boost)
       gain = 2^log_boost
    3. HDR_linear = (SDR_linear + offset_sdr) × gain − offset_hdr
    4. 色调映射：以高分位亮度为白点做归一化 + 高光软膝压缩
    5. 线性 → sRGB，输出 JPEG

第 4 步是成败关键：直接归一化会让画面发灰，纯 Reinhard 会压掉对比，
所以用「归一化 + 软膝 + 轻微 S 曲线 + 饱和度补偿」的组合。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import ltm, vivid_source

# Rec.709 亮度权重
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


@dataclass
class BakeOptions:
    long_edge: int = 2048
    strength: float = 0.70       # 增益强度 0..1，1.0 = 完全按元数据恢复
    white_percentile: float = 99.5
    knee: float = 0.75           # 软膝起点（归一化亮度）
    saturation: float = 1.12     # 饱和度补偿，抵消压缩后的暗淡
    contrast: float = 1.0        # 线性对比度拉伸；1.0 = 关（会把接近白的高光推成纯白）
    quality: int = 95
    # off   = 仅全局映射（旧行为，用于 A/B 对照）
    # local = 全局 + 局部色调映射，观感优先
    # wechat = 全局 + 克制的局部映射，为二次压缩留余量
    profile: str = "local"
    # sdr-white = 以 1.0 为 SDR 白点（推荐）；peak = 按高分位归一化（会压暗全图）
    anchor: str = "sdr-white"
    # 高光加权区间（线性亮度）：低于 blend_start 完全保持 SDR，高于 blend_end 完全采信 HDR。
    # 必要性：增益图在全图都有值（实测中位 86/255），直接乘会把**中间调一起提亮**——
    # 数学上无法靠调 strength 消除（strength→0 只会连 HDR 一起没了）。
    # 手机显示 HDR 时参考白仍对应 SDR 1.0，中间调观感与 SDR 一致，所以这里必须按亮度分流。
    blend_start: float = 0.35
    blend_end: float = 0.90
    # 单像素相对源 SDR 的最大提亮倍数（**安全垫，不随 strength 缩放**）。
    # 强度由 strength 作用在「HDR 相对 SDR 的增量」上实现，见 bake() 里的 amount。
    # 封顶仍是「发白」的解药：实测 192~223 被推 +8.5% 即可见。
    max_lift: float = 1.25


@dataclass
class BakeStats:
    base_size: tuple[int, int]
    gainmap_size: tuple[int, int]
    out_size: tuple[int, int]
    max_boost: float
    hdr_peak: float              # 合成后峰值亮度（线性，1.0 = SDR 白点）
    white_point: float           # 实际采用的白点
    clipped_ratio: float         # 压缩前单通道 >1 的像素占比（亮度口径恒为 0，别用）
    strength: float
    profile: str = "off"
    ltm: dict = field(default_factory=dict)


def _srgb_to_linear(x: np.ndarray) -> np.ndarray:
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055).astype(np.float32)


def _soft_knee(x: np.ndarray, knee: float, ceiling: float = 0.96) -> np.ndarray:
    """knee 以下保持线性，以上用指数曲线压缩到 ceiling。

    ceiling 不取 1.0 是有意的：收敛到 1.0 时，0.9~1.0 这一段会被挤在 0.99+，
    表现为大片纯白、高光丢层次。留 4% 余量后，高光之间重新拉开差距。
    """
    if knee >= 1.0:
        return np.minimum(x, ceiling)
    over = np.maximum(x - knee, 0.0)
    span = 1.0 - knee
    rolled = knee + (ceiling - knee) * (1.0 - np.exp(-over / span))
    return np.where(x > knee, rolled, x).astype(np.float32)


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / max(e1 - e0, 1e-6), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def _resize_rgb(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return img if img.size == size else img.resize(size, Image.Resampling.LANCZOS)


def bake(
    source: vivid_source.HdrSource,
    options: BakeOptions | None = None,
    *,
    return_pixels: bool = False,
) -> tuple[bytes, BakeStats] | tuple["Image.Image", BakeStats]:
    """把 HDR 源烘焙为 SDR JPEG 字节流。

    ``return_pixels=True`` 时返回**未经过 JPEG 编码**的 PIL 图像，仅供数值对照
    （`tests/android_parity/` 用它和 Kotlin 的像素结果逐点比对，避免编解码噪声）。
    """
    import io

    opt = options or BakeOptions()
    base = Image.open(io.BytesIO(source.primary_jpeg)).convert("RGB")
    if not source.gainmap_jpeg:
        raise ValueError("源图没有增益图，只能走纯 SDR 导出")

    # 统一到目标尺寸：只缩不放
    w, h = base.size
    cur = max(w, h)
    if cur > opt.long_edge:
        k = opt.long_edge / cur
        size = (max(1, round(w * k)), max(1, round(h * k)))
    else:
        size = (w, h)

    base_r = _resize_rgb(base, size)
    gm_img = Image.open(io.BytesIO(source.gainmap_jpeg)).convert("L")
    gm_r = gm_img if gm_img.size == size else gm_img.resize(size, Image.Resampling.BILINEAR)

    sdr = _srgb_to_linear(np.asarray(base_r, dtype=np.float32) / 255.0)
    recovery = np.asarray(gm_r, dtype=np.float32) / 255.0
    recovery = recovery[..., None]          # HxWx1，广播到三通道

    # ---- 按 hdrgm / ISO 21496-1 公式恢复 HDR ----
    meta = source.meta
    gm_max_log2 = meta.gain_map_max if meta else float(np.log2(4.0))
    gm_min_log2 = meta.gain_map_min if meta else 0.0
    off_sdr = meta.offset_sdr if meta else 1 / 64
    off_hdr = meta.offset_hdr if meta else 1 / 64

    log_boost = recovery * (gm_max_log2 - gm_min_log2) + gm_min_log2
    gain = np.power(2.0, log_boost * opt.strength).astype(np.float32)

    hdr = np.maximum((sdr + off_sdr) * gain - off_hdr, 0.0).astype(np.float32)

    # ---- 色调映射 ----
    # 安全垫：逐像素最多比源 SDR 亮 max_lift 倍（faithful 压到 1.05，几乎不提亮）。
    lift_cap = min(opt.max_lift, 1.05) if opt.profile == "faithful" else opt.max_lift
    # strength 作用在「HDR 相对 SDR 的增量」上。只让增益乘 strength 时，色调映射会把差异
    # 压平 —— 0.30 与 1.00 的输出几乎一致，真机反馈就是「强度不生效」。
    amount = float(np.clip(opt.strength, 0.0, 1.0))

    lum = (hdr * _LUMA).sum(axis=2)
    # 基准：SDR 主图的亮度。手机显示 HDR 时参考白仍对应 SDR 的 1.0，
    # 所以 SDR 亮度低的区域（暗部/中间调）观感必须保持原样，只有亮部才采信 HDR。
    sdr_lum = (sdr * _LUMA).sum(axis=2)
    w = _smoothstep(opt.blend_start, opt.blend_end, sdr_lum)

    # 白点锚定方式：
    #   sdr-white（默认）：HDR 重建后 1.0 天然就是 SDR 白点（增益为 0 时 hdr≈sdr），
    #                       直接用 1.0，只压缩超出部分。
    #   peak：按高分位亮归一化。**危险**：会把整张按峰值压暗，暗部直接钉死在 0，
    #         表现为「黑位糊死 + 整体发闷」，仅在确知源已整体过曝时才用。
    if opt.anchor == "peak":
        white = max(float(np.percentile(lum, opt.white_percentile)), 1e-3)
    else:
        white = 1.0
    peak = float(lum.max())

    ltm_stats: dict = {}
    ltm_p = ltm.params_for_profile(opt.profile)
    if ltm_p is not None:
        # 局部色调映射：输出已经是「1.0 = SDR 白点」的显示线性域。
        ltm_res = ltm.apply(hdr, white, ltm_p)
        mapped_rgb = np.clip(ltm_res[0], 0.0, 1.0)
        ltm_stats = ltm_res[1]
        mapped_lum = (mapped_rgb * _LUMA).sum(axis=2)
        blended = sdr_lum * (1.0 - w) + mapped_lum * w
        target = sdr_lum + (blended - sdr_lum) * amount
        target = np.minimum(target, sdr_lum * lift_cap)
        scale = np.where(mapped_lum > 1e-6, target / np.maximum(mapped_lum, 1e-6), 0.0)
        raw = mapped_rgb * scale[..., None]
        out = np.clip(raw, 0.0, 1.0)
    else:
        norm = lum / white
        mapped = _soft_knee(norm, opt.knee)
        # 加权混合：暗部/中间调取 SDR 原值，亮部取压缩后的 HDR
        blended = sdr_lum * (1.0 - w) + mapped * w
        target = sdr_lum + (blended - sdr_lum) * amount
        target = np.minimum(target, sdr_lum * lift_cap)
        scale = np.where(lum > 1e-6, target / np.maximum(lum, 1e-6), 0.0)[..., None]
        raw = hdr * scale
        out = np.clip(raw, 0.0, 1.0)

    # 裁切率：**按单通道**统计压缩前的 >1。用亮度判会恒为 0（亮度被归一化到 target ≤ 1），
    # 但单通道确实会溢出（scale>1 时主导通道先超），与 Kotlin 的统计口径一致。
    clip_mask = (raw > 1.0).any(axis=2)

    srgb = _linear_to_srgb(out)

    # 对比与饱和度必须在 sRGB（感知）域做。放在线性域会把 0.5 以下的亮度
    # 强力压向 0（线性 0.5 ≈ sRGB 188），暗部直接被钉死成纯黑——这是
    # 「暗部 19.2 → 0.7」的根因，与 anchor 无关。
    if opt.contrast != 1.0:
        srgb = np.clip((srgb - 0.5) * opt.contrast + 0.5, 0.0, 1.0)
    if opt.saturation != 1.0:
        l2 = (srgb * _LUMA).sum(axis=2, keepdims=True)
        srgb = np.clip(l2 + (srgb - l2) * opt.saturation, 0.0, 1.0)

    # 三角分布抖动（±0.5 LSB）：8bit 量化前打散台阶，抑制平滑渐变处的 banding。
    # 固定种子 → 同参数下输出确定性（Golden-file 测试要求）。
    rng = np.random.default_rng(0)
    tri = (rng.random(srgb.shape, dtype=np.float32) - rng.random(srgb.shape, dtype=np.float32))
    img = Image.fromarray(np.clip(srgb * 255.0 + 0.5 + tri * 0.5, 0, 255).astype(np.uint8), "RGB")

    # 微信档：4:2:0 + 较低质量，体积更小；本地档：4:4:4 保色度
    subsampling = 2 if opt.profile == "wechat" else 0
    quality = 88 if opt.profile == "wechat" else opt.quality

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, subsampling=subsampling, optimize=True)

    clipped = float(clip_mask.mean())
    stats = BakeStats(
        base_size=base.size,
        gainmap_size=gm_img.size,
        out_size=img.size,
        max_boost=float(2.0 ** gm_max_log2),
        hdr_peak=peak,
        white_point=white,
        clipped_ratio=clipped,
        strength=opt.strength,
        profile=opt.profile,
        ltm=ltm_stats,
    )
    return (img, stats) if return_pixels else (buf.getvalue(), stats)


def bake_file(src_path: str, out_path: str, options: BakeOptions | None = None) -> BakeStats:
    src = vivid_source.parse_file(src_path)
    data, stats = bake(src, options)
    with open(out_path, "wb") as f:
        f.write(data)
    return stats


def make_comparison(
    source: vivid_source.HdrSource,
    out_path: str,
    options: BakeOptions | None = None,
) -> str:
    """生成「原 SDR 主图 ｜ 烘焙后」左右对比图，中间加分隔线。"""
    import io

    opt = options or BakeOptions()
    baked_bytes, _ = bake(source, opt)
    baked = Image.open(io.BytesIO(baked_bytes)).convert("RGB")

    base = Image.open(io.BytesIO(source.primary_jpeg)).convert("RGB")
    base = _resize_rgb(base, baked.size)

    gap = 12
    canvas = Image.new("RGB", (baked.width * 2 + gap, baked.height), (24, 24, 24))
    canvas.paste(base, (0, 0))
    canvas.paste(baked, (baked.width + gap, 0))
    canvas.save(out_path, format="JPEG", quality=92, subsampling=0)
    return out_path
