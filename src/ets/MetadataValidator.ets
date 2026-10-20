/**
 * MetadataValidator.ets
 * ------------------------------------------------------------------
 * M5 元数据完整性校验：独立解析导出的 Ultra HDR JPEG。
 *
 * 设计原则：
 *   1. 不复用 encoder 的任何代码 —— 否则等于"自己给自己判卷"。
 *   2. 纯 Uint8Array 逻辑，不依赖任何平台 API，可在 Node/Python CI 里跑。
 *   3. 只做只读断言，不修复、不改写。
 *
 * 对应验收项：AC-3（A1~A5 五组断言）、AC-5（增益图非截图）
 */

/* ------------------------------------------------------------------ */
/* 类型                                                               */
/* ------------------------------------------------------------------ */

export interface JpegSegment {
  marker: number;          // 0xE1 / 0xE2 ...
  offset: number;          // 段起始（含 0xFF? 不含 —— 指向 marker 字节）
  payloadOffset: number;   // 段负载起点
  payloadLength: number;   // 负载长度（不含长度字段本身）
}

export interface MpfEntry {
  attribute: number;
  size: number;
  offset: number;          // 相对 MP Endian 字段起点
}

export interface CheckItem {
  id: string;              // A1..A5
  ok: boolean;
  detail: string;
}

export interface ValidationReport {
  ok: boolean;
  checks: CheckItem[];
  info: {
    baseWidth: number;
    baseHeight: number;
    gainWidth: number;
    gainHeight: number;
    gainScaleX: number;
    gainScaleY: number;
    fileSize: number;
  } | null;
}

const MPF_IDENT = [0x4d, 0x50, 0x46, 0x00];             // "MPF\0"
const XMP_NS    = 'http://ns.adobe.com/xap/1.0/\u0000';

/* ------------------------------------------------------------------ */
/* 主入口                                                             */
/* ------------------------------------------------------------------ */

export function validate(
  jpeg: Uint8Array,
  expect?: { baseW?: number; baseH?: number; gainW?: number; gainH?: number },
): ValidationReport {
  const checks: CheckItem[] = [];
  const push = (id: string, ok: boolean, detail: string) => checks.push({ id, ok, detail });

  // ---- 0. 基本形态 ----
  if (jpeg.length < 4 || jpeg[0] !== 0xff || jpeg[1] !== 0xd8) {
    push('A0', false, 'not a JPEG (missing SOI)');
    return { ok: false, checks, info: null };
  }
  if (jpeg[jpeg.length - 2] !== 0xff || jpeg[jpeg.length - 1] !== 0xd9) {
    push('A0', false, 'missing EOI');
    return { ok: false, checks, info: null };
  }

  const segs = scanSegments(jpeg);

  // ---- A1: 主图 XMP 必须带 hdrgm:Version（格式信号位）----
  const primaryXmpSeg = segs.find((s) => isXmpSegment(jpeg, s) && s.offset < (findSosOffset(jpeg) ?? jpeg.length));
  const primaryXmp = primaryXmpSeg ? readXmpText(jpeg, primaryXmpSeg) : '';
  const a1 = /hdrgm:Version\s*=\s*["']1\.0["']/.test(primaryXmp);
  push('A1', a1, a1
    ? 'primary XMP carries hdrgm:Version="1.0"'
    : 'primary XMP missing hdrgm:Version -> decoders will treat file as plain SDR');

  // ---- A2: MPF 存在且两个 entry 的 offset/size 自洽 ----
  const mpfSeg = segs.find((s) => isMpfSegment(jpeg, s));
  if (!mpfSeg) {
    push('A2', false, 'no APP2/MPF segment');
    return { ok: false, checks, info: null };
  }

  let entries: MpfEntry[] = [];
  try {
    entries = parseMpfEntries(jpeg, mpfSeg);
  } catch (e) {
    push('A2', false, `MPF parse failed: ${String(e)}`);
    return { ok: false, checks, info: null };
  }

  let a2ok = entries.length === 2;
  const a2detail: string[] = [`entries=${entries.length}`];
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    const end = e.offset + e.size;
    const inRange = e.offset > 0 && end <= jpeg.length;
    const soi = inRange && jpeg[e.offset] === 0xff && jpeg[e.offset + 1] === 0xd8;
    const eoi = inRange && jpeg[end - 2] === 0xff && jpeg[end - 1] === 0xd9;
    if (!inRange || !soi || !eoi) a2ok = false;
    a2detail.push(`[${i}] off=${e.offset} size=${e.size} soi=${soi} eoi=${eoi} attr=0x${e.attribute.toString(16)}`);
  }
  push('A2', a2ok, a2detail.join(' | '));

  // ---- A3: 增益图可独立解码，尺寸与声明一致 ----
  const gainBytes = jpeg.subarray(entries[1].offset, entries[1].offset + entries[1].size);
  const gDim = readJpegDimensions(gainBytes);
  const bDim = readJpegDimensions(jpeg.subarray(entries[0].offset, entries[0].offset + entries[0].size));

  let a3ok = !!gDim && !!bDim;
  let a3msg = '';
  if (a3ok && gDim && bDim) {
    const sx = bDim.width / gDim.width;
    const sy = bDim.height / gDim.height;
    a3msg = `base=${bDim.width}x${bDim.height} gain=${gDim.width}x${gDim.height} scale=${sx.toFixed(4)}x${sy.toFixed(4)}`;

    // 3a. 调用方给了期望尺寸 -> 必须精确匹配（构造保证，不容许 ±1 漂移）
    if (expect?.gainW != null && expect?.gainH != null) {
      if (gDim.width !== expect.gainW || gDim.height !== expect.gainH) {
        a3ok = false;
        a3msg += ` | MISMATCH expected ${expect.gainW}x${expect.gainH}`;
      }
    }
    if (expect?.baseW != null && expect?.baseH != null) {
      if (bDim.width !== expect.baseW || bDim.height !== expect.baseH) {
        a3ok = false;
        a3msg += ` | MISMATCH expected base ${expect.baseW}x${expect.baseH}`;
      }
    }
    // 3b. 比例必须是整数档（2 或 4），且 X/Y 一致
    const near = (v: number, t: number) => Math.abs(v - t) < 1e-6;
    const isStandard = (near(sx, 1) || near(sx, 2) || near(sx, 4)) && near(sx, sy);
    if (!isStandard) {
      a3ok = false;
      a3msg += ' | non-standard or anisotropic gain scale';
    }
  } else {
    a3msg = 'cannot read dimensions from base/gain streams';
  }
  push('A3', a3ok, a3msg);

  // ---- A4: 增益图自带 XMP，hdrgm 字段齐全且不等式成立 ----
  // 规范真正"必填"的只有 Version / GainMapMax / HDRCapacityMax，其余有默认值。
  // 但本方案自产文件完全可控，因此要求**全部显式写出**（更严格，便于排障）。
  // BaseRenditionIsHDR 可省略（默认为 False），但绝不可出现 True。
  const gSegs = scanSegments(gainBytes);
  const gXmpSeg = gSegs.find((s) => isXmpSegment(gainBytes, s));
  const gXmp = gXmpSeg ? readXmpText(gainBytes, gXmpSeg) : '';
  const required = [
    'GainMapMax', 'Gamma', 'OffsetSDR', 'OffsetHDR',
    'HDRCapacityMin', 'HDRCapacityMax',
  ];
  const missing = required.filter((k) => !new RegExp(`hdrgm:${k}\\s*=`).test(gXmp));

  let a4ok = missing.length === 0;
  let a4msg = a4ok ? 'all hdrgm fields present' : `missing: ${missing.join(',')}`;
  if (a4ok) {
    const capMax = num(gXmp, 'HDRCapacityMax');
    const capMin = num(gXmp, 'HDRCapacityMin');
    const gMax = num(gXmp, 'GainMapMax');
    const gMin = gXmp.includes('hdrgm:GainMapMin') ? num(gXmp, 'GainMapMin') : 0;
    if (!(capMax > capMin) || !(capMin >= 0) || !(gMax >= gMin)) {
      a4ok = false;
      a4msg += ` | inequality violated capMax=${capMax} capMin=${capMin} gMax=${gMax} gMin=${gMin}`;
    }
    if (/BaseRenditionIsHDR\s*=\s*["']True["']/.test(gXmp)) {
      a4ok = false;
      a4msg += ' | BaseRenditionIsHDR must be False for SDR base';
    }
    // 增益图必须是灰度（非截图）：检测 SOF 组件的通道数
    const comps = readJpegComponentCount(gainBytes);
    if (comps !== 1) {
      a4ok = false;
      a4msg += ` | gain map has ${comps} components, expected 1 (grayscale)`;
    }
  }
  push('A4', a4ok, a4msg);

  // ---- A5: 主图 ICC 必须是 SDR 描述，不得是 PQ/HLG ----
  const iccSeg = segs.find((s) => isIccSegment(jpeg, s));
  const iccDesc = iccSeg ? describeIcc(jpeg, iccSeg) : '';
  const pqLike = /BT\.?2100|SMPTE.?2084|PQ|HLG|Rec\.?2020/i.test(iccDesc);
  const a5ok = !pqLike;
  push('A5', a5ok, iccSeg
    ? (a5ok ? `ICC="${iccDesc.slice(0, 48)}"` : `ICC looks HDR transfer: "${iccDesc.slice(0, 64)}" -> SDR render will look washed out`)
    : 'no ICC embedded (acceptable if pixels are sRGB, but not recommended)');

  const ok = checks.every((c) => c.ok);
  return {
    ok,
    checks,
    info: bDim && gDim ? {
      baseWidth: bDim.width, baseHeight: bDim.height,
      gainWidth: gDim.width, gainHeight: gDim.height,
      gainScaleX: bDim.width / gDim.width,
      gainScaleY: bDim.height / gDim.height,
      fileSize: jpeg.length,
    } : null,
  };
}

/* ------------------------------------------------------------------ */
/* JPEG 段扫描                                                        */
/* ------------------------------------------------------------------ */

export function scanSegments(b: Uint8Array): JpegSegment[] {
  const out: JpegSegment[] = [];
  let i = 2; // skip SOI
  while (i < b.length - 1) {
    if (b[i] !== 0xff) { i++; continue; }
    let m = b[i + 1];
    // 填充字节
    while (m === 0xff && i + 2 < b.length) { i++; m = b[i + 1]; }
    if (m === 0xd8 || m === 0x01 || (m >= 0xd0 && m <= 0xd7)) { i += 2; continue; }
    if (m === 0xda) {            // SOS：之后是熵编码数据，扫描结束
      out.push({ marker: m, offset: i, payloadOffset: i + 2, payloadLength: 0 });
      break;
    }
    if (m === 0xd9) break;
    const len = (b[i + 2] << 8) | b[i + 3];
    out.push({ marker: m, offset: i, payloadOffset: i + 4, payloadLength: len - 2 });
    i += 2 + len;
  }
  return out;
}

function findSosOffset(b: Uint8Array): number | null {
  const s = scanSegments(b).find((x) => x.marker === 0xda);
  return s ? s.offset : null;
}

function startsWith(b: Uint8Array, off: number, sig: number[]): boolean {
  if (off + sig.length > b.length) return false;
  for (let i = 0; i < sig.length; i++) if (b[off + i] !== sig[i]) return false;
  return true;
}

function startsWithAscii(b: Uint8Array, off: number, sig: string): boolean {
  if (off + sig.length > b.length) return false;
  for (let i = 0; i < sig.length; i++) if (b[off + i] !== sig.charCodeAt(i)) return false;
  return true;
}

const isMpfSegment = (b: Uint8Array, s: JpegSegment) =>
  s.marker === 0xe2 && startsWith(b, s.payloadOffset, MPF_IDENT);

const isXmpSegment = (b: Uint8Array, s: JpegSegment) =>
  s.marker === 0xe1 && startsWithAscii(b, s.payloadOffset, 'http://ns.adobe.com/xap/1.0/');

const isIccSegment = (b: Uint8Array, s: JpegSegment) =>
  s.marker === 0xe2 && startsWithAscii(b, s.payloadOffset, 'ICC_PROFILE');

function readXmpText(b: Uint8Array, s: JpegSegment): string {
  let start = s.payloadOffset + XMP_NS.length;
  const end = s.payloadOffset + s.payloadLength;
  let out = '';
  for (let i = start; i < end; i++) out += String.fromCharCode(b[i]);
  return out;
}

/* ------------------------------------------------------------------ */
/* MPF 解析（CIPA DC-007）                                            */
/* ------------------------------------------------------------------ */

export function parseMpfEntries(b: Uint8Array, s: JpegSegment): MpfEntry[] {
  // payload = "MPF\0" + TIFF header
  const tiff = s.payloadOffset + 4;      // MP Endian 字段起点，offset 基准
  const little = b[tiff] === 0x49 && b[tiff + 1] === 0x49;
  if (!little && !(b[tiff] === 0x4d && b[tiff + 1] === 0x4d)) {
    throw new Error('bad TIFF endian marker');
  }
  const u16 = (o: number) => (little ? b[o] | (b[o + 1] << 8) : (b[o] << 8) | b[o + 1]);
  const u32 = (o: number) => (little
    ? (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0
    : ((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0);

  if (u16(tiff + 2) !== 0x2a) throw new Error('bad TIFF magic');
  const ifd = tiff + u32(tiff + 4);
  const count = u16(ifd);

  let mpEntryOff = -1;
  let mpEntryCount = 0;
  for (let i = 0; i < count; i++) {
    const e = ifd + 2 + i * 12;
    const tag = u16(e);
    const type = u16(e + 2);
    const n = u32(e + 4);
    if (tag === 0xb002) {                // MPEntry
      mpEntryOff = tiff + u32(e + 8);
      mpEntryCount = n;
      void type;
      break;
    }
  }
  if (mpEntryOff < 0) throw new Error('MPEntry (0xB002) not found');

  const out: MpfEntry[] = [];
  for (let i = 0; i < mpEntryCount / 16; i++) {
    const e = mpEntryOff + i * 16;
    out.push({
      attribute: u32(e),
      size: u32(e + 4),
      offset: u32(e + 8),   // 相对 MP Endian 字段起点
    });
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* SOF 尺寸 / 通道数                                                   */
/* ------------------------------------------------------------------ */

function readSof(b: Uint8Array): { width: number; height: number; components: number } | null {
  for (const s of scanSegments(b)) {
    const m = s.marker;
    const isSof = (m >= 0xc0 && m <= 0xcf) && m !== 0xc4 && m !== 0xc8 && m !== 0xcc;
    if (isSof) {
      const o = s.payloadOffset;
      return {
        height: (b[o + 1] << 8) | b[o + 2],
        width: (b[o + 3] << 8) | b[o + 4],
        components: b[o + 5],
      };
    }
  }
  return null;
}

const readJpegDimensions = (b: Uint8Array) => {
  const s = readSof(b);
  return s ? { width: s.width, height: s.height } : null;
};
const readJpegComponentCount = (b: Uint8Array) => readSof(b)?.components ?? -1;

/* ------------------------------------------------------------------ */
/* ICC 描述符（粗粒度，够用即可）                                       */
/* ------------------------------------------------------------------ */

function describeIcc(b: Uint8Array, s: JpegSegment): string {
  const o = s.payloadOffset + 'ICC_PROFILE\u0000'.length + 2; // seq + count
  const end = Math.min(s.payloadOffset + s.payloadLength, o + 4096);
  let out = '';
  for (let i = o; i < end; i++) {
    const c = b[i];
    out += (c >= 32 && c < 127) ? String.fromCharCode(c) : ' ';
  }
  return out;
}

function num(xmp: string, key: string): number {
  const m = new RegExp(`hdrgm:${key}\\s*=\\s*["']([-0-9.eE+]+)["']`).exec(xmp);
  return m ? parseFloat(m[1]) : NaN;
}
