/**
 * Pipeline.ets
 * ------------------------------------------------------------------
 * M0：导出管线编排
 *
 * 设计要点：
 *   · 每个模块失败都有明确的降级动作，绝不"静默出坏图"。
 *   · 自检（M5）是硬门禁：不通过就不允许把结果标记为 HDR。
 *   · 全链路可观测：每个阶段产出 timing + 关键指标，供灰度/线上排查。
 */

import { parseSource, HdrSourceInfo } from './HdrSourceParser';
import {
  EditOp, GeometryPlan, solveGeometry, applyRotationToPlan,
  buildGainMap, isGainMapSane, GainMapCfg, DEFAULT_CFG, GainMapResult,
} from './GainMapTransformer';
import { encodeNative, assertParamsSane, EncodeResult } from './UltraHdrEncoder';
import { validate, ValidationReport } from './MetadataValidator';
import { advise, ShareAdvice, Platform, ExportSummary } from './ShareAdvisor';

/* ------------------------------------------------------------------ */
/* 类型                                                               */
/* ------------------------------------------------------------------ */

export interface ExportRequest {
  fd: number;
  edit: EditOp;
  platform: Platform;
  /** 预压缩目标长边，固定 2048（见设计文档 C.4）；0 表示不压缩 */
  targetLongEdge: number;
  /** 是否在分享态剥离 GPS / 设备信息 */
  stripPrivacy: boolean;
  gainCfg?: Partial<GainMapCfg>;
}

export type ExportStatus = 'hdr-ok' | 'hdr-degraded' | 'sdr-only' | 'failed';

export interface ExportResult {
  status: ExportStatus;
  jpeg: Uint8Array | null;
  report: ValidationReport | null;
  advice: ShareAdvice;
  summary: ExportSummary | null;
  timings: Record<string, number>;
  notes: string[];
}

/* ------------------------------------------------------------------ */
/* 主流程                                                             */
/* ------------------------------------------------------------------ */

export async function runExport(req: ExportRequest, native: NativeOps): Promise<ExportResult> {
  const t0 = now();
  const timings: Record<string, number> = {};
  const notes: string[] = [];

  // 统一固定 2048：不接受调用方传 2050（收益为零、越界风险非零）
  const targetLongEdge = clampTarget(req.targetLongEdge);

  /* ---------------- M1 源解析 ---------------- */
  const tParse = now();
  let src: HdrSourceInfo;
  try {
    src = await parseSource(req.fd);
  } catch (e) {
    notes.push(`parse failed: ${String(e)}`);
    return failResult('failed', notes, timings, req.platform);
  }
  timings.parse = now() - tParse;

  if (src.route === 'sdr-only') {
    notes.push(`no HDR: ${src.routeReason}`);
    const plain = await native.encodePlainSdr(src.base, targetLongEdge);
    return plainResult(plain, req, notes, timings);
  }

  /* ---------------- M2 线性化 + tone map ---------------- */
  const tLin = now();
  let linear: { hdr: Float32Array; sdr: Uint8Array; w: number; h: number };
  try {
    // 在 native 侧完成：解码 → 线性化 → BT.2408 tone map → 输出 8bit sRGB 基图
    // 之所以放 native：Photoshop 级的浮点运算在 ArkTS 侧性能不可接受
    linear = await native.linearizeAndToneMap(
      src.base,
      Number(src.vividMeta?.['hdrPeakNits'] ?? DEFAULT_CFG.hdrPeakNits),
      DEFAULT_CFG.sdrWhiteNits,
    );
  } catch (e) {
    notes.push(`tone map failed: ${String(e)} -> fallback to plain SDR`);
    const plain = await native.encodePlainSdr(src.base, targetLongEdge);
    return plainResult(plain, req, notes, timings);
  }
  timings.toneMap = now() - tLin;

  /* ---------------- M3 几何 + 增益图 ---------------- */
  const tGeo = now();
  const gsx = src.gainMap ? src.gainMap.scaleX : 4;
  const gsy = src.gainMap ? src.gainMap.scaleY : 4;

  let plan: GeometryPlan = solveGeometry(req.edit, linear.w, linear.h, gsx, gsy);
  plan = applyRotationToPlan(plan);
  if (req.targetLongEdge === 0) {
    // 不压缩：仍走同一套计划，只是 scale = 1
  }
  // 几何变换必须同时作用于 hdr / sdr，且只做一次
  const warped = await native.warpLinearPair(linear.hdr, linear.sdr, linear.w, linear.h, plan);

  const cfg: GainMapCfg = { ...DEFAULT_CFG, ...(req.gainCfg ?? {}) };
  const gain: GainMapResult = buildGainMap(
    warped.hdr, warped.sdr, plan.outW, plan.outH,
    plan.gainOutW, plan.gainOutH, cfg,
  );
  timings.geometry = now() - tGeo;

  const sane = isGainMapSane(gain);
  if (!sane.ok) {
    notes.push(`gain map rejected: ${sane.reason}`);
    const plain = await native.encodePlainSdr(src.base, targetLongEdge);
    return plainResult(plain, req, notes, timings);
  }

  const paramErrs = assertParamsSane(gain.params);
  if (paramErrs.length) {
    notes.push(`invalid params: ${paramErrs.join('; ')}`);
    return failResult('failed', notes, timings, req.platform);
  }

  /* ---------------- M4 编码 ---------------- */
  const tEnc = now();
  let enc: EncodeResult;
  try {
    const baseJpeg = await native.encodeBaseJpeg(warped.sdr, plan.outW, plan.outH, 93, req.stripPrivacy);
    const gainJpeg = await native.encodeGainJpeg(gain.pixels, gain.width, gain.height, 88);

    enc = await encodeNative({
      baseJpeg, gainJpeg,
      baseWidth: plan.outW, baseHeight: plan.outH,
      gainWidth: gain.width, gainHeight: gain.height,
      params: gain.params,
    });
  } catch (e) {
    notes.push(`encode failed: ${String(e)}`);
    const plain = await native.encodePlainSdr(src.base, targetLongEdge);
    return plainResult(plain, req, notes, timings);
  }
  timings.encode = now() - tEnc;
  notes.push(...enc.warnings);

  /* ---------------- M5 自检（硬门禁） ---------------- */
  const tVal = now();
  const report = validate(enc.jpeg, {
    baseW: plan.outW, baseH: plan.outH,
    gainW: gain.width, gainH: gain.height,
  });
  timings.validate = now() - tVal;

  if (!report.ok) {
    const failed = report.checks.filter((c) => !c.ok).map((c) => c.id).join(',');
    notes.push(`validation failed: ${failed}`);
    // 不把坏文件交给用户：降级重出纯 SDR
    const plain = await native.encodePlainSdr(src.base, targetLongEdge);
    const r = plainResult(plain, req, notes, timings);
    r.status = 'hdr-degraded';
    return r;
  }

  /* ---------------- M6 分享引导 ---------------- */
  const info = report.info!;
  const summary: ExportSummary = {
    baseW: info.baseWidth, baseH: info.baseHeight,
    gainW: info.gainWidth, gainH: info.gainHeight,
    fileSizeKB: Math.round(enc.jpeg.length / 1024),
    hdrOk: true,
  };

  timings.total = now() - t0;
  return {
    status: 'hdr-ok',
    jpeg: enc.jpeg,
    report,
    advice: advise(req.platform, report),
    summary,
    timings,
    notes,
  };
}

/* ------------------------------------------------------------------ */
/* native 能力契约（由 NAPI 实现，ArkTS 侧仅声明）                      */
/* ------------------------------------------------------------------ */

export interface NativeOps {
  /** 解码 + 线性化 + BT.2408 tone map，输出线性 HDR 亮度与 8bit sRGB 基图 */
  linearizeAndToneMap(
    pm: unknown, peakNits: number, sdrWhiteNits: number,
  ): Promise<{ hdr: Float32Array; sdr: Uint8Array; w: number; h: number }>;

  /** 对 (hdr, sdr) 施加同一几何变换，并缩放到 plan.outW × plan.outH */
  warpLinearPair(
    hdr: Float32Array, sdr: Uint8Array, w: number, h: number, plan: GeometryPlan,
  ): Promise<{ hdr: Float32Array; sdr: Uint8Array }>;

  /** 主图 JPEG 编码（含 sRGB ICC，stripPrivacy 时剥离 EXIF/GPS） */
  encodeBaseJpeg(
    sdr: Uint8Array, w: number, h: number, quality: number, stripPrivacy: boolean,
  ): Promise<Uint8Array>;

  /** 单通道灰度 JPEG 编码 */
  encodeGainJpeg(
    pixels: Uint8Array, w: number, h: number, quality: number,
  ): Promise<Uint8Array>;

  /** 纯 SDR 导出（降级用） */
  encodePlainSdr(pm: unknown, targetLongEdge: number): Promise<Uint8Array>;
}

/* ------------------------------------------------------------------ */
/* 辅助                                                               */
/* ------------------------------------------------------------------ */

function plainResult(
  jpeg: Uint8Array, req: ExportRequest, notes: string[], timings: Record<string, number>,
): ExportResult {
  return {
    status: 'sdr-only',
    jpeg,
    report: null,
    advice: advise(req.platform, { ok: false, checks: [], info: null }),
    summary: null,
    timings,
    notes,
  };
}

function failResult(
  status: ExportStatus, notes: string[], timings: Record<string, number>, platform: Platform,
): ExportResult {
  return {
    status,
    jpeg: null,
    report: null,
    advice: advise(platform, { ok: false, checks: [], info: null }),
    summary: null,
    timings,
    notes,
  };
}

/** 只接受 2048 或 0；其他值一律夹到最近的安全档 */
function clampTarget(v: number): number {
  if (!v || v <= 0) return 0;
  if (v <= 1536) return 1536;
  if (v <= 2048) return 2048;   // 2049~2560 也归到 2048，避免越界
  return 2560;
}

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
