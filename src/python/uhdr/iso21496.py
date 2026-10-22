"""ISO 21496-1 增益图元数据（APP2 二进制形式）解析与构造。

与 Google 的 XMP GContainer 是**两套并存的元数据载体**：
  - XMP 形式：`<Container:Directory>` + `hdrgm:*`，可读性好，Android 14 早期实现依赖它
  - ISO 形式：JPEG APP2 段，签名 `urn:iso:std:iso:ts:21496:-1\\0`，二进制、体积小

实测（华为 Mate 30 收到的样张）两者可以只出现 ISO 形式而完全没有 XMP——
只认 XMP 的解析器会把这类文件误判成「无增益图」。

字段布局（实测样张，89 字节净荷）：
    sig(28) + header(5) + 7 × fraction(8)
每个 fraction = 大端 i32 分子 + u32 分母，实测分母为 1000000。

注意：header 的 5 字节语义与字段顺序尚未与标准原文逐字比对（见待核实项 H-11），
因此解析后必须做**语义合理性校验**，不通过则回退默认值并标注，绝不硬用。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

ISO_SIG = b"urn:iso:std:iso:ts:21496:-1\x00"
ISO_HEADER_LEN = 5
FRACTION_COUNT = 7
FRACTION_SIZE = 8
# 完整参数块的最小长度：header + 7 个 fraction。门槛必须按这个算，
# 否则 body 落在 13~60 字节的畸形段会走进循环并抛 ValueError，直接打断整条导出。
MIN_BODY_LEN = ISO_HEADER_LEN + FRACTION_COUNT * FRACTION_SIZE
DEFAULT_DENOMINATOR = 1_000_000


@dataclass
class IsoGainMapMeta:
    """ISO 21496-1 增益图参数（线性域，与 hdrgm 的 log2 域不同）。"""

    raw_values: list[float] = field(default_factory=list)
    min_content_boost: float = 1.0
    max_content_boost: float = 4.0
    gamma: float = 1.0
    offset_sdr: float = 1.0 / 64.0
    offset_hdr: float = 1.0 / 64.0
    hdr_capacity_min: float = 1.0
    hdr_capacity_max: float = 4.0
    header_bytes: bytes = b""
    notes: list[str] = field(default_factory=list)

    def is_plausible(self) -> bool:
        """语义合理性：不合理的值说明字段顺序假设错了。"""
        if not (0.1 <= self.gamma <= 4.0):
            return False
        if self.max_content_boost < 1.0:
            return False
        if self.min_content_boost <= 0 or self.min_content_boost > self.max_content_boost:
            return False
        if self.hdr_capacity_max < self.hdr_capacity_min:
            return False
        return True

    def to_log2_dict(self) -> dict[str, float]:
        """转换为 hdrgm（log2 域）等价参数。"""
        import math

        def lg(v: float) -> float:
            return math.log2(v) if v > 0 else 0.0

        return {
            "GainMapMin": lg(self.min_content_boost),
            "GainMapMax": lg(self.max_content_boost),
            "Gamma": self.gamma,
            "OffsetSDR": self.offset_sdr,
            "OffsetHDR": self.offset_hdr,
            "HDRCapacityMin": lg(self.hdr_capacity_min),
            "HDRCapacityMax": lg(self.hdr_capacity_max),
        }


def _read_fraction(buf: bytes, at: int) -> tuple[float, int, int]:
    if at + 8 > len(buf):
        raise ValueError("fraction 越界")
    num = struct.unpack(">i", buf[at:at + 4])[0]
    den = struct.unpack(">I", buf[at + 4:at + 8])[0]
    if den == 0:
        return 0.0, num, den
    return num / den, num, den


def parse_app2(payload: bytes) -> IsoGainMapMeta | None:
    """解析 ISO 21496-1 APP2 净荷；只含 version 块时返回 None（表示参数在别处）。"""
    if not payload.startswith(ISO_SIG):
        return None
    body = payload[len(ISO_SIG):]
    if len(body) < MIN_BODY_LEN:
        # 实测主图侧只有 4 字节 version 块，完整参数写在增益图自身的 APP2 里
        return None

    meta = IsoGainMapMeta(header_bytes=body[:ISO_HEADER_LEN])
    values = []
    for k in range(FRACTION_COUNT):
        # 逐项再验一次边界：第三方文件可能是「声明 7 项但实际被截断」的畸形段，
        # 这里必须降级为「无参数」而不是抛异常（与 Kotlin IsoGainMap 一致）。
        try:
            v, _num, _den = _read_fraction(body, ISO_HEADER_LEN + k * FRACTION_SIZE)
        except ValueError:
            return None
        values.append(round(v, 6))
    meta.raw_values = values

    # 先按标准字段顺序赋值：min, max, gamma, offset_sdr, offset_hdr, cap_min, cap_max
    (meta.min_content_boost, meta.max_content_boost, meta.gamma,
     meta.offset_sdr, meta.offset_hdr, meta.hdr_capacity_min,
     meta.hdr_capacity_max) = values

    if not meta.is_plausible():
        # 实测样本（华为 Mate 30 收到的 ISO Ultra HDR）原始值为
        #   [0, 2.148, 0, 2.169, 1.0, 0, 0]
        # 与「min, max, gamma, offset_sdr, offset_hdr, cap_min, cap_max」的顺序不符，
        # 但前两位显然对应 min/max（log2 域），第 5 位才是 1.0（即默认 gamma）。
        # 因此按**实测布局**回退：前两位当 log2 的 min/max，gamma 保守取 1.0。
        meta.notes.append(
            "字段顺序与标准假设不一致，按实测布局回退（原始值: "
            + ", ".join(f"{v:g}" for v in values) + "）；gamma 保守取 1.0"
        )
        v0, v1 = values[0], values[1]
        if 0.0 <= v0 < 8.0 and v0 < v1 <= 8.0:
            import math

            meta.min_content_boost = 2.0 ** v0
            meta.max_content_boost = 2.0 ** v1
        else:
            meta.min_content_boost = 1.0
            meta.max_content_boost = 4.0
        meta.gamma = 1.0
        meta.offset_sdr = 1.0 / 64.0
        meta.offset_hdr = 1.0 / 64.0
        meta.hdr_capacity_min = meta.min_content_boost
        meta.hdr_capacity_max = meta.max_content_boost
    return meta


def build_app2(meta: IsoGainMapMeta, *, version_block_only: bool = False) -> bytes:
    """构造 APP2 净荷。version_block_only=True 时只写 4 字节版本块（主图侧）。"""
    if version_block_only:
        return ISO_SIG + b"\x00\x00\x00\x00"
    body = bytearray(meta.header_bytes or b"\x00\x00\x00\x00\x40")
    values = [
        meta.min_content_boost,
        meta.max_content_boost,
        meta.gamma,
        meta.offset_sdr,
        meta.offset_hdr,
        meta.hdr_capacity_min,
        meta.hdr_capacity_max,
    ]
    for v in values:
        num, den = _to_fraction(v)
        body += struct.pack(">iI", num, den)
    return ISO_SIG + bytes(body)


def _to_fraction(v: float, max_den: int = DEFAULT_DENOMINATOR) -> tuple[int, int]:
    """用连分数逼近，返回最简分数（与 libultrahdr 的做法一致）。"""
    from fractions import Fraction

    f = Fraction(v).limit_denominator(max_den)
    return f.numerator, f.denominator


def is_iso_app2(payload: bytes) -> bool:
    return payload.startswith(ISO_SIG)
