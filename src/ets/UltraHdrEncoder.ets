/**
 * UltraHdrEncoder.ets
 * ------------------------------------------------------------------
 * M4：Ultra HDR / ISO 21496-1 JPEG 编码
 *
 * 两条路径：
 *   [A] 主路径 —— native + libultrahdr（推荐，API-4：直接封装已压缩主图 + 增益图 + 元数据）
 *   [B] 降级路径 —— HarmonyOS Image Kit 原生 Picture 多图编码（仅华为生态内可读）
 *   [C] 教学/兜底 —— 纯 ArkTS 字节组装（此处只给出 XMP 文本构建与布局常量，
 *       不提供 MPF 写入实现，原因见文档 D.4）
 *
 * 明确立场：**不要自研 MPF 写入器作为主路径**。
 *   MPF 的 Individual Image Attribute 位定义、APP2 长度回填、XMP 包长（含 padding）
 *   都对字节位置极度敏感，手写一次就可能只在部分解码器上崩。
 */

import { image } from '@kit.ImageKit';

/* ------------------------------------------------------------------ */
/* 类型                                                               */
/* ------------------------------------------------------------------ */

export interface HdrgmParams {
  GainMapMin: number;
  GainMapMax: number;
  Gamma: number;
  OffsetSDR: number;
  OffsetHDR: number;
  HDRCapacityMin: number;
  HDRCapacityMax: number;
}

export interface EncodeRequest {
  baseJpeg: Uint8Array;      // SDR 主图，已含 sRGB ICC（由 caller 编码）
  gainJpeg: Uint8Array;      // 单通道灰度 JPEG，增益图
  baseWidth: number;
  baseHeight: number;
  gainWidth: number;
  gainHeight: number;
  params: HdrgmParams;
}

export interface EncodeResult {
  jpeg: Uint8Array;
  path: 'native-libultrahdr' | 'harmony-picture' | 'arkts-assembly';
  warnings: string[];
}

/* ------------------------------------------------------------------ */
/* [A] 主路径：native 桥                                             */
/* ------------------------------------------------------------------ */

/**
 * NAPI 模块契约（对应 src/native/uhdr_bridge.cpp）
 *
 *   encode(baseJpeg: ArrayBuffer, gainJpeg: ArrayBuffer, params: object,
 *          baseW: number, baseH: number, gainW: number, gainH: number)
 *        -> Promise<ArrayBuffer>
 *
 * C++ 侧调用：uhdr_enc_set_compressed_image() + uhdr_enc_set_gainmap_image() 组合（API-4）
 *   —— 具体函数名以目标 tag 的 ultrahdr_api.h 为准 [需实测验证]
 */
declare const uhdrNative: {
  encode(
    baseJpeg: ArrayBuffer, gainJpeg: ArrayBuffer, params: Record<string, number | boolean>,
    baseW: number, baseH: number, gainW: number, gainH: number,
  ): Promise<ArrayBuffer>;
};

export async function encodeNative(req: EncodeRequest): Promise<EncodeResult> {
  const warnings: string[] = [];

  if (req.gainWidth * 2 !== req.baseWidth && req.gainWidth * 4 !== req.baseWidth) {
    warnings.push(`non-standard gain scale: ${req.baseWidth}/${req.gainWidth}`);
  }

  const buf = await uhdrNative.encode(
    toArrayBuffer(req.baseJpeg),
    toArrayBuffer(req.gainJpeg),
    {
      gainMapMin: req.params.GainMapMin,
      gainMapMax: req.params.GainMapMax,
      gamma: req.params.Gamma,
      offsetSdr: req.params.OffsetSDR,
      offsetHdr: req.params.OffsetHDR,
      hdrCapacityMin: req.params.HDRCapacityMin,
      hdrCapacityMax: req.params.HDRCapacityMax,
      baseRenditionIsHdr: false,
      /** 关键：告诉库增益图是 1 通道灰度，避免被当作 3 通道处理 */
      gainMapChannels: 1,
      /** 主图已是最终尺寸，禁止库内再缩放 */
      applyGainMapScale: 0,
    },
    req.baseWidth, req.baseHeight, req.gainWidth, req.gainHeight,
  );

  return { jpeg: new Uint8Array(buf), path: 'native-libultrahdr', warnings };
}

/* ------------------------------------------------------------------ */
/* [B] 降级路径：HarmonyOS Picture 多图编码                            */
/* ------------------------------------------------------------------ */

/**
 * 只保证"华为生态内可读"，不保证 Ultra HDR 兼容（能否被 Android/iOS 读，
 * 取决于系统写出的 XMP 是否带 hdrgm 命名空间 —— 需实测）。
 *
 * 两个参数缺一不可：
 *   needsPackProperties: true           → 保留多图元数据（漏了退化成单图 SDR）
 *   desiredDynamicRange: AUTO           → 保留 HDR 动态范围
 */
export async function encodeHarmonyPicture(
  basePixelMap: image.PixelMap,
  gainAux: image.AuxiliaryPicture,
  quality = 95,
): Promise<EncodeResult> {
  const packer = image.createImagePacker();
  const picture = image.createPicture(basePixelMap);           // [需实测验证]
  picture.setAuxiliaryPicture(image.AuxiliaryPictureType.GAINMAP, gainAux); // [需实测验证]

  const data = await packer.packing(picture, {
    format: 'image/jpeg',
    quality,
    desiredDynamicRange: image.PackingDynamicRange.AUTO,       // [S14]
    needsPackProperties: true,                                  // [S14][S15]
  });

  await packer.release();
  return {
    jpeg: new Uint8Array(data),
    path: 'harmony-picture',
    warnings: ['output may lack hdrgm XMP; run MetadataValidator before claiming HDR'],
  };
}

/* ------------------------------------------------------------------ */
/* [C] XMP 文本构建（这部分可以自研，风险低）                          */
/* ------------------------------------------------------------------ */

const HDRGM_NS = 'http://ns.adobe.com/hdr-gain-map/1.0/';

/**
 * 主图 XMP：只放信号位 + GContainer 目录（v1.1 定位方式）
 * 规范要求：存在增益图时，主图 XMP 必须含 hdrgm:Version。
 */
export function buildPrimaryXmp(baseW: number, baseH: number, gainLen: number): string {
  return `<?xpacket begin="\uFEFF" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:hdrgm="${HDRGM_NS}"
    xmlns:Container="http://ns.google.com/photos/1.0/container/"
    xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
    hdrgm:Version="1.0">
   <Container:Directory>
    <rdf:Seq>
     <rdf:li rdf:parseType="Resource">
      <Container:Item
        Item:Semantic="Primary"
        Item:Mime="image/jpeg"/>
     </rdf:li>
     <rdf:li rdf:parseType="Resource">
      <Container:Item
        Item:Semantic="GainMap"
        Item:Mime="image/jpeg"
        Item:Length="${gainLen}"/>
     </rdf:li>
    </rdf:Seq>
   </Container:Directory>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>`;
  void baseW; void baseH;
}

/**
 * 增益图 XMP：完整参数。规范规定参数存放在增益图自身的 XMP 包里。
 * 全部用单值（不用 rdf:Seq 三通道数组），避免多通道偏色。
 */
export function buildGainMapXmp(p: HdrgmParams): string {
  const f = (v: number) => (Number.isFinite(v) ? v.toFixed(6).replace(/0+$/, '').replace(/\.$/, '') : '0');
  return `<?xpacket begin="\uFEFF" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:hdrgm="${HDRGM_NS}"
    hdrgm:Version="1.0"
    hdrgm:BaseRenditionIsHDR="False"
    hdrgm:GainMapMin="${f(p.GainMapMin)}"
    hdrgm:GainMapMax="${f(p.GainMapMax)}"
    hdrgm:Gamma="${f(p.Gamma)}"
    hdrgm:OffsetSDR="${f(p.OffsetSDR)}"
    hdrgm:OffsetHDR="${f(p.OffsetHDR)}"
    hdrgm:HDRCapacityMin="${f(p.HDRCapacityMin)}"
    hdrgm:HDRCapacityMax="${f(p.HDRCapacityMax)}"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>`;
}

/** 参数自检：不等式必须成立，否则规范要求解码器忽略增益图 */
export function assertParamsSane(p: HdrgmParams): string[] {
  const errs: string[] = [];
  if (!(p.HDRCapacityMax > p.HDRCapacityMin)) errs.push('HDRCapacityMax must be > HDRCapacityMin');
  if (!(p.HDRCapacityMin >= 0)) errs.push('HDRCapacityMin must be >= 0');
  if (!(p.GainMapMax >= p.GainMapMin)) errs.push('GainMapMax must be >= GainMapMin');
  if (!(p.Gamma > 0)) errs.push('Gamma must be > 0');
  if (!(p.OffsetSDR >= 0) || !(p.OffsetHDR >= 0)) errs.push('offsets must be >= 0');
  if (p.GainMapMax <= 0) errs.push('GainMapMax <= 0 means no HDR headroom');
  return errs;
}

/* ------------------------------------------------------------------ */
/* helpers                                                            */
/* ------------------------------------------------------------------ */

function toArrayBuffer(u: Uint8Array): ArrayBuffer {
  // 必须切片，避免把整个底层 buffer（可能远大于视图）传给 native
  return u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength) as ArrayBuffer;
}
