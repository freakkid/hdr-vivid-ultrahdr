# HANDOVER — 交接文档

> 最后更新：2026-10-20 23:45 ｜ 交接原因：换一台电脑继续
> 读这一篇就能接手，不需要回看聊天记录。

---

## 0. 30 秒接手

| 问题 | 答案 |
|---|---|
| 这是什么 | 把华为 HDR Vivid 静图导出为「SDR 主图 + 增益图 + XMP + MPF」的 Ultra HDR JPEG |
| 到哪一步了 | **主链路已打通并在真机验证过**：Android App 能识别 ISO 21496-1 / XMP / CUVA 三种源，导出后 10 项自检全过，产物经 Python 独立校验 19 项全过 |
| 明天第一件事 | 真机点一次导出，确认 `HDRCapacityMax` 修正（1 分钟，见 §3.1） |
| 仓库 | `git@github.com:freakkid/hdr-vivid-ultrahdr.git`（私有），分支 `main` |
| 最新提交 | 见 `git log --oneline -1` |

**一句话状态：编码链路已验证可用；剩下的是「真实样张覆盖」与「第三方平台存活率」两类实测，以及一条未实现的重算路线。**

---

## 1. 仓库与环境

### 1.1 仓库结构

```
hdr-vivid-ultrahdr/
├── docs/
│   ├── 00-landscape.md      生态调研（libultrahdr / pyultrahdr / uhdrtool / Lightroom 基线）
│   ├── 01-design.md         主设计 A–H（结论/格式/管线/代码/微信/测试/风险/待核实）
│   ├── 02-verification.md   已验证事实 + 今晚真机排障全过程（§2.7 必读）
│   └── 03-toolchain-setup.md 构建环境安装（dl.google.com 不可达方案）
├── src/android/             ★ Android 实现（交付物）
├── src/python/uhdr/         容器参考实现与校验器（工具链，不进 APK）
├── src/harmony/, src/ets/   HarmonyOS/ArkTS 链路（骨架，未真机验证）
├── scripts/                 工程自检、工具链安装、一键提交
├── tests/                   回归测试 + 实测矩阵模板
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

# 4) 验证 Python 侧（27 项测试，1.6 秒）
./run.sh test
python scripts/check_android_project.py     # 工程静态自检 40 项

# 5) 构建 APK
export JAVA_HOME="$HOME/.workbuddy/binaries/jdk/17"
export ANDROID_HOME="$HOME/Android/Sdk"
cd src/android && ./gradlew assembleDebug --no-daemon
```

**注意**：本机 `dl.google.com` 完全不可达，所以 SDK 是「镜像下载 + 手工铺目录」装的（`sdkmanager` 用不了）。细节与三个必踩的坑见 `docs/03-toolchain-setup.md` §3.3。

如果新机器能正常访问 Google，直接装 Android Studio 更省事，无需上面的手工流程。

---

## 2. 已完成（有证据）

| 能力 | 状态 | 证据 |
|---|---|---|
| 识别 ISO 21496-1 载体（无 XMP） | ✅ | 真机：`容器：ISO_ULTRA_HDR / 增益图：332469 B` |
| 识别 XMP GContainer 载体 | ✅ | `out/sample.ultrahdr.jpg` 自检 19 项 PASS |
| 识别 CUVA（APP5 副图） | ✅ | 合成源 `samples/sample_cuva_3000x2250.jpg` |
| 增益图同步缩放 / 裁剪 / 旋转 | ✅ | 单测覆盖 1/4、0.3556 两种比例，误差 0 |
| 预压缩固定 2048 长边 | ✅ | 5760×4320 → 2048×1536，增益图 2048×1536 → 728×546 |
| Ultra HDR 容器封装 | ✅ | 纯 Kotlin 实现，无 NDK 也能出包 |
| 导出后结构自检 | ✅ | 真机 10 项全过；桌面 19 项全过 |
| 可安装 APK | ✅ | 2.2 MB，`com.finn.hdr`，minSdk 26 / targetSdk 34 |
| 双向交叉验证 | ✅ | Android 产物拉回桌面，Python 独立解析 19 项 PASS |
| Python 回归 | ✅ | 27 项通过，含 3 条负向（截断/篡改 offset/删 XMP） |

---

## 3. 未完成（按优先级）

### 3.1 【P0，1 分钟】真机确认 HDRCapacityMax 修正

代码已改（`40ef88b`）并构建安装，但**最后一版没在真机上跑过**（自动化操作在 picker 处被系统打断）。

操作：打开「HDR Vivid 导出器」→「选择 HDR 照片并导出」→ 选 `Download/IMG_20261018_182526_111421.heic`

判定：出现 `validate: ok=true errors=0` 且尾部「已写出 …HDR.jpg」即通过。
若出现 `✗ gainmap_xmp_present` 或 `✗ aspect_consistent`，把整屏文字发出来。

### 3.2 【P1】真实 HDR Vivid 样张覆盖

目前只有 1 张真实样张（ISO 载体）。**Mate 30（麒麟 990）拍不出 HDR Vivid**，它只能当接收/显示端。

需要补齐：逆光人像、高饱和、暗部细节、纯色背景 各 2–3 张，用来验证：
- 增益图语义换算是否引入偏色（对比 HDR 合成结果做 ΔE）
- 极端动态范围下 `max_content_boost` 会不会超界

获取途径：另一台华为设备（Mate 40/P50 之后）、或在网上找 `.jpg` 格式的 Vivid 样张。

### 3.3 【P1】微信三路径实测

模板：`tests/acceptance-matrix.md`、`tests/test_matrix.md`。需要三台设备（发送端/接收端/对照端），大量收发。

**这是唯一还没做、但直接决定产品能否对外承诺的部分。**

### 3.4 【P2】路线 A：线性光重算

当前走的是路线 B（复用源增益图直接封装），CUVA 源可能存在色偏。路线 A 需要：
解 HDR（PQ/HLG → 线性 + BT.2020 变换）→ 重新 tone map 出 SDR → 用 libultrahdr **API-1** 重算增益图。

需要先把 libultrahdr 编进 APK（`bash scripts/fetch-libultrahdr.sh` + `./gradlew assembleDebug -PwithNative=true`），且 JNI 侧目前只实现了 API-4 与 probe。

### 3.5 【P2】HEIC（ISOBMFF）容器不支持

当前只解析 JPEG。若源图是真 HEIC（`ftyp` 开头），会直接判 `plain_jpeg`。华为部分机型/设置下会产出 HEIC。

需引入 libheif 或 Android 的 `ImageDecoder` 读辅助图。**注意**：本次那张 `.heic` 其实是 JPEG（扩展名骗人），先看魔数再判断是否需要这条。

### 3.6 【P3】native 路径未验证

`uhdr_jni.cpp` 写完了但从未编译过（本机无 NDK）。`-PwithNative=true` 时才会编，App 会自动回退到纯 Kotlin 封装（日志里会写 `native 不可用（UnsatisfiedLinkError）`）。

---

## 4. 关键决策记录（避免后人推翻）

| 决策 | 原因 |
|---|---|
| Python + Kotlin 双实现 | 判断容器写得对不对，不能用写它的同一份代码。今晚的真机 bug 正是靠双实现对照、20 分钟定位 |
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
python scripts/check_android_project.py    # Android 工程静态自检 40 项

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

## 9. 明天第一步

```bash
git pull                                    # 确认拿到最新（当前 54d82ff 之后还有交接提交）
cd src/android && ./gradlew assembleDebug   # 环境若已就绪，应 20 秒出包
```

然后按 §3.1 在真机点一次，把结果补进 `docs/02-verification.md` §2.7 的「修复后的真机结果」。

**如果只有 10 分钟**：做 §3.1。**如果有半天**：§3.2 + §3.3 一起推，把实测矩阵填满。
