/**
 * HarmonyOS 侧 Ultra HDR 导出与自检。
 *
 * 两条导出通路，按可靠性排序：
 *
 *  A. native 组装（推荐，唯一可保证写出 GContainer XMP + MPF 的路径）
 *     ArkTS 只负责：解码 → 几何变换 → 分别编码主图与增益图 → 交给 libultrahdr API-4
 *
 *  B. 系统 ImagePacker（探索性）
 *     needsPackProperties = true + desiredDynamicRange = AUTO 只保证「HDR 属性」参与编码，
 *     是否写出 Ultra HDR 规定的 XMP/MPF 结构必须实测；若自检不通过，必须回落 A。
 *
 * 绝不允许「不校验就宣称已保留 HDR」。
 */
import { image } from '@kit.ImageKit';
import { fileIo } from '@kit.CoreFileKit';
// 自编译的 NAPI 模块，内部即 libultrahdr（见 src/android/app/src/main/cpp 的同构实现）
import uhdrNative from 'libuhdr.so';
import { HdrSource, SourceContainer } from './HdrSourceReader';

export interface ExportOptions {
  longEdge: number;          // 2048 / 2050
  baseQuality: number;       // 92
  gainMapQuality: number;    // 95
  maxBoostLog2: number;      // 2.0 => 4x
  gamma: number;
}

export interface CheckItem {
  id: string;
  ok: boolean;
  detail: string;
}

export interface ExportReport {
  ok: boolean;
  checks: CheckItem[];
  bytes: ArrayBuffer | null;
  warnings: string[];
}

const DEFAULT_OPTIONS: ExportOptions = {
  longEdge: 2048,
  baseQuality: 92,
  gainMapQuality: 95,
  maxBoostLog2: 2.0,
  gamma: 1.0,
};

function targetSize(w: number, h: number, longEdge: number): [number, number] {
  const cur = Math.max(w, h);
  if (cur <= longEdge) return [w, h];
  const k = longEdge / cur;
  return [Math.max(1, Math.round(w * k)), Math.max(1, Math.round(h * k))];
}

/** 主图与增益图同步缩放（增益图按原始比例换算，绝不由主图反推）。 */
async function resizePair(
  base: image.PixelMap, gm: image.PixelMap, longEdge: number,
): Promise<[image.PixelMap, image.PixelMap]> {
  const bi = base.getImageInfoSync();
  const gi = gm.getImageInfoSync();
  const [bw, bh] = targetSize(bi.size.width, bi.size.height, longEdge);
  const kx = gi.size.width / bi.size.width;
  const ky = gi.size.height / bi.size.height;
  const gw = Math.max(1, Math.round(bw * kx));
  const gh = Math.max(1, Math.round(bh * ky));

  const baseOut = await base.scale(bw / bi.size.width, bh / bi.size.height);
  const gmOut = await gm.scale(gw / gi.size.width, gh / gi.size.height);
  return [baseOut, gmOut];
}

async function encodeJpeg(pm: image.PixelMap, quality: number): Promise<ArrayBuffer> {
  const packer = image.createImagePacker();
  try {
    return await packer.packToData(pm, { format: 'image/jpeg', quality });
  } finally {
    await packer.release();
  }
}

/** 结构性自检：必须由 native 复核，ArkTS 层不做「猜」。 */
function validateStructure(jpeg: ArrayBuffer): CheckItem[] {
  const r = uhdrNative.probeStructure(jpeg);
  return [
    { id: 'xmp_present', ok: r.hasXmp, detail: '主图含 XMP(Container:Directory)' },
    { id: 'mpf_present', ok: r.hasMpf, detail: '主图含 MPF APP2' },
    { id: 'gainmap_item', ok: r.hasGainMapItem, detail: 'XMP 含 Semantic=GainMap' },
    { id: 'offset_ok', ok: r.offsetPointsToSoi, detail: 'MPF 偏移指向副图 SOI' },
    { id: 'aspect_ok', ok: r.aspectConsistent, detail: '主图/增益图比例一致' },
  ];
}

export async function exportUltraHdr(
  src: HdrSource, opts: Partial<ExportOptions> = {},
): Promise<ExportReport> {
  const o: ExportOptions = { ...DEFAULT_OPTIONS, ...opts };
  const warnings: string[] = [...src.notes];

  if (!src.gainMap) {
    return {
      ok: false,
      bytes: null,
      checks: [{ id: 'gainmap', ok: false, detail: '源图无增益图' }],
      warnings: ['源文件不含 GAINMAP，只能导出纯 SDR'],
    };
  }

  const [base, gm] = await resizePair(src.base, src.gainMap, o.longEdge);
  const baseJpeg = await encodeJpeg(base, o.baseQuality);
  const gmJpeg = await encodeJpeg(gm, o.gainMapQuality);

  if (src.container === SourceContainer.CUVA) {
    warnings.push('CUVA 源：当前按原增益图直接封装，色彩/亮度可能存在偏差（建议反解重算）');
  }

  const out = uhdrNative.encode(
    baseJpeg, gmJpeg,
    1.0, Math.pow(2, o.maxBoostLog2), o.gamma,
    o.baseQuality, o.gainMapQuality,
  );

  if (!out) {
    return { ok: false, bytes: null, checks: [{ id: 'encode', ok: false, detail: uhdrNative.lastError() }], warnings };
  }

  const checks = validateStructure(out);
  const ok = checks.every((c) => c.ok);
  if (!ok) warnings.push('自检未通过：文件已退化为 SDR，请勿向用户宣称 HDR 已保留');
  return { ok, checks, bytes: out, warnings };
}

/**
 * 探索性路径：尝试让系统 packer 直接把 Picture 编码为 JPEG。
 * 用途是实测「系统是否原生输出 Ultra HDR 结构」，结果写入兼容性台账。
 */
export async function trySystemPacker(uri: string): Promise<ExportReport> {
  const raw = await (async (): Promise<ArrayBuffer> => {
    const f = fileIo.openSync(uri, fileIo.OpenMode.READ_ONLY);
    try {
      const st = fileIo.statSync(f.fd);
      const buf = new ArrayBuffer(st.size);
      fileIo.readSync(f.fd, buf);
      return buf;
    } finally {
      fileIo.closeSync(f);
    }
  })();

  const source = image.createImageSource(raw);
  const picture = await source.createPicture();
  const packer = image.createImagePacker();
  try {
    const bytes = await packer.packToData(picture, {
      format: 'image/jpeg',
      quality: 95,
      desiredDynamicRange: image.PackingDynamicRange.AUTO,
      needsPackProperties: true,
    } as image.PackingOption);
    const checks = validateStructure(bytes);
    return {
      ok: checks.every((c) => c.ok),
      bytes,
      checks,
      warnings: ['系统 packer 路径为探索性实现，结果需写入实测台账'],
    };
  } finally {
    await packer.release();
    await source.release();
    await picture.release();
  }
}
