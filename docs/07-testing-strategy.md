# 测试策略（2026-10-25，四路 agent 调研结论）

本文回答一件事：**怎样让「本地全绿」真的等于「用户手里没问题」**。
写作背景：连续两次事故都是「测试全绿但用户一眼看出问题」——

| 事故 | 表面 | 测试为什么没拦住 |
|---|---|---|
| `maxLift` 只加字段没落地 | Python 验收全绿，App 仍过曝 | 验收只跑 Python；Kotlin 那段当时**没有任何执行** |
| 丢掉 Display P3 的 ICC | 验收全绿，烘焙图发灰 | 阈值只设了饱和度**上限**，合成样本还**不带 ICC**，天然免疫 |

结论不是「多加几条测试」，而是三件事：**让更多真实代码被执行、把跳过从 PASS 里摘出去、给指标补下限**。

---

## 1. 现状：哪些真被跑过

- **被真实执行**：`HdrBaker.kt`、`LocalToneMapping.kt`、`HeifGridRewriter.kt`（`tests/android_parity/`，kotlinc + 两个桩）；Python 的 `jpegseg/mpf/xmp/iso21496/vivid_source/validator/gainmap_ops/pipeline/bake/ltm/heif_grid`。
- **从未被执行**（只做过括号配平，类型错误都能过）：`HdrSourceParser`、`IsoGainMap`、`UltraHdrPackager`、`MetadataValidator`、`UltraHdrExporter`、`GainMapGeometry`、`HeifInspector`、`HeicDecoder`、`ShareGuidance`、`MainActivity`、「libultrahdr native `.so`」。

也就是说：**决定用户手里那个文件能不能被系统认成 HDR 的封装器与自检器，一次都没跑过。**

---

## 2. 七个「假绿」点（P0，必须先修）

1. **编译失败被当成"缺环境"**：`run_parity.py` / `run_grid_parity.py` 编译失败返回 2，`verify_bake.py` 把 2 当 PASS。→ 新增 `3=编译/运行失败`（FAIL），`2=工具缺失`（SKIP 且汇总禁止写"全部通过"）。
2. **退出码 2 处处当通过**：`verify_bake.py:135-137`、`run_step_optional`。→ 汇总里区分 PASS/FAIL/SKIP，`用例数==0` 直接 FAIL。
3. **指标只有上限**：`accept.py` 的 median/mean/clip/shadow 全是上界，SSIM/sat 的下界对「恒等变换」自然满足——**原图直出能拿 7/7 PASS**。→ 补下限：`shadow_lift ≥0.90`、高光区亮度 `/mean_lift ≥1.05`（Kotlin 侧 `Stats.meanLift` 已有，可对账）。
4. **验收强度 ≠ 出货强度**：`accept.py` 硬编码 `strength=0.40`，产品默认 `0.70`。→ 跑 `(0.40, 0.70)` 矩阵。
5. **基准由实现自己产出（自证）**：`accept.py` 用 `bake.to_srgb` 造对照基准，该函数 `except` 后静默退回不做色彩管理——色彩一坏，基准和输出一起变灰。→ `to_srgb` 失败应 raise；基准改用独立 `ImageCms` 或入库 golden。
6. **`ltm_parity/run_parity.sh` 从不参与门禁**：缺 kotlinc 就 `exit 0`，且 `verify_bake.py` 没接它。→ 接入，`exit 0` 改 `exit 2`。
7. **jar 复用靠 mtime**：`git checkout` / 换机器后可能对着**旧 Kotlin**比对。→ 源码树 sha256 写入侧车文件，不一致强制重编。

次级（P1）：`check_android_project.py` 的恒真项（`gradle.agp_version`）、`liftCap` 只数出现次数（注释也能凑数，应剥注释后匹配两处 `minOf(...)`）、括号配平 ≠ 可编译（真正门禁应是 `./gradlew compileDebugKotlin`）。

---

## 3. 分层金字塔（目标形态）

| 层 | 跑什么 | 覆盖 | 时长 | 场所 | 拦哪类 bug |
|---|---|---|---|---|---|
| L0 | 静态自检 `check_android_project.py` | 全工程 | <2s | 本地+CI | 缺文件、JNI 符号不匹配 |
| L1 | 纯 JVM 解析/容器（kotlinc+桩） | `HeifInspector`、`MetadataValidator`、`UltraHdrPackager`、`HdrSourceParser`、`HeifGridRewriter` | 10–40s | 本地+CI | XMP/MPF 偏移、iloc/iref、段挑错、**"看起来像 HDR 但元数据没写进去"** |
| L2 | 算法数值对照（已有） | `HdrBaker`、`LocalToneMapping` | ~10s | 本地+CI | 两端算法漂移、只加字段不落地 |
| L3 | 观感验收 `accept.py`（含下限） | bake 全流程 | ~10s | 本地+CI | 过曝、发灰、暗部压黑、HDR 没生效 |
| L4 | Robolectric（可选） | `UltraHdrExporter`、`GainMapGeometry`、UI 状态 | 1–3min | 本地+CI | 真实解码/编码链路、按钮状态 |
| L5 | 真机一次 | 容器权限、相册、ImageDecoder | 人工 | 设备 | L0–L4 都测不到的运行时 |

**本机还能再拿下 4 个 Kotlin 模块且不需要 Robolectric**（8–14 小时）：`HeifInspector`（零 import）、`MetadataValidator`（仅 `java.nio`）、`UltraHdrPackager`（仅 `android.util.Base64` 一个符号）、`HdrSourceParser`（仅 `java.io/nio`，还能直接用那 3 张真实华为样本跑）。

`MainActivity` 值得抽，但**只抽状态推导**（哪些按钮该亮、gen 令牌是否允许回写），不抽调度；成本 4–8 小时。

---

## 4. 变异测试（验证"测试有效"）

建 `tests/mutation/mutants.py`（纯数据表）+ 两个 runner：复制源码到临时目录 → 只替换一处（断言命中次数==1，否则 stale）→ 跑测试 → 红=KILLED，绿=SURVIVED。

| 变异 | 注入 | 应当被谁杀 | 现状 |
|---|---|---|---|
| 去掉 `liftCap` 封顶 | `minOf(..., sdr*liftCap)` → 去掉 | 逐像素 lift 断言 / accept | **存活** |
| 去掉 strength 增量 | `+ (blended-sdr)*amount` → 去掉 | `TestStrengthTakesEffect` | 已杀 |
| 去掉 `to_srgb` | 直接 `convert("RGB")` | `sat_ratio_min=0.92` | 合成样本无 ICC → **存活** |
| 去掉 blend 加权 | `target = ml` | accept 中位/均值 | 需测 |
| P3 ICC 改成错 profile | — | sat 下限 | 需测 |
| 裁切改回亮度口径 | 单通道 → 亮度 | 单通道回归 | 需测 |
| MPF 小端分支翻转 | `II` 处理 | 无 | **存活**（无用例） |
| `UltraHdrPackager` 偏移 ±1 | 偏移公式 | 无 | **存活**（从未执行） |
| `grirew` 丢 `ispe` | 重封时不补 | `run_grid_parity` 属性断言 | 已杀 |
| `IsoGainMap` 回退条件改 | 条件改错 | 无 | **存活** |

存活项就是下一步要补的测试。

---

## 5. 一个前提：仓库内必须有 fixture

`out/visual/samples` 被 gitignore，样例只能从手机拉 —— 于是 **parity / golden / 变异三件事在 CI 里恒为"跳过"**。

必须先造一个入库的合成 Ultra HDR fixture（`pipeline.export` 产出，约 80 KB），并让合成样本**带 P3 ICC**（`synthetic.py` 现在只写 EXIF，不写 ICC，导致色彩类变异天然免疫）。没有它，第 2、4 节的大半动作在 CI 上不成立。

---

## 6. CI 现状与改造

现在 `.github/workflows/android-build.yml` 只跑：`unittest discover -s tests`、`cli demo`、`assembleDebug`。
**验收、两端对照、静态对账全部不在 CI 里**；且 `tests/` 下没有 `__init__.py`，`acceptance/`、`android_parity/`、`ltm_parity/` 三个目录不会被 discover 收集。

改造清单：
1. CI 增加 job：跑 `scripts/verify_bake.py`（含 kotlinc、样例用入库 fixture，不用手机）。
2. 三个 parity 脚本显式列出并检查退出码。
3. 给 `tests/test_profiles.py`、`tests/test_heif_grid.py` 补 `sys.path.insert`（现在只有 `test_pipeline.py` 有，导致 CI 上这两个模块可能 import 失败）。

---

## 7. 执行顺序

| 优先级 | 动作 | 成本 | 收益 |
|---|---|---|---|
| P0 | 退出码三态 + 汇总区分 SKIP + 用例数为 0 判 FAIL | 1–2h | 消灭"跳过即通过" |
| P0 | accept 补下限（shadow/highlight/mean_lift）+ strength 矩阵 | 1–2h | 恒等变换、HDR 未生效立刻红 |
| P0 | 入库 fixture（带 P3 ICC 的合成 Ultra HDR） | 2–3h | 让 CI 真的能跑对照 |
| P1 | `to_srgb` 失败 raise；基准改独立工具链 | 1h | 消灭自证 |
| P1 | 接入 `ltm_parity`；jar 侧车哈希 | 1h | 两端算法不漂移 |
| P1 | JVM 化 `HeifInspector`/`MetadataValidator`/`UltraHdrPackager`/`HdrSourceParser` | 8–14h | 封装器与自检器首次被执行 |
| P2 | 变异测试 runner + 10 个变异 | 6–8h | 量化"测试有效性" |
| P2 | `MainActivity` 状态推导抽纯 + 单测 | 4–8h | 按钮矩阵不再靠真机撞 |
| P2 | Robolectric（走 aliyun 镜像取 android-all） | 4–8h + 每组 1–3h | 真实编解码链路 |

**验收标准**：把第 4 节的变异逐个注入，存活数为 0，才算这套测试真的有效。
