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
    """

    radius_frac: float = 1.0 / 48.0
    eps: float = 0.2
    range_compress: float = 0.88
    detail_gain: float = 1.30
    black_point: float = 0.0020
    highlight_start: float = 0.78


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
        )
    # local：观感优先
    return LtmParams(
        radius_frac=1.0 / 48.0,
        eps=0.2,
        range_compress=0.88,
        detail_gain=1.32,
        black_point=0.0020,
        highlight_start=0.78,
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


def _compress_highlights(x: np.ndarray, start_linear: float) -> np.ndarray:
    """log2 域高光软膝：start 以下不动作，以上单调收敛到 0（= 白点）。

    关键点：**必须收敛到 0，而不是收敛到 2·knee**。
    早期实现写成 knee - span*(1-exp(...))，会把高光越压越暗、甚至压成负值，
    表现为「高光区比中间调还黑」——这是最容易漏掉的符号错误。

    x_out = k + s·(1 − e^(−(x−k)/s))，其中 k = log2(start)、s = −k。
    在 x = k 处一阶导连续，因此不会出现可见的折线接缝。
    """
    k = float(np.log2(max(start_linear, 1e-4)))   # 负值（例如 0.78 → -0.358）
    if k >= 0.0:
        return np.minimum(x, 0.0).astype(np.float32)
    s = -k
    over = np.maximum(x - k, 0.0)
    rolled = k + s * (1.0 - np.exp(-over / s))
    return np.where(x > k, rolled, x).astype(np.float32)


# --------------------------------------------------------------------------
# 主函数
# --------------------------------------------------------------------------

def apply(
    hdr_linear: np.ndarray,
    white: float,
    params: LtmParams,
) -> tuple[np.ndarray, dict]:
    """把线性 HDR 压到显示线性域（1.0 = SDR 白点）。

    hdr_linear : HxWx3 float32，线性光，1.0 近似 SDR 白
    white      : 归一化白点（高分位亮度），来自调用方
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
    base_c = _compress_highlights(base_lift, params.highlight_start)
    x_out = base_c + detail * np.float32(params.detail_gain)

    # 不允许超过白点
    x_out = np.minimum(x_out, 0.0)

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
