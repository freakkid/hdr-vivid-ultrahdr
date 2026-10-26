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
    4. 色调映射（两种目标模式，见 target_mode_for_profile）：
       - display（local/wechat，出货默认）：目标即「HDR 映射结果本身」，
         按 strength 在 log 亮度域向 SDR 底图几何插值
       - sdr（off/faithful，旧行为）：按 sdr_lum 分流混合 + 增量缩放 + lift_cap，
         把结果锚回底图亮度，用于 A/B 对照与「不提亮」逃生阀
    5. 线性 → sRGB，sRGB 域饱和度补偿 → 三角抖动量化 → JPEG

第 4 步是成败关键。2026-10-25 深夜之前全链默认 sdr 锚定：门禁指标
（中位±3 / 均值±5 / SSIM）全部对比 SDR 底图，把烘焙图死死摁在平坦底图上 ——
实测中间调与底图 ±1 灰阶，用户装机后观感是「灰灰的、没效果」。而用户眼睛的
参照物是手机上的 HDR 显示（整体更亮、高光耀眼），两个目标根本不同。
因此出货档（local/wechat）改用 display 目标：让烘焙图真正带上 HDR 的亮度
结构，验收基准也随之从「≈底图」改为「可见增强 + 自然护栏」（accept.py）。
"""
from __future__ import annotations

import io
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
    # **仅 off/faithful 的 sdr 锚定路径使用**（见 target_mode_for_profile）。
    # 出货档 local/wechat 已改用 display 目标（log 域几何插值），不再按亮度分流 ——
    # 分流假设「手机显示 HDR 时中间调与 SDR 一致」，但用户的参照物是手机上更亮的
    # HDR 显示效果，分流把 HDR 增量摁死后观感就是「灰灰的、没效果」。
    blend_start: float = 0.35
    blend_end: float = 0.90
    # 单像素相对源 SDR 的最大提亮倍数（**安全垫，不随 strength 缩放**）。
    # 仅 off/faithful 的 sdr 锚定路径使用；display 路径的目标天然 ≤1
    # （LTM ceiling 钳制 + 几何插值 ≤ sdr^(1−strength)），不需要逐像素封顶。
    # 封顶仍是「发白」的解药：实测 192~223 被推 +8.5% 即可见。
    max_lift: float = 1.25


def target_mode_for_profile(profile: str) -> str:
    """profile → 色调映射目标模式。两端（Python/Kotlin）的唯一来源。

    display = 显示导向（local/wechat，出货默认）：底图锚定 + **HDR 观感曲线**。
               前半段与 sdr 一样按 sdr_lum 分流，保证中间调不动；曲线（见
               HDR_CURVE）在其后再做一次 stop 域的 S 形对比扩张：
               暗部更黑、高光更亮、饱和更足 —— 这正是增益图给出的真实差异。
    sdr     = 纯底图锚定（旧行为，不加曲线）。off 是 A/B 对照，faithful 是
               「嫌亮」用户的不提亮逃生阀 —— 两档都必须保持「不改变整体明暗」。

    注：2026-10-26 曾走过一版「log 域几何插值」的 display，实测把 LTM 的暗部抬升
    整条继承下来（暗部 ×1.88、高光 ×1.06），动态范围不增反降 → 画面发蒙，
    已废弃。判据：输出增益与增益图真值的相关系数须为正（几何插值版为 −0.89）。
    """
    return "sdr" if profile in ("off", "faithful") else "display"


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


# HDR 观感映射（仅 display 档）—— **每一个像素的提亮量由它自己的增益数据决定**。
#
# 为什么必须是「按增益算」而不是「一条固定曲线」：
#   增益图给的是每个像素的信噪比：`HDR = (SDR + off_sdr) × 2^log_boost − off_hdr`。
#   实测三个样本「超出 SDR 白点」的部分（excess = max(HDR−1, 0)）P99 分别是
#   1.34 / 1.69 / 3.38 —— 差 2.5 倍。用一条按 sRGB 码值查的固定曲线，等于给三张
#   需求完全不同的图套同一剂量：C 那种头上有 3.4 倍空间的图远远没用够，A 那种
#   只有 1.3 倍的反而被过量提亮。0.1.24/0.1.25 都犯了这个错。
#
# 三条量全部从增益数据推出：
#   1) 逐像素提亮：谁的 HDR 超出白点多，谁拿到的提亮多（对数压缩，避免爆）
#        lift(p) = lift_max × scale × log1p(excess(p)/eps) / log1p(S/eps)
#   2) 暗部让位：整张图有多少东西压在白点之上，才需要多深的 toe
#        toe = −toe_max × scale × (1 − smoothstep(0, toe_end, code))
#   3) 饱和度：同样按 scale 给，避免「本来就没什么 HDR 内容」的图被加艳
#   其中 S = excess 的 P99（该图真实头量），scale = clip(S/ref_S, 0, 1)。
#
# 三个样本的 S 对应 scale：A 0.67 / B 0.84 / C 1.00（ref_S = 2.0）——
# 这样 C 的 LED 那种 4.4× 的像素拿满剂量，A 的汤面只拿 2/3。
HDR_CURVE = {
    "toe_max": 0.52,      # 暗部最大压暗（stop，scale=1 时）
    "toe_end": 0.30,      # 压暗段在 sRGB 码值 0~0.30 之间衰减到 0
    "lift_max": 0.18,     # 超白像素最大提亮（stop，scale=1 且 excess=S 时）
    "lift_eps": 0.06,      # 超出量的软膝尺度（线性亮度）
    "sat_max": 0.14,      # 最大额外饱和度（比例，scale=1 时）
    # 参考值按**出货强度 0.70** 标定：strength 会缩增益，P99 excess 实测
    # A 0.81 / B ~1.0 / C ~2.0（strength 1.0 时 A 1.34 / B 1.69 / C 3.38）。
    # 取 1.3 使 A 拿 0.62、B 0.77、C 1.00 —— 保住三张图之间的剂量差异。
    "ref_excess": 1.3,
    # 头顶空间闸门（sRGB 码值）：code ≥ ceiling 时余量为 0，code ≤ ceiling−span 时满额。
    # 0.95 / 0.25 → code 0.70 以下满额、0.90 处仅 20%、0.95 以上完全不给。
    # 这样剂量落在「高光下沿」（真有 HDR 余量、8bit 也还有位置），而不是正在
    # 逼近 255 的最亮像素上 —— 否则加多少就压平多少（实测 A@0.70 spread 0.50→0.17）。
    "headroom_ceiling": 0.95,
    "headroom_span": 0.25,
}

# 高光软天花板：提亮之后必须收敛到这里，不能只靠 [0,1] 硬裁。
# 依据是用户手机截图实测：上格在 L 224~239 档被推到 ~246、240~255 档停在 ~249，
# 即「压进 246~250 这窄带」而不是顶到 255。硬裁会把整段推成纯白
# （实测 A 样本 clip_delta +4.3 个百分点、高光层次 0.50→0.25）。
CURVE_CEIL_KNEE = 0.97
CURVE_CEIL = 1.0


def hdr_content_scale(hdr_linear: np.ndarray) -> tuple[float, float]:
    """整张图的「HDR 含量」→ (scale∈[0,1], excess 的 P99)。

    excess = max(HDR 亮度 − 1, 0)：只有这部分是 SDR 图**物理上表现不出来**的内容。
    用 P99 而不是 max：单个镜面高光不该决定整图的剂量。
    """
    lum = np.maximum((hdr_linear * _LUMA).sum(axis=2), 0.0)
    ex = lum - 1.0
    m = ex > 1e-4
    if not np.any(m):
        return 0.0, 0.0
    s = float(np.percentile(ex[m], 99))
    return float(np.clip(s / HDR_CURVE["ref_excess"], 0.0, 1.0)), s


def hdr_lift_stops(hdr_linear: np.ndarray, sdr_lum: np.ndarray,
                   scale: float, s: float) -> np.ndarray:
    """逐像素提亮量（stop），由**该像素自己的增益数据**决定，并且受头顶空间约束。

    两条都要满足才发剂量：
      1. 该像素 HDR 真的超过 SDR 白点（excess > 0）—— 增益低的地方一分不给。
         0.1.24 恰好相反：给暗部（增益最低处）发剂量，所以正相关是负的。
      2. 该像素在 8bit 里**还有余量**（code 越接近 1，余量越小）。没有余量还硬提，
         只能把它推进 255 —— 实测 A@0.70 不给闸门时 hl_spread 从 0.42 掉到 0.17。
         这不是参数问题：手机 HDR 屏能显示「比白更亮」，8bit JPEG 不能。
    """
    if scale <= 0.0 or s <= 0.0:
        return np.zeros(hdr_linear.shape[:2], dtype=np.float32)
    lum = np.maximum((hdr_linear * _LUMA).sum(axis=2), 0.0)
    ex = np.maximum(lum - 1.0, 0.0)
    eps = HDR_CURVE["lift_eps"]
    norm = np.log1p(ex / eps) / max(float(np.log1p(s / eps)), 1e-6)
    # 可用余量：code ≥ 1 时为 0，code ≤ headroom_free 时为满额
    code = _linear_to_srgb(np.maximum(sdr_lum, 1e-7))
    headroom = np.clip((HDR_CURVE["headroom_ceiling"] - code)
                       / max(HDR_CURVE["headroom_span"], 1e-3), 0.0, 1.0)
    return (HDR_CURVE["lift_max"] * scale * norm * headroom).astype(np.float32)


def hdr_toe_stops(code: np.ndarray, scale: float) -> np.ndarray:
    """暗部让位量（负 stop）：只有这张图确实有超白内容时才压暗部。"""
    if scale <= 0.0:
        return np.zeros_like(code, dtype=np.float32)
    return (-HDR_CURVE["toe_max"] * scale
            * (1.0 - _smoothstep(0.0, HDR_CURVE["toe_end"], code))).astype(np.float32)


def _apply_hdr_curve(out_linear: np.ndarray, lift: np.ndarray, toe: np.ndarray) -> np.ndarray:
    """按亮度查总剂量、按比例缩放 RGB（保色相），高光端软收敛。

    为什么乘在**线性**域：2^delta 是亮度倍率，乘在线性光上才等于「亮 k 倍」；
    乘在 sRGB 码值上会变成非线性缩放，暗部被压得过狠。天花板判据则落在码值域
    （人眼与直方图都是码值）：线性乘 gain → 码值域软收敛 → 回线性。
    """
    lum = np.maximum((out_linear * _LUMA).sum(axis=2), 1e-7)
    code = _linear_to_srgb(lum)
    delta = (lift + toe).astype(np.float32)
    gain = np.power(2.0, delta).astype(np.float32)
    lifted = _srgb_to_linear(_soft_knee(_linear_to_srgb(lum * gain),
                                       CURVE_CEIL_KNEE, CURVE_CEIL))
    scale = (lifted / lum).astype(np.float32)
    return (out_linear * scale[..., None]).astype(np.float32)


def _resize_rgb(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return img if img.size == size else img.resize(size, Image.Resampling.LANCZOS)


def to_srgb(img: "Image.Image") -> "Image.Image":
    """把带 ICC 的图像转到 sRGB。

    为什么必须做：手机拍的 HDR 图常见 ICC 是 **Display P3**（实测
    `sRGB EOTF with DCI-P3 Color Gamut`）。若直接丢掉 ICC，系统会按 sRGB 解释这些
    数字，平均饱和度实测掉 8.8 个点，观感就是「烘焙图比原图灰」。

    只在能拿到 ICC 且不是 sRGB 时转换。有 ICC 但 ImageCms 转换失败时**必须抛错**：
    此前 except 后静默退回原图，色彩管理一坏基准和输出一起变灰，验收照样全绿
    （docs/07 §2-5「自证」）。无 ICC 或本身就是 sRGB 的正常路径不变。
    """
    icc = img.info.get("icc_profile")
    if not icc:
        return img.convert("RGB")
    from PIL import ImageCms

    src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    desc = ImageCms.getProfileDescription(src).lower()
    if "srgb" in desc and "p3" not in desc and "2020" not in desc:
        return img.convert("RGB")
    return ImageCms.profileToProfile(
        img.convert("RGB"), src, ImageCms.createProfile("sRGB"), outputMode="RGB"
    )


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
    # 必须做色彩空间归一：源图多为 Display P3，丢掉 ICC 会让输出明显发灰
    base = to_srgb(Image.open(io.BytesIO(source.primary_jpeg)))
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
    # 仅 off/faithful 的 sdr 锚定路径使用；display 路径不需要逐像素封顶
    # （目标 ≤ sdr^(1−amount) ≤ 1，见 _display_target）。
    lift_cap = min(opt.max_lift, 1.05) if opt.profile == "faithful" else opt.max_lift
    # display 路径里 strength 是「HDR 观感的总体剂量」（0=等同底图，1=完整映射）；
    # sdr 路径里 strength 作用在「HDR 相对 SDR 的增量」上。只让增益乘 strength 时，
    # 色调映射会把差异压平 —— 0.30 与 1.00 的输出几乎一致，真机反馈「强度不生效」。
    amount = float(np.clip(opt.strength, 0.0, 1.0))

    lum = (hdr * _LUMA).sum(axis=2)
    # 基准：SDR 主图的亮度。sdr 路径按它做亮度分流；display 路径用它做几何插值起点
    # （amount=0 时输出亮度精确回到底图）。
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
    use_curve = target_mode_for_profile(opt.profile) == "display"
    ltm_p = ltm.params_for_profile(opt.profile)
    # 显示档的剂量全部由增益数据推出（见 HDR_CURVE 注释），sdr 档一律 0。
    curve_lift = curve_toe = None
    curve_scale = 0.0
    if use_curve:
        curve_scale, _curve_s = hdr_content_scale(hdr)
        curve_lift = hdr_lift_stops(hdr, sdr_lum, curve_scale, _curve_s)
        curve_toe = hdr_toe_stops(_linear_to_srgb(
            np.maximum(sdr_lum, 1e-7)), curve_scale)
    if ltm_p is not None:
        # 局部色调映射：输出已经是「1.0 = SDR 白点」的显示线性域。
        ltm_res = ltm.apply(hdr, white, ltm_p)
        mapped_rgb = np.clip(ltm_res[0], 0.0, 1.0)
        ltm_stats = ltm_res[1]
        mapped_lum = (mapped_rgb * _LUMA).sum(axis=2)
        # 按 sdr_lum 分流 + 增量缩放 + lift_cap。display 与 sdr 的差别只在
        # 之后那条曲线（见 _apply_hdr_curve），底色映射是同一套 —— 这样
        # 「中间调不动」这条底线两种模式都成立。
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

    # HDR 观感曲线（display 档）：在底色映射之后、量化之前，
    # 做一次 stop 域的 S 形对比扩张 —— 暗部更黑、高光更亮。
    # 放在这里是因为它必须作用在「已经锚定过底图」的亮度上，且要在裁切统计之后，
    # 否则会把曲线带来的高光溢出算成色调映射的裁切。
    if use_curve:
        out = np.clip(_apply_hdr_curve(out, curve_lift, curve_toe), 0.0, 1.0)

    # 裁切率：**按单通道**统计压缩前的 >1。用亮度判会恒为 0（亮度被归一化到 target ≤ 1），
    # 但单通道确实会溢出（scale>1 时主导通道先超），与 Kotlin 的统计口径一致。
    clip_mask = (raw > 1.0).any(axis=2)

    srgb = _linear_to_srgb(out)

    # 对比与饱和度必须在 sRGB（感知）域做。放在线性域会把 0.5 以下的亮度
    # 强力压向 0（线性 0.5 ≈ sRGB 188），暗部直接被钉死成纯黑——这是
    # 「暗部 19.2 → 0.7」的根因，与 anchor 无关。
    if opt.contrast != 1.0:
        srgb = np.clip((srgb - 0.5) * opt.contrast + 0.5, 0.0, 1.0)
    sat_eff = opt.saturation * (1.0 + HDR_CURVE["sat_max"] * curve_scale)
    if sat_eff != 1.0:
        l2 = (srgb * _LUMA).sum(axis=2, keepdims=True)
        srgb = np.clip(l2 + (srgb - l2) * sat_eff, 0.0, 1.0)

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
