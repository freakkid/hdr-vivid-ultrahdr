# HANDOVER — 交接文档

> 最后更新：2026-10-22 00:10（v0.1.5 经三路独立审查修复后已推送；hdc 已就位；待装机验证）
> 读这一篇就能接手，不需要回看聊天记录。

---

## 0. 30 秒接手

| 问题 | 答案 |
|---|---|
| 这是什么 | 把华为 HDR Vivid 静图导出为「SDR 主图 + 增益图 + XMP + MPF」的 Ultra HDR JPEG；**并**同时产出一张「HDR 效果已烘焙进像素」的普通 JPEG（微信/任何设备可见） |
| 到哪一步了 | 两条链路（Ultra HDR 容器、像素烘焙）**代码全部完成**，自动化验证全绿（回归 28 / 静态自检 44 / APK 构建通过）。**卡在最后一步：v0.1.5 已构建，但没装到手机过** |
| 下一步唯一要做的事 | 手机插线解锁 → 开 USB 调试 → `hdc file send` 推 APK → 卓易通安装 → 选图，看到界面五段日志与左右预览（§3.0） |
| 部署到手机 | 用户设备是 **HarmonyOS NEXT（HUAWEI CLS-AL30 / 7.0.0.109 / API 26）**。NEXT 不原生解析 APK，但可装进**卓易通**容器运行，步骤见 `docs/05-harmonyos-zhuoyitong.md`。**早期文档写过「鸿蒙装不了 APK」，那是错的** |
| 手机调试工具 | **`C:/Users/ugnam/hdc_official/hdc.exe`（3.2.0f）**，能截屏/拉日志/推拉文件。这是**机器级工具，不在仓库里**，换机器需重新获取（§1.3-C、`docs/03` §3.8）|
| 下一个真问题 | 用户那张报错的照片**已确认是真 HEIC**（`ftyp` + `heic` 品牌，8.94 MB），且容器内系统转码不可用。v0.1.6 加了主图解码、`iloc` 长度诊断与「导出原图」，装上去跑一次即可判定里面有没有增益图（§3.1）|
| 仓库 | `git@github.com:freakkid/hdr-vivid-ultrahdr.git`（私有），分支 `main` |
| 最新提交 | 见 `git log --oneline -1`（写作时 `5e5eb96`）|
| 交付形态 | **纯 Kotlin，APK 内 `.so` 数量为 0**。native libultrahdr 只是备选，从未编译验证 —— 别一上手就去接 NDK |

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
│   ├── 04-HANDOFF-bake.md   烘焙链路算法决策记录
│   ├── 05-harmonyos-zhuoyitong.md 鸿蒙 NEXT 装 APK（卓易通容器）完整步骤
│   └── 06-heic-and-heif.md  HEIC 方案调研与「转 JPG 损耗」结论
├── src/android/             ★ Android 实现（交付物）
├── src/python/uhdr/         容器参考实现与校验器（工具链，不进 APK）
├── src/harmony/, src/ets/   HarmonyOS/ArkTS 链路（骨架，未真机验证）
├── web/index.html           网页版原型 —— **未完成，已冻结，不要投入**（仅解析部分可复用）
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

# 4) 验证 Python 侧（28 项测试，1 秒）
./run.sh test
python3 scripts/check_android_project.py  # 工程静态自检 45 项（纯标准库，任一 Python 3 均可，无需 venv）

# 5) 构建 APK
export JAVA_HOME="$HOME/.workbuddy/binaries/jdk/17"
export ANDROID_HOME="$HOME/Android/Sdk"
cd src/android && ./gradlew assembleDebug --no-daemon
```

**注意**：本机 `dl.google.com` 完全不可达，所以 SDK 是「镜像下载 + 手工铺目录」装的（`sdkmanager` 用不了）。细节与三个必踩的坑见 `docs/03-toolchain-setup.md` §3.3。

如果新机器能正常访问 Google，直接装 Android Studio 更省事，无需上面的手工流程。

### 1.3 两台机器的实际环境——均已验证可用

> 用户在两台机器上交替工作，环境不通用：`local.properties`、JDK/SDK 路径、
> `out/visual/` 都各自独立。换机器第一件事是跑 §1.2 第 3 步重建 `local.properties`。

**A. macOS（2026-10-21）**

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

**B. Windows（2026-10-21 22:38 实测拉取后重跑通过）**

| 组件 | 位置 / 版本 | 备注 |
|---|---|---|
| JDK 17 | `C:/Users/ugnam/.workbuddy/binaries/jdk/17`（17.0.20.1） | `scripts/install_android_toolchain.sh` 装的 |
| Android SDK | `C:/Users/ugnam/Android/Sdk`（platform 34 / build-tools 34.0.0 / platform-tools 37.0.1） | `dl.google.com` 不可达，走腾讯镜像手工铺目录 |
| Gradle | wrapper 8.7（腾讯镜像） | 增量构建 38 秒 |
| Python venv | `C:/Users/ugnam/.workbuddy/binaries/python/envs/default/Scripts/python.exe` | pillow + numpy 均可用 |

```bash
export JAVA_HOME="C:/Users/ugnam/.workbuddy/binaries/jdk/17"
export ANDROID_HOME="C:/Users/ugnam/Android/Sdk"
cd src/android && ./gradlew assembleDebug --no-daemon     # Windows 上可执行位有效
```

Windows 特有坑：Git Bash 会把 `adb shell` 里的 `/sdcard/...` 改写成
`…PortableGit/sdcard/…`，必须加 `MSYS_NO_PATHCONV=1`（见 §5.1）。

双实现数值对照（需 kotlinc，见 `tests/ltm_parity/run_parity.sh`）：
`bash tests/ltm_parity/run_parity.sh`（kotlinc 缺失时自动跳过，不算失败）。

**C. 鸿蒙调试工具 hdc（机器级，两机都需要各自装）**

| 项 | 值 |
|---|---|
| 路径（当前这台） | `C:/Users/ugnam/hdc_official/hdc.exe`（5.5 MB，Ver 3.2.0f）|
| 来源 | 华为 Command Line Tools（Windows 版）的 `sdk/default/openharmony/toolchains/` |
| 是否随仓库走 | **否**。`.gitignore` 不排除也没入库，换机器必须重新获取 |

```bash
HDC="C:/Users/ugnam/hdc_official/hdc.exe"
"$HDC" -v                       # Ver: 3.2.0f
"$HDC" list targets -v          # 正常应显示序列号；[Empty] = 没连上
```

获取方式与三个坑（**单拷 `hdc.exe` 会 exit 127**）见 `docs/03-toolchain-setup.md` §3.8。

---

## 2. 基线状态

Ultra HDR 主链路与烘焙链路均已实现并通过验证：回归 28 项、静态自检 45 项、
APK 可构建（**v0.1.7 / versionCode 8**）。**已完成项不在此罗列**——证据在 `git log` 与
`docs/02-verification.md`，烘焙算法决策在 `docs/04-HANDOFF-bake.md`。
尚未做的只有 §3。

**必须说清的一个事实：模拟器端到端实测（App 产物经 Python 独立校验 20/20、
App 与 Python 烘焙输出 SSIM 0.95，见 docs/02 §2.8）验的是 `2bd975e` 的产物，
那时 `versionCode = 1 / versionName = 0.1.0`。**

也就是说：**0.1.2 的实时预览、0.1.3 的 HEIC 探针、0.1.4 的保存相册与日志滚动、
0.1.5 的六项审查修复，从未在任何设备（含模拟器）上执行过。** 这是当前最大风险的来源。

Kotlin/Python 双实现数值对照（`tests/ltm_parity/run_parity.sh`）**只在 macOS 上验证通过**：
脚本探测的是 POSIX 路径 `…/envs/default/bin/python`，Windows 的 venv 在 `Scripts/python.exe`，
会自动回落到无 numpy 的系统 `python3` 而失败（见 §5.1）。

**2026-10-21 晚做过一次三路独立审查（Android / Python / 文档各一路，只读）**，
修掉 3 个阻断级缺陷，教训值得记住：

| 缺陷 | 教训 |
|---|---|
| 5 处 `infoView.text = ...` 在后台线程执行，必抛 `CalledFromWrongThreadException`，且**全在 catch 分支里** | 「错误上报机制自己把进程打死」是最坏的一种 bug：容器无 logcat 时只剩闪退。后台线程一律走 `say()` 切主线程 |
| 解析端用主图 XMP 覆盖增益参数，而主图按设计只写 `hdrgm:Version` 占位 → 导出再解析 4.43x 静默退化 4.00x | **双写元数据的项目，解析端必须知道「哪一份才是权威」**。已改为优先读增益图自身 XMP，并加往返回归测试（`TestRoundTrip`）|
| EXIF Orientation 6/8 旋转方向反 180°（PIL 逆时针）；镜像档 2/4/5/7 未实现却把 `0x0112` 清成 1 | 不支持的档位**宁可保留原值**，把标记改成「已归一化」比不处理更糟 |

高优先级同批修掉：导出器全分辨率解码（5760×4320≈99 MB，容器 256 MB 堆必 OOM）改采样解码、
`previewGen` 的 `@Volatile Int ++` 非原子改 `AtomicInteger`、滑杆逐格拉线程改 160ms 防抖、
导出线程读 Spinner 改主线程快照、`HeifInspector.u32` 返 Int 溢出改全程 Long、
Python 侧 `int.from_bytes` 传 `'<'` 导致 EXIF Make 永远缺失、`--anchor` 解析了没透传。


---

## 3. 未完成（按优先级）

### 3.0 【P0，唯一的阻塞项】把 v0.1.5 装到手机并跑一次

代码侧没有已知阻塞。**APK 已构建好，但从未在用户手机上运行过 v0.1.2 之后的版本**
（用户手机上装的确认为旧版，但**具体是哪一版还没有截屏确认**——用 §11.3 的表去判，
不要相信「只有一个按钮」这类描述：仓库里不存在"无日志框"的构建）。

```bash
HDC="C:/Users/ugnam/hdc_official/hdc.exe"

# 1) 确认连上（[Empty] 就是没连：需解锁屏幕 + USB 调试 + 手机上点允许调试）
"$HDC" list targets -v

# 2) 先删旧包。注意通配符千万别写 0.1.*——它会把马上要推的 0.1.5 一起匹配掉
"$HDC" shell "rm -f /storage/media/100/local/files/Docs/Download/HdrVividUltraHdr-0.1.[0-4]*.apk"

# 3) 再推新包（用绝对锚点，不依赖当前目录）
cd "$(git rev-parse --show-toplevel)/out"
"$HDC" file send HdrVividUltraHdr-0.1.7-debug.apk \
  /storage/media/100/local/files/Docs/Download/HdrVividUltraHdr-0.1.5.apk

# 4) 必须看到一行 2.2 MB 的 0.1.5.apk，否则推失败
"$HDC" shell "ls -l /storage/media/100/local/files/Docs/Download/*.apk"
```

> **这个 rm 顺序是有原因的**：若先推后删且写成 `0.1.*.apk`，`*` 会匹配 `5.apk`，
> 刚推上去的文件被自己删掉。更麻烦的是目录里若还有别的 apk，`ls` 照样列出、退出码 0，
> 看起来一切正常。这是本文初稿真实写错过的坑。

然后在手机上：**卓易通 → 右上角「+」→「本地安装包」→ 选该 APK**。
（hdc 装不了：`bm dump -n com.finn.hdr` 查不到容器内的 App，它是隔离沙箱，装/点必须人工。）

装完判定标准，按顺序看（**下面文案取自 v0.1.5 源码，逐字对得上才算装对**）：

| 检查 | 期望 |
|---|---|
| 按钮共 9 个，**分两处** | **顶部两个**：「选择 HDR 照片」「用文件管理器选择（相册不可用点这个）」；**滑杆下方七个**：「导出全尺寸（2048 长边）」「发送烘焙图（微信可见 HDR 效果）」「发送 Ultra HDR（需设备支持）」「保存烘焙图到相册」「导出原图（HEIC 无增益图时用）」「复制日志（短）」「导出日志到下载」|
| 中间控件（自上而下）| 左右两个图片位 → 一行状态文字 → **无标签的下拉框**（首项「本地观看（local：局部色调映射）」，然后「微信分享（wechat：克制映射+重压缩余量）」「仅全局映射（off：旧行为，A/B 对照）」）→ 「烘焙强度：0.70（1.0 = 完全按元数据恢复 HDR）」→ 滑杆 |
| 首行日志 | `===== 载入 content://<authority> =====`（只到 authority、不含路径；前面还有一个空行）。注意：**不是** `URI：`，早期文档写错过 |
| 格式行 | `格式=JPEG  头部=FF D8 FF E1 ...`；非 JPEG 时是 `格式=ISOBMFF(heic)` 等 |
| 若 JPEG 且有增益图 | `容器：ISO_ULTRA_HDR`、`增益图：有（xxxxx B）`、右侧图片位出图 |
| 若无增益图 | `容器：PLAIN_JPEG`、`该图无增益图（已是普通 SDR）。左图为原图。` |
| 导出 | 点「导出全尺寸」→ `validate: ok=true errors=0`，产出 `IMG_xxx_BAKED.jpg` 与 `IMG_xxx_HDR.jpg` |
| 报错时 | 点「复制全部日志」拿全文 —— 这是唯一可靠的反馈通道（容器无 logcat）|

**App 不会打印「图像数」「段序列」这类字段**（早期文档凭空写过）。它能给的是：
格式魔数 + 前 12 字节 hex、容器判定、增益图字节数、ISO/XMP 参数、`ftyp` 与 item 表（仅 HEIC）、
五段式 `[1/5]…[5/5]` 进度、异常类名 + message + cause 链 + 堆栈前 6 行。

截屏直接给结果，不用用户描述：`"$HDC" shell "snapshot_display -f /data/local/tmp/s.jpeg"` 然后 `file recv`。

### 3.1 【已定位，待决定实现力度】那张照片确认是**真 HEIC**

2026-10-22 00:29 用户在手机上选图，App 打印了完整日志。**这不是猜测，是字节证据**：

```
===== 载入 content://media =====
[1/5] 读取字节
格式=ISOBMFF(heic)  头部=00 00 00 1C 66 74 79 70 68 65 69 63
⚠ 不是 JPEG，系统也未能转码
长度：9376165 B
[2/5] 解析容器
ISOBMFF 顶层: ftyp meta mdat
```

解码这几行：

| 字节 | 含义 |
|---|---|
| `00 00 00 1C` | 第一个 box 长度 = 28 |
| `66 74 79 70` | `ftyp` |
| `68 65 69 63` | major brand = **`heic`** |
| `ftyp meta mdat` | 标准 HEIF 图片结构（有 `meta` 就有 item 表，就有可能是带 aux 的 HDR）|
| `9,376,165 B` | 8.94 MB —— 典型手机 HDR 照片体量 |

**两条结论：**

1. **它是真 HEIC，不是扩展名骗人的 JPEG**（对比赛张 `bluetooth_recv.heic` 是 `ff d8 ff e1` 的 JPEG，两者完全不同）。
2. **容器内系统转码不可用**：`openTypedAssetFileDescriptor(uri, "image/jpeg")` 失败，
   说明卓易通容器里的 MediaProvider 没提供该转换。所以「让系统转成 JPEG」这条路在容器里不存在。

**尚未确认的关键一项**：这张 HEIC 里到底有没有增益图。需要看 `aux URN` 与 `iloc 长度`，
而用户当时贴的日志停在第 2 段开头（后面还有内容没贴出来）。

v0.1.6 已针对此改了三件事，一次就能把信息取全（不必再反复贴日志）：

| 改动 | 作用 |
|---|---|
| `HeifInspector` 补 `iloc` 长度表 | 直接看到每个 item 的字节数，判断增益图在不在、多大 |
| `HeicDecoder`（新增）| 用系统 `ImageDecoder`/`BitmapFactory` 试解主图，成功就显示，失败给出明确原因（缺解码器 vs 内存不足）|
| 「导出原图」按钮 | 把原始字节写进 `下载/HDRRaw/*.heic`，用户用微信传出来 → 桌面 `pillow-heif` 可彻底分析 |
| 摘要 + 「导出日志到下载」 | 诊断压成 4~8 行 `HEIC 摘要` 置顶；完整结构折叠在后。**日志不再只靠剪贴板**：粘贴端（微信/输入法）会静默截断超长文本，表现为「复制不全」 |

**判定表**（装 v0.1.6 后看日志）：

| 日志特征 | 结论 | 下一步 |
|---|---|---|
| 摘要块里 `结论：有增益图（HDR 照片）` | **有增益图** | 值得做 HEIC 增益图提取（§3.5）|
| 摘要块里 `aux=0 → 没有辅助图，不是 HDR` | 普通 SDR 的 HEIC | 只需支持主图解码，不做增益图 |
| `解码通道：ImageDecoder` 且出图 | 容器有 HEVC 解码器 | 主图路径可行 |
| `解码通道：none` + `连宽高都读不出` | 容器缺 HEIF 解析器 | 只能靠「导出原图」+ 桌面处理 |

桌面侧若拿到原件：`"$PY" scripts/analyze_image.py <file> --extract`（见 §6）。

### 3.2 【P1】真实 HDR Vivid 样张覆盖 + App 端 LTM 目视确认

目前只有 1 张真实样张（ISO 载体，来自 `bluetooth_recv.heic`）。

**设备侧的最新认知**（此前文档判断有误，以此为准）：
- 用户当前设备 **HUAWEI CLS-AL30**（`pnputil` 报 `HUAWEI Mate 70`，HarmonyOS 7.0.0.109），
  属「Mate 40/P50 之后」，**本身具备拍 HDR Vivid 的能力**
- 真正的卡点**不是没设备，而是把照片取到电脑**：鸿蒙相册在受保护媒体库，
  `hdc` 读不到 `Docs/DCIM`，MTP 写不了（见 §5.2b）
- 可行路径：手机拍 → 在手机上用 App 选图看屏幕日志；或用户手动经微信/网盘把文件传到电脑

需要补齐：逆光人像、高饱和、暗部细节、纯色背景 各 2–3 张，用来验证：
- 增益图语义换算是否引入偏色（对比 HDR 合成结果做 ΔE）
- 极端动态范围下 `max_content_boost` 会不会超界
- App 端 LTM 三档目视：profile 下拉切 local / wechat / off 各导一次，
  重点看有无 halo（eps 已于 2026-10-21 调至 0.2，合成最坏情况有 ~15% 台阶高
  过冲，真实样张未测出；若真机见可见亮边，回调至 0.1）、灰雾、「假 HDR」感

获取途径：**直接用自己的 Mate 70 拍**（拍完在手机上用 App 选图看日志），
再设法把文件传出（微信/网盘手动转）；网上找 `.jpg` 格式的 Vivid 样张只作补充。

**目视材料不在仓库里**（`out/visual/` 被 gitignore，含真实照片）。换机器后自己生成一次：

```bash
# <源图> 是占位符，替换成真实路径再执行（见 §6 占位符约定）
PY="C:/Users/ugnam/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
"$PY" scripts/make_visual_compare.py <源图>       # 默认输出到 out/visual/
```

产出 `source_sdr.jpg`（对照组）/ `py_off|local|wechat.jpg`（三档）/
`00_compare.jpg`（2×2 拼图，带体积与暗部/高光标注）/ `00_preview.jpg`（缩略）。
若手上有模拟器实跑产物，另存为 `out/visual/app_local.jpg` 一并对照。
HDRCapacityMax 与导出链路已在模拟器验证通过（docs/02 §2.8）。

### 3.3 【P1】微信三路径实测

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

### 3.4 【P2】路线 A：线性光重算

当前走的是路线 B（复用源增益图直接封装），CUVA 源可能存在色偏。路线 A 需要：
解 HDR（PQ/HLG → 线性 + BT.2020 变换）→ 重新 tone map 出 SDR → 用 libultrahdr **API-1** 重算增益图。

需要先把 libultrahdr 编进 APK（`bash scripts/fetch-libultrahdr.sh` + `./gradlew assembleDebug -PwithNative=true`），且 JNI 侧目前只实现了 API-4 与 probe。

### 3.5 【P2】HEIC（ISOBMFF）容器不支持

当前只解析 JPEG。若源图是真 HEIC（`ftyp` 开头），不会崩，但会走到「已识别是 HEIC，
本版不支持解码，结构已列在下方日志」。

**已有部分**（v0.1.6 起，别重复造）：
- `HeifInspector.kt` —— 纯 Kotlin ISOBMFF 遍历：box 树 / item 表 / `iloc` 长度 / aux URN，不解码
- `HeicDecoder.kt` —— 系统解码器解**主图**（`ImageDecoder` API28+ → `BitmapFactory` 兜底），
  软件位图、按 2 的幂采样。**拿不到增益图**（那是 Android 14 的 `Bitmap#getGainmap`）
- 「导出原图」按钮 —— 原始字节写 `MediaStore.Downloads/HDRRaw/`，解决「原件取不出来」
- `scripts/analyze_image.py` —— 桌面侧，走 pillow-heif，`--extract` 能把 aux 图解成 PNG
- 系统转码兜底 —— `openTypedAssetFileDescriptor(uri, "image/jpeg", null)`，
  **实测在卓易通容器内不可用**，且即使可用增益图也会丢

**要做的**：任选一条，成本从低到高

| 路线 | 成本 | 风险 |
|---|---|---|
| 自研 ISOBMFF：把增益图 item 抽出来重封成最小 HEIF 再交给 `ImageDecoder` | 中（纯 Kotlin，无需 NDK）| 要自己处理 `iloc`/`hvcC` 依赖关系 |
| 引入 `libheif`（≥1.18 才有 ISO 21496-1 TMAP 支持）| 高（NDK + 子模块 + ABI）| APK 体积与构建复杂度显著上升 |
| 让用户在相册「另存为 JPG」 | 零 | **增益图永久丢失，HDR 全废**，等于放弃功能 |

**注意区分两张图，别混为一谈**：

- `%TEMP%\hdrprobe\bluetooth_recv.heic`（6.1 MB）—— **已完整分析过**，魔数 `ff d8 ff e1` 是 JPEG，
  走 ISO 21496-1 载体，增益图 332,469 B 完好。这张**不是** HEIC，已用于 §3.2 的三档目视。
- §3.1 那张「选图报不是合法 JPEG」的照片 —— **至今没拿到原件**，什么格式完全未知。

所以「本次那张 .heic 其实是 JPEG」只对前者成立，不能用来给 §3.1 结案。

### 3.6 【P3】native 路径未验证

`uhdr_jni.cpp` 写完了但从未编译过（本机无 NDK）。`-PwithNative=true` 时才会编，App 会自动回退到纯 Kotlin 封装（日志里会写 `native 不可用（UnsatisfiedLinkError）`）。

---

### 3.7 【P2/P3】审查发现、但故意没修的低风险项

上一轮审查点出这些，判断为「不影响当前主链路」，**一次性改太多反而无法验证**，故留下记录：

| 项 | 现象 | 影响 |
|---|---|---|
| `HdrBaker` 的 `clippedRatio` 在 LTM 档恒为 0 | LTM 路径没有回填该统计 | 只影响日志显示，输出正确 |
| `HdrBaker` 忽略 `meta.gamma` | 恒按 gamma=1.0 处理 | 源 gamma≠1 时 HDR 曲线会偏，当前样张恰为 1.0 |
| 导出丢 EXIF / ICC | 只保留像素与结构元数据 | 白平衡/镜头信息丢失；ICC 丢失在广色域源上可能偏色 |
| ~~`MediaStore` 插入缺 `IS_PENDING`~~ | **0.1.6 起已修**：导出原图与导出日志都走两段式提交 | —— |
| `MetadataValidator` 只认第一个 APP1 | 多 XMP 段时可能漏判 | 我们自己的产物只有一个 APP1，不受影响 |
| `web/index.html` 冻结 | 解析部分与 Python 对齐验证过，像素烘焙未做 | 用户明确要求「保留但别动」，**不要投入** |

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
| Git Bash 改写 adb 路径 | `adb shell ls /sdcard/` 报 `…/PortableGit/sdcard: No such file` | 命令前加 `MSYS_NO_PATHCONV=1`（Windows 专属） |
| **只拷 `hdc.exe` 单文件** | 运行直接 exit 127，无任何报错 | 缺同目录 DLL。必须整个 `toolchains/` 目录一起用 |
| hdc 下载源选择 | 华为下载中心 0.04–0.2 MB/s 且会卡死 | 用华为云镜像流式提取（3.6 MB/s），见 `docs/03` §3.8.1 |
| 下载中断的 zip 仍像 zip | `.crdownload` 有 PK 头，解压才报错 | `zipfile.is_zipfile()` 只有在中央目录完整时为真，先验再解 |
| **裸 `python` 不是 venv** | `python -c "import PIL"` → `ModuleNotFoundError`（指向 3.13.12 系统装）| 一律用 `…/envs/default/Scripts/python.exe`，或走 `./run.sh` |
| `run_parity.sh` 在 Windows 失败 | 脚本探测 `…/envs/default/bin/python`（POSIX），回落到无 numpy 的 `python3` | 该脚本**仅 macOS 验证过**；Windows 需手工指定解释器，且本机无 `kotlinc` |

### 5.2 真机（华为 Mate 30 / EMUI）

| 坑 | 现象 | 解 |
|---|---|---|
| HDB 通道 | 设备管理器有 `Huawei HDB Interface` 但 adb 空列表 | 开「USB 调试」+「仅充电时允许 ADB 调试」，USB 用途改「传输文件」 |
| **免打扰拦截授权弹窗** | `unauthorized`，换 adbkey 也不弹 | 关免打扰，或「撤销 USB 调试授权」后重插 |
| MSYS 路径改写 | `adb pull` 报 `...PortableGit/sdcard/...` | 命令前加 `MSYS_NO_PATHCONV=1` |
| 后台被回收 | picker 占前台后 App 被杀，无崩溃日志 | 手动操作；后续可做「导出后立即通知」保持前台优先级 |

### 5.2b 真机（HarmonyOS NEXT，2026-10-21 起）

**设备**：HUAWEI CLS-AL30，HarmonyOS 7.0.0.109(SP6C00E105R10P3)，API 26，序列号 `55K0225905000192`。

| 坑 | 现象 | 解 |
|---|---|---|
| **误判「装不了」** | 曾据「NEXT 只认 .hap」断言 APK 无法安装 | **错**。NEXT 原生不解析 APK，但可装进**卓易通**容器跑。详见 `docs/05-harmonyos-zhuoyitong.md` |
| 只有 HDC，没有 ADB | `adb devices` 永远为空；设备管理器只有 `HDC Device` | 正常现象，两套协议。用 `hdc`（§1.3-C），`adb` 在这台机器上就是废物 |
| **MTP 不是「没有」，是「只能读不能写」** | 早期文档写「WPD 为空」，那是查法错了：`Get-PnpDevice -Class WPD` 看不到，得用 `pnputil /enum-devices /connected` 才看到 `HUAWEI Mate 70`（驱动 `wpdmtp.inf`，已启动）| 读取方向可用（成功拉过文件）；**写入方向 `Shell.Application.CopyHere` 全部静默丢弃**，四种 flags/对象形式都试过。结论：传文件用 hdc，别用 MTP |
| 容器内 App 对外不可见 | `bm dump -n com.finn.hdr` 查不到 | 卓易通是隔离沙箱（iSulad）。**hdc 不能安装、不能启动容器内的 App**，这两步必须人工 |
| 相册读不到 | `ls /storage/media/100/local/files/Docs/DCIM` 为空 | 鸿蒙相册在受保护媒体库。**拿不到用户照片原件**，只能靠 App 屏幕日志反馈 |
| APK 可写的落地目录 | —— | `/storage/media/100/local/files/Docs/Download/`（hdc `file send` 目标就是这里）|
| 微信接收改名 | 文件名被加 `.1`，点击无法安装 | 文件管理 → 重命名，去掉 `.1` 恢复 `.apk`（华为官方说明）|
| 容器内选图受限 | 系统 Photo Picker 可能不存在，`PickVisualMedia` 直接报错 | App 已内置三条通道自动退化（PickVisualMedia → OpenDocument → GetContent）+ 独立按钮。见 §3.0 |
| debug 签名被拦 | 容器提示签名校验失败 | 装 Apktool M → 「删除签名验证」生成 `_kill.apk` 再装（**本次未遇到**，0.1.0 直接装上了）|
| 鸿蒙下载中心几乎不可用 | 浏览器 0.04–0.2 MB/s，卡在 400 MB 不动；页面混着 1 KB 的 `.zip.asc` 签名文件，极易点错 | 别让用户下。走华为云镜像或让用户先下完再检查 `zipfile.is_zipfile()`（§5.1）|

**hdc 实测能做与不能做**（省得再摸索一遍）：

| 能做 | 命令 |
|---|---|
| 列设备 / 版本 | `hdc list targets -v` / `hdc -v` |
| 截屏（**最有价值**，替代用户描述） | `hdc shell "snapshot_display -f /data/local/tmp/s.jpeg"` → `hdc file recv` |
| 推文件（14 ms 传完 2.2 MB） | `hdc file send <local> /storage/media/100/local/files/Docs/Download/` |
| 删文件 | `hdc shell "rm -f <path>"` |
| 读系统属性 | `hdc shell "param get const.product.model"` |

| 不能做 | 原因 |
|---|---|
| 装/启动容器内 App | 沙箱隔离，`bm dump` 看不到 |
| 读相册照片 | 受保护媒体库 |
| 拿到 App 的 logcat | 容器内没有这套；App 已自己落盘 `filesDir/hdr-log.txt` + 屏幕日志 + 复制按钮 |

### 5.3 格式

| 坑 | 现象 | 解 |
|---|---|---|
| 扩展名不可信 | `.heic` 实际是 JPEG | 一律看魔数：`FFD8FFE1`=JPEG，`....ftyp`=ISOBMFF |
| **扫描边界 `pos+3`** | 副图被静默丢弃，只剩主图 | 必须 `pos+1 < size`（EOI 可能贴文件尾）。Python 对、Kotlin 错过 |
| XMP 段长度少 2 字节 | 段边界漂移 | `len(payload)+2`；BOM 是 1 字符 3 字节 |
| `HDRCapacityMax` 写线性值 | 宣称 16 倍容量 | 该字段是 log2 域 |

---

## 6. 常用命令

> 两个必须同时满足：**① 用 venv 解释器**（裸 `python` 指向 3.13.12 系统装，没有 PIL/numpy）；
> **② `PYTHONPATH=src/python`**，否则 `ModuleNotFoundError: No module named 'uhdr'`。
> `./run.sh <子命令>` 已经替你处理好这两件事，能用它就别手敲。

> **全文占位符约定**：`<file>`、`<源图>`、`<你的文件>` 这类尖括号是**人工参数位，必须先替换成真实路径再执行**。
> 在 sh 里未加引号的 `<xxx>` 是**输入重定向符**，直接粘贴不会报错，只会静默读 stdin 然后什么都不做。

```bash
ROOT="$(git rev-parse --show-toplevel)"; cd "$ROOT"   # 每段都从仓库根开始，避免 cd 串味

# Python 侧（一律用 venv 解释器；裸 `python` 指向 3.13.12 系统装，无 PIL/numpy）
PY="C:/Users/ugnam/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

./run.sh test                              # 28 项回归
./run.sh demo                              # 端到端演示（合成源）
PYTHONPATH=src/python "$PY" -m uhdr.cli inspect  <file>    # 容器结构诊断
PYTHONPATH=src/python "$PY" -m uhdr.cli validate <file>    # 结构自检
"$PY" scripts/check_android_project.py     # Android 工程静态自检（45 项，纯标准库）
"$PY" scripts/analyze_image.py <file> [--extract]  # 一次判定 JPEG/HEIC + 增益图 URN
bash tests/ltm_parity/run_parity.sh        # LTM 双实现对照（仅 macOS 验证过，见 §5.1）
"$PY" tests/bake_quality_check.py <源.jpg>    # 烘焙画质体检（SSIM/等亮度HF/局部对比，信息性）
"$PY" tests/bake_quality_check.py --selftest  # 受控实验：判决 detail_gain 是否生效
"$PY" tests/bake_quality_check.py --eps-sweep # eps 权衡曲线（细节增益 vs 边缘过冲）
"$PY" tests/wechat_proxy.py <file.jpg>        # 微信路径代理测试（元数据存活 + 效果存活率）

# Android 侧
cd src/android
./gradlew assembleDebug --no-daemon                     # 不带 native
bash ../../scripts/fetch-libultrahdr.sh                 # 需要 native 时
./gradlew assembleDebug -PwithNative=true

# 真机 —— 鸿蒙（当前设备，用 hdc）
HDC="C:/Users/ugnam/hdc_official/hdc.exe"
cd "$ROOT" && "$HDC" list targets -v        # [Empty] = 没连上
cd "$ROOT/out" && "$HDC" file send HdrVividUltraHdr-0.1.7-debug.apk \
  /storage/media/100/local/files/Docs/Download/HdrVividUltraHdr-0.1.5.apk
"$HDC" shell "ls -l /storage/media/100/local/files/Docs/Download/*.apk"   # 必须出现 2.2 MB 的 0.1.5；只有旧包=推失败
"$HDC" shell "snapshot_display -f /data/local/tmp/s.jpeg"   # 截屏
cd "$ROOT/out" && "$HDC" file recv /data/local/tmp/s.jpeg "screen_$(date +%H%M%S).jpeg"
  # 别覆盖 phone_screen.jpeg——它已入库且被 §8 列为隐私文件

# 真机 —— 安卓（旧 Mate 30 / 模拟器，用 adb）
cd "$ROOT" && adb install -r out/HdrVividUltraHdr-0.1.7-debug.apk
F="cache/exports/IMG_xxxx_HDR.jpg"       # 换成实际文件名，别照抄 <file> 占位符
adb exec-out run-as com.finn.hdr cat "$F" > pulled.jpg \
  && test -s pulled.jpg || echo "拉取失败或文件不存在"   # 重定向会掩盖失败，必须 test -s
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
| H-11 | ISO 21496-1 的 header 5 字节语义与字段顺序 | **仍是推测**。实测布局是 `28B 签名 + 5B header + 7×8B fraction`，真实样张解出 `0, 2.1484, 0, 2.1699, 1.0, 0, 0`，与标准顺序对不上（第 3 位为 0、第 5 位为 1.0），因此 `iso21496.py` 在语义校验失败时走保守回退、gamma 固定 1.0。**要准必须查规范原文或抓两台设备的同类文件对比** |

**H-11 的验证捷径**：手上有 2 张以上**不同设备/不同参数**的 ISO Ultra HDR 原图时，
对比 `raw_values` 的差异位置，就能反推字段顺序（`iso21496.parse_iso_app2` 会保留原始 7 个分数）。
目前只有 1 张，无法交叉验证。

---

## 8. 隐私提示（重要）

`out/` 下**三个**文件含真实照片或设备状态，已推送到私有仓库：

| 文件 | 内容 | 建议 |
|---|---|---|
| `out/real_sample.ultrahdr.jpg` | 由你的样张导出的 HDR 文件（1.5 MB） | 若要移除：`git rm out/real_sample.ultrahdr.jpg && ./scripts/ship.sh "chore: 移除含隐私的样例产物"`（历史仍保留，彻底清除需 rewrite history） |
| `out/app_screenshot.png` | App 界面截图（曾一度截到通知栏） | 同上 |
| `out/phone_screen.jpeg` | 手机整屏截图（含状态栏、通知、电量、蓝牙状态） | 含设备状态信息。不需要时同理 `git rm` |
| `out/visual/` | 由真实照片生成的三档烘焙图 | **已被 gitignore，不会入库**，但换机器需重新生成（§3.2）|

另外 `C:/Users/ugnam/AppData/Local/Temp/hdrprobe/bluetooth_recv.heic` 是**唯一一份真实样张原件**，
它在系统临时目录，随时会被清理。**交接时务必先把它复制到云盘或仓库外的固定位置**，
否则 §3.2 的样张覆盖要从头找设备重拍。

仓库是私有的，风险可控。若将来要开源，**务必先清理这三个文件并重写历史**。

---

## 9. 接手路径（按手上时间选）

> **前提**：下表时间只对「这台 Windows 机器 + 环境已装好 + 手机能连上」成立。
> 换新机器要先做 §1.2（JDK/SDK/Gradle，约 10 分钟）与 §1.3-C（hdc，另需 2.5 GB 工具包），
> 这部分时间是刚性的。

构建/安装命令见 §1.3 与 §6。真机结果补进 `docs/02-verification.md`。

| 手上时间 | 做什么 |
|---|---|
| **10 分钟** | 走 §3.0：插线 → `hdc file send` → 卓易通安装 → 选图 → 截屏。**这一步能验证掉 90% 的未知**，因为代码改了很多轮但一次都没在真机跑过 |
| **1 小时** | §3.0 + §3.1（从截屏里的 `格式=` 与 aux URN 判断那张报错照片到底是什么）|
| **半天** | 再推 §3.2 目视 LTM 三档 + §3.3 微信代理测试复核 |
| **要交付给外部** | 必须先做完 §3.3，否则不能承诺「微信发出去对方能看到 HDR」。当前对外话术只能是「原图路径发 Ultra HDR、普通发送发烘焙图」双轨 |

**接手第一小时不要做的事**：不要动 `src/ets/`、`src/harmony/`（鸿蒙原生骨架，需开发者账号 + 实名 +
Profile 绑 UDID，成本远高于收益）；不要给 `web/index.html` 投入（用户明确要求冻结）；
不要去接 libultrahdr native（§3.6，当前纯 Kotlin 已能产出合规容器）。

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

- `strength = 0.70`（目视定档，见 `docs/04-HANDOFF-bake.md` 的 **「7. B 侧定档结论」** 一节；
  该文有重复的 §6/§7 标题，按标题文字找，别按序号找）
- 三档 profile：`off`（仅全局）/ `local`（+局部色调映射）/ `wechat`（克制版，为二次压缩留余量）
- 命令（`$PY` 见 §6）：`PYTHONPATH=src/python "$PY" -m uhdr.cli bake <源文件> -o out.jpg --profile local --strength 0.70`
  （`./run.sh` 目前没有 `bake` 子命令，只能这样调；`--profile off|local|wechat`、`--anchor sdr-white|peak`）

### 10.3 烘焙相关的未完成项

| 优先级 | 事项 | 说明 |
|---|---|---|
| P2 | 微信档实测 | `wechat` profile 的 detail_gain=1.12 未经真机二次压缩验证 |

（真实样张覆盖与 LTM 目视即 §3.2，不重复列。LTM 参数决策已于 2026-10-21
执行：eps 调至 0.2/0.15，依据与验证数据见 docs/04 §8.5。）

### 10.4 踩过的坑

- **`contrast` 不能在线性域做**：线性 0.5 ≈ sRGB 188，S 曲线会把暗部钉死成纯黑（实测 19.8 → 0.7）。必须在 `_linear_to_srgb` 之后。
- 暗部亮度不能作为验收硬指标：源图自身暗部带增益，烘然后暗部必然高于原图，应看「无灰雾、无死黑、高光裁切 <1%」。


---

## 11. APK 版本对照（弄错版本会白忙一场）

`.gitignore` 里是全局 `*.apk`，**构建产物不入库**，每台机器只有自己构建的那份。
本地现存：`out/HdrVividUltraHdr-0.1.7-debug.apk`（约 2.2 MB）。

### 11.1 ⚠ 同一个 versionCode 1 / versionName 0.1.0 被两套界面用过

这是最容易搞混的地方。版本号在早期没有随代码递增，导致「0.1.0」实际有两个不同的 App：

| 构建来源 | 提交 | 按钮 | 预览位 | 日志框 |
|---|---|---|---|---|
| Windows 侧（**大概率是手机上装的那个，未截屏确认**）| `40ef88b` | 「选择 HDR 照片并导出」+「以文件发送（保留 HDR 信息）」| 无 | 有（纯文本 TextView）|
| macOS 侧（**模拟器验的那个**）| `2bd975e` | 「选择 HDR 照片并处理」+「发送烘焙图（微信可见 HDR 效果）」+「发送 Ultra HDR（需设备支持）」+ 强度滑杆 | 无 | 有 |

两者都是 `versionCode = 1`。所以**不能用版本号判断手机上跑的是哪一版**，只能看界面。

### 11.2 各版本关键变化

| 版本 | versionCode | 提交 | 关键变化 | 界面独有特征 |
|---|---|---|---|---|
| 0.1.0 | 1 | `40ef88b` / `2bd975e` | 首个可安装包；纯 Kotlin 封装；烘焙功能（macOS 版）| 见 §11.1，两个界面 |
| 0.1.1 | 2 | `1388289` | 选图三条通道自动退化（PickVisualMedia → OpenDocument → GetContent），错误详情上屏 | 「用文件管理器选择」按钮 |
| 0.1.2 | 3 | `97b0505` | **实时预览**（左原图 / 右烘焙后）、五段式 `[1/5]…[5/5]` 日志、强度滑杆联动重算 | **两个图片位** + 「导出全尺寸」+「复制全部日志」|
| 0.1.3 | 4 | `3b3c777` | `HeifInspector` 结构探针（不解码也读出 aux URN）| 选到 HEIC 时日志出现 `ftyp` / item 表 |
| 0.1.4 | 5 | `7a9ba54` | 日志自动滚到底、保存到相册、导出只留 10 份、旋转不重建 Activity | 「保存烘焙图到相册」按钮 |
| 0.1.5 | 6 | `5e5eb96` | 三路审查修复：线程崩溃 / 往返丢增益参数 / EXIF 方向 / 采样解码防 OOM / 预览防抖 / 解析溢出 | 外观同 0.1.4，行为更稳 |
| 0.1.6 | 7 | `59bfb27` | **HEIC 主图解码** + `iloc` 长度诊断 + 「导出原图」 | 「导出原图（HEIC 无增益图时用）」按钮 |
| **0.1.7** | **8** | 见 `git log` | 诊断压成 4~8 行摘要 + 日志落盘导出（解决「复制不全」）| 「复制日志（短）」「导出日志到下载」两个按钮 |

### 11.3 一眼判定手机上装的是哪版

从下往上判，命中哪个就是哪个：

| 看到这个 | 版本 |
|---|---|
| 「导出日志到下载」按钮 | **0.1.7** |
| 「导出原图（HEIC 无增益图时用）」按钮 | ≥ 0.1.6 |
| 「保存烘焙图到相册」按钮 | ≥ 0.1.4 |
| 「复制全部日志」按钮 + 两个图片位 | ≥ 0.1.2 |
| 只有「用文件管理器选择」多出来，没有图片位 | 0.1.1 |
| 「选择 HDR 照片并**导出**」+「以文件发送（保留 HDR 信息）」 | 0.1.0（Windows 构建）。**手机上装的很可能是这个，但未经截屏确认** |
| 「选择 HDR 照片并**处理**」+「发送烘焙图（微信可见 HDR 效果）」+「发送 Ultra HDR（需设备支持）」+ 强度滑杆 | 0.1.0（macOS 构建，模拟器验的就是这个）|

`hdc` 截屏是最快的判定方式，不用问用户。

---

## 12. 一句话总结

**代码写完了，验证卡在「装到设备上跑一次」。** 所有剩余风险都源于同一个事实：
v0.1.2 之后的所有改动（预览、日志、HEIC 探针、保存相册、审查修复）**从未在任何设备执行过**
——连模拟器验的都是 v0.1.0 的产物。先做 §3.0，再谈别的。
