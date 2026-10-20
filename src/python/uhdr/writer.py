"""Ultra HDR JPEG 组装器（纯 Python 参考实现）。

职责：把「SDR 主图 JPEG + 增益图 JPEG + hdrgm 元数据」封装成
「SDR 主图 + XMP(GContainer) + MPF + 增益图副图」的单个 JPEG 文件。

注意：本模块用于结构验证与离线回归，生产环境应以 libultrahdr
（C++ / NDK，API-4 路径）为准，二者产出的容器语义需做逐字节交叉验证。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import jpegseg, mpf, xmp

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

_APP1_XMP_PREFIX = xmp.XMP_APP1_SIG
_APP2_MPF_PREFIX = mpf.MPF_SIG


@dataclass
class UhdrLayout:
    """组装完成后的布局信息，供自检与日志使用。"""

    primary_len: int
    gainmap_len: int
    gainmap_offset: int       # 相对 MPF TIFF 头
    xmp_len: int
    mpf_len: int
    total_len: int


def _app1_xmp(xml_text: str) -> bytes:
    payload = _APP1_XMP_PREFIX + xml_text.encode("utf-8")
    if len(payload) + 2 > 0xFFFF:
        raise ValueError("XMP 段超长（>65533 字节），需精简元数据")
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def strip_container_segments(jpeg: bytes) -> bytes:
    """移除源图里已有的 XMP / MPF 段，保留 EXIF、ICC 等其余段。

    避免出现「两份 Container:Directory」导致读取端取到错误的偏移。
    """
    end = jpegseg.find_image_end(jpeg, 0)
    out = bytearray(jpeg[0:2])
    for seg in jpegseg.iter_header_segments(jpeg, 0):
        payload = jpeg[seg.payload_start:min(seg.payload_end, seg.payload_start + 32)]
        if seg.marker == 0xFFE1 and payload.startswith(_APP1_XMP_PREFIX):
            continue
        if seg.marker == 0xFFE2 and payload.startswith(_APP2_MPF_PREFIX):
            continue
        if seg.marker == jpegseg.SOS:
            out += jpeg[seg.start:end]
            return bytes(out)
        out += jpeg[seg.start:seg.end]
    raise jpegseg.JpegParseError("主图缺少 SOS 段")


def _attach_gainmap_xmp(gainmap_jpeg: bytes, meta: xmp.GainMapMeta) -> bytes:
    """把完整 hdrgm 参数写入增益图自身的 XMP（规范要求参数随增益图走）。"""
    return gainmap_jpeg[:2] + _app1_xmp(xmp.build_gainmap_xmp(meta)) + gainmap_jpeg[2:]


def build_uhdr_jpeg(
    primary_jpeg: bytes,
    gainmap_jpeg: bytes,
    meta: xmp.GainMapMeta | None = None,
) -> tuple[bytes, UhdrLayout]:
    """组装 Ultra HDR JPEG。返回（文件字节, 布局信息）。"""
    meta = meta or xmp.GainMapMeta()
    errs = meta.validate()
    if errs:
        raise ValueError("增益图元数据非法: " + "; ".join(errs))

    if not primary_jpeg.startswith(SOI) or not primary_jpeg.endswith(EOI):
        raise ValueError("主图不是完整的 JPEG（SOI/EOI 缺失）")
    if not gainmap_jpeg.startswith(SOI) or not gainmap_jpeg.endswith(EOI):
        raise ValueError("增益图不是完整的 JPEG（SOI/EOI 缺失）")

    primary = strip_container_segments(primary_jpeg)
    rest = primary[2:]                      # 去掉 SOI，保留 EXIF/ICC/量化表等
    # 增益图侧写入完整 hdrgm 参数；主图侧只留 Version 信号位
    gm = _attach_gainmap_xmp(strip_container_segments(gainmap_jpeg), meta)

    # 两个未知量互相依赖（primary_len 影响 XMP 内数字位数，XMP 长度又影响 primary_len），
    # 采用不动点迭代收敛；通常 2~3 轮即稳定。
    primary_len = 2 + len(rest) + 1024
    gm_offset = 0
    app1 = app2 = b""
    for _ in range(12):
        directory = xmp.ContainerDirectory([
            xmp.ContainerItem("image/jpeg", xmp.SEMANTIC_PRIMARY, primary_len, 0),
            xmp.ContainerItem("image/jpeg", xmp.SEMANTIC_GAINMAP, len(gm), gm_offset),
        ])
        app1 = _app1_xmp(xmp.build_primary_xmp(directory, meta))
        app2 = mpf.wrap_app2(mpf.build_mpf([
            mpf.MpEntry(mpf.ATTR_PRIMARY, primary_len, 0),
            mpf.MpEntry(0, len(gm), gm_offset),
        ]))
        new_primary_len = 2 + len(app1) + len(app2) + len(rest)
        new_gm_offset = new_primary_len - (2 + len(app1) + 8)   # 8 = APP2 头(4) + "MPF\0"(4)
        if (new_primary_len, new_gm_offset) == (primary_len, gm_offset):
            break
        primary_len, gm_offset = new_primary_len, new_gm_offset
    else:
        raise RuntimeError("布局迭代未收敛")

    out = SOI + app1 + app2 + rest + gm
    layout = UhdrLayout(
        primary_len=primary_len,
        gainmap_len=len(gm),
        gainmap_offset=gm_offset,
        xmp_len=len(app1),
        mpf_len=len(app2),
        total_len=len(out),
    )
    if out[primary_len:primary_len + 2] != SOI:
        raise AssertionError(
            f"增益图起点错位：primary_len={primary_len}，实际标记 {out[primary_len:primary_len + 2]!r}"
        )
    tiff_abs = 2 + len(app1) + 8
    if out[tiff_abs + gm_offset:tiff_abs + gm_offset + 2] != SOI:
        raise AssertionError("MPF 记录的增益图偏移未指向 SOI")
    return out, layout
