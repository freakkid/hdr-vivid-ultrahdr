# HEIC / HEIF 与「转 JPG 有没有损耗」

本文回答两个被反复问到的问题，并记录行业方案的调研结论，避免以后重新推一遍。

---

## 1. 转 JPG 有损耗吗

**有，而且不止一层。** 但要看走的是哪条路。

| 场景 | 损耗 | 说明 |
|---|---|---|
| 源图本来就是 **JPEG** | **无额外损耗** | 直接读原始字节，不解码再编码。我们的解析器就工作在这一层 |
| 源图是 **HEIC → 系统转码成 JPEG** | **双重损耗** | ①HEIC 本身已是有损（HEVC）；②转码等于第二次有损重编码；③10 bit → 8 bit，动态范围被截断；④**增益图不跟过来，HDR 信息全废** |
| 我们输出的烘焙图（JPEG q95） | 一次有损 | 这是最终交付格式，无法避免；q95 + 抖动已把损失压到可见阈值以下 |

**关键提醒：扩展名不可信。** 实测过文件名是 `.heic`、字节却是 JPEG（`FF D8 FF E1`）的样本——
这是华为把 HDR Vivid JPEG 存成 `.heic` 后缀导致的。判断格式必须看魔数：

```
FF D8 FF          → JPEG
.. .. .. .. 66 74 79 70 ("ftyp")  → ISOBMFF（HEIC/HEIF/AVIF/MP4）
89 50 4E 47       → PNG
52 49 46 46       → RIFF（WebP）
```

工具：`python scripts/analyze_image.py <文件>` 会直接打印真实格式与 12 字节头部。

---

## 2. 为什么不能「直接支持 HEIC」

不是不愿意，是 **Android 公开 API 卡在中间**。

| 层 | 能不能 | 说明 |
|---|---|---|
| 解 HEIF **主图** | ✅ 可以 | `ImageDecoder` / `BitmapFactory` 能解，系统带 HEVC 解码器 |
| 解 HEIF 里的 **增益图** | ❌ 不行 | `ImageDecoder` 只解主图；`Bitmap#getGainmap` / `Gainmap` 类是 **Android 14** 才有的 API，而容器（鸿蒙卓易通）是 Android 12/13 |
| 容器结构解析 | ✅ 可以 | ISOBMFF 是公开格式，自己写 box 遍历即可（已实现 `HeifInspector.kt`） |

所以缺的是「把某一个辅助 item 单独解码成 Bitmap」这一步。

---

## 3. 行业方案调研

| 方案 | 原理 | 成熟度 | 用于本项目的代价 |
|---|---|---|---|
| **libheif ≥ 1.18** | 完整 HEIF/AVIF 编解码库，1.18 起支持 **ISO 21496-1 的 TMAP 派生图**（iOS 18 照片必须 ≥1.18，否则报 "Too many auxiliary image references"） | 事实标准 | 需 NDK 交叉编译 + 引入 libde265/HEVC 解码器，APK 体积与构建复杂度显著上升 |
| **pillow-heif**（Python） | libheif 的 Python 绑定，`heif_file.info["aux"]` 拿到 URN → `get_aux_image(id)` | 可直接用 | **已在桌面侧接入**（`scripts/analyze_image.py`），用于验证样张；不影响 App |
| **Android 14 `Gainmap` API** | 系统级 API，`Bitmap#getGainmap` | 官方 | 容器 SDK 太低，用不了 |
| **自研 ISOBMFF 拆解 + MediaCodec** | 解析 `meta → iinf/iloc/iprp`，把增益图 item 单独取出来重封装成「只含该 item 的最小 HEIF」，再交给 `ImageDecoder`；或把 item 的 HEVC 码流喂给 `MediaCodec` | 可行，纯 Kotlin | 工作量中等（约 400–600 行 + 若干边界情况），**不需要 native** |
| 让用户在相册「另存为 JPG」 | 绕开问题 | 兜底 | 增益图丢失，HDR 信息全废，等于放弃需求 |

### 3.1 已知的增益图 URN

| URN | 来源 | 备注 |
|---|---|---|
| `urn:iso:std:iso:ts:21496:-1` | ISO/IEC 21496-1 | 标准增益图，HEIF 里以 `tmap` 派生图或辅助 item 出现 |
| `urn:com:apple:photo:2020:aux:hdrgainmap` | Apple | 早期 iPhone HDR 增益图 |
| `urn:com:huawei:photo:aux:hdrgainmap` | 华为（候选） | 未实测确认，`HeifInspector` 已按前缀匹配 |
| `urn:com:huawei:hdr:aux:gainmap` | 华为（候选） | 同上 |

> 华为的两个 URN 是**推测值**，尚未在真实样张上验证。`HeifInspector.kt` 会打印实际读到的 URN，
> 以实测为准，不要拿这张表当结论。

---

## 4. 当前决策

1. **不引入 libheif**（暂）：代价与收益不匹配，且我们的容器目标是 Android 12/13，缺 API 的问题照样存在
2. **先拿到事实**：`HeifInspector.kt`（App 内）+ `scripts/analyze_image.py`（桌面）都只做解析不做解码，
   目的是确认样张到底是不是 HEIC、aux URN 叫什么
3. **确认之后再决定**：若确为 HEIC 且带 ISO 增益图 → 走「自研 ISOBMFF + MediaCodec」；
   若 aux 里根本没有增益图 → 说明这张照片本来就不是 HDR，问题在别处，不必再投入

---

## 5. 相关文件

| 文件 | 作用 |
|---|---|
| `src/android/.../HeifInspector.kt` | App 内 ISOBMFF 结构探针（只解析） |
| `src/android/.../MainActivity.kt` | 读到 ISOBMFF 时打印结构并停止，提示复制日志反馈 |
| `scripts/analyze_image.py` | 桌面统一分析器（JPEG 走 uhdr / HEIC 走 pillow-heif） |
