# hdr-vivid-ultrahdr

> **接手项目先读 [HANDOVER.md](HANDOVER.md)** —— 现状、待办优先级、环境搭建、已知坑、常用命令都在里面。

把**华为 HDR Vivid 静图**转换为 **Ultra HDR 式 JPEG**（SDR 主图 + 增益图 + XMP + MPF）的
工程骨架与可运行参考实现。

- 目标：普通设备当 SDR 看（不发灰、不过曝），支持 Ultra HDR 的设备能读增益图还原 HDR
- 边界：**编码保真在本地可控，第三方平台是否保留元数据不可控**（详见 `docs/01-design.md` 第 A 章）

## 源码导航（Android 实现在这里）

Android 源码目录较深（`src/android/app/src/main/java/com/finn/hdr/`），GitHub 上容易被折叠，直接点下面：

| 模块 | 文件 | 行数 |
|---|---|---|
| 入口界面（选图 → 双轨导出 + profile/强度选择） | [MainActivity.kt](src/android/app/src/main/java/com/finn/hdr/MainActivity.kt) | 256 |
| **HDR 源解析**（ISO 21496-1 / XMP / CUVA 判定） | [HdrSourceParser.kt](src/android/app/src/main/java/com/finn/hdr/HdrSourceParser.kt) | 326 |
| **ISO 21496-1 元数据** | [IsoGainMap.kt](src/android/app/src/main/java/com/finn/hdr/IsoGainMap.kt) | 129 |
| 增益图几何变换 | [GainMapGeometry.kt](src/android/app/src/main/java/com/finn/hdr/GainMapGeometry.kt) | 110 |
| **Ultra HDR 封装**（纯 Kotlin，无 NDK 也能跑） | [UltraHdrPackager.kt](src/android/app/src/main/java/com/finn/hdr/UltraHdrPackager.kt) | 199 |
| **烘焙**：HDR → 普通 JPEG（微信/任何设备可见） | [HdrBaker.kt](src/android/app/src/main/java/com/finn/hdr/HdrBaker.kt) | 264 |
| **局部色调映射**（guided filter，移植自 ltm.py） | [LocalToneMapping.kt](src/android/app/src/main/java/com/finn/hdr/LocalToneMapping.kt) | 265 |
| 导出编排 | [UltraHdrExporter.kt](src/android/app/src/main/java/com/finn/hdr/UltraHdrExporter.kt) | 142 |
| 导出后自检 | [MetadataValidator.kt](src/android/app/src/main/java/com/finn/hdr/MetadataValidator.kt) | 223 |
| 分享引导（文件/原图/直接发图） | [ShareGuidance.kt](src/android/app/src/main/java/com/finn/hdr/ShareGuidance.kt) | 97 |
| libultrahdr JNI 门面 | [UltraHdrNative.kt](src/android/app/src/main/java/com/finn/hdr/UltraHdrNative.kt) | 64 |
| JNI 实现（API-4） | [uhdr_jni.cpp](src/android/app/src/main/cpp/uhdr_jni.cpp) | 184 |
| 构建配置 | [app/build.gradle.kts](src/android/app/build.gradle.kts) ｜ [CMakeLists.txt](src/android/app/src/main/cpp/CMakeLists.txt) | — |

合计 **Kotlin 1891 行 + C++ 184 行**，产物 `app-debug.apk` 2.2 MB。

> `src/android/local.properties` 是唯一未入库的文件：它记录本机 SDK 绝对路径，
> 每台机器不同，已由 `.gitignore` 排除（Android 项目通行做法）。缺它时构建会提示
> `SDK location not found`，按 `docs/03-toolchain-setup.md` 的 3.3-3 重建即可。

## 这是什么 / 不是什么

这是**一套 JPEG 容器格式工程**，落地形态有两个 App 实现 + 一套桌面工具链：

| 类别 | 目录 | 语言 | 是否进安装包 |
|---|---|---|---|
| Android App（主实现） | `src/android/` | Kotlin + C++(JNI) | ✅ 进（APK 2.2 MB，110 条目，**0 个 Python 文件**） |
| HarmonyOS App（另一条链路） | `src/harmony/`、`src/ets/` | ArkTS | ✅ 进对应 HAP |
| 网页版原型（**未完成，已冻结**） | `web/index.html` | 单文件 HTML+JS | ❌ 见下方说明 |
| 容器参考实现与校验器 | `src/python/uhdr/` | Python | ❌ 不进，只在开发机/CI 跑 |
| 回归测试 | `tests/` | Python | ❌ 不进 |
| 工具链脚本 | `scripts/` | Python / Shell | ❌ 不进 |

> **`web/` 状态：未完成的原型，已冻结，不再继续开发。**
> 已完成的部分：JPEG 段解析（ISO 21496-1 / XMP / CUVA 判定、增益图切片）与 Node 侧解析对齐验证
> （与 Python 结果一致：容器 `iso`、增益图 332,469 B、`GainMapMax=2.1484`）。
> 未完成的部分：像素烘焙未与桌面版做数值对照，未在真机浏览器验证，无错误兜底。
> 保留它只是因为解析代码可复用；**不要把它当作可用产物**。

**为什么要有 Python**（不是重复劳动）：

1. **独立校验**：判断「写出的容器对不对」不能用写它的同一份代码。Python 解析器与 Kotlin
   实现由两套独立代码互证——真机产物拉回桌面跑 `uhdr.cli validate`，是唯一的外部证据。
2. **桌面取证**：真机上「增益图不见了」这类问题，必须把文件拉下来逐段拆（本次就是靠它
   定位到 ISO 21496-1 载体缺失 + Kotlin 的 `pos+3` 边界 bug）。
3. **快速回归**：Python 27 项测试 1.6 秒，Android 构建 20–60 秒，反馈速度差一个量级。

如果你只需要 App、不打算维护桌面工具链，见文末「收敛为纯安卓项目」。

## 目录

```
hdr-vivid-ultrahdr/
├── docs/
│   ├── 00-landscape.md       ← 生态调研（libultrahdr / pyultrahdr / uhdrtool / Lightroom 基线）
│   ├── 01-design.md          ← 主设计：A 结论 / B 格式 / C 管线 / D 代码 / E 微信 / F 测试 / G 风险 / H 待核实
│   ├── 02-verification.md    ← 已验证事实：20 项单测、19 项自检、5 条踩坑记录
│   ├── 03-toolchain-setup.md ← 构建环境安装（dl.google.com 不可达方案）
│   ├── 04-HANDOFF-bake.md    ← 烘焙算法决策记录
│   └── 05-harmonyos-zhuoyitong.md ← 鸿蒙手机装本 APK 的方法（卓易通容器）
├── src/python/uhdr/          ← 可运行参考实现（容器解析 / 组装 / 自检 / 几何变换）
├── src/android/              ← 可编译 APK 工程（Kotlin + 可选 JNI），含 gradlew/wrapper，见其 README
├── src/ets/                  ← ArkTS 交付骨架（6 个模块）
├── src/harmony/              ← HarmonyOS 另版实现（Picture/GAINMAP 读取 + 导出编排）
├── web/index.html            ← 网页版原型（未完成，已冻结，勿当产物使用）
├── scripts/
│   ├── check_android_project.py  ← 无 SDK 也能跑的工程静态自检（40 项）
│   └── fetch-libultrahdr.sh      ← 拉取第三方库（仅 native 路线需要）
├── tests/
│   ├── test_pipeline.py    ← 20 项回归测试（含 3 项负向）
│   ├── ltm_parity/         ← Kotlin/Python 双实现数值对照（需 kotlinc，缺失自动跳过）
│   ├── acceptance-matrix.md ← 验收矩阵
│   └── test_matrix.md      ← 真机实测台账模板
├── samples/                  ← 合成的 CUVA 风格测试源
└── out/                      ← 生成产物与自检报告
```

## 快速开始

```bash
# 端到端演示：合成 CUVA 源 → 导出 → 17 项自检
python -m uhdr.cli demo --root .

# 诊断任意 JPEG（是否含增益图、容器类型、元数据完整性）
python -m uhdr.cli inspect path/to/photo.jpg

# 导出
python -m uhdr.cli export path/to/photo.jpg -o out/photo.hdr.jpg --long-edge 2048

# 自检
python -m uhdr.cli validate out/photo.hdr.jpg

# 回归测试
python -m unittest discover -s tests -v

# Android 工程静态自检（不需要 Android SDK，40 项）
python scripts/check_android_project.py
```

Python 依赖仅 `Pillow`（用于像素采样）。运行环境建议使用隔离 venv。

## 当前状态

| 项 | 状态 |
|---|---|
| CUVA / ISO 容器识别 | 已实现（合成源验证；真实样张待验证） |
| XMP / MPF 构造与解析（含增益图侧双写） | 已实现，往返测试通过 |
| 增益图同步缩放 / 裁剪 / 旋转 | 已实现，比例断言通过 |
| 端到端导出 + 自检 | 通过（19 项全 PASS） |
| Android 工程骨架（Gradle/Manifest/Activity/Wrapper） | 已补齐，静态自检 40 项通过；**BUILD SUCCESSFUL，APK 2.1 MB** |
| 构建环境（JDK 17 + SDK 34 + Gradle 8.7） | 已在本机安装并验证，镜像方案见 `docs/03-toolchain-setup.md` |
| CI 云端出包（GitHub Actions） | 已配置，推送后可直接下载 APK |
| 纯 Kotlin 封装器（无 NDK 也能跑路线 B） | 已完成，可在任意 Android Studio 环境编出 APK |
| Android JNI（libultrahdr API-4） | 代码完成，**需 NDK + submodule 后编译验证** |
| HarmonyOS ArkTS | 代码骨架完成，**待真机验证 `getAuxiliaryPicture(GAINMAP)`** |
| 微信三路径实测 | **未开始**，模板见 `tests/test_matrix.md` |

## 关键约束（违反会导致 HDR 静默丢失）

1. 读 HDR 源必须走 `Picture`，不能用 `createPixelMap`（会丢增益图）
2. 增益图只能从**原始增益图**重采样，不能由缩放后的主图反推
3. 主图缩放 / 裁剪 / 旋转，增益图必须同参变换，并重算所有 offset / length
4. EXIF `Orientation` 必须先归一化（物理旋转后置 1）
5. 导出后必须自检，失败不得宣称「HDR 已保留」
6. 剥离源图的 `_cuva` 标记，避免被误判为 CUVA 容器

## 不在本仓库范围

- 真实华为样张（受版权与隐私限制，需自备）
- libultrahdr 源码（建议以 submodule 引入 `third_party/libultrahdr`）
- CUVA → hdrgm 的精确色彩换算（属 H-5 待核实项）

## 收敛为纯安卓项目（可选）

如果你只交付 App、不打算长期维护桌面工具链，可以这样做而**不影响 App 任何功能**：

```bash
# 1) 工具链移出源码树，语义上降级为 devtools
mkdir -p tools && git mv src/python tools/uhdr-ref && git mv scripts tools/scripts
# 2) 同步 CI 与文档路径（.github/workflows/android-build.yml 里的 PYTHONPATH）
# 3) App 侧只保留 src/android/ + third_party/libultrahdr
```

代价：失去「独立解析器交叉验证」与「1.6 秒快速回归」，出问题时只能靠真机 logcat 盲猜。
本次那个「桌面通过、真机必挂」的边界 bug，正是靠双实现对照才在 10 分钟内定位的。

保留建议：**Kotlin 与 Python 两套解析器至少留一套独立校验**，否则容器写错了没人能发现。
