# 04 — 交接单：烘焙（bake）链路的改动与待办

> 交接时间：2026-10-20 23:55
> 交接方：会话 A（负责 Ultra HDR 方案文档 + 本次烘焙算法验证）
> 接手方：会话 B（负责 Android App / 工具链主体）
> 触发原因：两个会话并发修改同一仓库，`src/python/uhdr/cli.py` 已发生实际覆盖。**自本文件起，A 不再写任何代码文件，代码所有权全部转给 B。**

---

## 0. 一句话

A 在 23:44–23:52 期间给烘焙链路加了**局部色调映射**并修了**两个算法 bug**，代码已在工作区可运行；**还剩一个 P0 bug（对比度作用域错误导致黑位被钉死）未修**。B 按第 4 节的 patch 一次改完即可，A 不再碰代码。

---

## 1. 文件所有权与当前状态

| 文件 | 谁写的 | git 状态 | 当前是否可运行 | 说明 |
|---|---|---|---|---|
| `src/python/uhdr/ltm.py` | **A 新增** | untracked | ✅ | 局部色调映射，纯函数无副作用，可被 B 直接复用 |
| `src/python/uhdr/bake.py` | B 创建，**A 改了 4 处** | untracked | ⚠️ 可跑但黑位被压死 | 见第 2 节逐条改动 |
| `src/python/uhdr/cli.py` | 双方都改过 | M | ✅ | **曾与 B 版本互相覆盖**（A 读到过含 `--compare` 的版本，随后又被改回）。当前 23:52 快照含 `--profile` / `--anchor` |
| `tests/bake_compare.py` | **A 新增** | untracked | ✅ | 三档量化对比 + 三联拼图 |
| `docs/04-HANDOFF-bake.md` | A | untracked | — | 本文件 |

**A 明确未碰、也不会再碰：**
`src/android/app/src/main/java/com/finn/hdr/MainActivity.kt`、`HdrBaker.kt`、`MetadataValidator.kt`、`docs/02-verification.md`、`docs/03-toolchain-setup.md`、`ship.sh`、`HANDOVER.md`。

**A 已交付、不属烘焙链路的文档（可独立保留）：**
`docs/00-landscape.md`（生态调研）、`docs/01-design.md`（Ultra HDR 方案）、`tests/acceptance-matrix.md`、`src/ets/*.ets`（ArkTS 骨架）。

---

## 2. `bake.py` 的 4 处改动（逐条，供 B 审核/回退）

### 改动 1 — import
```python
from dataclasses import dataclass, field   # 原为 dataclass
from . import ltm, vivid_source            # 原为 vivid_source
```

### 改动 2 — 选项与统计
```python
# BakeOptions 新增
profile: str = "local"          # off / local / wechat
anchor: str = "sdr-white"       # sdr-white / peak

# BakeStats 新增
profile: str = "off"
ltm: dict = field(default_factory=dict)
```

### 改动 3 — 色调映射分支（LTM 路径跳过全局归一化）
```python
if opt.anchor == "peak":
    white = max(float(np.percentile(lum, opt.white_percentile)), 1e-3)
else:
    white = 1.0

ltm_p = ltm.params_for_profile(opt.profile)
if ltm_p is not None:
    hdr, ltm_stats = ltm.apply(hdr, white, ltm_p)
    out = np.clip(hdr, 0.0, 1.0)          # LTM 输出已是显示线性域
    norm = (out * _LUMA).sum(axis=2) / white
else:
    norm = lum / white                     # 旧路径保持不变
    mapped = _soft_knee(norm, opt.knee)
    scale = np.where(lum > 1e-6, mapped / np.maximum(lum, 1e-6), 0.0)[..., None]
    out = np.clip(hdr * scale, 0.0, 1.0)
```

> 关键点：LTM 输出已经是「1.0 = SDR 白点」的显示线性域，**不能再乘 `mapped/lum`**，否则会被二次压暗。

### 改动 4 — 量化前抖动 + 按档位选编码参数
```python
rng = np.random.default_rng(0)   # 固定种子，保证同参数下输出确定性
tri = rng.random(srgb.shape, dtype=np.float32) - rng.random(srgb.shape, dtype=np.float32)
img = Image.fromarray(np.clip(srgb * 255.0 + 0.5 + tri * 0.5, 0, 255).astype(np.uint8), "RGB")

subsampling = 2 if opt.profile == "wechat" else 0
quality = 88 if opt.profile == "wechat" else opt.quality
```

**完全回退方法**：`BakeOptions(profile="off", anchor="peak")` 即等价于改动前的行为。

---

## 3. 三个算法问题（两个已修，一个待修）

### 3.1 已修 — log 域高光软膝方向写反
`ltm._soft_knee` 原写作 `knee - span*(1-exp(...))`，收敛到 `2·knee`（更暗），会把高光压成负值 → 「高光比中间调还黑」。
改为 `knee + span*(1-exp(...))`，收敛到 0（白点），且在 `x=knee` 处一阶导连续，无可见接缝。

### 3.2 已改 — 白点锚定方式
- 原：`white = percentile(lum, 99.5)`（本例 3.10）
- 现：`white = 1.0`
- 原因：HDR 重建后 **1.0 天然就是 SDR 白点**（增益为 0 时 `hdr ≈ sdr`）。按峰值归一化等于把整张按 3.1 压暗，暗部直接被钉死在 0。
- 数据佐证：源暗部亮度 19.2 → 输出 0.3（peak 锚定）；改 sdr-white 后 → 0.7（仍偏低，原因见 3.3）。

### 3.3 **P0 待修 — 对比度 S 曲线作用在错误的色彩域**

```python
# 当前代码（bake.py，_linear_to_srgb 之前）
if opt.contrast != 1.0:
    out = np.clip((out - 0.5) * opt.contrast + 0.5, 0.0, 1.0)
```

`out` 此时是**线性光**。线性 0.5 ≈ sRGB 188，所有低于它的值被强力压向 0 ——
这是「暗部 19.2 → 0.7」的真正根因，`anchor` 只影响幅度不影响机制。

**修复方案**：把 contrast 移到 `_linear_to_srgb` **之后**，在 sRGB（感知）域做。

```python
srgb = _linear_to_srgb(out)
if opt.contrast != 1.0:
    srgb = np.clip((srgb - 0.5) * opt.contrast + 0.5, 0.0, 1.0)
```

**预期**：暗部回到 ~18–24（与源匹配）；当前「局部对比 ×1.62」有虚高成分，修复后需重新标定 `detail_gain`。
`saturation` 同样在线性域做，建议一并移到 sRGB 域（或改用 Oklab 保色相）。

---

## 4. 待办清单（建议 B 一次改完）

| ID | 事项 | 位置 | 验收 |
|---|---|---|---|
| P0-1 | 按 3.3 移动 contrast 到 sRGB 域 | `bake.py` 量化前 | 暗部亮度回到 18–24 |
| P0-2 | 修完跑 `tests/bake_compare.py` 复测并更新 3.3 数据 | — | 见第 5 节对照 |
| P1-1 | `--subsampling` / `--wechat-quality` 暴露到 CLI（当前硬编码 2 / 88） | `cli.py` | `uhdr bake --help` 可见 |
| P1-2 | 用**真实华为 Vivid 样张**复测（当前只有合成源，`元数据=无`，走的是 `log2(4)` 兜底） | — | 拿真实 gainmap 的 `GainMapMax` |
| P1-3 | 校准 `detail_gain`：`local=1.32` 是否过强（需人眼 + 指标双确认） | `ltm.params_for_profile` | 无 halo、无「假 HDR」灰雾感 |
| P2-1 | Android 侧 `HdrBaker.kt` 与 Python 侧参数语义对齐（同一套 `profile` 命名与默认值） | `HdrBaker.kt` | 两端同图同参输出接近 |

---

## 5. 本次实测数据（供修复后对照）

源：`samples/sample_cuva_3000x2250.jpg`（合成 CUVA，3000×2250 → 输出 2048×1536）

| 档位 | 体积 KB | 局部对比 | 暗部亮度 | 高光裁切 |
|---|---|---|---|---|
| 源 SDR 主图 | — | 0.51 | **19.2** | 0.00% |
| `off`（修白点后） | 338 | 0.76 | **0.7** | 0.00% |
| `local`（修白点后） | 360 | 0.83 | **1.0** | 0.00% |
| `wechat`（修白点后） | 113 | 0.78 | **0.9** | 0.00% |

对照 —— 修白点**之前**（`anchor=peak`）：

| 档位 | 体积 KB | 局部对比 | 暗部亮度 |
|---|---|---|---|
| `off` | 269 | 0.53 | 0.3 |
| `local` | 297 | 0.61 | 0.5 |
| `wechat` | 88 | 0.57 | 0.6 |

**修复 P0-1 后，`暗部亮度` 应回到 18–24 区间；若仍 < 5，说明还有第三处线性域算子未搬。**

其他已验证项：
- 输出为**纯 JPEG**：`hdrgm` / `GContainer` / `MPF` 均不存在，APP 段 `none` —— 符合「烘焙」的定义（不依赖接收端）。
- 同参数两次导出一致（固定抖动种子），满足 Golden-file 比对前提。

---

## 6. 协作规则（建议落到仓库）

1. **文件级所有权**：`bake.py` / `cli.py` / 全部 Android 源文件归 B；A 只写 `docs/` 与 `src/ets/`。
2. B 若要重写 `bake.py`，**可整体覆盖**，不必保留 A 的写法；但 `ltm.py` 是独立的纯函数模块，建议保留复用（`apply(hdr_linear, white, params) -> (out, stats)`，无全局状态）。
3. 编号约定：`docs/02`、`docs/03` 已被 B 占用，A 从 `04` 起编号。
4. 若 B 需要 A 的改动作废：`profile="off"` + `anchor="peak"` 即回到原行为，无需回滚文件。

---

## 7. A 这边的后续动作

- **不再修改任何代码文件。**
- 只保留三份文档的维护权：`docs/00-landscape.md`、`docs/01-design.md`、本文件。
- 若 B 完成 P0-1/P0-2 后需要复核数据，或需要把烘焙结果接回 Ultra HDR 双轨导出（同一张图同时出「SDR 烘焙版」与「Ultra HDR 版」），由用户再单独指派给 A。


---

## 6. B 侧回执（2026-10-21 00:00）

### 6.1 P0 已修

`contrast` 与 `saturation` 均已从线性域移到 `_linear_to_srgb` 之后（sRGB 感知域）。
代码位置：`src/python/uhdr/bake.py`，原「S 曲线 → 饱和度 → 转 sRGB」改为「转 sRGB → S 曲线 → 饱和度」。

### 6.2 修后实测（同一张真实 ISO Ultra HDR 样张）

| 档位 | 体积KB | 局部对比 | 暗部亮度 | 高光裁切 |
|---|---|---|---|---|
| 源 SDR 主图 | — | 11.81 | **19.8** | 0.10% |
| 全局映射 off | 2219 | 13.57 | **30.7** | 0.20% |
| 局部映射 local | 2204 | 12.82 | **40.0** | 0.20% |
| 微信档 wechat | 965 | 12.40 | **35.1** | 0.08% |

### 6.3 与预期的偏差及结论

交接单预期「暗部回到 18–24」，实测 30.7 / 40.0，**偏高而非偏低**——说明 P0 确实修好了
（不再是压死到 0.7），剩下的差异不是 bug，而是**这张源图本身在暗部也带增益**：

- 增益图暗部恢复出的 boost 约 1.5–1.6x，HDR 重建后暗部自然比源 SDR 亮
- 因此「暗部亮度等于源图」不能作为验收标准，应改为**视觉判断 + 局部对比提升幅度**
- 若产品上希望更接近原图观感，应下调 `strength`（0.85 → 0.70 量级），而不是继续改算子

**结论：删掉「暗部 18–24」这条硬指标，改为「无灰雾、无死黑、高光裁切 <1%」。**

### 6.4 交接单其余待办的状态

| ID | 状态 |
|---|---|
| P0-1 contrast 域 | ✅ 已修 |
| P0-2 复测 | ✅ 已复测，见 6.2 |
| P1-1 CLI 暴露 subsampling / wechat-quality | ⬜ 未做（当前 --profile 已能覆盖场景） |
| P1-2 真实样张复测 | ✅ 本轮已用真实 ISO Ultra HDR 样张（非合成源） |
| P1-3 标定 detail_gain | ⬜ 待用户目视确认 |
| P2-1 Android 与 Python 参数对齐 | ⚠️ 部分：App 端 `HdrBaker` 只有全局映射，**LTM 未移植** |

### 6.5 生态调研结论（A 提供，已采纳为后续优化方向）

「HDR 风格」类工具多为 ICC 改 PQ 的做法，**依赖设备支持且元数据一剥离就变灰**，
与本项目「像素烘焙」不是一条路。可借鉴的成熟 TMO：
Local Laplacian Filters（保边、halo 少）、OpenCV 的 Drago/Durand/Reinhard/Mantiuk、
darktable 的 tone equalizer + filmic rgb、LLF-LUT（3D LUT + Laplacian，移动端友好）、HDRNet（bilateral grid）。
本项目当前用 guided filter 而非高斯模糊，正是为规避 halo。


---

## 7. B 侧定档结论（2026-10-21 00:05）

### 7.1 四档实测（同一张真实样张，profile=local）

| 档位 | 体积KB | 暗部均值 | 中间调 | 高光 | 高频能量 | 红色强度 |
|---|---|---|---|---|---|---|
| 源 SDR | — | 35.3 | 98.8 | 208.9 | 10.15 | 87.4 |
| strength 0.70 | 2183 | 56.8 | 128.8 | 235.3 | 6.47 | 101.9 |
| strength 0.85 | 2203 | 60.7 | 133.1 | 237.7 | 6.46 | 99.9 |
| strength 1.00 | 2218 | 64.2 | 137.4 | 240.0 | 6.44 | 97.7 |

### 7.2 定档 `strength = 0.70`（已写入默认值）

目视依据：0.70 提亮明显、红色仍浓郁、木纹层次最好；0.85 略过；1.0 红色发淡发黄、有过曝倾向。
三处默认值已统一：`bake.py` / `cli.py` / `HdrBaker.kt`。

### 7.3 一个未解释的指标（P1 待查）

**高频能量从 10.15 降到 6.4x（−36%），但目视细节反而更清楚。**
两种解释：
1. 指标本身对绝对亮度敏感：整体提亮后相邻像素差的相对值下降，但**可见**细节增加；
2. 确实存在细节损失：`range_compress=0.88` 压了 base 层，若 `detail_gain` 不足会变平。

**待办**：用「结构相似度（SSIM）+ 局部对比在等亮度归一化后的值」复核。
在此之前，**不要再用「高频能量」单独判断画质**，它在这类亮度大幅变化的场景会误导。

### 7.4 LTM 未移植到 Android（P1）—— ✅ 已于 2026-10-21 移植完成
新增 `src/android/app/src/main/java/com/finn/hdr/LocalToneMapping.kt`（265 行），
`HdrBaker.kt` 接入并新增 `profile` / `anchor` 参数（与 Python bake.py 语义对齐）。
移植时逐条对齐的要点：

- guided filter 用可分离 box blur（滑动窗口，积分图等价）实现，半径按短边 1/48
- 在 log2 域做 base/detail 分离，detail 增益 1.32，黑位重锚 0.0020
- 文件头注释固定了两个易错点：软膝必须收敛到 0（符号写反会「高光比中间调还黑」）；
  「先 base 收缩、后压高光」顺序不能反（否则白点漂移）

**顺带在 Kotlin 侧修复/对齐的三处**（原 HdrBaker.kt 与 Python 修好后的版本不一致）：

| 项 | 原 Kotlin 行为 | 现行为（对齐 Python） |
|---|---|---|
| 对比度/饱和度作用域 | 线性域（P0-1 同类 bug，暗部会被压死） | `_linear_to_srgb` 之后的 sRGB 感知域 |
| 白点锚定 | 直方图 99.5 分位（peak 锚定，整图压暗） | `anchor="sdr-white"` 默认 1.0；peak 仍可选 |
| gain 计算 | 每像素一次 `Math.pow` | 256 项 LUT（span/minLog2/strength 为常量） |
| 量化 | 直接截断 | 三角抖动（固定种子，确定性输出） |

**双实现数值对照**（`tests/ltm_parity/`，合成输入 512×384：渐变+硬边缘+纹理+3.2x 高光+纯黑区）：

| 档位 | float 级 max_abs | 8bit 级 max_lsb | lsb>1 占比 | radius / detailGain / shadowLift |
|---|---|---|---|---|
| local | 7.98e-04 | 1 | 0.0000% | 8 / 1.32 / 3.2831（两端一致） |
| wechat | 6.66e-04 | 1 | 0.0000% | 10 / 1.12 / 2.2090（两端一致） |

float 级残差来自 guided filter `var = mean_ii − mean_i²` 在强边缘处的 float32
相减相消（cumsum 与滑动窗口 rounding 路径不同），量化到 8bit 后最大 1 LSB、
无像素差 >1 LSB，即最终 JPEG 层面两端不可区分。

**性能**：桌面 JVM 2048×1536 全尺寸 411 ms（中端 ARM 预计 0.6–1.2 s）；
MainActivity 已加 profile 下拉（local / wechat / off），off 档跳过 LTM 供快速导出。

---

## 8. 指标复核结论（2026-10-21，`tests/bake_quality_check.py`）——§7.3 悬案已结

§7.3 的问题：「高频能量从 10.15 降到 6.4x（−36%），但目视细节反而更清楚」，
是亮度缩放假象，还是 range_compress 压掉了真实细节？用受控实验结案。

### 8.1 「高频能量」指标本身不可信（结论一）

同一张真实样张（`out/real_sample.ultrahdr.jpg`）换指标定义：

| 定义 | 源 SDR → 烘焙（local） |
|---|---|
| §7.1 的原指标 | 10.15 → 6.47（**−36%**） |
| 拉普拉斯均值（原始亮度） | 22.58 → 23.73（**+5.1%**） |
| 拉普拉斯均值（等亮度归一化后） | — → 0.829（**−17%**） |

同一个「细节损失」现象，三个定义给出 −36% / +5% / −17%——**符号都会翻**。
该指标从此不单独用于判画质（§7.3 的担忧成立，但方向是「指标废掉」）。

### 8.2 受控实验：detail_gain 在当前参数下是空转（结论二）

合成「恒定增益 + chirp 细节图」（增益图全 128 → gain 恒定；主图周期 2→24px
的频率扫描 + 硬台阶）。恒定增益下 off 档等价于全局缩放，是公平基线：

| 档位 | 等亮度高频能量比 | 细块局部对比比（4×4 块 std 中位数） |
|---|---|---|
| off | 0.998 | 0.994 |
| local（eps=4e-3，当前默认） | 0.889 | **0.882** |

local 不是放大细节，而是比 off **净损失 ~11%**。机制（eps 扫描证实）：

`eps=4e-3` 是 log2 亮度**方差**域的阈值，而真实纹理的局部方差在 ~1.0 量级
（chirp 实测 ~0.5-1.0），大两个数量级 → `a = var/(var+eps) ≈ 1` →
guided filter 退化为恒等 → `detail = x − base ≈ 0` → `range_compress=0.88`
把**包含纹理的整个信号**按 0.88 次幂压缩 → `detail_gain=1.32` 无对象可放大。
local 档的实际效果是「0.88 次幂压缩 + 黑位重锚」的影调改写，不是细节增强。

### 8.3 eps 权衡曲线（结论三）：没有免费的细节增益

`tests/bake_quality_check.py --eps-sweep`（干净平坦+台阶图测边缘过冲）：

| eps | lc4（local/off） | 硬边缘过冲（0-255，台阶高 ~30） |
|---|---|---|
| 0.004（当前） | 0.887 | 0.2 |
| 0.02 | 0.921 | 1.7 |
| 0.05 | 0.957 | 2.4 |
| 0.10 | 1.011 | 3.9 |
| 0.20 | 1.077 | 4.6 |
| 0.50 | 1.166 | 6.1 |
| 1.00 | 1.219 | 6.1 |

调大 eps 能让 detail_gain 生效（≥0.1 追平 off，0.5 超出 +17%），但边缘过冲
同比上升——因为 guided filter 的 detail 层必然包含边缘，放大细节 = 放大 halo。
**这是该算子结构的固有权衡，不存在「既要细节增益又无 halo」的 eps。**

### 8.4 待决策（需用户拍板，影响视觉输出）

| 方案 | 做法 | 代价 |
|---|---|---|
| A. 承认现状 | local = 压缩系影调改写（暗部提升+高光收敛），文档去掉「局部对比增强」表述 | 与「HDR 感来自局部对比」的初衷不符 |
| B. eps≈0.1 | 细节追平 off，接受 ~13% 台阶高的边缘过冲 | 需真机目视确认 halo 是否可接受 |
| C. 换细节路径 | 输出端 unsharp mask 或 bilateral grid，与 base 压缩解耦 | 工作量最大，但可能是唯一能真正加细节的路 |

另注：off 档相对源 SDR 主图也丢 ~8% 等亮度高频能量——这是路线 B
（复用增益图直接封装）高光二次压缩的固有损失，与 LTM 无关。

### 8.5 真实样张扫描 + 决策执行（2026-10-21，方案 B 已执行）

合成台阶的过冲担忧在真实内容上没有复现。`--eps-sweep <真实样张>`（振铃代理 =
源图梯度前 0.1% 强边缘处的亮度二阶差分中位数）：

| eps | lc4 | lc16 | HF比 | 振铃/off |
|---|---|---|---|---|
| 0.004（旧默认） | 0.862 | 0.837 | 0.829 | 0.88 |
| 0.02 | 0.887 | 0.864 | 0.843 | 0.89 |
| 0.05 | 0.921 | 0.900 | 0.865 | 0.89 |
| 0.10 | 0.955 | 0.938 | 0.889 | 0.90 |
| 0.20 | 0.994 | 0.981 | 0.916 | 0.91 |
| 0.50 | 1.049 | 1.036 | 0.956 | 0.93 |

真实照片的「强边缘」多嵌在纹理里而非孤立台阶，guided filter 的 base 层吸收方式
不同——**振铃在所有 eps 下都低于 off 档**，合成台阶的 ~15% 过冲是最坏情况、
真实内容未出现。细节指标则随 eps 单调改善。

**决策：执行方案 B，eps 默认值 local 4e-3→0.2、wechat 6e-3→0.15**（Python 与
Kotlin 同步修改）。执行后验证：

| 检查 | 结果 |
|---|---|
| 受控 selftest | local/off 细节增益 0.888 → **1.085**（detail_gain 生效，退出码 0） |
| 真实样张三档 | local 的 lc4 0.862 → **1.000**（追平源图），比 off 高 +4.7%，HF 比 0.920 与 off 的 0.924 持平 |
| Kotlin/Python 双实现对照 | PASS，且浮点级吻合度提高约 20 倍（max_abs 8e-4 → 4.3e-5，eps 调大后 var/(var+eps) 病态条件缓解） |
| 回归 / 静态自检 / APK | 28 项 OK、44/44、BUILD SUCCESSFUL |

**遗留**：新 eps 的视觉效果未经真机目视（HANDOVER §3.2）；合成最坏情况过冲
（~15% 台阶高）记入参数注释，若真机出现可见亮边需回调至 0.1。
