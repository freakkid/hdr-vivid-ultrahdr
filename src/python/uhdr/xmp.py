"""Ultra HDR（GContainer）XMP 构造与解析。

结构参考 Android「Ultra HDR Image Format v1.1」：
  - 主图 XMP 内 <Container:Directory> 列出至少 2 个媒体项（Primary / GainMap）
  - Item:Data 为 base64 编码的 8 字节：4 字节 offset + 4 字节 length（大端）
  - HDR 重建参数放在 hdrgm:* 命名空间（值为 log2 空间的浮点）
"""
from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass, field

XMP_APP1_SIG = b"http://ns.adobe.com/xap/1.0/\x00"

NS_CONTAINER = "http://ns.google.com/photos/1.0/container/"
NS_ITEM = "http://ns.google.com/photos/1.0/container/item/"
NS_HDRGM = "http://ns.adobe.com/hdr-gain-map/1.0/"

SEMANTIC_PRIMARY = "Primary"
SEMANTIC_GAINMAP = "GainMap"


@dataclass
class GainMapMeta:
    """HDR / 增益图重建参数。

    数值语义（与 libultrahdr 输出保持一致）：
      - gain_map_min / gain_map_max / hdr_capacity_* 均以 log2 表示
      - offset_sdr / offset_hdr 为线性域常量，避免除零
      - gamma 必须 > 0
    """

    version: str = "1.0"
    gain_map_min: float = 0.0
    gain_map_max: float = 2.0
    gamma: float = 1.0
    offset_sdr: float = 1.0 / 64.0
    offset_hdr: float = 1.0 / 64.0
    hdr_capacity_min: float = 0.0
    hdr_capacity_max: float = 2.0
    base_rendition_is_hdr: bool = False

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.gamma <= 0:
            errs.append("hdrgm:Gamma 必须 > 0")
        if self.gain_map_max < 0:
            errs.append("hdrgm:GainMapMax 必须 >= 0（log2 空间，对应 boost >= 1.0）")
        if self.hdr_capacity_max < self.hdr_capacity_min:
            errs.append("HDRCapacityMax 不能小于 HDRCapacityMin")
        if self.offset_sdr <= 0 or self.offset_hdr <= 0:
            errs.append("OffsetSDR / OffsetHDR 应 > 0（否则暗部除法不稳定）")
        return errs

    def to_items(self) -> list[tuple[str, str, str]]:
        return [
            ("hdrgm:Version", "str", self.version),
            ("hdrgm:GainMapMin", "float", f"{self.gain_map_min:.6f}"),
            ("hdrgm:GainMapMax", "float", f"{self.gain_map_max:.6f}"),
            ("hdrgm:Gamma", "float", f"{self.gamma:.6f}"),
            ("hdrgm:OffsetSDR", "float", f"{self.offset_sdr:.6f}"),
            ("hdrgm:OffsetHDR", "float", f"{self.offset_hdr:.6f}"),
            ("hdrgm:HDRCapacityMin", "float", f"{self.hdr_capacity_min:.6f}"),
            ("hdrgm:HDRCapacityMax", "float", f"{self.hdr_capacity_max:.6f}"),
            ("hdrgm:BaseRenditionIsHDR", "bool", "True" if self.base_rendition_is_hdr else "False"),
        ]


@dataclass
class ContainerItem:
    mime: str
    semantic: str
    length: int
    offset: int

    @property
    def data_b64(self) -> str:
        return base64.b64encode(struct.pack(">II", self.offset, self.length)).decode("ascii")


@dataclass
class ContainerDirectory:
    items: list[ContainerItem] = field(default_factory=list)

    def by_semantic(self, semantic: str) -> ContainerItem | None:
        for it in self.items:
            if it.semantic.lower() == semantic.lower():
                return it
        return None


def encode_item_data(offset: int, length: int) -> str:
    return base64.b64encode(struct.pack(">II", offset, length)).decode("ascii")


def decode_item_data(b64: str) -> tuple[int, int]:
    raw = base64.b64decode(b64)
    if len(raw) != 8:
        raise ValueError(f"Item:Data 应为 8 字节，实际 {len(raw)}")
    offset, length = struct.unpack(">II", raw)
    return offset, length


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _xmp_doc(attrs: list[tuple[str, str]], directory: ContainerDirectory | None,
             with_packet_header: bool = True) -> str:
    head = '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' if with_packet_header else ""
    tail = '\n<?xpacket end="w"?>' if with_packet_header else ""
    attr_str = "".join(f' {k}="{_esc(v)}"' for k, v in attrs)

    dir_xml = ""
    if directory is not None:
        items = []
        for it in directory.items:
            items.append(
                '      <rdf:li rdf:parseType="Resource">\n'
                '        <Container:Item'
                f' Item:Mime="{_esc(it.mime)}"'
                f' Item:Semantic="{_esc(it.semantic)}"'
                f' Item:Length="{it.length}"'
                f' Item:Data="{it.data_b64}"'
                '/>\n'
                '      </rdf:li>'
            )
        dir_xml = (
            '      <Container:Directory>\n'
            '        <rdf:Seq>\n'
            + "\n".join(items) + "\n"
            '        </rdf:Seq>\n'
            '      </Container:Directory>\n'
        )

    return (
        f'{head}<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '    <rdf:Description rdf:about=""\n'
        f'        xmlns:Container="{NS_CONTAINER}"\n'
        f'        xmlns:Item="{NS_ITEM}"\n'
        f'        xmlns:hdrgm="{NS_HDRGM}"{attr_str}>\n'
        f'{dir_xml}'
        '    </rdf:Description>\n'
        '  </rdf:RDF>\n'
        '</x:xmpmeta>'
        f'{tail}'
    )


def build_primary_xmp(directory: ContainerDirectory, meta: GainMapMeta) -> str:
    """主图 XMP：只放 hdrgm:Version 信号位 + GContainer 目录。

    规范把它当作「这张图是否带增益图」的快速判定入口，不需要扫全文件；
    完整的重建参数写在增益图自身的 XMP 里（见 build_gainmap_xmp）。
    两侧都写是有意的冗余：只写一处时，部分解码器会读不到参数。
    """
    return _xmp_doc([("hdrgm:Version", meta.version)], directory)


def build_gainmap_xmp(meta: GainMapMeta) -> str:
    """增益图自身的 XMP：承载完整 hdrgm 重建参数。"""
    attrs = [(k, v) for k, _t, v in meta.to_items()]
    return _xmp_doc(attrs, None)


def build_xmp(directory: ContainerDirectory, meta: GainMapMeta, *, with_packet_header: bool = True) -> str:
    """兼容入口：主图 + 完整参数（用于需要单处兼容旧读取器的场景）。"""
    if not with_packet_header:
        attrs = [(k, v) for k, _t, v in meta.to_items()]
        return _xmp_doc(attrs, directory, with_packet_header=False)
    return _xmp_doc([(k, v) for k, _t, v in meta.to_items()], directory)


_HDRGM_ATTR_RE = re.compile(r'hdrgm:(\w+)\s*=\s*"([^"]*)"')
_ITEM_RE = re.compile(r"<Container:Item\b([^>]*)/?>", re.S)
_ATTR_RE = re.compile(r'([\w:]+)\s*=\s*"([^"]*)"')


def parse_xmp(xml: str) -> tuple[ContainerDirectory, dict[str, str]]:
    """解析 XMP，返回（目录, hdrgm 原始属性字典）。"""
    hdrgm = {k: v for k, v in _HDRGM_ATTR_RE.findall(xml)}
    directory = ContainerDirectory()
    for raw_attrs in _ITEM_RE.findall(xml):
        attrs = dict(_ATTR_RE.findall(raw_attrs))
        data = attrs.get("Item:Data", "")
        try:
            offset, length = decode_item_data(data) if data else (0, int(attrs.get("Item:Length", "0")))
        except Exception:
            offset, length = 0, 0
        directory.items.append(
            ContainerItem(
                mime=attrs.get("Item:Mime", ""),
                semantic=attrs.get("Item:Semantic", ""),
                length=length,
                offset=offset,
            )
        )
    return directory, hdrgm


def meta_from_dict(d: dict[str, str]) -> GainMapMeta:
    def f(key: str, default: float) -> float:
        try:
            return float(d.get(key, default))
        except (TypeError, ValueError):
            return default

    return GainMapMeta(
        version=d.get("Version", "1.0"),
        gain_map_min=f("GainMapMin", 0.0),
        gain_map_max=f("GainMapMax", 2.0),
        gamma=f("Gamma", 1.0),
        offset_sdr=f("OffsetSDR", 1 / 64),
        offset_hdr=f("OffsetHDR", 1 / 64),
        hdr_capacity_min=f("HDRCapacityMin", 0.0),
        hdr_capacity_max=f("HDRCapacityMax", 2.0),
        base_rendition_is_hdr=str(d.get("BaseRenditionIsHDR", "False")).lower() == "true",
    )
