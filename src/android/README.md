# Android 端：能不能装到手机上？

**结论：能装。本机已完成全链路验证 —— BUILD SUCCESSFUL，产出 2.1 MB 的 `app-debug.apk`，包名 `com.finn.hdr`，minSdk 26 / targetSdk 34，debug 签名有效，启动 Activity 为 `MainActivity`。** 目前只差「连上手机执行 adb install」这一步。

---

## 1. 现在缺什么（已补齐 / 仍缺）

| 项 | 状态 |
|---|---|
| Kotlin 源码（解析/几何/导出/自检/分享） | ✅ 已就绪 |
| Gradle 工程（settings / build / app） | ✅ 已补齐 |
| Gradle Wrapper（gradlew / gradlew.bat / wrapper.jar） | ✅ 已内置，jar 经 zip 校验有效（33 entries，含 GradleWrapperMain） |
| AndroidManifest + FileProvider + 入口 Activity | ✅ 已补齐 |
| 纯 Kotlin 封装器（无 NDK 也能跑路线 B） | ✅ 已补齐 |
| 工程静态自检脚本 | ✅ `scripts/check_android_project.py`，40 项通过 |
| CI 云端出包 | ✅ `.github/workflows/android-build.yml`（无需本地 SDK） |
| `third_party/libultrahdr` | ❌ 需自行引入（仅 `-PwithNative=true` 时需要），可用 `scripts/fetch-libultrahdr.sh` |
| JDK 17 + Android SDK 34 | ✅ **已安装并验证**：`~/.workbuddy/binaries/jdk/17`、`~/Android/Sdk`（详见 `docs/03-toolchain-setup.md`） |
| 实际构建 | ✅ **BUILD SUCCESSFUL**，产物 `app-debug.apk` 2.1 MB / 110 entries / debug 签名有效 |
| 签名 | debug 构建自动；release 需自备 keystore |

> 本机 `dl.google.com` 不可达，SDK 采用「镜像下载 + 手工布局」安装，AGP 依赖走阿里云 Maven。
> 全流程记录见 `docs/03-toolchain-setup.md`，可复用脚本 `scripts/install_android_toolchain.sh`。

## 2. 构建与安装

### 步骤 0：先跑静态自检（无需 SDK，2 秒出结果）

```bash
python scripts/check_android_project.py
```

校验 40 项：文件完整性、wrapper jar 有效性、Manifest 资源引用、JNI 符号与 Kotlin `external fun` 是否一一对应、CMake 相对路径层数、AGP 与 Gradle 版本兼容、SDK 级别。**这一步能提前发现绝大多数不会在编译期报错、只会在运行期崩掉的问题。**

### 路径 A：只装 SDK（推荐先走这条，最快见到真机效果）— 已实测通过

不编译 native，使用 `UltraHdrPackager`（纯 Kotlin 封装，路线 B）：

```bash
cd src/android
# 需要 JDK 17 与 Android SDK 34（ANDROID_HOME 已配置）
./gradlew assembleDebug          # 产物：app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

或用 Android Studio 直接 Open 本目录 → Sync → Run。

> Wrapper 已内置 `gradle-wrapper.jar`，首次执行会自动下载 Gradle 8.7 发行包（约 130 MB）。国内网络可在 `gradle/wrapper/gradle-wrapper.properties` 里换成腾讯镜像（文件内已注释）。

### 路径 C：完全没有本地环境 → 用 CI 出包

推送到 GitHub 后，`.github/workflows/android-build.yml` 会在云端装 JDK + SDK 并执行 `assembleDebug`，APK 作为 artifact 下载即可安装：

```
Actions → Android Build → 最新一次运行 → Artifacts → app-debug
```

同一条流水线还会跑 Python 侧的 20 项回归与端到端自检，保证两套实现（Kotlin / Python）的容器逻辑不漂移。

### 路径 B：带 libultrahdr（路线 A / 官方 API-4）

```bash
./scripts/fetch-libultrahdr.sh          # 从 GitHub 或 Gitee 镜像拉取到 third_party/

cd src/android
./gradlew assembleDebug -PwithNative=true
```

需要额外安装：NDK 26.x + CMake 3.22.1（Android Studio SDK Manager 内勾选）。

## 3. 装上去之后怎么验

1. 打开 App → 「选择 HDR 照片并导出」→ 选一张**华为拍摄的 HDR Vivid 照片**
2. 界面会逐行打印：容器类型 / 增益图大小 / 每一步变换 / 19 项自检结果
3. `自检` 全 ✓ 才算成功；出现 ✗ 就说明结构没写对，不能对外说"HDR 已保留"
4. 「以文件发送」→ 发到另一台支持 HDR 的手机，用系统看图器打开对比
5. 「微信直接发图」→ 预期降级为 SDR（这是平台行为，不是 bug）

把每次结果填进 `tests/test_matrix.md`。

## 4. 现实约束（必须知道，否则会误判）

| 约束 | 说明 |
|---|---|
| **华为 HDR Vivid ≠ Android Ultra HDR** | 前者是 CUVA 容器（JPEG MPO，副图 APP5 + EXIF `_cuva`），Android 原生没有任何 API 读它的增益图，必须靠本项目自研的 `HdrSourceParser` 扫描 |
| **显示 HDR 需要 Android 14+** | 写入文件不需要（API 26 即可），但"看出来是 HDR"要系统看图器支持 Ultra HDR + HDR 屏 |
| **路线 B 可能有色偏** | 直接搬 CUVA 增益图到 hdrgm 语义，两者不等价；要准确必须走路线 A（线性光重算，需 native） |
| **增益图不是灰度 JPEG** | `Bitmap.compress` 只能出 3 通道，当前先置灰保证三通道同值；省体积需 native 单通道编码 |
| **EXIF 未回插** | `Bitmap.compress` 不写 EXIF，当前版本导出图不带拍摄信息（不影响 HDR，但影响体验） |
| **内存** | 4000×3000 解码约 48 MB，低端机可能 OOM；生产需 `inSampleSize` 分级解码 |

## 5. 代码地图

```
app/src/main/java/com/finn/hdr/
├── MainActivity.kt          探针界面：选图 → 导出 → 打印自检 → 分享
├── HdrSourceParser.kt       CUVA / ISO 容器识别（纯 Kotlin，无需 native）
├── GainMapGeometry.kt       主图/增益图同步缩放、裁剪、旋转
├── UltraHdrPackager.kt      纯 Kotlin 封装（无 NDK 兜底）
├── UltraHdrExporter.kt      编排：优先 native，失败回退 Kotlin 封装
├── UltraHdrNative.kt        libultrahdr JNI 门面
├── MetadataValidator.kt     导出后结构自检
└── ShareGuidance.kt         三通道分享引导 + FileProvider 分享

app/src/main/cpp/
├── uhdr_jni.cpp             libultrahdr API-4 JNI 实现
└── CMakeLists.txt           submodule 路径与编译开关
```
