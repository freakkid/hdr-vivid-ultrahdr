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
