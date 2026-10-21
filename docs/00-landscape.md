# 00 — 线上生态调研：HDR Vivid → Ultra HDR / Gain Map

> 调研时间：2026-10（检索结果标注为该时点可见内容）
> 目的：确认本项目在现有生态中的位置、可直接复用的轮子、以及"没有现成方案"的空白点。

---

> **⚠ 2026-10-21 现状更正（以此为准）**
>
> 本文最初结论是「不建议自研字节级 encoder、主路径走 libultrahdr native」。
> **实际交付走的是纯 Kotlin 自研封装**（`UltraHdrPackager.kt`，APK 内 `.so` 数量为 0），
> 已验证可产出合规容器并通过 20 项自检。
> native libultrahdr 仅作为 API-4 备选保留，**尚未编译验证**。
> 阅读下文时请把「推荐 native」理解为**当时的论证**，不是当前方案。

## 0.1 调研范围

| 维度 | 检索对象 |
|---|---|
| 参考编解码库 | Google libultrahdr 及其镜像/fork |
| 纯语言实现（可读字节结构） | Python / Go / JavaScript 版 Ultra HDR 写入器 |
| 增益图提取（源解析侧） | HEIC/HEIF aux image 提取工具、libheif 绑定 |
| 商业 App 行为基线 | Adobe Lightroom Mobile、Adobe Gain Map Demo App、Apple 原生相机/照片 |
| 解码端支持面 | Android 14+、iOS 18+、Windows 11 24H2、Chromium |
| 传输链路 | 微信默认发图 / 原图 / 文件三条路径的社区与媒体实测 |

**重要结论前置**：没有检索到任何"专门做 HDR Vivid 静图 → Ultra HDR 互转"的开源项目或商业 App。这是一个真实空白点（见 0.5）。

---

## 0.2 开源实现盘点

### A. 参考编解码库（生产级，建议作为核心依赖）

| 项目 | 语言/形态 | 能力 | 对本项目的用途 |
|---|---|---|---|
| [google/libultrahdr](https://github.com/google/libultrahdr) | C++17, CMake, 含 `Android.bp` | Ultra HDR 参考编解码器。编码 API-0~5，解码 API；提供 `ultrahdr_app`、单测、benchmark、fuzzer | **核心编码依赖**。走 NAPI/JNI 封装，App 内不做字节拼接 |
| [strukturag/libheif](https://github.com/strukturag/libheif) | C++ | HEIF/HEIC 读写，`heif-convert` | 华为 HDR Vivid 若为 HEIF 容器，用于源解析兜底 |

libultrahdr 关键事实（来自其 README 与 `ultrahdr_api.h`）：

- 编码侧输入"HDR raw + SDR raw"或"SDR compressed + gain map compressed + gain map metadata"，自动完成 gain map 计算与 JPEG 封装。
- **API-4 允许直接传入已压缩的主图 JPEG + 已压缩的增益图 JPEG + GainMap Metadata** —— 这正是"路线 B（复用源增益图）"所需的能力，无需重新计算 gain map。
  注：部分二手资料把它写作 "API-5"，属对官方 README 编码路径表格末列数字的误读——该表只定义 API-0…4，末列的 5 是脚注编号（已在本次核实中确认）。
- 输出为向后兼容 JPEG：主图（SDR）+ 增益图（附加图）+ MPF/XMP 元数据。

> 待核实：各 fork 的 API 版本差异（见 `01-design.md` H 节 H-5）。

### B. 字节结构参考实现（**对本项目价值最高**）

| 项目 | 语言 | 特点 | 用途 |
|---|---|---|---|
| [hanfeisun/pyultrahdr](https://github.com/hanfeisun/pyultrahdr) | Python，~230 行，仅依赖 numpy + Pillow | 纯 Python 组装 `SOI + XMP(hdrgm) + MPF(APP2) + baseJPEG + gainmapJPEG`，声称与 Google 参考样本做 hex diff 校验一致 | **自研 encoder 的字节级教科书**；也适合做 CI 里的 golden-file 比对 |
| [vearutop/ultrahdr](https://github.com/vearutop/ultrahdr) + `uhdrtool` | Go，静态二进制 | `split` / `join` / `resize` / `rebase` / `detect` 子命令 | `resize` 会**同步缩放增益图并重算 MPF**，与本项目"预压缩"模块需求完全同构；`detect` 可作 validator 交叉验证 |
| [bhouston/hdrify](https://github.com/bhouston/hdrify) | TypeScript，零原生依赖 | 读写 EXR / HDR / Ultra HDR / Adobe Gain Map；内置 ACES / AgX / Khronos Neutral / Reinhard tone mapper | tone mapping 算法参考 + Web 端快速验证 |
| [overcloud81/ComfyUI-Create-UltraHDR](https://github.com/overcloud81/ComfyUI-Create-UltraHDR) | Python 节点，内置 `ultrahdr_app` | SDR → Ultra HDR，**单通道灰度增益图**避免多通道偏色；高光自适应扩展 | 单通道增益图的工程经验；其"不做修复、只做显示空间扩展"的定位说明也值得引用 |

### C. 源解析侧（HEIC / gain map 提取）

| 项目 | 语言 | 能力 | 用途 |
|---|---|---|---|
| [twn39/pylibheif](https://github.com/twn39/pylibheif) | Python (nanobind) | `extract_gain_map()` / `parse_gain_map_metadata()` / `reconstruct_hdr_image()`，输出 `linear` / `srgb_clip` / `pq`(10bit) | **Phase 0 桌面验证与 ground truth 生成**；支持 Apple HEIC 与 ISO gain map |
| [bezig01/heic-shenanigans](https://github.com/bezig01/heic-shenanigans) | Python | 提取 base + 全部 aux（含 gain map + depth）+ metadata JSON（ICC/EXIF/XMP base64） | 源解析模块的字段清单参考 |
| [m13253/heif-hdrgainmap-decode](https://github.com/m13253/heif-hdrgainmap-decode) | Python | iPhone HEIC gainmap → EXR | 线性 HDR 参考输出 |

### D. 编解码与工具链补充

- `libjpeg-turbo`：libultrahdr 默认依赖，编码质量/速度基线。
- **Adobe Gain Map Demo App v17**：免费桌面工具，能显示文件用的是 ISO 21496-1 还是 legacy Adobe/Google 编码（`-I` 显示格式）。**这是本项目导出物最实用的第三方验真工具**。
- [gregbenzphotography.com/hdr](https://gregbenzphotography.com/hdr/)：第三方维护的格式/支持矩阵与测试图库，权威度高于厂商宣传页。

---

## 0.3 商业 App 行为基线

| 产品 | 行为 | 对本项目的意义 |
|---|---|---|
| **Adobe Lightroom Mobile (iOS/Android)** | `Export as` → JPG + 开启 `HDR Output` 时，HDR 数据以 **Gain Map** 形式写入 JPG；导出尺寸提供 **Small (2048 px)** 预设 | 证明"2048 长边 + JPEG gain map"是业界通行交付规格，不是本项目臆造 |
| **Apple 相机 / 照片 (iOS 18+)** | 原生相机在支持的模式下直接写 **ISO 21496-1** 的 JPG/HEIF；照片、iMessage、QuickLook 支持读取 | iOS 18 是"支持端"基线；同时说明 iOS 18 以下只能看到 SDR |
| **Google 相册 / Android 14+** | Ultra HDR 原生解码显示 | 主验证端 |
| **Windows 11 24H2 照片 / Chrome 116+** | 支持 ISO JPG gain map | 桌面验证端 |

Adobe 官方说明中明确写了一条对产品设计很关键的话：

> "以 JPG 导出的 HDR 影像包含 HDR 增益地图元数据，Lightroom 和桌面版 Chrome 均可显示此数据。**其他应用可能会将这些 JPG 文件显示为 SDR**。"

这与本项目"不可承诺全平台保真"的边界判断一致，可直接引用为产品文案依据。

---

## 0.4 解码端支持矩阵（截至检索时点）

| 平台/应用 | Ultra HDR / ISO JPEG gain map | 备注 |
|---|---|---|
| Android 14+ 系统相册、Google Photos | ✅ | 主验证端 |
| iOS 18+ 照片 / QuickLook | ✅（有偏差报告） | 有第三方实测称 Apple 对 ISO gain map 的处理"像当成固定 3 档"，效果弱于 Chrome；**需实测** |
| iOS < 18 | ❌ → 显示 SDR | 必须保证 SDR 观感正常 |
| Windows 11 24H2 照片、Chrome/Edge 116+ | ✅ | 桌面验证端 |
| Safari / WebKit (iOS) | ❌（检索时点） | 移动端浏览器无法看 HDR，需注意分享落地页方案 |
| 国产 ROM 相册（小米/OPPO/vivo/荣耀） | ⚠️ 不一 | 逐机型实测，列入测试矩阵 |
| 华为设备相册 | ⚠️ 双通路 | 源为 HDR Vivid 通路，转成 Ultra HDR 后走的是 gain map 通路，观感可能不同于原生 Vivid |
| 微信 | ❌（普通图片链路） | 见 0.5 与 `01-design.md` E 节 |

---

## 0.5 空白点判断（本项目为什么必须自研核心模块）

**没有找到任何开源项目或商业 App 提供"HDR Vivid 静图 → Ultra HDR JPEG"的转换。** 现有工具全部集中在以下三类：

1. SDR → Ultra HDR（伪 HDR，用逆 tone mapping 造高光）—— pyultrahdr、ComfyUI 节点、hdrify。
2. HDR（EXR/HEIF/Apple gain map）→ Ultra HDR —— hdrify、libultrahdr API-1。
3. Ultra HDR 内部操作（split/join/resize/rebase）—— uhdrtool。

由此得出三条对本项目有决定性的结论：

| 结论 | 依据 | 影响 |
|---|---|---|
| **核心难点不在"封装"，在"Vivid 语义 → hdrgm 语义"的映射** | 封装有 3 个成熟参考实现可抄；Vivid 元数据到 `hdrgm` 字段的映射无任何公开实现 | 方案重心放在 M1（源解析）+ M2（线性化/tone map），封装层直接复用 libultrahdr |
| **不建议自研字节级 encoder 作为主路径** | MPF offset / APP2 长度回填 / XMP 包长等细节极易出错，且自研无法保证与全生态解码器兼容 | 主路径 = libultrahdr（native）；纯 ArkTS 拼接仅作降级与教学 |
| **"2048 长边 + JPG gain map"是业界已验证的交付规格** | Lightroom Mobile 提供 2048 预设 + JPG HDR Output | 用户的预压缩策略有外部依据，不需要重新论证 |

---

## 0.6 来源清单

| 编号 | 来源 |
|---|---|
| S1 | https://github.com/google/libultrahdr （README、`ultrahdr_api.h`、API-0~5 表） |
| S2 | https://developer.android.com/media/platform/hdr-image-format （Ultra HDR Image Format v1.1，hdrgm 字段表、MPF 存储、GContainer） |
| S3 | https://github.com/hanfeisun/pyultrahdr （字节组装顺序、hdrgm 字段、BT.2408 203 nit） |
| S4 | https://github.com/vearutop/ultrahdr （`uhdrtool resize` 同步缩放 gain map 与重算 MPF） |
| S5 | https://github.com/bhouston/hdrify （tone mapper 列表、读写能力） |
| S6 | https://comfy.icu/node/SDRToUltraHDR （单通道灰度增益图、显示空间 HDR 的边界声明） |
| S7 | https://github.com/twn39/pylibheif （`extract_gain_map` / `reconstruct_hdr_image`） |
| S8 | https://github.com/bezig01/heic-shenanigans （HEIC aux 提取与元数据字段） |
| S9 | https://helpx.adobe.com/lightroom-cc/using/save-and-export-photo-ios.html （JPG + Gain Map、2048 预设、"其他应用可能显示为 SDR"） |
| S10 | https://learn.adobe.com/tw/lightroom/mobile/share-save-and-export/export-hdr-photos.html （iOS 版本显示兼容性表） |
| S11 | https://gregbenzphotography.com/hdr-photos/iso-21496-1-gain-maps-share-hdr-photos/ （ISO 21496-1 与 legacy 编码差异、各端支持面、Apple 处理偏差） |
| S12 | https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/multimedia-hdr-vivid （HDR Vivid 图片：编码/解码/动态元数据生成/单层↔双层转换，均由 Image Kit 提供） |
| S13 | https://device.harmonyos.com/cn/docs/apiref/harmonyos-guides/image-glossary （GainMap / HDR 双层图 / HDR 单层图 / Picture 术语定义） |
| S14 | https://developer.huawei.com/consumer/cn/doc/doccenter-references/api/arkts-apis-image-imagepacker （`PackingOption`：`desiredDynamicRange: PackingDynamicRange.AUTO`、`needsPackProperties: true`） |
| S15 | https://device.harmonyos.com/cn/docs/apiref/harmonyos-guides/image-picture-encoding （Picture 多图编码，`needsPackProperties` 保留多图元数据） |
| S16 | https://news.qq.com/rain/a/LNK2025052212388600 、https://m.huxiu.com/article/4353926.html （微信剥离 ICC/忽略 HDR 的媒体实测，"荧光蛙"案例） |
| S17 | https://club.honor.com/thread-28335615-1-1.html （微信发图规格：相册原图 >25MB 会降为 1080p；文件管理分享大小/分辨率无限制） |
| S18 | https://post.m.smzdm.com/p/arzelnrx （微信三重降级：色彩空间强制转码 / 动态范围压缩 / 元数据剥离） |

---

## 0.7 一句话结论

> 生态里不缺"Ultra HDR 封装器"，缺的是"华为 Vivid 语义解算器"。本项目应把工程量投在 **M1 源解析 + M2 语义映射 + M5 自检** 三处，封装层直接站在 libultrahdr 上，并把"2048 长边"当作已验证的交付规格而非猜测。
