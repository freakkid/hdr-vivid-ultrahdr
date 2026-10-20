/**
 * HdrSourceParser.ets
 * ------------------------------------------------------------------
 * M1：HDR Vivid 源图解析
 *
 * 职责：
 *   · 打开文件，判定容器（jpeg / heif）
 *   · 解码主图（显式保留 HDR 动态范围，不默认降到 SDR）
 *   · 取多图对象 → 辅助图（GAINMAP）
 *   · 读取 Vivid 元数据 / EXIF 摘要
 *   · 输出路由判定（路线 A / 路线 B / 纯 SDR）
 *
 * 注意：Image Kit 的 API 名称与可用版本随 HarmonyOS 版本变化，
 *       标注 [需实测验证] 的调用需在目标 ROM 上先跑通。
 */

import { image } from '@kit.ImageKit';

/* ------------------------------------------------------------------ */
/* 类型                                                               */
/* ------------------------------------------------------------------ */

export type Container = 'jpeg' | 'heif';
export type Route = 'reuse-gainmap' | 'recompute' | 'sdr-only';

export interface GainMapRef {
  /** 增益图位图。可能为单通道或已解为多通道，需在 M3 前转灰度 */
  pixelMap: image.PixelMap;
  width: number;
  height: number;
  /** baseW / gainW，通常 2 或 4 */
  scaleX: number;
  scaleY: number;
  /** 增益图自带的 hdrgm 参数（若源是 Ultra HDR 而非纯 Vivid） */
  hdrgm: Record<string, string> | null;
}

export interface HdrSourceInfo {
  container: Container;
  mimeType: string;
  base: image.PixelMap;
  baseWidth: number;
  baseHeight: number;
  baseIsHdr: boolean;
  /** 主图传输函数：'sdr' | 'pq' | 'hlg' | 'unknown' */
  transfer: 'sdr' | 'pq' | 'hlg' | 'unknown';
  gainMap: GainMapRef | null;
  vividMeta: Record<string, string> | null;
  exif: Record<string, string>;
  route: Route;
  routeReason: string;
}

export interface ParseOptions {
  /** 是否在失败时抛错。false 表示返回 sdr-only 结果（默认） */
  strict?: boolean;
}

/* ------------------------------------------------------------------ */
/* 主入口                                                             */
/* ------------------------------------------------------------------ */

export async function parseSource(fd: number, opt: ParseOptions = {}): Promise<HdrSourceInfo> {
  const source = image.createImageSource(fd);

  let sourceInfo: image.ImageInfo;
  try {
    sourceInfo = await source.getImageInfo();
  } catch (e) {
    return fail(opt, `cannot read image info: ${String(e)}`);
  }

  const mimeType = sourceInfo.mimeType ?? 'image/jpeg';
  const container: Container = /heif|heic/i.test(mimeType) ? 'heif' : 'jpeg';

  // ---- 1. 解码主图：desiredDynamicRange = AUTO 是保留 HDR 的关键 ----
  // 若用默认参数，Image Kit 会走"SDR 解码"路径，HDR 辅助数据直接丢失。
  let base: image.PixelMap;
  try {
    base = await source.createPixelMap({
      desiredDynamicRange: image.DecodeDynamicRange.AUTO, // [需实测验证]
    });
  } catch (e) {
    try {
      base = await source.createPixelMap();
    } catch (e2) {
      return fail(opt, `decode failed: ${String(e2)}`);
    }
  }

  const info = await base.getImageInfo();
  const transfer = detectTransfer(info);
  const baseIsHdr = transfer === 'pq' || transfer === 'hlg';

  // ---- 2. 取增益图（多图对象 → 辅助图）----
  let gainMap: GainMapRef | null = null;
  let gainErr = '';
  try {
    const picture = await source.createPicture({
      desiredDynamicRange: image.DecodeDynamicRange.AUTO, // [需实测验证]
    }); // [需实测验证] API 版本

    const aux = picture.getAuxiliaryPicture(image.AuxiliaryPictureType.GAINMAP); // [需实测验证]

    if (aux) {
      const auxInfo = aux.getAuxiliaryPictureInfo(); // [需实测验证]
      const gm = await aux.getPixelMap();            // [需实测验证]
      if (gm && auxInfo.size.width > 0 && auxInfo.size.height > 0) {
        gainMap = {
          pixelMap: gm,
          width: auxInfo.size.width,
          height: auxInfo.size.height,
          scaleX: info.size.width / auxInfo.size.width,
          scaleY: info.size.height / auxInfo.size.height,
          hdrgm: readHdrgmFromAux(auxInfo),
        };
      }
    }
  } catch (e) {
    gainErr = String(e);
  }

  // ---- 3. 元数据 ----
  const exif = readExifSummary(info);
  const vividMeta = readVividMeta(info);

  // ---- 4. 路由判定 ----
  const { route, routeReason } = decideRoute(baseIsHdr, gainMap, gainErr);

  return {
    container, mimeType,
    base, baseWidth: info.size.width, baseHeight: info.size.height,
    baseIsHdr, transfer, gainMap, vividMeta, exif,
    route, routeReason,
  };
}

/* ------------------------------------------------------------------ */
/* 路由判定表（与设计文档 D.1 的表一致）                                */
/* ------------------------------------------------------------------ */

export function decideRoute(
  baseIsHdr: boolean,
  gainMap: GainMapRef | null,
  gainErr: string,
): { route: Route; routeReason: string } {
  if (gainMap) {
    // 双层图，且增益图语义可判定
    if (gainMap.hdrgm && gainMap.hdrgm['GainMapMax']) {
      return { route: 'reuse-gainmap', routeReason: 'source already carries hdrgm-compatible gain map' };
    }
    // 双层图但增益图语义是 Vivid 自有 —— 默认仍走重算，除非校准表命中
    return { route: 'recompute', routeReason: 'Vivid gain map present but semantics need mapping; recompute by default' };
  }
  if (baseIsHdr) {
    return { route: 'recompute', routeReason: 'single-layer HDR, must tone map to SDR base first' };
  }
  return {
    route: 'sdr-only',
    routeReason: gainErr ? `no gain map (${gainErr}); source is SDR` : 'no gain map; source is SDR',
  };
}

/* ------------------------------------------------------------------ */
/* helpers                                                            */
/* ------------------------------------------------------------------ */

function detectTransfer(info: image.ImageInfo): 'sdr' | 'pq' | 'hlg' | 'unknown' {
  // [需实测验证] ImageInfo 上的动态范围/色彩空间字段名随版本变化。
  // 保守策略：能读到 HDR 标记就信；读不到就按 sdr 处理（宁可降级，不可误判）。
  const anyInfo = info as unknown as Record<string, unknown>;
  const dr = anyInfo['dynamicRange'];
  if (dr == null) return 'unknown';
  const s = String(dr).toUpperCase();
  if (s.includes('HDR') || s.includes('AUTO')) return 'pq';   // 交由 M2 再精判
  return 'sdr';
}

function readHdrgmFromAux(auxInfo: unknown): Record<string, string> | null {
  // 若源本身就是 Ultra HDR / ISO 21496-1，可直接复用其参数（路线 B）
  const any = auxInfo as Record<string, unknown>;
  const meta = any?.['metadata'] as Record<string, string> | undefined;
  if (!meta) return null;
  for (const k of Object.keys(meta)) {
    if (k.startsWith('hdrgm:')) return meta;
  }
  return null;
}

function readVividMeta(info: image.ImageInfo): Record<string, string> | null {
  // Vivid 元数据字段读取方式需按 Image Kit 文档实现 [需实测验证]。
  // 目标是拿到：峰值亮度 nits、SDR 白点、场景最大亮度（供 M2 的 tone map 使用）。
  const any = info as unknown as Record<string, unknown>;
  const out: Record<string, string> = {};
  for (const k of ['hdrPeakNits', 'maxContentLightLevel', 'sdrWhiteNits']) {
    const v = any[k];
    if (v != null) out[k] = String(v);
  }
  return Object.keys(out).length ? out : null;
}

function readExifSummary(info: image.ImageInfo): Record<string, string> {
  const any = info as unknown as Record<string, unknown>;
  const out: Record<string, string> = {};
  for (const k of ['orientation', 'density', 'stride', 'pixelFormat', 'alphaType']) {
    if (any[k] != null) out[k] = String(any[k]);
  }
  // GPS / 设备信息不在此收集；分享态默认剥离（见设计文档 G.3 C1）
  return out;
}

function fail(opt: ParseOptions, msg: string): never {
  // 无法解析时不静默降级：抛给 Pipeline，由 Pipeline 决定是否退化为普通 SDR 导出
  throw new Error(opt.strict ? `[strict] ${msg}` : msg);
}
