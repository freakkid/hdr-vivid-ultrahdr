/**
 * GainMapTransformer.ets
 * ------------------------------------------------------------------
 * M3：增益图几何变换 + 派生缩放 + hdrgm 参数构建
 *
 * 核心设计（对应设计文档 C.3 / D.3）：
 *   1. 唯一变换真源：主图与增益图共用同一个 EditOp，坐标系换算只做一次。
 *   2. 增益图尺寸由主图新尺寸推导，不独立缩放 —— 从构造上消除比例错位。
 *   3. 元数据不做增量修改，编码时全量重算。
 *
 * 对应验收项：AC-4（比例一致、无错位）、AC-5（增益图非截图）
 */

/* ------------------------------------------------------------------ */
/* 类型                                                               */
/* ------------------------------------------------------------------ */

export interface Rect { x: number; y: number; w: number; h: number }

export interface EditOp {
  crop?: Rect;                 // 主图坐标系
  rotate?: 0 | 90 | 180 | 270;
  flipH?: boolean;
  flipV?: boolean;
  targetLongEdge?: number;     // 预压缩目标，固定 2048（见设计文档 C.4）
}

export interface GeometryPlan {
  outW: number;
  outH: number;
  gainOutW: number;
  gainOutH: number;
  baseCrop: Rect;
  gainCrop: Rect;
  rotate: 0 | 90 | 180 | 270;
  flipH: boolean;
  flipV: boolean;
  scale: number;
}

export interface GainMapParams {
  GainMapMin: number;
  GainMapMax: number;
  Gamma: number;
  OffsetSDR: number;
  OffsetHDR: number;
  HDRCapacityMin: number;
  HDRCapacityMax: number;
  BaseRenditionIsHDR: false;
}

export interface GainMapResult {
  pixels: Uint8Array;          // 单通道 8bit，log2 增益量化值
  width: number;
  height: number;
  params: GainMapParams;
  stats: { min: number; max: number; mean: number; allZero: boolean; allFull: boolean };
}

export interface GainMapCfg {
  hdrPeakNits: number;         // 源 HDR 峰值亮度（缺失时兜底 1000）
  sdrWhiteNits: number;        // BT.2408 参考白，203
  gamma: number;               // 默认 1.0
  offsetSdr: number;           // 默认 1/64
  offsetHdr: number;           // 默认 1/64
}

export const DEFAULT_CFG: GainMapCfg = {
  hdrPeakNits: 1000,
  sdrWhiteNits: 203,
  gamma: 1.0,
  offsetSdr: 1 / 64,
  offsetHdr: 1 / 64,
};

/* ------------------------------------------------------------------ */
/* 1. 几何计划求解                                                     */
/* ------------------------------------------------------------------ */

/**
 * @param bw    源主图宽
 * @param bh    源主图高
 * @param gsx   源增益图比例（baseW / gainW），通常 2 或 4
 * @param gsy   源增益图比例（baseH / gainH）
 */
export function solveGeometry(
  op: EditOp,
  bw: number,
  bh: number,
  gsx: number,
  gsy: number,
): GeometryPlan {
  // --- 归一化裁剪框，夹到图像范围内 ---
  const raw = op.crop ?? { x: 0, y: 0, w: bw, h: bh };
  const x0 = clamp(Math.round(raw.x), 0, bw - 1);
  const y0 = clamp(Math.round(raw.y), 0, bh - 1);
  const cw = clamp(Math.round(raw.w), 1, bw - x0);
  const ch = clamp(Math.round(raw.h), 1, bh - y0);
  const baseCrop: Rect = { x: x0, y: y0, w: cw, h: ch };

  // --- 计算缩放（永不放大） ---
  const target = op.targetLongEdge && op.targetLongEdge > 0 ? op.targetLongEdge : 0;
  const scale = target > 0 ? Math.min(1, target / Math.max(cw, ch)) : 1;

  // --- 目标尺寸：取偶，保证 4:2:0 色度块对齐 ---
  const outW = toEven(Math.max(2, Math.round(cw * scale)));
  const outH = toEven(Math.max(2, Math.round(ch * scale)));

  // --- 增益图尺寸：由主图推导（关键）---
  // 用 outW + (gsx-1) 再整除，等价于 ceil(outW/gsx) 但避免浮点
  const gainOutW = toEven(Math.max(2, Math.ceil(outW / gsx)));
  const gainOutH = toEven(Math.max(2, Math.ceil(outH / gsy)));

  // --- 增益图源矩形：主图裁剪框按同一比例映射（向下取整扩边，保证覆盖）---
  const gainCrop: Rect = {
    x: Math.max(0, Math.floor(baseCrop.x / gsx)),
    y: Math.max(0, Math.floor(baseCrop.y / gsy)),
    w: Math.min(Math.ceil(bw / gsx), Math.ceil(baseCrop.w / gsx) + 1),
    h: Math.min(Math.ceil(bh / gsy), Math.ceil(baseCrop.h / gsy) + 1),
  };

  return {
    outW, outH, gainOutW, gainOutH,
    baseCrop, gainCrop,
    rotate: op.rotate ?? 0,
    flipH: !!op.flipH,
    flipV: !!op.flipV,
    scale,
  };
}

/**
 * 旋转 90/270 后宽高互换。注意：必须在 solve 之后、编码之前应用一次，
 * 且 EXIF Orientation 一律归一化为 1（旋转烧进像素），否则会双重旋转。
 */
export function applyRotationToPlan(p: GeometryPlan): GeometryPlan {
  if (p.rotate === 90 || p.rotate === 270) {
    return { ...p, outW: p.outH, outH: p.outW, gainOutW: p.gainOutH, gainOutH: p.gainOutW };
  }
  return p;
}

/* ------------------------------------------------------------------ */
/* 2. 增益图构建（HDR 线性光 vs SDR 基图）                              */
/* ------------------------------------------------------------------ */

/**
 * @param hdrLin  线性 HDR 亮度（float，已几何变换、已缩放到 srcW×srcH），长度 = srcW*srcH
 * @param sdrLin  线性 SDR 亮度（同上）
 * @param srcW    线性图的宽（= GeometryPlan.outW）
 * @param srcH    线性图的高（= GeometryPlan.outH）
 * @param gw,gh   目标增益图尺寸（来自 GeometryPlan.gainOutW/H）
 * @param cfg     参数
 */
export function buildGainMap(
  hdrLin: Float32Array,
  sdrLin: Float32Array,
  srcW: number,
  srcH: number,
  gw: number,
  gh: number,
  cfg: GainMapCfg = DEFAULT_CFG,
): GainMapResult {
  if (hdrLin.length !== srcW * srcH || sdrLin.length !== srcW * srcH) {
    throw new Error(`linear buffer size mismatch: expect ${srcW * srcH}`);
  }
  const gainMaxLog2 = Math.log2(Math.max(1.0001, cfg.hdrPeakNits / cfg.sdrWhiteNits));

  const out = new Uint8Array(gw * gh);
  let min = 255, max = 0, sum = 0;

  for (let gy = 0; gy < gh; gy++) {
    for (let gx = 0; gx < gw; gx++) {
      const ratio = sampleBoxAverage(hdrLin, sdrLin, srcW, srcH, gw, gh, gx, gy, cfg);
      let g = Math.log2(Math.max(ratio, 1e-6)) / gainMaxLog2;
      g = clamp(g, 0, 1);
      if (cfg.gamma !== 1.0) g = Math.pow(g, cfg.gamma);
      const q = Math.round(g * 255);
      out[gy * gw + gx] = q;
      if (q < min) min = q;
      if (q > max) max = q;
      sum += q;
    }
  }

  return {
    pixels: out, width: gw, height: gh,
    params: {
      GainMapMin: 0,
      GainMapMax: gainMaxLog2,
      Gamma: cfg.gamma,
      OffsetSDR: cfg.offsetSdr,
      OffsetHDR: cfg.offsetHdr,
      HDRCapacityMin: 0,
      HDRCapacityMax: gainMaxLog2,
      BaseRenditionIsHDR: false,
    },
    stats: {
      min, max,
      mean: sum / (gw * gh),
      allZero: max === 0,
      allFull: min === 255,
    },
  };
}

/**
 * 盒式平均下采样：把 outW×outH 的线性亮度和按 gw×gh 分块平均。
 * 注意：必须在**线性光**域平均，不能在 8bit sRGB 域平均，否则暗部会偏亮。
 *
 * 生产实现应改为积分图（O(1)/block），此处保留可读性版本。
 */
function sampleBoxAverage(
  hdr: Float32Array, sdr: Float32Array,
  srcW: number, srcH: number,
  gw: number, gh: number, gx: number, gy: number, cfg: GainMapCfg,
): number {
  const bx0 = Math.floor((gx * srcW) / gw), bx1 = Math.max(bx0 + 1, Math.floor(((gx + 1) * srcW) / gw));
  const by0 = Math.floor((gy * srcH) / gh), by1 = Math.max(by0 + 1, Math.floor(((gy + 1) * srcH) / gh));

  let sh = 0, ss = 0, n = 0;
  for (let y = by0; y < by1; y++) {
    const row = y * srcW;
    for (let x = bx0; x < bx1; x++) {
      sh += hdr[row + x];
      ss += sdr[row + x];
      n++;
    }
  }
  const lh = sh / n, ls = ss / n;
  return (lh + cfg.offsetHdr) / (ls + cfg.offsetSdr);
}

/* ------------------------------------------------------------------ */
/* 3. 增益图有效性判定                                                 */
/* ------------------------------------------------------------------ */

/**
 * 全 0 = 无增益（等于纯 SDR）；全 255 = 严重异常。
 * 两者都应判定为无效，走降级纯 SDR 导出，而不是硬写一个坏增益图。
 */
export function isGainMapSane(r: GainMapResult): { ok: boolean; reason?: string } {
  if (r.stats.allZero) return { ok: false, reason: 'gain map is all zero (no HDR headroom detected)' };
  if (r.stats.allFull) return { ok: false, reason: 'gain map is saturated everywhere (invalid source metadata)' };
  if (r.stats.max - r.stats.min < 4) return { ok: false, reason: 'gain map dynamic range too small (< 4 LSB)' };
  return { ok: true };
}

/* ------------------------------------------------------------------ */
/* helpers                                                            */
/* ------------------------------------------------------------------ */

const clamp = (v: number, lo: number, hi: number) => (v < lo ? lo : v > hi ? hi : v);
const toEven = (v: number) => (v % 2 === 0 ? v : v + 1);
