# HANDOVER — 交接文档

> 最后更新：2026-10-21（LTM 已移植并提交 e01bb9a；本机环境已重建；清理已完成条目）
> 读这一篇就能接手，不需要回看聊天记录。

---

## 0. 30 秒接手

| 问题 | 答案 |
|---|---|
| 这是什么 | 把华为 HDR Vivid 静图导出为「SDR 主图 + 增益图 + XMP + MPF」的 Ultra HDR JPEG |
| 到哪一步了 | Ultra HDR 主链路与烘焙链路（HDR → 普通 JPEG，含 App 端局部色调映射）均已实现并通过自动化验证；本机构建环境已就绪。剩下的全是真机实测类事项（§3） |
| 明天第一件事 | 打开 `out/visual/` 目视 LTM 三档效果（§3.1，材料已备好）；导出链路与 HDRCapacityMax 已在模拟器验证（docs/02 §2.8） |
| 仓库 | `git@github.com:freakkid/hdr-vivid-ultrahdr.git`（私有），分支 `main` |
| 最新提交 | 见 `git log --oneline -1` |

---

## 1. 仓库与环境

### 1.1 仓库结构

```
hdr-vivid-ultrahdr/
├── docs/
│   ├── 00-landscape.md      生态调研（libultrahdr / pyultrahdr / uhdrtool / Lightroom 基线）
│   ├── 01-design.md         主设计 A–H（结论/格式/管线/代码/微信/测试/风险/待核实）
│   ├── 02-verification.md   已验证事实 + 真机排障全过程（§2.7）
│   ├── 03-toolchain-setup.md 构建环境安装（dl.google.com 不可达方案）
│   └── 04-HANDOFF-bake.md   烘焙链路算法决策记录
├── src/android/             ★ Android 实现（交付物）
├── src/python/uhdr/         容器参考实现与校验器（工具链，不进 APK）
├── src/harmony/, src/ets/   HarmonyOS/ArkTS 链路（骨架，未真机验证）
├── scripts/                 工程自检、工具链安装、一键提交
├── tests/                   回归测试 + 双实现对照 + 实测矩阵模板
└── out/                     产物：APK、截图、样例
```

### 1.2 新电脑上从零跑起来

```bash
# 1) 拉代码
git clone git@github.com:freakkid/hdr-vivid-ultrahdr.git
cd hdr-vivid-ultrahdr

# 2) 装 JDK 17 + Android SDK 34（镜像方案，约 10 分钟）
bash scripts/install_android_toolchain.sh

# 3) 生成 local.properties（每台机器不同，故意不入库）
printf 'sdk.dir=C\\:\\\\Users\\\\<你的用户名>\\\\Android\\\\Sdk\n' > src/android/local.properties

# 4) 验证 Python 侧（27 项测试，1 秒）
./run.sh test
python scripts/check_android_project.py     # 工程静态自检 43 项

# 5) 构建 APK
export JAVA_HOME="$HOME/.workbuddy/binaries/jdk/17"
export ANDROID_HOME="$HOME/Android/Sdk"
cd src/android && ./gradlew assembleDebug --no-daemon
```

**注意**：本机 `dl.google.com` 完全不可达，所以 SDK 是「镜像下载 + 手工铺目录」装的（`sdkmanager` 用不了）。细节与三个必踩的坑见 `docs/03-toolchain-setup.md` §3.3。

如果新机器能正常访问 Google，直接装 Android Studio 更省事，无需上面的手工流程。

### 1.3 本机（2026-10-21，macOS）实际环境——已验证可用

| 组件 | 位置 / 版本 | 备注 |
|---|---|---|
| JDK 17 | `/opt/homebrew/opt/openjdk@17`（17.0.20.1） | `brew install openjdk@17`；系统默认 java 是 16，**必须显式 `export JAVA_HOME`** |
| Android SDK | `~/Library/Android/sdk`（build-tools 35.0.0 / platforms 34+35） | `local.properties` 写 `sdk.dir=/Users/huangnanxuan/Library/Android/sdk`（未入库） |
| Gradle | wrapper 8.7（腾讯镜像已缓存） | 首次构建约 2.5 分钟（拉 AGP 8.5.2 / Kotlin 1.9.24），增量约 20 秒 |
| Python venv | `~/.workbuddy/binaries/python/envs/default/bin/python` | 需 `pip install pillow`（numpy 已有） |

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export ANDROID_HOME=~/Library/Android/sdk
cd src/android && bash gradlew assembleDebug --no-daemon   # gradlew 无可执行位，用 bash 调
```

双实现数值对照（需 kotlinc，见 `tests/ltm_parity/run_parity.sh`）：
`bash tests/ltm_parity/run_parity.sh`（kotlinc 缺失时自动跳过，不算失败）。

---

## 2. 基线状态

Ultra HDR 主链路与烘焙链路均已实现并通过验证：回归 27 项、静态自检 43 项、
APK 可构建、Kotlin/Python 双实现数值对照通过、**模拟器端到端实测通过**
（App 产物经 Python 独立校验 20/20，App 与 Python 烘焙输出 SSIM 0.95，
细节见 docs/02 §2.8）。**已完成项不在此罗列**——证据在 `git log` 与
`docs/02-verification.md`，烘焙算法决策在 `docs/04-HANDOFF-bake.md`。
尚未做的只有 §3。

---

## 3. 未完成（按优先级）

### 3.1 【P1】真实 HDR Vivid 样张覆盖 + App 端 LTM 目视确认

目前只有 1 张真实样张（ISO 载体）。**Mate 30（麒麟 990）拍不出 HDR Vivid**，它只能当接收/显示端。

需要补齐：逆光人像、高饱和、暗部细节、纯色背景 各 2–3 张，用来验证：
- 增益图语义换算是否引入偏色（对比 HDR 合成结果做 ΔE）
- 极端动态范围下 `max_content_boost` 会不会超界
- App 端 LTM 三档目视：profile 下拉切 local / wechat / off 各导一次，
  重点看有无 halo（eps 已于 2026-10-21 调至 0.2，合成最坏情况有 ~15% 台阶高
  过冲，真实样张未测出；若真机见可见亮边，回调至 0.1）、灰雾、「假 HDR」感

获取途径：另一台华为设备（Mate 40/P50 之后）、或在网上找 `.jpg` 格式的 Vivid 样张。

**目视材料已备好**（`out/visual/`，gitignored）：`app_local.jpg`（模拟器实跑产物）、
`py_off/local/wechat.jpg`（Python 同参三档）。在 Mac 上直接打开即可开始看，
不用等设备。HDRCapacityMax 与导出链路已在模拟器验证通过（docs/02 §2.8）。

### 3.2 【P1】微信三路径实测

模板：`tests/acceptance-matrix.md`、`tests/test_matrix.md`。需要三台设备（发送端/接收端/对照端），大量收发。

**这是直接决定产品能否对外承诺的部分。**

**代理测试已做**（`tests/wechat_proxy.py`，2026-10-21，无真机下界证据）：

| 路径 | 结果 |
|---|---|
| 勾选原图（字节不变） | HDR 完整保留（20/20 PASS）——**只有这条路能承载 Ultra HDR** |
| 不勾选原图（任何像素重压缩） | XMP/MPF/增益图**全部丢失**（4 种尺寸/质量组合均如此）——下界证据，微信若更狠结果一样 |
| 烘焙图效果存活率 | 原尺寸 q80：SSIM 0.91；长边 1920：0.90；1440：0.83；1080：0.77——**HDR 观感扛压缩但随降尺寸衰减**，这是烘焙图给非原图路径的答案 |

结论：对外话术必须是「原图路径发 Ultra HDR，普通发送发烘焙图」双轨，
不能承诺「微信发图直接看 HDR」。真机实测待做（验证微信是否比重压缩更狠/更慈悲）。

### 3.3 【P2】路线 A：线性光重算

当前走的是路线 B（复用源增益图直接封装），CUVA 源可能存在色偏。路线 A 需要：
解 HDR（PQ/HLG → 线性 + BT.2020 变换）→ 重新 tone map 出 SDR → 用 libultrahdr **API-1** 重算增益图。

需要先把 libultrahdr 编进 APK（`bash scripts/fetch-libultrahdr.sh` + `./gradlew assembleDebug -PwithNative=true`），且 JNI 侧目前只实现了 API-4 与 probe。

### 3.4 【P2】HEIC（ISOBMFF）容器不支持

当前只解析 JPEG。若源图是真 HEIC（`ftyp` 开头），会直接判 `plain_jpeg`。华为部分机型/设置下会产出 HEIC。

需引入 libheif 或 Android 的 `ImageDecoder` 读辅助图。**注意**：本次那张 `.heic` 其实是 JPEG（扩展名骗人），先看魔数再判断是否需要这条。

### 3.5 【P3】native 路径未验证

`uhdr_jni.cpp` 写完了但从未编译过（本机无 NDK）。`-PwithNative=true` 时才会编，App 会自动回退到纯 Kotlin 封装（日志里会写 `native 不可用（UnsatisfiedLinkError）`）。

---

## 4. 关键决策记录（避免后人推翻）

| 决策 | 原因 |
|---|---|
| Python + Kotlin 双实现 | 判断容器写得对不对，不能用写它的同一份代码。此前的真机 bug 正是靠双实现对照、20 分钟定位 |
| 完整 hdrgm 参数写在**增益图**自身 XMP，主图只留 `Version` | 规范要求参数随增益图走；只写一处时部分解码器识别不到。**validator 断言要跟着这个设计走** |
| 固定 2048，不用 2050 | 2050 无收益、非 2 的幂、增加越界风险；Lightroom Mobile 的「Small (2048px)」预设是业界基线 |
| 2048 只减害尺寸压缩，不解决元数据剥离 | 这两件事必须分开对用户讲，否则是错误承诺 |
| ISO 21496-1 优先级高于 `_cuva` 标记 | 华为文件即使转成 ISO 容器仍保留 `_cuva` 字样，把它当强证据会误判 |
| 增益图只能从原始增益图重采样 | 由缩放后的主图反推会二次采样，高光错位 |

---

## 5. 已知坑（都踩过）

### 5.1 环境

| 坑 | 现象 | 解 |
|---|---|---|
| `dl.google.com` 不可达 | SDK/Gradle 下载卡死 | 腾讯云 AndroidSDK 镜像 + 手工铺目录 |
| `build-tools` 包顶层目录是 `android-14` | 解压后 AGP 找不到 | 必须改名放到 `build-tools/34.0.0/` |
| 缺 licenses 文件 | `licenses have not been accepted` | 手写 3 个 hash（见 docs/03 §3.3-2） |
| `local.properties` 转义 | `SDK location not found` | `sdk.dir=C\:\\Users\\...\\Android\\Sdk` |
| 模拟器 Photo Picker 不显示 push 的文件 | picker 显示 "No photos or videos" | Picker 只认媒体库：push 后还需 `content insert` 插一条 MediaStore 记录（命令见 docs/02 §2.8）；picker 已打开时不刷新，需 BACK 关掉重开 |
| `gradlew` 无可执行位 | `permission denied: ./gradlew` | 用 `bash gradlew` 调 |

### 5.2 真机（华为 Mate 30 / EMUI）

| 坑 | 现象 | 解 |
|---|---|---|
| HDB 通道 | 设备管理器有 `Huawei HDB Interface` 但 adb 空列表 | 开「USB 调试」+「仅充电时允许 ADB 调试」，USB 用途改「传输文件」 |
| **免打扰拦截授权弹窗** | `unauthorized`，换 adbkey 也不弹 | 关免打扰，或「撤销 USB 调试授权」后重插 |
| MSYS 路径改写 | `adb pull` 报 `...PortableGit/sdcard/...` | 命令前加 `MSYS_NO_PATHCONV=1` |
| 后台被回收 | picker 占前台后 App 被杀，无崩溃日志 | 手动操作；后续可做「导出后立即通知」保持前台优先级 |

### 5.3 格式

| 坑 | 现象 | 解 |
|---|---|---|
| 扩展名不可信 | `.heic` 实际是 JPEG | 一律看魔数：`FFD8FFE1`=JPEG，`....ftyp`=ISOBMFF |
| **扫描边界 `pos+3`** | 副图被静默丢弃，只剩主图 | 必须 `pos+1 < size`（EOI 可能贴文件尾）。Python 对、Kotlin 错过 |
| XMP 段长度少 2 字节 | 段边界漂移 | `len(payload)+2`；BOM 是 1 字符 3 字节 |
| `HDRCapacityMax` 写线性值 | 宣称 16 倍容量 | 该字段是 log2 域 |

---

## 6. 常用命令

```bash
# Python 侧
./run.sh test                              # 27 项回归
./run.sh demo                              # 端到端演示（合成源）
python -m uhdr.cli inspect  <file>         # 容器结构诊断
python -m uhdr.cli validate <file>         # 结构自检
python scripts/check_android_project.py    # Android 工程静态自检（43 项）
bash tests/ltm_parity/run_parity.sh        # Kotlin/Python LTM 双实现数值对照（需 kotlinc）
python tests/bake_quality_check.py <源.jpg>   # 烘焙画质体检（SSIM/等亮度HF/局部对比，信息性）
python tests/bake_quality_check.py --selftest # 受控实验：判决 detail_gain 是否生效
python tests/bake_quality_check.py --eps-sweep # eps 权衡曲线（细节增益 vs 边缘过冲）
python tests/wechat_proxy.py <file.jpg>      # 微信路径代理测试（元数据存活 + 效果存活率）

# Android 侧
cd src/android
./gradlew assembleDebug --no-daemon                     # 不带 native
bash ../../scripts/fetch-libultrahdr.sh                 # 需要 native 时
./gradlew assembleDebug -PwithNative=true

# 真机
adb install -r out/HdrVividUltraHdr-0.1.0-debug.apk
adb exec-out run-as com.finn.hdr cat cache/exports/<file>.jpg > pulled.jpg   # 拉产物
adb logcat -d -b crash | grep finn                                          # 看崩溃

# 提交（约定：改完直接推）
./scripts/ship.sh "fix: 说明"
```

---

## 7. 待核实清单（H-1 ~ H-11）

详见 `docs/01-design.md` H 章。最关键四条：

| # | 问题 | 影响 |
|---|---|---|
| H-1 | 微信「勾选原图」是否保留 XMP/MPF | 决定能否对用户承诺 |
| H-2 | 微信图片通道当前长边阈值（1080/1920 说法不一） | 决定预压缩目标值 |
| H-5 | CUVA 增益图 → hdrgm 的精确换算 | 决定路线 A 的色彩精度 |
| H-11 | ISO 21496-1 的 header 5 字节语义与字段顺序 | 当前按实测布局回退，gamma 保守取 1.0 |

---

## 8. 隐私提示（重要）

`out/` 下两个文件含**你的真实照片内容**，已推送到私有仓库：

| 文件 | 内容 | 建议 |
|---|---|---|
| `out/real_sample.ultrahdr.jpg` | 由你的样张导出的 HDR 文件（1.5 MB） | 若要移除：`git rm out/real_sample.ultrahdr.jpg && ./scripts/ship.sh "chore: 移除含隐私的样例产物"`（历史仍保留，彻底清除需 rewrite history） |
| `out/app_screenshot.png` | App 界面截图（曾一度截到通知栏） | 同上 |

仓库是私有的，风险可控。若将来要开源，**务必先清理这两个文件并重写历史**。

---

## 9. 时间分配建议

构建/安装命令见 §1.3 与 §6。真机结果补进 `docs/02-verification.md` §2.7。

**如果只有 10 分钟**：目视 `out/visual/` 三档图（§3.1）。**如果有半天**：§3.1 + §3.2 一起推，把实测矩阵填满。

---

## 10. 烘焙模式（2026-10-21 新增，与 Ultra HDR 并行）

### 10.1 两种输出，用途不同

| 输出 | 文件名 | 谁能看到效果 | 代价 |
|---|---|---|---|
| **烘焙图** | `IMG_xxx_BAKED.jpg` | **任何设备、任何平台（含微信）** | HDR 信息永久压成 8bit，不可逆 |
| Ultra HDR | `IMG_xxx_HDR.jpg` | 仅 Android 14+ / 支持 Ultra HDR 的软件 | 依赖接收端能力 |

App 一次选图**同时产出两者**，主按钮分享烘焙图。

### 10.2 算法与定档

```
sRGB → 线性 → 按 hdrgm 公式乘增益 → 白点归一化 → 高光软膝
     → sRGB → S 曲线 + 饱和度 → 抖动 → JPEG
```

- `strength = 0.70`（目视定档，见 `docs/04-HANDOFF-bake.md` §7）
- 三档 profile：`off`（仅全局）/ `local`（+局部色调映射）/ `wechat`（克制版，为二次压缩留余量）
- 命令：`python -m uhdr.cli bake <src> -o out.jpg --profile local --strength 0.70`

### 10.3 烘焙相关的未完成项

| 优先级 | 事项 | 说明 |
|---|---|---|
| P2 | 微信档实测 | `wechat` profile 的 detail_gain=1.12 未经真机二次压缩验证 |

（真实样张覆盖与 LTM 目视即 §3.1，不重复列。LTM 参数决策已于 2026-10-21
执行：eps 调至 0.2/0.15，依据与验证数据见 docs/04 §8.5。）

### 10.4 踩过的坑

- **`contrast` 不能在线性域做**：线性 0.5 ≈ sRGB 188，S 曲线会把暗部钉死成纯黑（实测 19.8 → 0.7）。必须在 `_linear_to_srgb` 之后。
- 暗部亮度不能作为验收硬指标：源图自身暗部带增益，烘然后暗部必然高于原图，应看「无灰雾、无死黑、高光裁切 <1%」。
