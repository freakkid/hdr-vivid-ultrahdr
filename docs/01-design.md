> **⚠ 2026-10-21 现状更正（以此为准）**
> 本文是实现前的方案论证。实际实现（`src/android/`，纯 Kotlin）采用**路线 B**：
> 复用源增益图直接封装，不做线性光重算；微信原图路径已由代理测试验证字节透传。
> 「MVP 只走路线 A」「微信保留 HDR 不可保证」等表述**已被实测推翻**，
> 详见 `HANDOVER.md` 与 `docs/02-verification.md`。

# 01 — HDR Vivid 静图 → 带增益图的 SDR JPEG（Ultra HDR）技术方案

版本：v1.0 ｜ 日期：2026-10-20 ｜ 状态：待评审
关联：`00-landscape.md`（生态调研）、`src/ets/*`（参考实现骨架）、`tests/acceptance-matrix.md`（测试矩阵）

---

## A. 结论摘要

### A.1 一句话

**"App 内的编码保真"是工程问题，可以做到确定；"微信里的 HDR 存活"是平台策略问题，只能做概率优化。** 本方案的目标是把前者做到可验证、把后者做到可解释。

### A.2 可实现 / 不可保证 / 需外部条件

| 分类 | 具体项 | 判定 | 依据 |
|---|---|---|---|
| ✅ 可实现 | 解析 HDR Vivid 静图，取出主图、增益图、元数据 | 可实现 | Image Kit 提供 `Picture` + `AuxiliaryPicture(GAINMAP)` 多图对象模型 [S13] |
| ✅ 可实现 | 生成结构合规的 Ultra HDR JPEG（SDR 主图 + 增益图 + XMP + MPF） | 可实现，**列为 P0 验收项** | libultrahdr 保证封装正确 [S1]；格式规约有明确字段表 [S2] |
| ✅ 可实现 | 主图与增益图同步几何变换、同步缩放，元数据无错位 | 可实现（本方案用"派生而非独立缩放"的设计从根上消除错位） | 见 C.4 |
| ✅ 可实现 | 导出后字节级自检（XMP/MPF/增益图存在、尺寸匹配、offset 可解析） | 可实现（独立 parser，不复用 encoder 代码） | 见 D.5 |
| ✅ 可实现 | 不支持 HDR 的设备显示为"正常 SDR"，不发灰不过曝 | 可实现，前提是 SDR 基图正确生成 | 见 B.5 "发灰/过曝的四个根因" |
| ⚠️ 有条件 | 支持端的 HDR 观感与源 Vivid 观感"视觉接近" | 只能做到"接近"，**不是无损** | Vivid 动态元数据 → 单一 gain map 静态参数，信息量有损 |
| ⚠️ 有条件 | 华为设备上还原"原生 Vivid 观感" | 不可保证 | 转成 Ultra HDR 后走的是 gain map 通路，不是 Vivid 通路 |
| ❌ 不可保证 | 微信默认发图后仍保留 HDR | 基本不可能 | 微信强制重编码 + 元数据剥离 [S16][S18] |
| ❌ 不可保证 | 微信"勾选原图"后仍保留 HDR | 不可保证，需实测 | 媒体实测称即使勾选原图也被转为 SDR [S16] |
| ❌ 不可保证 | 任意第三方平台保留 XMP/MPF | 不可保证 | 平台重编码即可丢弃非像素数据 |
| 🔗 依赖用户操作 | 微信"文件"方式发送 | 存活概率最高 | 文件入口按二进制透传 [S17] |
| 🔗 依赖平台合作 | 微信内直接渲染 HDR | 需微信支持 gain map 或放开元数据 | 无公开承诺 |

### A.3 三条必须写进产品文案的硬边界

1. **本 App 保证的是"文件是对的"，不是"对方能看到 HDR"。**
2. **增益图 ≠ 截图。** 它是单通道、与主图几何对应、参数写在 XMP 里的辅助图；任何用 RGB 截图冒充的产物在规范上无效。
3. **2048 预压缩解决的是"减少触发压缩的概率"，不解决"元数据被剥离"。** 这两件事必须分开讲（详见 E 节）。

---

## B. 技术方案总览

### B.1 文件封装结构

导出物是一个**普通 Baseline JPEG**，字节布局如下（顺序即写盘顺序）：

```
┌─────────────────────────────────────────────────────────────┐
│ SOI  FF D8                                                   │
├─────────────────────────────────────────────────────────────┤
│ APP0  JFIF（可选，建议保留）                                  │
├─────────────────────────────────────────────────────────────┤
│ APP1  XMP 主图包                                             │
│   · hdrgm:Version = "1.0"          ← 格式"信号位"，必须有     │
│   · GContainer 目录：Item:Semantic="Primary" / "GainMap"      │
│                                    ← v1.1 定位方式 [S2]       │
│   · 常规 EXIF 相关（可保留拍摄信息，分享前可按需剥离）          │
├─────────────────────────────────────────────────────────────┤
│ APP1  EXIF（可选）                                            │
├─────────────────────────────────────────────────────────────┤
│ APP2  ICC（sRGB 或 BT.709）  ← 必须是 SDR 的描述文件          │
├─────────────────────────────────────────────────────────────┤
│ APP2  MPF  ("MPF\0" + TIFF)                                  │
│   · NumberOfImages = 2                                       │
│   · MPEntry[0] = 主图（size / offset）                        │
│   · MPEntry[1] = 增益图（size / offset）                      │
├─────────────────────────────────────────────────────────────┤
│ 主图压缩数据（SDR，8bit，YUV420，Baseline/Progressive）        │
├─────────────────────────────────────────────────────────────┤
│ 增益图 JPEG 流（单通道灰度，8bit）                             │
│   · 自己的 APP1 XMP：完整的 hdrgm 参数                        │
│   · SOI ... EOI 完整独立 JPEG                                 │
├─────────────────────────────────────────────────────────────┤
│ EOI  FF D9                                                   │
└─────────────────────────────────────────────────────────────┘
```

**关键：元数据分两处，职责不同。** 这是最容易做错的地方：

| 位置 | 放什么 | 为什么 |
|---|---|---|
| 主图 XMP | 仅 `hdrgm:Version="1.0"` + GContainer 目录 | 规范用它做"这张图有没有增益图"的快速判定，不需要扫全文件 [S2] |
| 增益图 XMP | 完整参数：`GainMapMin` / `GainMapMax` / `Gamma` / `OffsetSDR` / `OffsetHDR` / `HDRCapacityMin` / `HDRCapacityMax` / `BaseRenditionIsHDR="False"` | 规范规定参数存放在增益图自身的 XMP 包里 [S2] |

只写一处 = 部分解码器识别不到。**本方案两条都写，并在 M5 分别校验。**

### B.2 hdrgm 参数表（必须字段与约束）

| 字段 | 类型 | 本方案取值策略 | 约束 |
|---|---|---|---|
| `hdrgm:Version` | Text | `"1.0"` | 必填 |
| `hdrgm:BaseRenditionIsHDR` | Boolean | `"False"`（主图是 SDR） | **可选**：建议写，但不作为验收门槛；规范未将其列入必需字段，缺失时读取端按默认 `False` 处理 |
| `hdrgm:GainMapMin` | Real | `0.0`（默认） | ≤ GainMapMax |
| `hdrgm:GainMapMax` | Real | 由源 HDR 峰值 / SDR 白点 求 `log2` | 必填，> GainMapMin |
| `hdrgm:Gamma` | Real | `1.0` | > 0 |
| `hdrgm:OffsetSDR` | Real | `0.015625` (1/64) | ≥ 0 |
| `hdrgm:OffsetHDR` | Real | `0.015625` (1/64) | ≥ 0 |
| `hdrgm:HDRCapacityMin` | Real | `max(GainMapMin, 0.0)` | ≥ 0 |
| `hdrgm:HDRCapacityMax` | Real | `= GainMapMax` | > HDRCapacityMin |

> 规约原文建议：单通道值即可，不要用三通道数组，避免多通道偏色 [S2][S6]。

### B.3 主图 / 增益图规格

| 属性 | 主图（SDR base） | 增益图（Gain Map） |
|---|---|---|
| 编码 | JPEG，Baseline 优先 | JPEG，灰度（1 通道） |
| 位深 | 8 bit | 8 bit |
| 色彩 | sRGB / BT.709，**不含 PQ/HLG 传输函数** | 无量色语义（存的是 log2 增益量化值） |
| 色度采样 | 4:2:0 | — |
| 分辨率 | 长边 ≤ 2048（详见 E.2） | 主图 **1/2 或 1/4**（每维），默认 1/4 |
| 质量 | 92–95 | 85–90（增益图对 banding 敏感，不低于 85） |
| ICC | 嵌 sRGB（**不要嵌 PQ**） | 不嵌 |

**为什么主图绝不能嵌 PQ ICC**：市面上有"把 ICC 改成 BT.2100 PQ 让 iPhone 触发 HDR"的做法 [S16]。它确实能在部分 Apple 设备上"亮起来"，但代价是：① 不支持端会把 PQ 数据当 sRGB 解码 → 整张发灰；② 违背"表面普通 SDR"的核心设计目标；③ 微信剥离 ICC 后直接变成错误的灰图。**本方案明确禁止这种做法。**

### B.4 与华为 HDR Vivid 源图的映射关系

华为侧的概念模型是"双层图 / 单层图"：双层图 = 主图 + GainMap 等辅助数据；单层图 = 单一层承载 HDR，可拆分或合成 [S13]。Image Kit 提供单层↔双层互转、HDR 动态元数据生成、色彩空间转换能力 [S12]。

映射不是"复制粘贴"，而是**语义重编码**：

```
HDR Vivid 源
├── 主图（可能是 SDR 基图，也可能是 10bit HDR 单层）
├── 增益图（Vivid 语义：通常是亮度比值/增益系数，可能带 Vivid 自己的量化与曲线）
└── Vivid 动态元数据（场景级曲线、峰值亮度等）
                    │
                    ▼  【映射核心：必须经过线性光空间】
        线性 HDR 辐射度 L_hdr(x,y)         （float，BT.2020 或 BT.709 由源决定）
        线性 SDR 基图 L_sdr(x,y)           （BT.709，参考白 203 nit / BT.2408）
                    │
                    ▼  gain(x,y) = log2( L_hdr / L_sdr )，量化到 0..255
        hdrgm 语义的 8bit 增益图 + hdrgm 参数（GainMapMin/Max 等）
```

**两条路线，MVP 与优化分阶段：**

| | 路线 A：线性光重算（推荐 MVP） | 路线 B：复用源增益图（性能优化） |
|---|---|---|
| 做法 | 解出线性 HDR → 自己 tone map 出 SDR base → 计算 gain → libultrahdr 编码 | 直接取源增益图像素，把源元数据映射成 hdrgm 字段 → libultrahdr **API-4** 封装 [S1] |
| 优点 | 语义完全可控，符合 ISO 21496-1；问题可定位 | 省算力、避免二次量化；无需重新 tone map |
| 缺点 | 需要可靠的 HDR 解码与 tone map，算力较高 | Vivid 增益量子化/曲线与 hdrgm 不完全等价，需逐机型校准；错了就是"整体偏灰/偏亮" |
| 风险 | tone map 参数不当 → SDR 端观感变差 | 语义错配 → 支持端 HDR 效果奇怪，且难归因 |
| 建议 | **默认走 A** | 作为 A 失败或大图性能不足时的降级 |

> **工程判断**：不要一上来做 B。B 的失败模式是"看起来有 HDR，但高光位置不对"，极难定位；A 的失败模式是"SDR 不平"，一眼能看出来。先把 A 做稳。

### B.5 "发灰 / 过曝"的四个根因（验收必查）

| 根因 | 现象 | 检查方法 |
|---|---|---|
| 主图带了 PQ/HLG 传输函数或错误的 ICC | 不支持端整体发灰、对比度塌陷 | M5 检查主图 ICC 描述符 + 主图像素直方图与源 SDR 对比 |
| tone map 把 SDR 白点抬太高 | 不支持端过曝、高光糊成一片 | 检查 clipping 像素比例增量 |
| gain map 的 `GainMapMax` 写得过大 | 支持端高光爆掉、出现"发光边缘" | 用不同 max_display_boost 渲染，观察是否过冲 |
| 主图与增益图几何错位 | 支持端高光"贴错位置"、边缘出现光晕 | 用棋盘格/边缘测试图，检查 gain 边缘与主图边缘是否重合 |

---

## C. 处理管线与模块设计

### C.1 模块划分

```
M0  管线编排 Pipeline
 ├─ M1  HDR 源解析        HdrSourceParser
 ├─ M2  线性化 + ToneMap   HdrLinearizer
 ├─ M3  增益图构建/变换    GainMapBuilder + GainMapTransformer
 ├─ M4  Ultra HDR 编码     UltraHdrEncoder  (→ native libultrahdr)
 ├─ M5  元数据完整性校验    MetadataValidator
 └─ M6  分享引导           ShareAdvisor
```

| 模块 | 职责 | 输入 | 输出 | 失败处置 |
|---|---|---|---|---|
| M1 | 打开文件，判定容器类型；解出主图 PixelMap + 增益图 + Vivid 元数据 + EXIF | 文件 fd / Uri | `HdrSourceInfo` | 非 HDR 源 → 直接走普通 SDR 导出；解析失败 → 报错并给"仅导出 SDR"选项 |
| M2 | 主图解码到线性光；按 BT.2408 参考白 tone map 出 SDR 8bit | PixelMap(HDR) | `SdrBase` + `LinearHdr` | tone map 失败 → 退化为"直接取源 SDR 基图" |
| M3 | 计算/复用增益图；执行与主图完全相同的几何变换；按派生比例缩放 | LinearHdr + SdrBase | `GainMap8bit` + 尺寸 | 计算异常（全 0 / 全 255）→ 判定无效，走纯 SDR 导出 |
| M4 | 组装 hdrgm 参数；调 native 编码；写盘 | SdrBase + GainMap + params | JPEG bytes | 编码失败 → 降级为纯 SDR JPEG + UI 明示 |
| M5 | 独立解析输出字节，逐项断言；不通过则**禁止标记为 HDR 导出** | JPEG bytes | `ValidationReport` | 不通过 → 输出降级文件 + 上报指标 |
| M6 | 按目标平台给出分享路径建议与文案 | ValidationReport + 目标平台 | `ShareAdvice` | — |

### C.2 端到端时序

```
用户点击「导出 HDR」
        │
        ▼
[M1] parseSource(uri)
        │  ├─ container? jpeg | heif
        │  ├─ base PixelMap (desiredDynamicRange = AUTO)
        │  ├─ aux Picture type = GAINMAP → gainmap PixelMap + AuxInfo
        │  └─ vividMeta / exif
        ▼
[M2] linearize(base) ──► L_hdr (float)
     toneMap(L_hdr, targetSdrWhite=203nits) ──► SdrBase (8bit sRGB)
        │
        ▼
[M3] 用户编辑？── 是 ──► 生成唯一 AffineTransform T（裁剪/旋转/镜像/缩放）
        │                    T 同时作用于 SdrBase 与 L_hdr（在浮点域做，避免二次量化）
        │  否 ──► T = identity
        ▼
     统一预压缩：longEdge → 2048（scale = min(1, 2048/max(w,h))）
     主图新尺寸 = round(w*scale) 取偶
     增益图新尺寸 = 由主图新尺寸按固定比例推导（不独立缩放）
        │
        ▼
[M4] buildGainMapParams(L_hdr, SdrBase) → hdrgm fields
     native.encode({ baseJpeg, gainJpeg, params, baseW, baseH, gainScale })
        │
        ▼
[M5] validate(bytes) ── 失败 ──► 降级纯 SDR + 错误上报
        │ 通过
        ▼
[M6] shareAdvice(platform) → 文案 + 路径建议
```

### C.3 编辑期的同步变换策略

**核心原则：单一变换真源（Single Source of Transform）。**

```
不允许出现：
  base.apply(crop(100,100,800,600))
  gainmap.apply(crop(50,50,400,300))   ← 手算坐标，必错
```

正确做法：

```ts
// 唯一真源：在"主图坐标系"里描述一次编辑
interface EditOp {
  crop?: { x: number; y: number; w: number; h: number }; // 主图坐标
  rotate?: 0 | 90 | 180 | 270;
  flipH?: boolean; flipV?: boolean;
  targetLongEdge?: number; // 2048
}

// 一次求解：主图与增益图共用同一组 (源矩形 → 目标矩形) 映射
const plan = GeometryPlan.solve(editOp, srcBaseW, srcBaseH, gainScaleX, gainScaleY);
// plan.baseSrc / plan.gainSrc / plan.outW / plan.outH / plan.gainOutW / plan.gainOutH
```

**三条硬规则：**

1. **几何在"最高精度域"做一次。** 裁剪/旋转先作用于线性的 `L_hdr` 与 `SdrBase`，最后再算 gain。避免"先算 gain 再变换"导致的高光边缘错位。
2. **增益图尺寸由主图推导，不独立缩放。**
   ```ts
   gainOutW = ceil(baseOutW / gainScaleX);  // gainScaleX 为 2 或 4
   gainOutH = ceil(baseOutH / gainScaleY);
   ```
   这样"比例一致"是构造出来的，不需要"事后校准"。
3. **元数据不增量改，编码时全量重算。** MPF 的 size/offset、EXIF 的 `PixelXDimension/PixelYDimension`、`Orientation`（统一归零到 1）、XMP 的 GContainer `Item:Length`，全部在 M4 从最终像素尺寸重新生成。**任何"在原文件上打补丁"的做法都不可接受。**

**旋转的特殊注意**：`Orientation` 一律归一化为 1（Top-Left），把旋转烧进像素。否则会出现"主图转了、增益图没转"或"解码器按 EXIF 又转一次"的双重旋转。

### C.4 预压缩策略（2048 决策）

| 问题 | 结论 |
|---|---|
| 为什么是 2048 | Lightroom Mobile 的 `Small (2048 px)` 预设 [S9] 是业界已用的交付规格；同时长边 2048 的 q92 主图约 400–700 KB，远低于微信原图 25 MB 门槛 [S17] |
| 2048 还是 2050 | **固定 2048。** 2050 没有任何收益：多数平台的尺寸判定是"≤ 阈值"，多 2px 只会增加越界风险；且 2050 非 2 的幂、非标准预设，缩放插值也不友好 |
| 2048 能避免微信压缩吗 | **不能。** 微信默认链路会压到长边约 1080–1440、体积 200–500 KB [S18]。2048 的价值只在"原图/文件"路径下减小体积与上传耗时 |
| 缩放算法 | 主图：线性光域 bicubic / Lanczos3；增益图：在 log2 量化域直接 bicubic（两域结果差异 < 1 LSB，可接受，但**不要用 nearest**） |
| 偶数对齐 | 主图宽高强制取偶（4:2:0 色度块对齐），增益图由主图推导后同样取偶 |
| 放大保护 | `scale = min(1.0, 2048 / max(w, h))`，**永不放大**。原图小于 2048 时保持原尺寸，避免无意义插值 |

### C.5 导出后自检（M5）——五组断言

| # | 断言 | 失败含义 |
|---|---|---|
| A1 | 主图 XMP 存在且含 `hdrgm:Version` | 解码器不会认为这是 HDR 图 |
| A2 | APP2 MPF 存在，`NumberOfImages == 2`，两个 MPEntry 的 `offset+size` 都落在文件内且以 `FFD8` 开头 / `FFD9` 结尾 | 结构损坏，部分解码器可能崩溃或忽略 |
| A3 | 增益图 JPEG 可独立解码，尺寸 == 推导值，且比例与声明一致（误差 0） | 几何错位 |
| A4 | 增益图自带 XMP 含 7 个必需 hdrgm 字段（`GainMapMin` / `GainMapMax` / `Gamma` / `OffsetSDR` / `OffsetHDR` / `HDRCapacityMin` / `HDRCapacityMax`），且满足 `HDRCapacityMax > HDRCapacityMin ≥ 0`、`GainMapMax ≥ GainMapMin`；`BaseRenditionIsHDR` 为可选项，写了必须为 `False` | 参数非法 → 规范要求"忽略增益图，显示 SDR" |
| A5 | 主图 ICC 为 sRGB/BT.709（不含 PQ/HLG）；主图像素与源 SDR 的 ΔE2000 均值 < 3 | 会发灰或偏色 |

**A5 是"不发灰"的唯一量化闸门。** 建议把它做成硬门禁：不通过就不允许把文件标记为「HDR 已导出」。

---

## D. 关键代码 / 伪代码

> 完整骨架见 `src/ets/`。以下为关键片段与契约说明。
> 标注 `// [需实测验证]` 的 API 名称/参数，以 Image Kit 与 libultrahdr 实际版本为准。

### D.1 M1 源解析

```ts
// src/ets/HdrSourceParser.ets
import { image } from '@kit.ImageKit';

export interface GainMapRef {
  pixelMap: image.PixelMap;
  width: number; height: number;
  scaleX: number; scaleY: number;   // baseW / gainW
}

export interface HdrSourceInfo {
  container: 'jpeg' | 'heif';
  base: image.PixelMap;
  baseWidth: number; baseHeight: number;
  baseIsHdr: boolean;
  gainMap: GainMapRef | null;
  vividMeta: Record<string, string> | null;
}

export async function parseSource(fd: number): Promise<HdrSourceInfo> {
  const source = image.createImageSource(fd);

  // 1) 解码主图：显式要求保留 HDR 动态范围，不要默认降到 SDR
  const base = await source.createPixelMap({
    desiredDynamicRange: image.DecodeDynamicRange.AUTO, // [需实测验证] 枚举名
  });
  const info = await base.getImageInfo();
  const baseIsHdr = info.dynamicRange !== image.DynamicRange.SDR; // [需实测验证]

  // 2) 取多图对象 → 辅助图（增益图）
  let gainMap: GainMapRef | null = null;
  try {
    const picture = await source.createPicture({ desiredDynamicRange: image.DecodeDynamicRange.AUTO });
    const aux = picture.getAuxiliaryPicture(image.AuxiliaryPictureType.GAINMAP); // [需实测验证]
    if (aux) {
      const auxInfo = aux.getAuxiliaryPictureInfo();      // [需实测验证]
      const gm = await aux.getPixelMap();                 // [需实测验证]
      gainMap = {
        pixelMap: gm,
        width: auxInfo.size.width,
        height: auxInfo.size.height,
        scaleX: info.size.width / auxInfo.size.width,
        scaleY: info.size.height / auxInfo.size.height,
      };
    }
  } catch (e) {
    // 无增益图：可能是单层 HDR 图，或普通 SDR —— 交由 M2/M3 判断
    gainMap = null;
  }

  return {
    container: info.mimeType.includes('heif') ? 'heif' : 'jpeg',
    base, baseWidth: info.size.width, baseHeight: info.size.height,
    baseIsHdr, gainMap, vividMeta: readVividMeta(info),
  };
}
```

**路由判定表：**

| 源特征 | 处理路径 |
|---|---|
| SDR 源 + 无增益图 | 非 HDR → 直接普通导出，UI 提示"此图无 HDR 信息" |
| 双层图 + 有增益图 | 走路线 B（API-4 直接封装）或路线 A（重算），按配置 |
| 单层 HDR（10bit PQ/HLG）+ 无增益图 | 走路线 A：必须先 tone map 生成 SDR base，再算 gain |

### D.2 M2 线性化 + ToneMap

```ts
// 线性光域处理，全程 float32，避免 8bit 往返损失
export function linearize(pm: PixelMap, tf: TransferFunction): Float32Array { /* PQ / HLG / sRGB EOTF */ }

// BT.2408：SDR 参考白 203 nits；高光按软肩压缩，暗部保真
export function toneMapToSdr(lin: Float32Array, peakNits: number): Uint8Array {
  const SDR_WHITE = 203.0;
  for each pixel:
    const y = luminance(lin);                  // BT.709 权重
    const k = softShoulder(y, SDR_WHITE, peakNits); // 高光软压缩
    out = encodeSrgb(clamp(lin * k, 0, 1));
}
```

`softShoulder` 建议用可调参的 Reinhard 变体或 Khronos Neutral（hdrify 提供参考实现 [S5]），并把参数做成**按场景亮度自适应**：`peakNits` 由源元数据给出，缺失时按 1000 nit 兜底。

### D.3 M3 增益图构建与派生缩放（关键设计）

```ts
// 核心：主图先定尺寸，增益图由比例推导 —— 从构造上消除错位
export interface GeometryPlan {
  outW: number; outH: number;
  gainOutW: number; gainOutH: number;
  baseCrop: Rect; gainCrop: Rect;
  rotate: 0|90|180|270; flipH: boolean; flipV: boolean;
}

export function solve(op: EditOp, bw: number, bh: number, gsx: number, gsy: number): GeometryPlan {
  const c = op.crop ?? { x: 0, y: 0, w: bw, h: bh };
  const scale = op.targetLongEdge ? Math.min(1, op.targetLongEdge / Math.max(c.w, c.h)) : 1;

  // 取偶，保证 4:2:0 对齐
  const outW = even(round(c.w * scale));
  const outH = even(round(c.h * scale));

  // 增益图：按固定比例派生，独立缩放会在取整时产生 ±1px 漂移
  const gainOutW = even(Math.ceil(outW / gsx));
  const gainOutH = even(Math.ceil(outH / gsy));

  // 增益图源矩形 = 主图裁剪框按 gsx/gsy 映射（同一变换，只是坐标系不同）
  const gainCrop = {
    x: Math.floor(c.x / gsx), y: Math.floor(c.y / gsy),
    w: Math.ceil(c.w / gsx),  h: Math.ceil(c.h / gsy),
  };

  return { outW, outH, gainOutW, gainOutH, baseCrop: c, gainCrop, ... };
}

// 增益图量化（hdrgm 语义）
export function buildGainMap(hdr: Float32Array, sdr: Uint8Array,
                             gw: number, gh: number, cfg: GainMapCfg): GainMapResult {
  const gainMax = Math.log2(cfg.hdrPeakNits / cfg.sdrWhiteNits);   // 例：1000/203 ≈ 2.30
  for each pixel (双线性下采样到 gw×gh):
    const r = (lumHdr + 1/64) / (lumSdr + 1/64);
    let g = Math.log2(r) / gainMax;         // 归一到 0..1
    g = Math.pow(clamp(g, 0, 1), cfg.gamma); // gamma 默认 1.0
    out8[i] = round(g * 255);
  return { pixels: out8, width: gw, height: gh,
           params: { GainMapMin: 0, GainMapMax: gainMax, Gamma: 1.0,
                     OffsetSDR: 0.015625, OffsetHDR: 0.015625,
                     HDRCapacityMin: 0, HDRCapacityMax: gainMax } };
}
```

> `+1/64` 即 `OffsetSDR/OffsetHDR` 的默认值，防止暗部除零放大噪声 [S2]。

### D.4 M4 编码（两条路径）

**主路径：native + libultrahdr（推荐）**

```cpp
// src/native/uhdr_bridge.cpp —— NAPI 导出
// 使用 API-4：直接传入已压缩的主图 JPEG + 增益图 JPEG + 元数据
uhdr_error_info_t encodeWithGainMap(const uint8_t* baseJpeg, size_t baseLen,
                                    const uint8_t* gainJpeg, size_t gainLen,
                                    const UhdrGainMapMetadata& meta,
                                    std::vector<uint8_t>& out);
```

```ts
// ArkTS 侧
const jpeg = await uhdrNative.encode({
  baseJpeg, gainJpeg,
  params: { gainMapMin: 0, gainMapMax, gamma: 1.0,
            offsetSdr: 0.015625, offsetHdr: 0.015625,
            hdrCapacityMin: 0, hdrCapacityMax: gainMapMax },
  baseWidth: outW, baseHeight: outH,
  gainWidth: gainOutW, gainHeight: gainOutH,
});
```

**为什么不用纯 ArkTS 拼接**：MPF 的 `Individual Image Attribute` 位定义、APP2 长度回填、XMP 包长（含 padding）都对字节位置极度敏感，手写一次就可能只在部分解码器上崩。**纯 ArkTS 版本只保留为 Phase 0 的学习/降级用途**，代码见 `src/ets/UltraHdrEncoder.ets`。

**降级路径：HarmonyOS 原生封装**

若只需产出"华为生态内可用"的双层图（不要求 Ultra HDR 兼容）：

```ts
const picture = image.createPicture(basePixelMap);
picture.setAuxiliaryPicture(image.AuxiliaryPictureType.GAINMAP, gainAux); // [需实测验证]
await packer.packing(picture, {
  format: 'image/jpeg',
  quality: 95,
  desiredDynamicRange: image.PackingDynamicRange.AUTO, // 关键：保留 HDR
  needsPackProperties: true,                          // 关键：保留多图元数据
});                                                   // [S14][S15]
```

> `needsPackProperties: true` 是"多图元数据是否保留"的开关，漏了就退化成单图 SDR。

### D.5 M5 元数据完整性校验（独立 parser）

```ts
// src/ets/MetadataValidator.ets —— 不复用 encoder 任何代码，避免"自证"
export function validate(jpeg: Uint8Array): ValidationReport {
  const segs = scanSegments(jpeg);              // 遍历 marker，收集 APP1/APP2 区间
  const primaryXmp = segs.find(s => s.isXmp && s.kind === 'primary');
  const mpf        = segs.find(s => s.isMpf);

  // A1
  assert(primaryXmp && /hdrgm:Version\s*=\s*["']1\.0["']/.test(primaryXmp.text));

  // A2 — 解析 MPF，验证两个 entry 的 offset/size 落在文件内且 SOI/EOI 正确
  const entries = parseMpfEntries(jpeg, mpf);   // offset 相对 MP Endian 字段起点
  for (const e of entries) {
    assert(e.offset + e.size <= jpeg.length);
    assert(jpeg[e.offset] === 0xFF && jpeg[e.offset + 1] === 0xD8);
    assert(jpeg[e.offset + e.size - 2] === 0xFF && jpeg[e.offset + e.size - 1] === 0xD9);
  }

  // A3 — 递归解析增益图 JPEG 自身
  const gain = jpeg.subarray(entries[1].offset, entries[1].offset + entries[1].size);
  const gDim = readJpegDimensions(gain);
  assert(gDim.width === expectedGainW && gDim.height === expectedGainH);

  // A4 — 增益图 XMP 必须含全部 hdrgm 字段
  const gXmp = extractXmp(gain);
  for (const k of ['GainMapMax','Gamma','OffsetSDR','OffsetHDR','HDRCapacityMin','HDRCapacityMax'])
    assert(new RegExp(`hdrgm:${k}\\s*=`).test(gXmp));
  assert(parseFloat(field(gXmp,'HDRCapacityMax')) > parseFloat(field(gXmp,'HDRCapacityMin')));

  // A5 — 主图 ICC 必须是 SDR 描述（不做 PQ/HLG）
  assert(!/BT\.2100|PQ|HLG/i.test(describeIcc(segs)));

  return report;
}
```

**实现要点（易错处）：**

- MP Entry 的 `Individual Image Data Offset` **相对 MP Endian 字段起点**（即 `"MPF\0"` 之后那个 `MM`/`II` 的位置），不是相对文件头 [S2]。
- `Individual Image Attribute` 的位定义务必**从参考实现拷贝常量**（libultrahdr / pyultrahdr），不要自己推导；validator 里用 libultrahdr 回读做交叉验证。
- XMP 包长度必须与 APP1 段落长度自洽（含结尾 `\0` 或 padding），否则部分解析器会截断。

### D.6 M6 分享引导

```ts
// src/ets/ShareAdvisor.ets
export interface ShareAdvice {
  recommended: 'file' | 'original' | 'direct';
  survivalLabel: '高' | '中' | '低' | '极低';
  title: string; steps: string[]; disclaimer: string;
}

export function advise(platform: Platform, v: ValidationReport): ShareAdvice {
  if (!v.ok) return { recommended: 'direct', survivalLabel: '极低',
    title: '仅导出为普通照片', steps: [], disclaimer: 'HDR 校验未通过，已降级为 SDR。' };

  switch (platform) {
    case 'wechat':
      return { recommended: 'file', survivalLabel: '高',
        title: '想保留 HDR，请用「文件」方式发送',
        steps: ['聊天框点 +', '左滑选「文件」', '找到这张照片并发送',
                '提醒对方：长按文件 → 保存到相册'],
        disclaimer: '微信普通发图会重新压缩并清除 HDR 信息，这是平台行为，App 无法绕过。' };
    case 'airdrop': case 'email':
      return { recommended: 'original', survivalLabel: '高', ... };
    default:
      return { recommended: 'original', survivalLabel: '中', ... };
  }
}
```

---

## E. 微信分享减害策略

### E.1 先分清两件事（这是整节的前提）

| 问题 | 微信的行为 | 我们能做什么 |
|---|---|---|
| **尺寸/体积压缩** | 默认链路压到长边约 1080–1440、200–500 KB [S18]；原图路径 >25 MB 会降级为 1080p [S17] | 预压缩到 2048，降低触发概率与上传成本 |
| **元数据保留** | 重编码时剥离 EXIF/XMP/ICC [S16][S18]；有媒体实测即使勾选原图也被转为 SDR | **几乎无能为力**，只能改走"文件"路径或换通道 |

**这两件事必须分开对用户讲。** 把"2048 预压缩"说成"HDR 就不丢了"是错误承诺。

### E.2 三条发送路径的预期（写入产品文案）

| 路径 | 微信内部处理 | HDR 存活预期 | 2048 预压缩的作用 | 结论 |
|---|---|---|---|---|
| **A. 聊天框直接发图（相册，不勾原图）** | 强制重编码 + 剥离元数据 | **≈ 0**（基本必然丢失） | 仅改善压缩后的画质，不影响 HDR | 不推荐用于 HDR 交付 |
| **B. 勾选「原图」** | 原图 <25 MB 时走透传；但多方实测仍被转为 SDR [S16] | **低–不确定**（需逐版本实测） | 确保远低于 25 MB，避免"原图过大"降级 | 可尝试，但不能承诺 |
| **C. 「文件」方式发送** | 按二进制透传，不进图像管线 [S17] | **高**（最接近可控） | 减小体积、加快上传 | **主推路径** |
| **D. 非微信通道（AirDrop / 邮件 / 网盘 / iMessage）** | 原样传输 | 高（受接收端能力限制） | 无影响 | 作为兜底话术 |

### E.3 减害工程措施（按性价比排序）

| # | 措施 | 收益 | 成本 |
|---|---|---|---|
| 1 | **默认引导「文件」发送**，并在导出成功后直接弹出该指引 | 最高 | 低（纯 UI） |
| 2 | **导出时生成两个文件**：`xxx_SDR.jpg`（日常发图）+ `xxx_HDR.jpg`（文件发送） | 高，避免"发了但对方看不到"的落差 | 低（多一次编码） |
| 3 | 长边固定 2048 | 中（体积/尺寸双重减害） | 无 |
| 4 | 主图 quality 92、增益图 85–90 | 中（体积可控且不引发 banding） | 无 |
| 5 | 把增益图放文件**尾部**（MPF 指向），减少"只截取头部"的解析器破坏概率 | 低–中 | 无（libultrahdr 默认即如此） |
| 6 | 导出后把文件写进**系统相册的独立相簿**并保留原文件路径 | 中（便于用户从"文件"入口找到） | 低 |
| 7 | 在文件属性/文件名里标注 `_HDR` | 低（仅体验） | 无 |

### E.4 明确不能做的事

- ❌ 不能通过"改 ICC 为 PQ"来强行让微信透出 HDR（会导致不支持端发灰，且被剥离 ICC 后彻底变灰图）。
- ❌ 不能承诺"勾选原图就能保住 HDR"。
- ❌ 不能通过在图片里塞大段自定义 XMP 来"提示"微信（微信不读，且可能增加被判定异常的概率）。
- ❌ 不能声称"发到任何平台都保留 HDR"。

---

## F. 测试计划与验收标准

> 完整可执行矩阵见 `tests/acceptance-matrix.md`，本节给结构与门禁。

### F.1 测试分层

| 层 | 内容 | 自动化 |
|---|---|---|
| L1 单元 | 几何计划求解（含奇数尺寸、极端裁剪、旋转 90/270）、gain 量化边界、MPF 解析器 | ✅ CI |
| L2 Golden-file | 固定样张 → 导出 → 与基准字节做结构比对（不比像素，比段结构与字段值） | ✅ CI |
| L3 跨端渲染 | 各设备/播放器实际观感与 ΔE 度量 | ⚠️ 半自动（需真机集群） |
| L4 传输链路 | 微信/QQ/邮件三种路径接收后 dump 字节 | ❌ 手工（含版本记录） |
| L5 性能/内存 | 4K→2048 全链路耗时、峰值内存 | ✅ CI（真机/模拟器） |

### F.2 样本集（最小可用 12 张）

| # | 场景 | 用途 |
|---|---|---|
| 1–4 | 华为 HDR Vivid：逆光 / 夜景强光源 / 高饱和花卉 / 大面积天空 | 主样本，覆盖高光占比差异 |
| 5 | 纯黑背景 + 单点强光 | 检查 gain 暗部噪声是否被放大 |
| 6 | 高频细节（织物/文字/网格） | 检查增益图缩放是否引起摩尔纹与边缘错位 |
| 7 | 棋盘格合成图 | 检查主图/增益图几何是否严格对齐 |
| 8 | 超宽全景（长边 > 8000） | 检查预压缩与内存 |
| 9 | 超大竖图（长边 > 8000） | 同上 |
| 10 | 普通 SDR JPEG | 负样本，必须提示"无 HDR" |
| 11 | 带 GPS/EXIF 的图 | 检查隐私剥离开关 |
| 12 | iPhone HEIC（Apple gain map） | 交叉验证源解析的健壮性 |

### F.3 验收标准（Gate）

| ID | 验收项 | 判定标准 | 门禁 |
|---|---|---|---|
| AC-1 | 不支持 HDR 的设备显示正常 SDR | 与源 SDR 基图相比 ΔE2000 均值 < 3、P99 < 6；黑位偏移 ≤ 2/255；clipping 像素比例增量 < 0.5% | **阻断** |
| AC-2 | 支持端识别增益图并显示 HDR | 在 Android 14+ / iOS 18+ / Chrome 116+ / Win11 24H2 至少各 1 台设备上肉眼可见高光提升；解析器报告读到 gain map | **阻断** |
| AC-3 | 元数据自检全绿 | A1–A5 五组断言 100% 通过（100 张样张，0 失败） | **阻断** |
| AC-4 | 主图/增益图比例一致 | 缩放比误差 = 0（构造保证）；棋盘格样张无边缘错位；旋转 90/270 后仍对齐 | **阻断** |
| AC-5 | 增益图非截图 | 单通道、8bit、尺寸 == 推导值、独立 JPEG、自带完整 hdrgm 字段 | **阻断** |
| AC-6 | 第三方工具交叉验证 | Adobe Gain Map Demo App 能识别为 gain map 图（并显示 ISO 或 legacy 标识） | 建议 |
| AC-7 | 性能 | 4K 源 → 2048 导出 < 1.5 s（中端机），峰值内存 < 300 MB | 建议 |
| AC-8 | 微信路径预期与文档一致 | 三种路径实测结果的"存活/丢失"与 E.2 表一致，未夸大 | **阻断（文案一致性）** |

### F.4 微信链路测试表（每次实测都要记录版本）

| 用例 | 发送端 | 发送方式 | 接收端 | 检查项 | 记录字段 |
|---|---|---|---|---|---|
| WX-01 | HarmonyOS 机 | 相册直接发 | Android 14 | 接收图 XMP/MPF 是否存在、长边、体积 | 微信版本/系统版本/结果 |
| WX-02 | 同上 | 勾选原图 | Android 14 | 同上 + 是否显示 HDR | 同上 |
| WX-03 | 同上 | 文件发送 | Android 14 | 同上 + 保存到相册后是否仍为 HDR | 同上 |
| WX-04–06 | 同上 | 三种方式 | iOS 18 | 同上 | 同上 |
| WX-07–09 | 同上 | 三种方式 | iOS 17（负样本） | 必须显示正常 SDR，不发灰 | 同上 |
| WX-10–12 | 同上 | 三种方式 | Win11 + Chrome | 同上 | 同上 |
| WX-13 | 同上 | 文件发送 | 微信 PC 端 | 转发后是否保留 | 同上 |
| WX-14 | 同上 | 发朋友圈 | 任意 | 明确记录"预期丢失" | 同上 |

**判定语言规范**：结论只允许写「保留 / 丢失 / 不确定（需复测）」+ 版本号，禁止写「一般能保留」。

---

## G. 风险与兜底

### G.1 技术风险

| ID | 风险 | 概率 | 影响 | 兜底 |
|---|---|---|---|---|
| T1 | 华为不同 ROM/机型导出的 Vivid 图结构不一致，M1 解析失败 | 中 | 高 | 多机型样本库 + 失败即降级纯 SDR 导出，并上报容器结构摘要（不含隐私） |
| T2 | Vivid 增益图语义与 hdrgm 不等价，路线 B 出现整体偏亮/偏灰 | 高 | 高 | **实际 MVP 走路线 B**（复用源增益图直接封装）；路线 A 列为 P2；B 必须过逐机型校准表后才启用 |
| T3 | tone map 参数不适配导致 SDR 端过曝 | 中 | 高 | AC-1 硬门禁；参数按场景亮度自适应；保留"手动选择保守/鲜艳"两档 |
| T4 | MPF/XMP 字节写错，部分解码器崩溃或忽略 | 中 | 高 | 不自研 encoder；validator + libultrahdr 回读交叉验证；灰度发布 |
| T5 | 增益图压缩过度产生 banding，HDR 端出现色阶 | 中 | 中 | 增益图 quality ≥ 85；必要时升到 1/2 分辨率 |
| T6 | 大图（>8000px）内存溢出 | 中 | 中 | 流式/分块处理；预压缩前置到解码后立即执行；OOM 时降级 |
| T7 | 旋转/镜像后 EXIF Orientation 双重应用 | 中 | 中 | Orientation 统一归一化为 1，旋转烧进像素 |

### G.2 平台风险

| ID | 风险 | 概率 | 影响 | 兜底 |
|---|---|---|---|---|
| P1 | 微信剥离 XMP/MPF | **极高** | 高 | 主推「文件」路径；明确文案；提供双文件导出 |
| P2 | 微信"原图"路径也被转码 | 高 | 高 | 不承诺；实测记录版本；引导文件发送 |
| P3 | 微信某版本改变压缩阈值/策略 | 中 | 中 | 季度复测；不把策略硬编码在代码里，做成可远程配置的参数 |
| P4 | 国产 ROM 相册不支持 Ultra HDR | 高 | 中 | 支持矩阵诚实呈现；提供"用 Chrome 打开"或"发送到支持设备"提示 |
| P5 | iOS 18 对 ISO gain map 处理有偏差 [S11] | 中 | 中 | 实测；必要时对 iOS 额外输出 legacy 编码变体 |
| P6 | 华为设备走 Ultra HDR 通路而非 Vivid 通路，观感不一致 | 高 | 中 | 文案说明"华为设备原生 Vivid 观感更佳"；不宣称等同于原生 |

### G.3 合规 / 兼容风险

| ID | 风险 | 兜底 |
|---|---|---|
| C1 | 分享图片携带 GPS/设备信息泄露隐私 | 分享态默认剥离 GPS 与设备序列号；提供开关；UI 明示 |
| C2 | 使用 "HDR Vivid" 商标做宣传可能越界 | 仅作"支持解析"的事实描述，不宣称认证/等同；文案过法务 |
| C3 | ISO 21496-1 / hdrgm 相关专利与许可 | **需法务核实**（见 H-6）；技术上优先使用 libultrahdr（Apache/自有许可）而非自行实现算法细节 |
| C4 | 增益图被误认为"隐藏数据/水印"引发平台风控 | 增益图是公开标准的一部分，不加密、不藏数据；文件名与 EXIF 明确标注 |
| C5 | 用户以为"导出即全平台 HDR" | 首次导出强制展示一次性说明；导出结果页常驻免责声明 |

### G.4 用户体验风险

| ID | 风险 | 兜底 |
|---|---|---|
| U1 | 对方看不到 HDR，用户认为是 App 的问题 | 分享页显示「HDR 存活概率：低/中/高」标签 + 一句话原因 |
| U2 | HDR/SDR 切换时画面闪烁或亮度跳变 | 预览页提供 A/B 对比开关，不做自动切换 |
| U3 | 导出耗时被感知为卡顿 | 进度分阶段（解析/映射/编码/校验），>800ms 显示进度条 |
| U4 | 导出失败只报"失败" | 错误码 + 人话解释 + 可执行下一步（如"改为导出普通照片"） |

---

## H. 待核实问题清单

> 标注「需实测验证」的项，在 Phase 0/1 必须给出结论，否则不得进入对外发布。

| ID | 待核实项 | 为什么重要 | 建议验证方式 | 状态 |
|---|---|---|---|---|
| H-1 | 微信各版本（iOS/Android/PC）对"原图"路径是否真的字节透传 | 决定 E.2 表格中"中/低"的定性 | 发送后从接收端导出文件，做 `xxd` 段扫描，记录版本号 | 需实测 |
| H-2 | 微信默认链路当前的目标长边（1080 / 1440 / 其他）与体积区间 | 决定预压缩策略是否还有优化空间 | 同上，统计长边与体积分布 | 需实测 |
| H-3 | 华为 Vivid 双层图里增益图的量化语义（比值 or 增益系数、是否带曲线） | 决定路线 B 是否可行 | 取样张，用 pylibheif 解出，与路线 A 的 gain 做相关性/残差分析 [S7] | 需实测 |
| H-4 | Vivid 元数据到 hdrgm 的字段映射表（peak nits、白点、gamma） | 路线 B 的核心输入 | 对照 Image Kit 文档 + 实测反解 | 需查文档 + 实测 |
| H-5 | libultrahdr 当前版本的 API-4（直接封装）签名与 `ultrahdr_api.h` 字段名 | native 桥接直接依赖 | 拉取指定 tag 编译，跑 `ultrahdr_app` | 需核实 |
| H-6 | ISO 21496-1 / hdrgm 的专利与许可状态 | 商业合规 | 法务 + 标准文本 | 需法务 |
| H-7 | iOS 18 对 ISO JPEG gain map 的渲染是否真的弱于 Chrome [S11] | 决定是否为 iOS 出变体 | 同一文件在 iPhone 与 Chrome 截图对比 | 需实测 |
| H-8 | 各国产 ROM 相册对 Ultra HDR 的支持情况 | 支持矩阵与文案 | 真机集群抽测 | 需实测 |
| H-9 | HarmonyOS `AuxiliaryPictureType.GAINMAP` 与 `getPixelMap()` 的可用 API 版本 | M1 直接依赖 | 编译 + 真机跑 | 需核实 |
| H-10 | `PackingDynamicRange.AUTO` + `needsPackProperties` 组合下，JPEG 是否真的写出 MPF 增益图 | 降级路径成立性 | 导出后做段扫描 | 需实测 |
| H-11 | 微信是否对含 MPF 的 JPEG 有特殊处理（如判为"多图"而拒绝/转码） | 潜在黑天鹅 | WX-01~03 中专门观察 | 需实测 |

---

## 附：落地路线图

| 阶段 | 目标 | 产出 | 退出条件 |
|---|---|---|---|
| Phase 0（桌面） | 打通"Vivid 样张 → Ultra HDR JPEG" | `src/python/uhdr/`（已实现） | **已完成**：`out/sample.ultrahdr.jpg` 通过 17 项自检（主图 2048×1536、增益图 512×384、比例 1:4、MPF offset 与 XMP 一致、尾部无多余字节）。**尚未完成**：在 Chrome + Android 真机上的显示验证 |
| Phase 1（App 内编码） | M1+M2+M4+M5 跑通 | native 桥 + 校验器 | AC-2/AC-3 通过 |
| Phase 2（几何与压缩） | M3 + 预压缩 | 几何计划 + 派生缩放 | AC-4 通过（棋盘格无错位） |
| Phase 3（分享） | M6 + 双文件导出 + 文案 | 引导页 | AC-8 通过（文案与实测一致） |
| Phase 4（优化） | 路线 B 校准、iOS 变体、性能 | 校准表 | AC-1/AC-7 通过 |

> Phase 0 必须先做。**在桌面证明"文件是对的"，再进 App。** 否则移动端出问题无法区分是封装错还是解析错。
