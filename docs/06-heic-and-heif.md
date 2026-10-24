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

## 4.1 实测更正：增益图是 4 块 grid（2026-10-24）

真实华为样张（7 张，结构一致）推翻了此前「增益图 = aux item / tmap 参数块」的猜测：

| 项 | 实测 | 说明 |
|---|---|---|
| 增益图载体 | `tmap` 派生项的 `dimg` 指向 **增益图 grid**，该 grid 由 **4 块 `hvc1`** 拼成 | 既不是 `auxC` 辅助图，也不是单张图 |
| 主图 | grid 拼 12 块或 30 块 `hvc1` | 用块数即可与增益图（4 块）区分 |
| `tmap` item 载荷 | 62 B，是**嵌套 meta box**（`746d6170`="tmap" + meta，自带 iloc） | **不是** ISO 21496-1 参数；此前「5+56 字节参数块」的猜测是错的 |
| 华为私有参数 | `mime` 项 380 KB（`application/vnd.huawei/...`）+ `it35` 项 237 B | **没有公开解析器** |
| 系统转码结果 | 相册/系统转出的 JPEG 是 **ISO Ultra HDR**，增益图 535~620 KB、`GainMapMax=2.1484` | 绕行成立，且可作为自研结果的对照基线 |

**取法（v0.1.14）**：不自己解 HEVC，而是把「grid + 4 块 tile + `hvcC`/`ispe` 等属性」重封成
一个最小 HEIF，交给系统 `ImageDecoder`（它本来就会拼 grid）。tile 码流原样搬运，
所以不需要 NDK、不需要 libheif，APK 仍是 0 个 `.so`。

```bash
# 桌面侧：看结构 / 抽 item / 重封（纯标准库，无需 pillow）
PYTHONPATH=src/python python -m uhdr.heif_grid <file.heic>
PYTHONPATH=src/python python -m uhdr.heif_grid <file.heic> --dump 42:grid.bin --hex 47
PYTHONPATH=src/python python -m uhdr.heif_grid <file.heic> --rewrap 42 -o gainmap.heic
```

App 侧同一套逻辑在 `HeifGridRewriter.kt`；选图后日志 `[2c/5]` 段会打印 grid 结构与重封解码结果。
**仍未做**：把解出的增益图接进烘焙链路；以及解析华为 `mime` / `it35` 参数项（未知格式）。

## 5. 相关文件

| 文件 | 作用 |
|---|---|
| `src/android/.../HeifInspector.kt` | App 内 ISOBMFF 结构探针（只解析） |
| `src/android/.../HeifGridRewriter.kt` | grid 增益图重封成最小 HEIF（v0.1.14，纯 Kotlin） |
| `src/android/.../MainActivity.kt` | 读到 ISOBMFF 时打印结构 + grid 摘要 + 重封试解 |
| `src/python/uhdr/heif_grid.py` | 同一套解析/重封的桌面实现 + CLI（`--dump` / `--rewrap`） |
| `tests/test_heif_grid.py` | 合成 HEIC 的回归测试（16 项） |
| `scripts/analyze_image.py` | 桌面统一分析器（JPEG 走 uhdr / HEIC 走 pillow-heif） |
