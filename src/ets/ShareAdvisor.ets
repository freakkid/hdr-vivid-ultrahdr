/**
 * ShareAdvisor.ets
 * ------------------------------------------------------------------
 * M6：分享引导
 *
 * 原则：
 *   · 只输出"概率"与"原因"，不输出"保证"。
 *   · 文案必须能直接展示给用户，不含技术黑话。
 *   · 校验失败时不允许引导"发原图"，必须先降级。
 *
 * 对应验收项：AC-8（文案与实测一致，未夸大）
 */

import { ValidationReport } from './MetadataValidator';

/* ------------------------------------------------------------------ */
/* 类型                                                               */
/* ------------------------------------------------------------------ */

export type Platform = 'wechat' | 'wechat-moments' | 'qq' | 'airdrop' | 'email' | 'cloud' | 'other';
export type SendMode = 'direct' | 'original' | 'file';
export type Survival = 'high' | 'medium' | 'low' | 'none';

export interface ShareAdvice {
  /** 推荐发送方式 */
  recommended: SendMode;
  /** HDR 存活概率档位（不是保证） */
  survival: Survival;
  /** 存活概率的人类可读标签 */
  survivalLabel: string;
  title: string;
  steps: string[];
  /** 必须随 UI 一起展示的免责说明 */
  disclaimer: string;
  /** 是否建议同时提供 SDR 版本 */
  suggestSdrTwin: boolean;
}

/* ------------------------------------------------------------------ */
/* 主入口                                                             */
/* ------------------------------------------------------------------ */

export function advise(platform: Platform, report: ValidationReport): ShareAdvice {
  // 校验不过：不引导任何"HDR 发送"，直接降级
  if (!report.ok) {
    return {
      recommended: 'direct',
      survival: 'none',
      survivalLabel: '不适用',
      title: '本次已导出为普通照片',
      steps: [],
      disclaimer: 'HDR 自检未通过，已自动降级为 SDR，保证任何设备都能正常显示。',
      suggestSdrTwin: false,
    };
  }

  switch (platform) {
    case 'wechat':
      return {
        recommended: 'file',
        survival: 'high',
        survivalLabel: '较高',
        title: '想保留 HDR，请用「文件」方式发送',
        steps: [
          '在聊天框点右下角「+」',
          '向左滑，选择「文件」',
          '找到这张照片并发送',
          '提醒对方：长按文件 → 保存到相册',
        ],
        disclaimer:
          '微信「直接发图」会重新压缩并清除 HDR 信息，这是微信的处理方式，App 无法绕过。' +
          '「文件」方式按原样传输，是目前保留 HDR 概率最高的做法，但我们无法保证对方设备一定能看到 HDR 效果 —— ' +
          '这取决于对方的手机型号和系统版本。',
        suggestSdrTwin: true,
      };

    case 'wechat-moments':
      return {
        recommended: 'direct',
        survival: 'none',
        survivalLabel: '极低',
        title: '朋友圈会清除 HDR 信息',
        steps: [],
        disclaimer:
          '朋友圈图片会经过平台重新处理，HDR 信息会被清除。' +
          '如需展示 HDR 效果，建议改用「文件」发送或分享到支持 HDR 的相册/网盘。',
        suggestSdrTwin: true,
      };

    case 'qq':
      return {
        recommended: 'file',
        survival: 'medium',
        survivalLabel: '中等',
        title: '建议用「文件」发送，或勾选原图',
        steps: ['优先选择「文件」方式', '若用图片方式，请勾选「原图」'],
        disclaimer:
          'QQ 与微信类似，普通发图会重新压缩。原图方式是否保留 HDR 信息需以实际接收结果为准，我们不对此作出保证。',
        suggestSdrTwin: true,
      };

    case 'airdrop':
    case 'email':
      return {
        recommended: 'original',
        survival: 'high',
        survivalLabel: '较高',
        title: '该方式按原文件传输',
        steps: [],
        disclaimer:
          '传输过程不会改动文件，HDR 信息可完整保留。对方能否看到 HDR 效果取决于其设备与所用软件是否支持。',
        suggestSdrTwin: false,
      };

    case 'cloud':
      return {
        recommended: 'original',
        survival: 'high',
        survivalLabel: '较高',
        title: '网盘/云盘按原文件存储',
        steps: [],
        disclaimer: '下载后 HDR 信息完整保留；在线预览是否显示 HDR 取决于网盘的渲染能力。',
        suggestSdrTwin: false,
      };

    default:
      return {
        recommended: 'original',
        survival: 'medium',
        survivalLabel: '不确定',
        title: '建议以原文件方式分享',
        steps: [],
        disclaimer:
          '不同平台的图片处理策略不同，可能压缩尺寸或清除 HDR 信息。建议以原文件方式发送，并在接收端确认效果。',
        suggestSdrTwin: true,
      };
  }
}

/* ------------------------------------------------------------------ */
/* 导出结果卡片文案（一处生成，避免多端不一致）                        */
/* ------------------------------------------------------------------ */

export interface ExportSummary {
  baseW: number; baseH: number;
  gainW: number; gainH: number;
  fileSizeKB: number;
  hdrOk: boolean;
}

export function summaryLines(s: ExportSummary): string[] {
  const lines: string[] = [];
  lines.push(`主图 ${s.baseW} × ${s.baseH}｜增益图 ${s.gainW} × ${s.gainH}（1:${Math.round(s.baseW / s.gainW)}）`);
  lines.push(`文件大小 ${s.fileSizeKB} KB`);
  lines.push(s.hdrOk
    ? 'HDR 信息已写入并通过自检'
    : 'HDR 自检未通过，已降级为普通照片');
  return lines;
}

/** 必须常驻在导出结果页的一句话（法务/产品共用的标准表述） */
export const STANDING_NOTICE =
  '本文件保留了 HDR 增强信息。能否看到 HDR 效果取决于接收方设备与所用软件；' +
  '第三方平台（如微信、微博）在转发时可能压缩图片并清除 HDR 信息，这不在本 App 的控制范围内。';
