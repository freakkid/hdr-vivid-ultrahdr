"""局部色调映射（Local Tone Mapping, LTM）。

为什么需要它：
    纯全局映射（归一化 + 软膝 + S 曲线）只能把 HDR 的**亮度范围**压回 SDR，
    画面会变成「高光不爆但整体平、暗部糊成一片」。人眼感知到的「HDR 感」，
    主要来自**局部对比度**而非绝对亮度 —— SDR 屏幕的物理亮度上限无法突破，
    能改的只有「同一画面内相邻区域的亮度差」。

核心做法：对数域 base/detail 分解
    x = log2(L / white)
    base   = guided_filter(x)        # 保边平滑，去掉细节只留大尺度亮度分布
    detail = x - base                # 局部细节（纹理、边缘、局部明暗差）

    base   *= range_compress         # < 1：向白点方向压缩，抬暗部、保住高光
    detail *= detail_gain            # > 1：局部对比度增强 —— 「HDR 感」的来源
    x'      = base + detail

为什么用 guided filter 而不是高斯模糊：
    高斯 base 会在强边缘两侧产生振铃（halo），表现为高光周围一圈灰白光晕，
    是「假 HDR」最典型的穿帮点。guided filter 保边，halo 显著更少。

参数取向（与平台强相关）：
    local  ：本地查看 / 相册 / 邮件，细节增益高，追求观感
    wechat ：微信转发，细节增益压到接近 1，因为平台二次压缩会把高频细节
             变成块效应与振铃，过强的局部对比在压缩后反而更难看

双斜率高光肩（2026-10-25，替代指数软膝）
    指数软膝把 1.5~2.4× 白点的亮度全部收敛到同一条渐近线，叠加 0.96 天花板后
    实测高光被拍平在 [0.95, 0.96]（mapped 的 spread 只剩源的 8%，输出高光
    spread 恒为 0.417×源）。改为不收敛的双斜率结构：
      SDR 带 [highlight_start..1.0]：线性压到 shoulder_white（0.93），
          给 HDR 增量让出 [0.93..1.0] 的空间；
      HDR 超白带 [0..shoulder_span]：小斜率线性延伸到 0.96（log2 域），
          覆盖实测峰值 lum≈2.4× 白点（x≈1.26，span 取 1.3）；
    更高的亮度由 apply 的 ceiling 钳制兜底。ceiling 随之放开到 1.0 ——
    sRGB 250→255 这 5 个灰阶是「比白更亮」的唯一去处；纯白由 clip_delta
    阈值看守（旧教训「收敛到 1.0 挤成纯白」针对旧全局映射，新结构下不成立）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


@dataclass
class LtmParams:
    """局部色调映射参数。

    radius_frac      guided filter 半径（相对短边），越大越不容易出 halo，但越慢
    eps              保边阈值（log2 亮度方差单位）。**标定值 0.2**（2026-10-21 由
                     4e-3 上调，依据 docs/04 §8.5 实测）：eps 必须与真实纹理的
                     log2 方差（~0.5-1.0）同量级，否则 a=var/(var+eps)≈1，
                     guided filter 退化为恒等、detail≈0，range_compress 会压掉
                     纹理本身而 detail_gain 空转。上调后在真实样张上细节指标
                     全面改善且强边缘振铃低于 off 档（无可测 halo）。
                     注意合成「平坦+硬台阶」最坏情况下仍有 ~15% 台阶高过冲。
    range_compress   base 压缩系数，1.0 = 不压缩（保留原始全局对比）
    detail_gain      detail 增益，1.0 = 不变
    black_point      黑位重锚（线性归一化亮度），用来消除压缩带来的灰雾
    highlight_start  高光软膝起点（线性亮度，1.0 = SDR 白点），1.0 = 不压
    shoulder_white   双斜率肩：SDR 带 [highlight_start..1.0] 的线性映射目标
                     （线性亮度）。压到 0.93 给 HDR 超白增量让出 [0.93..1.0]；
                     实测 0.90/0.95 时高光 spread 回落，0.93 最优（sweep2）
    shoulder_span    双斜率肩：HDR 超白带覆盖范围（log2 域，相对白点）。
                     实测峰值 lum≈2.4× 白点（x≈1.26），取 1.3 全覆盖
    """

    radius_frac: float = 1.0 / 48.0
    eps: float = 0.2
    range_compress: float = 0.88
    detail_gain: float = 1.30
    black_point: float = 0.0020
    highlight_start: float = 0.78
    shoulder_white: float = 0.93
    shoulder_span: float = 1.3


def params_for_profile(profile: str) -> LtmParams | None:
    """按使用场景返回参数；返回 None 表示不启用 LTM（保持旧行为）。"""
    if profile == "off":
        return None
    if profile == "wechat":
        # 微信会重压缩：局部对比别做太狠，否则压缩后出块状伪影
        return LtmParams(
            radius_frac=1.0 / 40.0,
            eps=0.15,
            range_compress=0.92,
            detail_gain=1.12,
            black_point=0.0025,
            highlight_start=0.72,
            shoulder_white=0.93,
            shoulder_span=1.3,
        )
    if profile == "faithful":
        # 保真档：几乎不做 base 收缩（range_compress≈1 表示不抬暗部），
        # 只保留少量局部对比。给「嫌亮」的用户一档不改变整体明暗的选择。
        return LtmParams(
            radius_frac=1.0 / 48.0,
            eps=0.2,
            range_compress=0.97,
            detail_gain=1.08,
            black_point=0.0020,
            highlight_start=0.70,
            shoulder_white=0.93,
            shoulder_span=1.3,
        )
    # local：观感优先。2026-10-26 随 HDR 观感曲线上线把 detail_gain 1.32→1.35、
    # highlight_start 0.78→0.72：曲线负责整体对比扩张，LTM 这里只补局部对比，
    # 两个参数由「截图反解目标」的扫参定（见 bake.HDR_CURVE 注释）。
    return LtmParams(
        radius_frac=1.0 / 48.0,
        eps=0.2,
        range_compress=0.88,
        detail_gain=1.35,
        black_point=0.0020,
        highlight_start=0.72,
        shoulder_white=0.93,
        shoulder_span=1.3,
    )


# --------------------------------------------------------------------------
# 基础算子
# --------------------------------------------------------------------------

def _box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """O(1)/像素的盒式模糊（积分图，双 cumsum）。边界用 edge 复制。"""
    if r <= 0:
        return a
    h, w = a.shape
    p = np.pad(a, ((r, r), (r, r)), mode="edge")

    c = np.cumsum(p, axis=0, dtype=np.float32)
    c = np.vstack([np.zeros((1, c.shape[1]), np.float32), c])
    s = c[2 * r + 1:, :] - c[:-2 * r - 1, :]          # (h, w+2r)

    c2 = np.cumsum(s, axis=1, dtype=np.float32)
    c2 = np.hstack([np.zeros((c2.shape[0], 1), np.float32), c2])
    out = c2[:, 2 * r + 1:] - c2[:, :-2 * r - 1]       # (h, w)
    return (out / np.float32((2 * r + 1) ** 2)).astype(np.float32)


def guided_filter(guide: np.ndarray, r: int, eps: float) -> np.ndarray:
    """自引导滤波（self-guided）：保边平滑。

    a = var / (var + eps) —— 方差大的区域（强边缘）a→1，滤波退化为恒等，
    因此边缘不被模糊，从根上减少 halo。
    """
    mean_i = _box_blur(guide, r)
    mean_ii = _box_blur(guide * guide, r)
    var = np.maximum(mean_ii - mean_i * mean_i, 0.0)
    a = (var / (var + np.float32(eps))).astype(np.float32)
    b = (mean_i - a * mean_i).astype(np.float32)
    return (_box_blur(a, r) * guide + _box_blur(b, r)).astype(np.float32)


# 双斜率肩的 HDR 段名义落点（线性亮度）：肩曲线在 x=shoulder_span 处到达该值，
# 更亮的像素由 apply 的 ceiling 钳制兜底。取 0.96 而非 ceiling 本身：
# ceiling=1.0 时若让曲线直落 1.0，实测峰值（x≈1.26）附近会直接顶纯白；
# 留 4% 名义余量后，250~255 灰阶只被 x>span 的极亮像素使用（clip_delta +0.01%）。
_SHOULDER_TARGET = 0.96


def _compress_highlights(
    x: np.ndarray,
    start_linear: float,
    shoulder_white: float = 0.93,
    shoulder_span: float = 1.3,
) -> np.ndarray:
    """log2 域双斜率高光肩（2026-10-25 替代指数软膝，单调、保斜率、不收敛）。

    指数软膝（k + s·(1 − e^(−(x−k)/s))）把所有高光收敛到同一渐近线 0，
    叠加 0.96 天花板后实测把 1.5~2.4× 白点的亮度全部拍平在 [0.95, 0.96]。
    双斜率结构（x = log2(L/white)，0 = SDR 白点）：
      x <= 0（SDR 带 [start..1.0]）：y = k + (x−k)·a1，把该带线性压到
           log2(shoulder_white)，a1 = 1 − log2(shoulder_white)/k < 1；
      x >  0（HDR 超白带）        ：y = log2(shoulder_white) + x·a2，
           a2 = (log2(_SHOULDER_TARGET) − log2(shoulder_white)) / span，
           小斜率线性延伸，在 x = span 处到达 _SHOULDER_TARGET。
    两段在 x = 0 处函数值连续（都等于 log2(shoulder_white)）；x = k 以下恒等。
    """
    k = float(np.log2(max(start_linear, 1e-4)))   # 负值（例如 0.78 → -0.358）
    y0 = float(np.log2(max(shoulder_white, 0.5)))
    a1 = 1.0 - y0 / k if k < 0 else 1.0
    a2 = (float(np.log2(_SHOULDER_TARGET)) - y0) / max(shoulder_span, 1e-3)
    below = k + (x - k) * np.float32(a1)
    above = y0 + x * np.float32(a2)
    return np.where(x > 0.0, above, below).astype(np.float32)


# --------------------------------------------------------------------------
# 主函数
# --------------------------------------------------------------------------

def apply(
    hdr_linear: np.ndarray,
    white: float,
    params: LtmParams,
    ceiling: float = 1.0,
) -> tuple[np.ndarray, dict]:
    """把线性 HDR 压到显示线性域（1.0 = SDR 白点）。

    hdr_linear : HxWx3 float32，线性光，1.0 近似 SDR 白
    white      : 归一化白点（高分位亮度），来自调用方
    ceiling    : 高光硬钳制（线性亮度）。1.0 = 允许顶到纯白 —— 配合双斜率肩，
                 sRGB 250→255 是「比白更亮」的唯一去处；纯白占比由验收的
                 clip_delta（≤+1 个百分点）看守。0.96 及以下是旧全局映射时代的
                 保守值，会把肩曲线让出的高光增量又拍平。
    返回 (out_linear, stats)
    """
    h, w = hdr_linear.shape[:2]
    lum = (hdr_linear * _LUMA).sum(axis=2).astype(np.float32)

    # 归一化到白点，转 log2 域。下限 1e-5 防止 log(0)
    L = np.maximum(lum / max(white, 1e-6), 1e-5)
    x = np.log2(L).astype(np.float32)

    r = max(2, int(round(min(h, w) * params.radius_frac)))
    base = guided_filter(x, r, params.eps)
    detail = x - base

    # 先抬暗部（base 向 0 收缩），再压高光 —— 顺序不能反，
    # 否则被压缩的高光会被 range_compress 再次提起，导致白点漂移。
    base_lift = base * np.float32(params.range_compress)
    base_c = _compress_highlights(base_lift, params.highlight_start,
                                  params.shoulder_white, params.shoulder_span)
    x_out = base_c + detail * np.float32(params.detail_gain)

    # 硬钳制到 ceiling（默认 1.0 = 纯白）。双斜率肩已保证 x<=span 时曲线
    # 落在 0.96 以内，这里只拦截 span 之外的极亮像素；纯白占比由验收
    # clip_delta 看守，不再需要「留 4% 余量」那种一刀切收敛。
    x_out = np.minimum(x_out, float(np.log2(max(ceiling, 0.5))))

    L_out = np.exp2(x_out).astype(np.float32)

    # 黑位重锚：压缩会抬起暗部，这里扣掉一个很小的黑位，恢复深黑、消除灰雾
    bp = np.float32(params.black_point)
    L_out = np.clip((L_out - bp) / (1.0 - bp), 0.0, 1.0)

    # 按亮度比例缩放 RGB，保持色相；lum≈0 处退化为 0
    scale = np.where(lum > 1e-7, L_out / np.maximum(lum, 1e-7), 0.0).astype(np.float32)
    out = hdr_linear * scale[..., None]

    stats = {
        "ltm_radius_px": r,
        "ltm_detail_gain": float(params.detail_gain),
        "ltm_range_compress": float(params.range_compress),
        "shadow_lift_x": float(_shadow_lift_factor(x, x_out)),
        "halo_guard": "guided_filter",
    }
    return out.astype(np.float32), stats


def _shadow_lift_factor(x: np.ndarray, x_out: np.ndarray) -> float:
    """暗部（x < -4）的平均亮度提升倍数，用于报告可解释性。"""
    mask = x < -4.0
    if not np.any(mask):
        return 1.0
    return float(np.exp2(np.mean(x_out[mask]) - np.mean(x[mask])))
