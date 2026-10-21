# 02 — 参考实现的验证记录

> 配套：`01-design.md`（设计）｜`../tests/test_pipeline.py`（自动化）｜`../out/demo_report.json`（最近一次结果）
> 定位：本文件只记录**已经跑出来的事实**，不含设计论证。设计与风险判断见 01。

---

## 2.1 验证环境

| 项 | 值 |
|---|---|
| OS | Windows 11（Git Bash） |
| Python | 3.13.12（隔离 venv） |
| 依赖 | Pillow 13.0.0（仅用于像素采样） |
| 参考实现 | `src/python/uhdr/`（纯结构层，不依赖 libultrahdr） |
| 输入样本 | `samples/sample_cuva_3000x2250.jpg`（合成 CUVA 风格：主图 3000×2250 + APP5 副图 750×562） |

> 合成样本只模拟**容器结构与几何关系**，不模拟真实 Vivid 的 BT.2020 / PQ 语义，因此不能用于评估色彩准确性。

## 2.2 自动化测试：20 项全通过

```bash
./run.sh test        # python -m unittest discover -s tests -v
```

| 组 | 用例数 | 关键断言 |
|---|---|---|
| JPEG 段解析 | 2 | CUVA 副图首段必须是 APP5(0xFFE5)；每个图像区间以 FFD9 结束 |
| MPF | 1 | 构造→解析往返后 entry 的 size/offset/attribute 一致 |
| XMP | 2 | GContainer 往返；`gamma=0` 必须判非法 |
| 几何变换 | 5 | 缩放保比（误差 1e-9）、不放大、裁剪坐标映射、90° 旋转、比例不符抛错 |
| 端到端 | 5 | 导出自检全绿、SDR 可独立解码、主图是首图、增益图可解码 |
| **双写结构** | 2 | 增益图 XMP 含 7 个 hdrgm 字段；主图只留 Version |
| **负向检出** | 3 | 截断增益图 / 篡改 MPF 偏移 / 删除 XMP，三者均被判失败 |

## 2.3 端到端自检：19 项 PASS

`out/sample.ultrahdr.jpg`（152,526 B）实测结果：

```
[PASS]
  事实: hdrgm_source=gainmap
        hdrgm={Version:1.0, GainMapMax:2.0, HDRCapacityMax:2.0, Gamma:1.0}
        primary_size=2048x1536  gainmap_size=512x384  gainmap_scale=0.25

  [OK] file.soi_eoi                文件首尾为 SOI/EOI
  [OK] container.two_images        检测到 2 个 JPEG 图像
  [OK] meta.xmp_present            存在 XMP APP1 段
  [OK] meta.xmp_parsed             解析到 2 个媒体项
  [OK] meta.primary_item           XMP 含 Semantic=Primary
  [OK] meta.gainmap_item           XMP 含 Semantic=GainMap
  [OK] meta.primary_version        hdrgm:Version="1.0"
  [OK] meta.mpf_present            MPF 含 2 个 entry
  [OK] meta.gainmap_xmp_present    增益图 XMP 字段数=9
  [OK] meta.hdrgm_valid            hdrgm 参数自洽（来源：增益图）
  [OK] offset.mpf_points_to_soi    偏移 145496 → 绝对 146691 指向 SOI
  [OK] offset.mpf_length_match     MPF size=5322 == XMP length=5322
  [OK] offset.xmp_mpf_agree        MPF offset == XMP offset
  [OK] offset.gainmap_complete     增益图范围 [146691, 152526)
  [OK] offset.no_trailing_garbage  尾部剩余 0 字节
  [OK] geometry.aspect_consistent  宽比 0.2500 == 高比 0.2500
  [OK] geometry.gainmap_not_larger 增益图不大于主图
  [OK] offset.primary_len_match    primary length == 增益图绝对起点
  [OK] offset.primary_offset_zero  primary offset = 0（惯例）
```

**这 19 项对应 01-design 的 C.5 断言组**（A1→A4）。A5（色彩/ΔE）未实现，需真机与真实样张。

## 2.4 与设计的对应关系

| 01-design 设计点 | 实现位置 | 验证状态 |
|---|---|---|
| 主图 XMP 仅 Version + GContainer 目录 | `xmp.build_primary_xmp()` | ✅ 实测主图 XMP 无 `GainMapMax` |
| 增益图自带完整 hdrgm | `xmp.build_gainmap_xmp()` + `writer._attach_gainmap_xmp()` | ✅ 9 字段写入，自检来源=gainmap |
| 固定 2048、永不放大 | `gainmap_ops.target_size_for_long_edge()` | ✅ 2048×1536；800px 输入保持不变 |
| 增益图按原比例同步缩放 | `gainmap_ops.scale_gainmap_for()` | ✅ 1/4 → 512×384，比例误差 0 |
| 偏移与长度必须自洽 | `writer.build_uhdr_jpeg()` 不动点迭代 + 硬断言 | ✅ 双向断言（SOI 起点、MPF 指向） |
| 自检失败不得宣称 HDR | `validator.validate_bytes()` → `rep.ok` | ✅ 负向用例 3/3 检出 |

## 2.5 踩坑记录（都已在代码中固化为断言）

| # | 现象 | 根因 | 固化方式 |
|---|---|---|---|
| P1 | 组装后第二个 SOI 前多出 2 字节，段边界漂移 | APP1 长度字段写成 `len(payload)`，漏加长度字段自身的 2 字节 | `_app1_xmp` 修正为 `len(payload)+2`；新增 `out[primary_len:...] == FFD8` 硬断言 |
| P2 | XMP 长度在「字符数」与「字节数」间差 2 | `<?xpacket begin="\ufeff"?>` 的 BOM 是 1 字符 / 3 字节 | 一律以 `len(xml.encode("utf-8"))` 计算 |
| P3 | 主图长度与 XMP 长度互为输入，无法直接求解 | 循环依赖 | 不动点迭代（≤12 轮），未收敛直接抛错 |
| P4 | Git Bash 下 `PYTHONPATH` 失效 | Windows 需 `;` 分隔且需 `C:/` 形式路径 | `run.sh` 用 `cygpath -m` + 平台判断 |
| P5 | 自检器遇损坏文件直接抛异常 | 未做异常兜底 | `validate_bytes` 外层捕获 → 转为 `internal.validator_crashed` 失败项 |

## 2.6 尚未验证（不得对外声称通过）
| 项 | 原因 | 下一步 |
|---|---|---|
| 真实华为 HDR Vivid 样张 | 无版权合规样张 | 用自有机型拍摄 12 张覆盖逆光/高饱和/暗部 |
| native 侧 libultrahdr 编译与调用 | 需 Android NDK / HarmonyOS native 工具链 | 以 submodule 引入并跑 `ultrahdr_app` 交叉验证 |
| HarmonyOS `getAuxiliaryPicture(GAINMAP)` 真机可用性 | 需设备 | 按 `tests/test_matrix.md` 第 3 节填表 |
| 微信三路径存活率 | 需真实收发 | 按 `acceptance-matrix.md` 第 1 节填表 |
| MPF Primary offset 的跨端共识（0 vs 相对 TIFF 头） | 本文按业界惯例取 0 | 用 libultrahdr 解码器反读本实现产物，比对 |
| CUVA → hdrgm 的色彩等价性 | 缺换算依据 | 见 01 的 H-5 |


---

## 2.7 真机实测：ISO 21496-1 载体与两类边界 bug（2026-10-20 晚）

### 现象

用户在华为 Mate 30 上通过蓝牙接收一张 HDR 样张，用旧版 App 打开显示
**「容器：PLAIN_JPEG / 增益图：无」**，据此判断「传输把增益图丢了」。

### 实测结论：增益图完好，是解析器认不出

把文件拉回桌面逐段分析（6,102,654 B）：

```
APP1 Exif
APP2 urn:iso:std:iso:ts:21496:-1   ← ISO 元数据（二进制，非 XMP）
APP2 MPF                            ← 索引副图
APP2 ICC_PROFILE
[主图 5,770,185 B  5760×4320]
[增益图 332,469 B  2048×1536]        ← 完整存在，比例 0.3556
```

- 元数据只有 **ISO 21496-1** 形式，**完全没有 XMP GContainer**
- MPF：entry0 size=5770185 offset=0；entry1 size=332469 offset=5768701
- 主图 APP2 只有 version 块（32 B），完整参数在**增益图的 APP2**（89 B）
- 文件扩展名是 `.heic`，但字节是 JPEG（`ffd8ffe1`）——扩展名不可信，必须看魔数

### 成因一：只认 XMP 的解析器把 ISO 误判成 CUVA

判定顺序错误：「EXIF 含 `_cuva`」被当成强证据，而华为文件即使转成 ISO 容器仍保留
`_cuva` 字样，于是判定为 CUVA，且因为不见 APP5 而继续降级，最终丢到「无增益图」。

**修正**：判定改为严格优先级
ISO 21496-1 APP2 → libultrahdr probe → XMP GContainer → APP5 → 厂商标记（弱证据，最后）。
新增 `iso21496.py` 解析 APP2 二进制元数据（28 B 签名 + 5 B header + 7×8 B fraction）。

### 成因二：Kotlin 扫描边界差 2 字节，副图被静默丢弃

```kotlin
while (pos + 3 < b.size)      // 旧
while (pos + 1 < b.size)      // 新
```

增益图的 `FFD9` 正好位于文件最后 2 字节，`pos + 3` 的循环条件提前退出，
`imageEnd()` 返回 `null`，`findImages()` 直接 `break` —— 只剩主图。
Python 侧用的是 `pos + 1 < len`，所以**桌面测试一直通过、真机一直失败**。
`MetadataValidator` 存在同一处复制粘贴缺陷，一并修正。

### 成因三：HDRCapacityMax 写了线性值

Android 侧 `hdrCapacityMax = 2^log2` 把线性 boost（4.0）写进 log2 域字段，
等于宣称 16 倍容量。桌面 validator 已补 `meta.capacity_matches_gainmap`
（差 >0.01 报警），并在旧产物上实测拦下：`GainMapMax=2.0 vs HDRCapacityMax=4.0`。

### 修复后的真机结果

```
容器：ISO_ULTRA_HDR          增益图：有（332469 B）   Make：HUAWEI
完整增益参数取自增益图 APP2（原始值: 0, 2.1484, 0, 2.1699, 1.0, 0, 0）
resize: base=2048x1536 gm=728x546
package[kotlin-packager]: 1334476B
validate: ok=true errors=0
✓ two_images ✓ xmp_present ✓ xmp_primary_item ✓ xmp_gainmap_item
✓ primary_version ✓ mpf_present ✓ mpf_points_to_soi
✓ gainmap_xmp_present ✓ aspect_consistent ✓ gm_not_larger
已写出：IMG_1792510226792_HDR.jpg（1334476 B）
```

**交叉验证**：把 Android 产出的文件 `run-as` 拉回桌面，用 Python 独立解析器校验，
**19 项全 PASS**（`primary_size=2048x1536`、`gainmap_size=728x546`、
`gainmap_scale=0.3555`、offset 双向一致、尾部 0 字节）。

### 可复用检查清单

1. 扩展名不可信，一律看魔数（`FFD8FFE1` = JPEG，`....ftyp` = ISOBMFF）
2. 多图 JPEG 的扫描循环边界必须允许 `pos+1`，因为 EOI 可能贴着文件尾
3. 「厂商标记」只能是弱证据，不能压过容器结构证据
4. 同一套解析逻辑在 Kotlin / Python 双实现时，必须用真实样张双向交叉验证
5. 双写元数据后，validator 的断言要跟着改（主图只查 Version，完整参数查增益图）

## 2.8 无真机验证路径：模拟器实测（2026-10-21，HDRCapacityMax 修正确认）

真机不在身边时，用本机模拟器（Pixel_XL_API_33 / Android 13 / arm64）复现了
完整导出流程。**结论：HDRCapacityMax 修正生效，`validate: ok=true errors=0`。**

操作链（关键步骤）：

1. `adb push` 样张到 `/sdcard/Download/` **不够**——Photo Picker 只认媒体库，
   需再插一条 MediaStore 记录（否则 picker 显示 "No photos or videos"）：
   ```bash
   adb shell content insert --uri content://media/external/images/media \
     --bind _data:s:/sdcard/Download/test_hdr.jpg --bind title:s:test_hdr \
     --bind _display_name:s:test_hdr.jpg --bind mime_type:s:image/jpeg \
     --bind date_added:l:$(date +%s) --bind width:i:2048 --bind height:i:1536
   ```
   picker 已打开时不刷新，需 `KEYCODE_BACK` 关掉重开。
2. `uiautomator dump` 找按钮/网格项坐标，`input tap` 驱动（按钮
   「选择 HDR 照片并处理」→ Recent 网格第一张 → 等约 20 秒）
3. 再 `uiautomator dump` 读回日志 TextView；产物用
   `adb exec-out run-as com.finn.hdr cat cache/exports/<file>.jpg` 拉回桌面

模拟器实测输出（源：`out/real_sample.ultrahdr.jpg`，profile=local，强度 0.70）：

```
容器：ISO_ULTRA_HDR      增益图：有（45529 B）   Make：HUAWEI
HDR 峰值：2.24x  白点：1.00  最大 boost：4.00x  高光裁切：0.00%
LTM：半径=32px  detail增益=1.32  暗部提升=1.49x
package[kotlin-packager]: 1231649B
validate: ok=true errors=0          ← 10 项 ✓ 全过
已写出：…_BAKED.jpg（1478 KB）  …_HDR.jpg（1202 KB）
```

交叉验证（双实现互证，两条都过）：

| 验证 | 结果 |
|---|---|
| App 的 Ultra HDR 产物 → Python 独立校验 | **20/20 全 PASS**，`capacity_matches_gainmap: GainMapMax=2.0000 HDRCapacityMax=2.0000 差=0.0000`（log2 域修正确认） |
| App 的烘焙图 vs Python 同参烘焙 | **SSIM 0.9521**；亮度差 mean 1.38 / p99 7.72（0-255）；LTM 统计逐项一致（r=32 / detail=1.32 / 暗部提升 1.49x）。残差来自 Android 编码器 4:2:0 vs Python 4:4:4、抖动 RNG、增益图重采样——实现级差异，非算法分歧 |

模拟器的局限（不得对外声称的部分）：HDR **显示**效果（Ultra HDR 增益图还原）
在模拟器上看不到，只能验证容器结构；微信传输实测仍需真机。
