# hdr-vivid-ultrahdr

把**华为 HDR Vivid 静图**转换为 **Ultra HDR 式 JPEG**（SDR 主图 + 增益图 + XMP + MPF）的
工程骨架与可运行参考实现。

- 目标：普通设备当 SDR 看（不发灰、不过曝），支持 Ultra HDR 的设备能读增益图还原 HDR
- 边界：**编码保真在本地可控，第三方平台是否保留元数据不可控**（详见 `docs/技术方案.md` 第 A 章）

## 目录

```
hdr-vivid-ultrahdr/
├── docs/
│   ├── 00-landscape.md       ← 生态调研（libultrahdr / pyultrahdr / uhdrtool / Lightroom 基线）
│   ├── 01-design.md          ← 主设计：A 结论 / B 格式 / C 管线 / D 代码 / E 微信 / F 测试 / G 风险 / H 待核实
│   └── 02-verification.md    ← 已验证事实：20 项单测、19 项自检、5 条踩坑记录
├── src/python/uhdr/          ← 可运行参考实现（容器解析 / 组装 / 自检 / 几何变换）
├── src/android/              ← 可编译 APK 工程（Kotlin + 可选 JNI），含 gradlew/wrapper，见其 README
├── src/ets/                  ← ArkTS 交付骨架（6 个模块）
├── src/harmony/              ← HarmonyOS 另版实现（Picture/GAINMAP 读取 + 导出编排）
├── scripts/
│   ├── check_android_project.py  ← 无 SDK 也能跑的工程静态自检（40 项）
│   └── fetch-libultrahdr.sh      ← 拉取第三方库（仅 native 路线需要）
├── tests/test_pipeline.py    ← 20 项回归测试（含 3 项负向）
├── tests/acceptance-matrix.md ← 验收矩阵
├── tests/test_matrix.md      ← 真机实测台账模板
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
