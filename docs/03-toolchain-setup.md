# 03 — 构建环境安装记录（dl.google.com 不可达场景）

> 目的：让「装什么、装到哪、从哪下、为什么这么装」可复现。本机已按此流程安装并成功产出 APK。

---

## 3.1 网络实测（2026-10-20，本机）

| 源 | 结果 | 用途 |
|---|---|---|
| `dl.google.com` | ❌ 超时（15s 无响应） | 官方 SDK / Google Maven 均不可用 |
| `mirrors.cloud.tencent.com/AndroidSDK` | ✅ 206 | **SDK 包来源** |
| `mirrors.cloud.tencent.com/gradle` | ✅ 206 | **Gradle 发行包来源** |
| `mirrors.tuna.tsinghua.edu.cn/Adoptium` | ✅ 200 | **JDK 来源** |
| `maven.aliyun.com/repository/google` | ✅ 200 | **AGP 依赖来源** |
| `repo1.maven.org` | ✅ 200 但慢（8s） | 备选 |

**结论**：Android Studio 的官方安装器在本机会卡在 SDK 下载。因此**不用 sdkmanager，直接按官方目录布局手工铺 SDK**。

## 3.2 安装位置

| 组件 | 路径 | 体积 |
|---|---|---|
| JDK 17 (Temurin 17.0.20.1) | `~/.workbuddy/binaries/jdk/17` | 190 MB |
| Android SDK | `~/Android/Sdk` | — |
| ├ platform 34（android.jar） | `platforms/android-34` | 63 MB |
| ├ build-tools 34.0.0 | `build-tools/34.0.0` | 58 MB |
| ├ platform-tools（adb） | `platform-tools` | 8 MB |
| ├ licenses | `licenses/android-sdk-license` | — |
| Gradle 8.7 | `~/.gradle/wrapper/dists`（自动） | 143 MB |
| 依赖缓存 | `~/.gradle/caches` | 217 MB |

一键安装：`bash scripts/install_android_toolchain.sh`

## 3.3 手工布局的三个关键点

1. **包内顶层目录要改名**
   - `platform-34-ext7_r03.zip` → 顶层 `android-34/` → 放 `platforms/android-34/`
   - `build-tools_r34-windows.zip` → 顶层 **`android-14/`**（不是 34）→ 必须改名为 `build-tools/34.0.0/`
   - `platform-tools_r37.0.1-win.zip` → 顶层 `platform-tools/` → 原样放置

2. **必须手写 license 文件**，否则 AGP 报 "licenses have not been accepted"：
   ```
   ~/Android/Sdk/licenses/android-sdk-license
     8933bad161af4178b1185d1a37fbf41ea5269c55
     d56f5187479451eabf01fb78af6dfcb131a6481e
     24333f8a63b6825ea9c5514f83c2829b004d1fee
   ```

3. **`local.properties` 的路径必须转义**（Windows）：
   ```properties
   sdk.dir=C\:\\Users\\<name>\\Android\\Sdk
   ```

## 3.4 仓库侧的镜像改造（已提交）

| 文件 | 改动 | 原因 |
|---|---|---|
| `src/android/settings.gradle.kts` | 阿里云 `google` / `public` / `gradle-plugin` 镜像优先，官方源兜底 | `dl.google.com` 不可达，AGP 拉不下来 |
| `src/android/gradle/wrapper/gradle-wrapper.properties` | `distributionUrl` 指向腾讯镜像 | `services.gradle.org` 不稳定 |

## 3.5 构建与产物

```bash
export JAVA_HOME="$HOME/.workbuddy/binaries/jdk/17"
export ANDROID_HOME="$HOME/Android/Sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"

cd src/android
./gradlew assembleDebug --no-daemon
```

实测结果：

| 项 | 值 |
|---|---|
| 结果 | **BUILD SUCCESSFUL in 32s**（首次 16m33s，含 Gradle 与依赖下载） |
| 任务 | 35 actionable tasks |
| 产物 | `app/build/outputs/apk/debug/app-debug.apk`（2.1 MB） |
| 包名 / 版本 | `com.finn.hdr` / 0.1.0 |
| minSdk / targetSdk | 26 / 34 |
| 启动 Activity | `com.finn.hdr.MainActivity` |
| 签名 | Android Debug 证书（SHA-256: `430ea510…`） |
| 内容 | 110 entries，classes.dex / classes2.dex / classes3.dex |

编译期仅 1 条告警（`Elvis operator always returns the left operand`），属防御性写法，不影响功能。

## 3.6 安装到手机

```bash
# 手机开「开发者选项 → USB 调试」，连接后：
"$ANDROID_HOME/platform-tools/adb.exe" devices          # 应看到设备
"$ANDROID_HOME/platform-tools/adb.exe" install -r \
  src/android/app/build/outputs/apk/debug/app-debug.apk
```

也可以直接把 `out/HdrVividUltraHdr-0.1.0-debug.apk` 传到手机点击安装
（需允许「安装未知来源应用」）。

### 3.6.1 手机端开启 USB 调试

**华为 / 荣耀（HarmonyOS、EMUI、MagicOS）** —— 本项目主要取证机型

1. 设置 → 关于手机 → **版本号**连续点 7 次（HarmonyOS NEXT：设置 → 关于本机 → 软件版本），
   提示「您已处于开发者模式」
2. 设置 → 系统和更新 → **开发人员选项** → 打开「USB 调试」
3. 插线后下拉通知栏，把 USB 连接方式从「仅充电」改为 **「传输文件 / MTP」**
   （只充电模式下 adb 通常不通）
4. 手机弹「是否允许 USB 调试」→ 勾选「一律允许使用这台计算机进行调试」→ 允许
5. 部分机型还需在开发人员选项里额外打开「仅充电时允许 ADB 调试」

**小米 / Redmi（HyperOS / MIUI）**

- 设置 → 我的设备 → 全部参数 → 连续点「MIUI 版本 / HyperOS 版本」7 次
- 设置 → 更多设置 → 开发者选项 → 打开「USB 调试」**和「USB 调试（安全设置）」**
  （后者是 `adb install` 装应用的必需项，需登录小米账号）

**OPPO / 一加 / realme（ColorOS）**

- 设置 → 关于本机 → 版本信息 → 连续点「版本号」7 次
- 设置 → 系统设置 → 开发者选项 → 「USB 调试」

**vivo / iQOO（OriginOS）**

- 设置 → 系统管理 → 关于手机 → 连续点「软件版本号」7 次
- 设置 → 系统管理 → 开发者选项 → 「USB 调试」

**原生 Android / 三星 / 索尼**

- 设置 → 关于手机 → 连续点「版本号」7 次
- 设置 → 系统 → 开发者选项 → 「USB 调试」

> 通用前提：必须用**数据线**（部分随附线只供电）；USB 3.0 口不稳定时可换 USB 2.0 口；
> Windows 若识别不到，装厂商驱动（华为 HiSuite / 小米助手）或 Google USB Driver。

### 3.6.2 adb 状态对照表

| `adb devices` 输出 | 含义 | 处理 |
|---|---|---|
| 空列表 | 完全未识别 | 换数据线 / 换 USB 口 / 确认 USB 用途为「传输文件」/ 换驱动 |
| `unauthorized` | 已识别但未授权 | 看手机屏幕点「允许」；无效则「撤销 USB 调试授权」后重插 |
| `offline` | 连接异常 | `adb kill-server` 再 `adb devices`；重启手机 |
| `device` | 正常，可安装 | `adb install -r <apk>` |
| `no permissions`（Linux） | udev 规则缺失 | 配置 `51-android.rules` |

### 3.6.3 华为 Mate 30 / EMUI 实测踩坑（本轮实际遇到）

| 现象 | 根因 | 解法 |
|---|---|---|
| `adb devices` 空，但设备管理器有 `VID_12D1&PID_108A Huawei HDB Interface` | 手机暴露的是 **HDB 通道**（华为手机助手用），不是 ADB 接口 | 开发者选项开「USB 调试」+「仅充电时允许 ADB 调试」，通知栏把 USB 用途改成「传输文件」 |
| 有序列号但一直 `unauthorized`，**换新 adbkey 也不弹窗** | **手机开了「免打扰/勿扰模式」，系统把 USB 调试授权弹窗静音拦掉了** | 关闭免打扰，或去「开发人员选项 → 撤销 USB 调试授权」后重新插线 |
| 通知栏出现「已连接 USB 调试」但电脑仍 unauthorized | 授权是分次的：通知只表示通道建立，仍需屏幕弹窗确认 | 撤销授权重来，确保弹窗点「一律允许」 |
| adb pull/screencap 报 `No such file` 且路径变成 `...PortableGit/sdcard/...` | Git Bash 的 MSYS 路径转换把 `/sdcard/` 改写了 | 命令前加 `MSYS_NO_PATHCONV=1` |

> 另注：Mate 30 可用空间不足 10% 时导出大图有失败风险，实操前建议先清理。

## 3.7 排查对照

| 报错 | 根因 | 处理 |
|---|---|---|
| `Failed to install the following SDK packages … licenses have not been accepted` | 缺 licenses 文件 | 按 3.3-2 写入 |
| `Could not find com.android.tools.build:gradle:8.5.2` | 走不到 Google Maven | 确认 settings 里阿里云镜像在最前 |
| `SDK location not found` | local.properties 缺失或转义错误 | 按 3.3-3 重写 |
| `Unsupported class file major version` | 用了 JDK 21+ 编译 AGP 8.5 | 固定 JDK 17 |
| Gradle 下载卡住 | services.gradle.org 不可达 | 换腾讯镜像（已默认） |
