# HANDOVER — 交接文档

> 最后更新：2026-10-25（v0.1.22：**修「烘焙图发灰」= 丢 Display P3 ICC**；提交/打包前强制本地自检；**测试策略调研见 `docs/07-testing-strategy.md`**）
> 读这一篇就能接手，不需要回看聊天记录。**先看 §0 和 §10.2，这两处是这一晚的结论。**

---

## 0. 30 秒接手

| 问题 | 答案 |
|---|---|
| 这是什么 | 把华为 HDR Vivid 静图导出为「SDR 主图 + 增益图 + XMP + MPF」的 Ultra HDR JPEG；**并**同时产出一张「HDR 效果已烘焙进像素」的普通 JPEG（微信/任何设备可见） |
| 到哪一步了 | **真机全链路已跑通**（`docs/02` §2.9：真实华为样本，Python 独立校验 **20/20**）。烘焙算法 v3 + 4 档 profile；本机可真跑 Kotlin（`tests/android_parity/`）；一键自检 `scripts/verify_bake.py` 7 步全绿。**当前手机上是 0.1.22（未安装）**，观感待用户最后一次确认 |
| 下一步唯一要做的事 | ① **改任何东西先跑 `python scripts/verify_bake.py`**（拉样例→静态→两端常量→回归 53→验收 10/10→Kotlin 像素 9/9→Kotlin grid 7/7，缺 kotlinc/样例会标 SKIP）；② 装 0.1.22 确认**发灰已修**（饱和度从 60.2 回到 66.3，正确值 69.0）；③ 仍未解决的是「层次少」——见 `docs/07-testing-strategy.md` §4 的量化方法，先补对比度类指标再调参 |
| 部署到手机 | 用户设备是 **HarmonyOS NEXT（HUAWEI CLS-AL30 / 7.0.0.109 / API 26）**。NEXT 不原生解析 APK，但可装进**卓易通**容器运行，步骤见 `docs/05-harmonyos-zhuoyitong.md`。**早期文档写过「鸿蒙装不了 APK」，那是错的** |
| 手机调试工具 | **`C:\DevEco\command-line-tools\sdk\default\openharmony\toolchains\hdc.exe`（3.2.0f）**，能截屏/拉日志/推拉文件。随 HarmonyOS Command Line Tools 安装，**不在仓库里**，换机器需重装（§1.3-C）|
| 下一个真问题 | **观感对齐缺少参照物**。目前以「源 SDR 主图的中位亮度」为基准（依据：手机显示 HDR 时参考白仍对应 SDR 1.0）。若这样仍不一致，唯一可靠办法是拿**手机截屏**当目标对齐 —— 用户暂不愿意，所以这是唯一没有客观解的部分 |
| 已解决的真问题 | 那张 HEIC **确实有增益图**：不是 aux，而是 `tmap` 派生项引用「主图 grid + 增益图 grid（4 块 hvc1）」；且系统/微信转 JPEG 时会写成 ISO Ultra HDR（增益图 535~620 KB），**可绕行**（§3.5） |
| 仓库 | `git@github.com:freakkid/hdr-vivid-ultrahdr.git`（私有），分支 `main` |
| 最新提交 | 见 `git log --oneline -1`（截至 v0.1.16）|
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

# 4) 验证 Python 侧（32 项测试，约 2 秒）
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

**C. 鸿蒙调试工具 hdc（2026-10-24 起走官方 Command Line Tools）**

| 项 | 值 |
|---|---|
| 工具根目录 | `C:\DevEco\command-line-tools\`（HarmonyOS Command Line Tools 26.0.0.851，绿色免安装，7.68 GB）|
| hdc 路径 | `C:\DevEco\command-line-tools\sdk\default\openharmony\toolchains\hdc.exe`（Ver 3.2.0f）|
| 其余入口 | `bin\` 下 `hvigorw`(6.26.8) / `ohpm`(26.0.0.630) / `codelinter` / `hstack` / `arktsdoc` / `Emulator`；内置 Node v24.14.1 |
| 用户环境变量 | `DEVECO_SDK_HOME=C:\DevEco\command-line-tools\sdk`；用户 `PATH` 追加 `bin` 与 `sdk\default\openharmony\toolchains` |
| 是否随仓库走 | **否**。换机器需重装一次；本机已实测 `hdc -v` / `ohpm -v` / 设备连接均正常 |
| 与旧路径关系 | 旧的 `C:/Users/ugnam/hdc_official/hdc.exe` 是单拷出来的副本，可删；两者版本同为 3.2.0f |

```bash
HDC="C:/DevEco/command-line-tools/sdk/default/openharmony/toolchains/hdc.exe"
"$HDC" -v                       # Ver: 3.2.0f
"$HDC" list targets -v          # 正常应显示序列号；[Empty] = 没连上
```

> 早先的坑（**只拷 `hdc.exe` 单文件会 exit 127**，缺同目录 DLL）对官方工具链不适用，
> 但 `docs/03-toolchain-setup.md` §3.8 记录了当时的获取经过，留作历史。

---

## 2. 基线状态

Ultra HDR 主链路与烘焙链路均已实现并通过验证：回归 32 项、静态自检 45 项、
APK 可构建（**v0.1.16 / versionCode 17**）。**已完成项不在此罗列**——证据在 `git log` 与
`docs/02-verification.md`，烘焙算法决策在 `docs/04-HANDOFF-bake.md`。
尚未做的只有 §3。

**必须说清的一个事实：模拟器端到端实测（App 产物经 Python 独立校验 20/20、
App 与 Python 烘焙输出 SSIM 0.95，见 docs/02 §2.8）验的是 `2bd975e` 的产物，
那时 `versionCode = 1 / versionName = 0.1.0`。**

2026-10-24 真机验证已覆盖「解析 → 解码 → 烘焙 → 封装 → 自检 → 导出」全链路
（v0.1.13 + 真实华为 HDR 样本，产物经 Python 独立校验 20/20，见 `docs/02` §2.9）。
**仍未覆盖**：显示效果（需第二台 Ultra HDR 设备）、多场景样张、微信不勾原图时的行为。

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

**2026-10-23（v0.1.8）又做了一轮通读审查，修掉 5 个「会静默产出错误结果」的缺陷**：

| 缺陷 | 为什么危险 | 修法 |
|---|---|---|
| Kotlin 解析 XMP 载体源图时用 `probeMeta`（无 native 时恒 null），不读增益图自身 XMP | 纯 Kotlin 包（0 个 `.so`）下**每次**回读自己导出的 `_HDR.jpg` 都拿不到增益参数，退回默认 `GainMapMax=2.0`（源真实值常为 2.1484）→ 导出再解析掉增益。与 Python 侧已修的「4.43x→4.00x」同源，Kotlin 侧漏了 | 新增 `gainMetaFromXmp()`：解析增益图 APP1 里的 hdrgm 属性（单/双引号都认），取不到才回退 probe → 默认值，并在日志写明来源 |
| Python `iso21496.parse_app2` 长度门槛写成 `header+1 个 fraction`（13 B），循环却读 7 个（56 B） | 第三方畸形段 body 落在 13~60 B 时抛 `ValueError`，`vivid_source` 未捕获 → 整条 export 崩掉 | 门槛改 `MIN_BODY_LEN=61`，循环内逐项兜底返回 None（与 Kotlin 一致） |
| Python `jpegseg` 遇 `FF FF` 填充时前进 2 字节 | JPEG 允许 marker 前任意多个 `0xFF`，前进 2 会跳过真 marker 起始位 → 合法文件被判「段边界错位 / 不是合法 JPEG」 | 填充只前进 1 字节；`find_image_end` 同样跳过 |
| 两端自检都取「第一个 APP1」当 XMP（APP2 当 MPF） | 相机文件通常 EXIF 在 XMP 之前 → **正确文件被报 `xmp_present` ERROR**；ISO 21496-1 也是 APP2，在前时会顶掉 MPF | 改为按段内签名匹配（`XMP_SIG` / `MPF_SIG`） |
| `pipeline.export` 对 CUVA/unknown_mpo（无 hdrgm 参数）静默套默认值且 `ok=True` | 结构合法 ≠ 增益语义正确，调用方只会看到 ok | 新增 `ExportResult.semantics_verified`，警告置顶，`steps` 写 `meta(ASSUMED)`；CLI 打 stderr 并提供 `--require-verified`（退出码 3） |

回归从 28 项增至 **32 项**（新增：0xFF 填充、ISO 截断降级、EXIF 在前的 XMP 判定、语义未验证上报）。

**2026-10-24（v0.1.9）装机实测暴露一个环境级问题**：App 报「日志已导出」但宿主机取不到文件。
排查结论是容器存储隔离（见 §5.2b），`hdc` 侧三条路全封死：
`shell` 无权限 → `file recv` 也无权限 → `hdc smode` 零售版禁用。
导出通道因此改为 SAF `ACTION_CREATE_DOCUMENT`（由系统文件选择器提供目标位置）。

**v0.1.10 装机实测：SAF 也不行。** 保存后宿主机 `Download`/`Documents`/`Desktop` 全空，
`find /storage/media/100/local/files -name 'hdr-log-*'` 无结果——容器的 DocumentsUI 同样是容器组件，
选中的位置仍在容器私有文件系统里。**结论：宿主机无法以任何方式读取容器文件，
唯一可用出口是剪贴板**（App 写剪贴板 → 用户在宿主机应用里粘贴）。
SAF 按钮保留，权当「让用户能在容器内自己找到文件」，不再指望它跨容器。


---

## 3. 未完成（按优先级）

### 3.0 【已完成 2026-10-24】v0.1.13 真机全链路验证

代码侧没有已知阻塞。**APK 已构建好（`out/HdrVividUltraHdr-0.1.13-debug.apk`，2.15 MB）。**
v0.1.8 已在真机上跑通（界面 9 按钮、HEIC 主图 `4320x5760` 解码成功）；
**v0.1.9 之后（SAF 导出、清理残留）还没装过**——这两项是为解决「容器内写在私有存储、
宿主机取不出来」加的，落点是否通要靠 §3.0 的实操确认。

```bash
HDC="C:/DevEco/command-line-tools/sdk/default/openharmony/toolchains/hdc.exe"

# 1) 确认连上（[Empty] 就是没连：需解锁屏幕 + USB 调试 + 手机上点允许调试）
"$HDC" list targets -v

# 2) 先删旧包。两个坑：
#    ① 必须 rm -rf：鸿蒙把每个 .apk 呈现为「目录包文件」，rm -f 会报 Is a directory 删不掉
#    ② 通配符写 0.1.[0-9].apk（单数字版本），别写 0.1.*——会把马上要推的 0.1.10 一起匹配掉
"$HDC" shell "rm -rf /storage/media/100/local/files/Docs/Download/HdrVividUltraHdr-0.1.[0-9].apk"

# 3) 再推新包（用绝对锚点，不依赖当前目录）
cd "$(git rev-parse --show-toplevel)/out"
"$HDC" file send HdrVividUltraHdr-0.1.13-debug.apk \
  /storage/media/100/local/files/Docs/Download/HdrVividUltraHdr-0.1.13.apk

# 4) 必须看到一行 2.15 MB 的 0.1.10-debug.apk，否则推失败
"$HDC" shell "ls -l /storage/media/100/local/files/Docs/Download/*.apk"
```

> **这个 rm 顺序是有原因的**：若先推后删且写成 `0.1.*.apk`，`*` 会匹配 `5.apk`，
> 刚推上去的文件被自己删掉。更麻烦的是目录里若还有别的 apk，`ls` 照样列出、退出码 0，
> 看起来一切正常。这是本文初稿真实写错过的坑。

然后在手机上：**卓易通 → 右上角「+」→「本地安装包」→ 选该 APK**。
（hdc 装不了：`bm dump -n com.finn.hdr` 查不到容器内的 App，它是隔离沙箱，装/点必须人工。）

装完判定标准，按顺序看（**下面文案取自 v0.1.16 源码，逐字对得上才算装对**）：

| 检查 | 期望 |
|---|---|
| 按钮共 11 个，**分两处** | **顶部两个**：「选择 HDR 照片」「用文件管理器选择（相册不可用点这个）」；**滑杆下方九个**：「导出全尺寸（2048 长边）」「发送烘焙图（微信可见 HDR 效果）」「发送 Ultra HDR（需设备支持）」「保存烘焙图到相册」「导出原图（选保存位置，HEIC 用这个）」、「复制日志（短）」与「导出日志（选位置）」（并排一行）、「清理导出残留」与「查看全部日志（全屏）」（并排一行）|
| 中间控件（自上而下）| 左右两个图片位 → 一行状态文字 → **无标签的下拉框**（首项「本地观看（local：局部色调映射）」，然后「微信分享（wechat：克制映射+重压缩余量）」「仅全局映射（off：旧行为，A/B 对照）」）→ 「烘焙强度：0.70（1.0 = 完全按元数据恢复 HDR）」→ 滑杆 |
| 首行日志 | `===== 载入 content://<authority> =====`（只到 authority、不含路径；前面还有一个空行）。注意：**不是** `URI：`，早期文档写错过 |
| 格式行 | `格式=JPEG  头部=FF D8 FF E1 ...`；非 JPEG 时是 `格式=ISOBMFF(heic)` 等 |
| 若 JPEG 且有增益图 | `容器：ISO_ULTRA_HDR`、`增益图：有（xxxxx B）`、右侧图片位出图 |
| 若无增益图 | `容器：PLAIN_JPEG`、`该图无增益图（已是普通 SDR）。左图为原图。` |
| 导出 | 点「导出全尺寸」→ `validate: ok=true errors=0`，产出 `IMG_xxx_BAKED.jpg` 与 `IMG_xxx_HDR.jpg` |
| **回读自检（v0.1.8 的关键验证点）** | 把刚导出的 `IMG_xxx_HDR.jpg` **再选一次**，日志应出现 `· hdrgm 参数取自增益图自身 XMP`，且 `meta(source): GainMapMax=` 与首次导出一致。若出现 `⚠ 增益图与 probe 均未提供 hdrgm 参数`，说明回退逻辑被触发，这次修复没生效 |
| **要取日志给外部看** | 点「查看全部日志（全屏）」把日志铺满屏幕 → 宿主机 `hdc snapshot_display` 截图读取。**这是唯一可靠的跨容器通道**：容器写出的文件（MediaStore / SAF）宿主机一律读不到 |
| 报错时 | 点「复制日志（短）」拿全文，或「查看全部日志（全屏）」让排查者截图（容器无 logcat）|

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
| 摘要块里 `aux=0` 但安装了 v0.1.7+ 后仍显示无增益图 | 可能是「主图本身 HDR 编码」 | **用桌面 `analyze_image.py` 看 `transfer=`**：16=PQ / 18=HLG 即为 HDR，此时不需要增益图，需要 PQ/HLG 逆变换（§3.4 路线 A）|
| 摘要块里 `aux=0` 且 transfer=13(sRGB) | 普通 SDR 的 HEIC | 只需支持主图解码，不做增益图 |
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

> **先分清 HDR 的两种形态，否则会一直找错东西**：
>
> | 形态 | 判据 | 处理方式 |
> |---|---|---|
> | **增益图型**（大多数手机默认）| 有 `aux`，URN 是 ISO 21496-1 / 厂商 hdrgainmap | SDR 主图 + 增益图，按 hdrgm 公式重建 —— 我们已实现（JPEG 容器）|
> | **tmap 派生型** | `compat` 含 `tmap`（ISO 21496-1 的 HEIF 品牌），增益图是 `iref` 的 `dimg` 指向的独立 item（**实测是 4 块 `hvc1` 拼的 grid**），**auxC 里看不到** | 按 `dimg` 取出那 4 块并拼图（v0.1.14 用「重封最小 HEIF 交给系统解码」实现，待真机确认）|
> | **直接编码型** | 无 aux，但 `nclx transfer` = 16(PQ) / 18(HLG)，常配 BT.2020 + 10bit | 主图本身就是 HDR，需要 **PQ/HLG 逆变换 → 线性 → 色调映射**，属 §3.4 路线 A |
>
> **2026-10-24 实测教训**：用户那张华为 HEIC 是 `aux=0`，旧解析器直接判「无增益图，普通 SDR」；
> 但它的 `compat=heic,mif1,tmap` 且 `meta` 里有 `iref`——**`aux=0` 不能推出「没有增益图」**。
> 0.1.12 起摘要会同时打印 `compat-tmap` / `tmap项` / `iref` 关系，判定不再只看 aux。
>
> 桌面 `scripts/analyze_image.py` 已能自动区分这两者并给出结论（v0.1.7 后）。
> 所以「有没有增益图」不是 HDR 的唯一判据 —— 这点此前一直没说清。

当前只解析 JPEG（HEIC 分支只做结构诊断 + grid 重封试解，**还没接进烘焙链路**）。
若源图是真 HEIC（`ftyp` 开头），不会崩，但会走到「已识别是 HEIC，
本版不支持解码，结构已列在下方日志」。

**已有部分**（v0.1.6 起，别重复造）：
- `HeifInspector.kt` —— 纯 Kotlin ISOBMFF 遍历：box 树 / item 表 / `iloc` 长度 / aux URN，不解码
- `HeicDecoder.kt` —— 系统解码器解**主图**（`ImageDecoder` API28+ → `BitmapFactory` 兜底），
  软件位图、按 2 的幂采样。**拿不到增益图**（那是 Android 14 的 `Bitmap#getGainmap`）
- 「导出原图」按钮 —— 原始字节写 `MediaStore.Downloads/HDRRaw/`，解决「原件取不出来」
- `scripts/analyze_image.py` —— 桌面侧，走 pillow-heif，`--extract` 能把 aux 图解成 PNG
- 系统转码兜底 —— `openTypedAssetFileDescriptor(uri, "image/jpeg", null)`，
  **实测在卓易通容器内不可用**，且即使可用增益图也会丢
- `HeifGridRewriter.kt`（v0.1.14）—— 解析 `grid` / `dimg` / `iloc` / `ipma`，把指定 grid
  （含 4 块增益图）**重封成最小 HEIF** 交给系统 `ImageDecoder`。tile 码流与 `hvcC` 原样搬运，
  **不碰 HEVC、不引 NDK、APK 仍是 0 个 `.so`**。选图日志 `[2c/5]` 段会打印 grid 结构并试解一次
- `src/python/uhdr/heif_grid.py` —— 同一套逻辑的桌面参考实现 + 诊断 CLI
  （`--json` / `--dump ID:PATH` / `--hex ID` / `--rewrap GRID -o out.heic`），
  回归见 `tests/test_heif_grid.py`（16 项，合成文件；**真机华为文件仍待验证**）

**2026-10-24 拿到 7 张真 HEIC 原件（微信「文件」通道，`FileStorage/File/2026-10/`），结构完全一致**：

| 事实 | 值 |
|---|---|
| 增益图载体 | **不是 aux**，是 `tmap` 派生项。`iref` 里 `dimg <tmap> -> [<主图 grid>, <增益图 grid>]` |
| 主图 | grid 拼接：12 块（大图）或 30 块（更高分辨率）`hvc1` |
| 增益图 | grid 拼接：**固定 4 块 `hvc1`**，合计约 265 KB |
| tmap item | 62 字节，内容是**嵌套的 meta box**（`746d6170`="tmap" + meta），**不是 hdrgm 参数**（此前猜"5+56 字节参数块"是错的） |
| 华为私有元数据 | `mime` 项 380 KB（`application/vnd.huawei/...`）+ `it35` 项 237 字节 |

结论：自研提取的真实成本是「解 4 块 HEVC grid + 按 grid 拼装」，不是「取一个 aux item」。

**v0.1.14 把「拼 grid」这步做成了重封**：`HeifGridRewriter` 把 grid 与其 4 块 tile 重封成
最小 HEIF，**HEVC 解码交还给系统**（`ImageDecoder` 本来就会拼 grid），因此不需要 NDK、
也不需要 libheif。剩下的未知只有两个：① 系统/容器是否真的能解重封图（真机待验）；
② 华为私有参数项（`mime` 380 KB / `it35` 237 B）怎么解析 —— **没有任何开源实现**。

**绕行方案已验证可行**：系统/微信把 HEIC 转成 JPEG 时，会写成 **ISO Ultra HDR**（增益图 535~620 KB、
`GainMapMax=2.1484`、原始值 `[0, 2.148445, 0, 2.169925, 1.0, 0, 0]`），App 走 JPEG 分支即可正常处理。
所以 §3.5 的优先级应下调：能绕行，不必急着自研 grid 提取。

**要做的**：任选一条，成本从低到高

| 路线 | 成本 | 风险 |
|---|---|---|
| 自研 ISOBMFF：把增益图 item 抽出来重封成最小 HEIF 再交给 `ImageDecoder` | 中（纯 Kotlin，无需 NDK）| **v0.1.14 已实现 grid 版**（`HeifGridRewriter` + 桌面 `uhdr.heif_grid`），待真机验证 |
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
| ~~`MetadataValidator` 只认第一个 APP1~~ | **0.1.8 起已修**：按段内签名匹配 APP1(XMP) / APP2(MPF)，不再被 EXIF / ISO 段顶掉 | —— |
| `web/index.html` 冻结 | 解析部分与 Python 对齐验证过，像素烘焙未做 | 用户明确要求「保留但别动」，**不要投入** |

**2026-10-23 通读审查还发现以下问题，判断为不影响当前主链路，本次「只修会静默出错的那五个」故未动**。
一条改太多就无法归因，所以留档而不是顺手改：

| 项 | 现象 | 影响 |
|---|---|---|
| `clippedRatio` 在 LTM 档恒为 0（`HdrBaker.kt` 与 `bake.py` 同病） | 先 clip 到 [0,1] 再判 `l/white > 1`，恒不成立 | 只影响日志与调参依据，输出正确 |
| 两端自检等级不一致 | Kotlin `ok` = 所有 check 通过；Python 只算 `ERROR` | `capacity_matches_gainmap` 等 WARN 项在 App 是硬失败 → 同一文件 CI 与真机结论可能相反 |
| `ltm.py` 的 `LtmParams.detail_gain` 默认 1.30，`params_for_profile("local")` 是 1.32 | 只有直接构造 `LtmParams()` 才触发 | 当前调用都走 profile，不受影响；但 `LocalToneMapping.kt` 注释写的「两端同默认值」已不成立 |
| `--anchor peak` 两端算法不同 | Python 精确 P99.5；Kotlin 用 1024 桶直方图（先截断到 1.0）+ `peak*0.85` 兜底 | 只影响 A/B 对照与未来的 Golden-file 回归 |
| `xmp.py` 的 hdrgm 正则只认双引号属性；`Item:Data` 解码失败静默变 `(0,0)` | 第三方 XMP 用元素形式 / 单引号时读不到 | 静默退回默认 2.0，与已修的退化同源，只是触发条件更窄 |
| `mpf.parse_mpf` 无边界校验 | 短 buffer 抛 `struct.error` 而非 `ValueError`；截断 entry 产生假 offset | `validator` 用 `except` 兜住不会崩，但丢失错误原因 |
| `bake.py` 全分辨率解码；增益图重采样器与 `export` 不一致 | `long_edge` 只约束输出不约束解码（48MP 图内存峰值 2 GB+）；比例不一致时非等比拉伸 | 桌面工具链，不在 App 链路 |
| `MainActivity` 日志逐行主线程落盘；无增益图分支在主线程解码整图 | 日志上百行时掉帧；超大图可能 ANR / OOM | 真机观感与稳定性 |
| `MainActivity.loadSource` 开头未清 `parsed / pvBase / pvGain` | 换图后拖滑杆可能用上一张的增益图重算预览 | 界面错乱，不崩 |

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
| **`ACTION_SEND` 能跨容器**（2026-10-24 实测）| App 点「发送 Ultra HDR」→ 弹出微信并成功发出 | **这是取产物的唯一好用通道**（拿的是原文件，不是截图）。微信接收端存成 `WeChat Files/<wxid>/…/Image/*.dat`，单字节 XOR `0x7B` 即还原原图；`FileStorage/File/YYYY-MM/` 则是「文件」通道（`.heic` 等原样保留）|
| 相册读不到 | `ls /storage/media/100/local/files/Docs/DCIM` 为空 | 鸿蒙相册在受保护媒体库。**拿不到用户照片原件**，只能靠 App 屏幕日志反馈 |
| **容器内 MediaStore 写的文件宿主机读不到** | App 显示「日志已导出：下载/HDRLog/x.txt（2238 字）」，宿主机 `Docs/Download` 下根本没有 `HDRLog` | 安卓兼容层（anco）的媒体库是**私有**的，真实落点是 `/data/virt_service/rgm_hmos/anco_hmos_data/media/0/`：`ls` 与 `hdc file recv` 全部 **Permission denied**，`hdc smode` 报 `Cannot set root run mode in undebuggable version`（零售版不可提权）。容器内「下载」≠ 宿主机 `Docs/Download`（后者是 sharefs，只负责把文件送进去）。**0.1.9 起导出改走 SAF（用户选位置）**。⚠️ **SAF 也打不通**：v0.1.10 实测「导出日志（选位置）」保存后，宿主机 `Download`/`Documents`/`Desktop` 全空、`find /storage/media/100/local/files` 无结果——**容器的 DocumentsUI 也是容器组件，目标 URI 仍指向容器私有文件系统**。两个可选位置都验证过：`下载内容` = DownloadsProvider（容器）、`CLS-AL30` = `content://com.android.externalstorage.documents/root/primary`（**容器的内部存储**，不是宿主机 `Docs`）。存到 `CLS-AL30/Download/HDRLog/` 后，宿主机 `Docs/Download` 下依然没有该目录，`hdc file recv` 报 `no such file or directory`。**跨容器取数据的唯一可用通道是「屏幕」**：v0.1.11 起 App 提供「查看全部日志（全屏）」，宿主机 `hdc snapshot_display` 截图即可读全部内容；其次是**剪贴板**（App 复制 → 宿主机应用粘贴），两条都已验证 |
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

./run.sh test                              # 48 项回归
"$PY" tests/acceptance/accept.py --real <样本.jpg>   # 烘焙验收（6 指标，改参数必跑）
"$PY" tests/acceptance/accept.py                     # 同上，仅合成样本（CI 用）
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
HDC="C:/DevEco/command-line-tools/sdk/default/openharmony/toolchains/hdc.exe"
cd "$ROOT" && "$HDC" list targets -v        # [Empty] = 没连上
cd "$ROOT/out" && "$HDC" file send HdrVividUltraHdr-0.1.13-debug.apk \
  /storage/media/100/local/files/Docs/Download/HdrVividUltraHdr-0.1.13.apk
"$HDC" shell "ls -l /storage/media/100/local/files/Docs/Download/*.apk"   # 必须出现 2.15 MB 的 0.1.10-debug.apk；只有旧包=推失败
"$HDC" shell "snapshot_display -f /data/local/tmp/s.jpeg"   # 截屏
cd "$ROOT/out" && "$HDC" file recv /data/local/tmp/s.jpeg "screen_$(date +%H%M%S).jpeg"
  # 别覆盖 phone_screen.jpeg——它已入库且被 §8 列为隐私文件

# 真机 —— 安卓（旧 Mate 30 / 模拟器，用 adb）
cd "$ROOT" && adb install -r out/HdrVividUltraHdr-0.1.13-debug.apk
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
| `out/screen_*.jpeg`（2026-10-24 曾误提交两张，已删） | `hdc snapshot_display` 的整屏截图 | 已在 `.gitignore` 加 `out/screen_*`；**排查时优先用 `snapshot_display` + `file recv` 到 `%TEMP%`，不要落到 `out/`** |
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

### 10.2 算法 v3（2026-10-24 重做，接手先读这一节）

```
sRGB → 线性 → 按 hdrgm 还原 HDR 亮度
     → 按 SDR 亮度加权：暗部/中间调保持 SDR，只有亮部采信 HDR
     → 高光软膝收敛到 0.96（不是 1.0）
     → 每像素封顶 min(target, sdr × maxLift)
     → sRGB → 饱和度 → 抖动 → JPEG
```

**为什么不是「直接乘增益再压高光」**（v2 的做法）：
增益图在**全图都有值**（实测中位 86/255）。只要 `strength > 0`，中间调就会被一起提亮，
而 `strength → 0` 只会连 HDR 一起丢掉 —— **调参在数学上救不回来**。
依据：手机显示 HDR 时屏幕参考白仍对应 SDR 的 1.0，所以中间调观感必须与 SDR 一致。

关键参数（`HdrBaker.Options` 与 `bake.BakeOptions` 同名同值，改一端必须改另一端）：

| 参数 | 值 | 作用 | 调它会发生什么 |
|---|---|---|---|
| `strength` | 0.70（滑杆 0.30~1.00） | **按比例缩放「HDR 相对 SDR 的增量」**，不是只乘增益 | 0.30 → 几乎等同源图；1.00 → 完整效果。**必须能看出差别**，见 §10.4 |
| `blendStart/blendEnd` | 0.35 / 0.90 | SDR 亮度低于起始值完全不采信 HDR | 上移 → 更保守，HDR 感更弱 |
| `maxLift` | 1.25 | 单像素最多比源 SDR 亮 25%（安全垫） | **嫌亮就降**；faithful 档固定 1.05 |
| `knee` | 0.75 | 软膝起点 | 调小 → 更早压高光 |
| `ceiling` | 0.96 | 高光收敛上限 | 不取 1.0，否则 0.9~1.0 挤成纯白 |
| `contrast` | 1.0 | 线性对比度 | **别开**，会把接近白的高光推成 255 |

四档 profile：

| profile | LTM | maxLift | 用途 |
|---|---|---|---|
| `local`（默认） | range_compress 0.88 / detail 1.32 | 1.25 | 观感优先 |
| `wechat` | 0.92 / 1.12 | 1.25 | 为二次压缩留余量 |
| `off` | 无（仅全局） | 1.25 | A/B 对照旧行为 |
| **`faithful`**（v0.1.17） | 0.97 / 1.08 | **1.05** | **嫌亮时用这档**：不做整体提亮，只保留高光细节 |

命令（`$PY` 见 §6）：`PYTHONPATH=src/python "$PY" -m uhdr.cli bake <源> -o out.jpg --profile local --strength 0.70`

### 10.2b 验收（改任何烘焙参数后必须先跑）

```bash
# 一条命令跑完：自动从手机拉样例 → 静态自检 → 两端一致性 → 回归 → 观感验收
python scripts/verify_bake.py

# 顺便构建并把 APK 推到手机（之后只剩「点安装」是人工的）
python scripts/verify_bake.py --apk
```

`scripts/check_parity.py` 是其中防「一边改了另一边没跟上」的那一层：对账 `blend/maxLift/
strength 增量表达式/faithful 参数`，任一不一致即失败。它不需要编译器，也不需要设备。

**Kotlin 像素对照（本机真跑，不需要手机）**：

```bash
python tests/android_parity/run_parity.py            # 或随 verify_bake.py 自动跑
```

做法：`tools/kotlin_parity/` 里给了两个桩（`android.graphics.Bitmap`、`UltraHdrNative`），
用 kotlinc 把**真实的** `HdrBaker.kt` + `LocalToneMapping.kt` 编成 jar，读同一份 PNG，
与 `bake(..., return_pixels=True)` 的输出逐像素比对。当前 3 样例 × 3 档全部通过：
均值差 0.36~0.53、P99 ≤3、最大 ≤5（阈值 1 / 4 / 12），即只有查表与浮点舍入差异。

前提：kotlinc 在 `~/.workbuddy/binaries/kotlinc/kotlinc/bin/kotlinc.bat`（或用 `KOTLINC` 指定）；
缺了会自动跳过，不算失败。

6 个可计算指标，阈值与依据见 `tests/acceptance/README.md`。当前 **5/5 PASS**：
中位差 +0.0、纯白比源多 0.00%、暗部 1.02×、SSIM 0.945~0.992。
**不通过就不要推给用户** —— 这套验收就是为此建的。

### 10.3 烘焙相关的未完成项

| 优先级 | 事项 | 说明 |
|---|---|---|
| P2 | 微信档实测 | `wechat` profile 的 detail_gain=1.12 未经真机二次压缩验证 |

（真实样张覆盖与 LTM 目视即 §3.2，不重复列。LTM 参数决策已于 2026-10-21
执行：eps 调至 0.2/0.15，依据与验证数据见 docs/04 §8.5。）

### 10.4 踩过的坑

- **`contrast` 不能在线性域做**：线性 0.5 ≈ sRGB 188，S 曲线会把暗部钉死成纯黑（实测 19.8 → 0.7）。必须在 `_linear_to_srgb` 之后。
- **调 `strength` 解决不了「整体偏亮」**（2026-10-24 血的教训）：增益图全图有值，
  中间调必然被提亮；0.70→0.30 只把 +24 降到 +13，且高光一起没了。**必须按 SDR 亮度分流。**
- **`contrast=1.06` 会让纯白暴涨**：它把 0.98 推成 1.0，实测纯白比源多 3.6 个百分点。已改默认 1.0。
- **软膝别收敛到 1.0**：0.9~1.0 会挤在 0.99+，表现是大片纯白、高光没层次。收敛到 0.96。
- **统计口径要一致**：曾用「源 BT.601 亮度 vs 输出 RGB 算术平均」算出「已对齐」，
  实际差 +13。`tests/acceptance/` 两边统一用 `convert("L")`。
- **字段加了不等于落地**（2026-10-24）：Kotlin `HdrBaker.Options` 有 `maxLift`，但两个分支的
  `target` 都没用它 —— 于是 Python 验收全绿、App 端照样过曝。两道防线已加：
  `tests/test_profiles.py`（faithful 不得比源亮）与 `scripts/check_android_project.py`
  的 `bake.lift_cap_applied`（要求 `liftCap` 出现 ≥3 次）。
- **固定封顶会把强度滑杆压死**（v0.1.18）：`maxLift` 恒为 1.12 时，strength=0.30 的 boost
  已经 1.47x，0.30 与 1.00 的输出几乎一致，真机反馈就是「烘焙强度参数不生效」。
  修法不是调大封顶，而是把 strength 作用到「HDR 相对 SDR 的增量」上
  （`target = sdr + (blended - sdr) * strength`），封顶退回安全垫角色。
  回归：`tests/test_profiles.py::TestStrengthTakesEffect` 要求 0.90 比 0.35 明显更亮。


---

## 11. APK 版本对照（弄错版本会白忙一场）

`.gitignore` 里是全局 `*.apk`，**构建产物不入库**，每台机器只有自己构建的那份。
本地现存与手机上待装的都是 `out/HdrVividUltraHdr-0.1.18-debug.apk`（2.28 MB）。

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
| 0.1.7 | 8 | 见 `git log` | 诊断压成 4~8 行摘要 + 日志落盘导出（解决「复制不全」）| 「复制日志（短）」「导出日志到下载」两个按钮 |
| 0.1.8 | 9 | 见 `git log` | 修复：增益参数退化（改读增益图自身 XMP）、畸形 ISO 段崩溃、`0xFF` 填充误判、XMP/MPF 段按签名匹配、CUVA 语义未验证不再静默 | 界面同 0.1.7；日志新增「hdrgm 参数取自增益图自身 XMP」/「⚠ 语义未验证」 |
| 0.1.9 | 10 | 见 `git log` | **导出改走 SAF**：原 MediaStore 写的文件落在容器私有存储，宿主机 `hdc` 读不到 | 两个按钮文案变了：「导出原图（选保存位置，HEIC 用这个）」「导出日志（选位置）」 |
| 0.1.10 | 11 | 见 `git log` | 新增**清理导出残留**：删除 0.1.8 及之前写进容器私有媒体库的 HDRLog / HDRRaw / HDRBake | 「清理导出残留」按钮；两个日志按钮并排成一行 |
| 0.1.11 | 12 | 见 `git log` | 新增**全屏查看日志**：容器写出的文件宿主机一律读不到（MediaStore / SAF 均实测失败），**屏幕是唯一跨容器通道** | 「查看全部日志（全屏）」按钮，点开为 460dp 可滚动对话框，`HEIC 摘要` 在顶部 |
| **0.1.12** | **13** | 见 `git log` | **补 HEIC 增益图的第二形态**：原来只查 `auxC`，现在同时解析 `iref`（dimg/auxl/thmb）与 `tmap` 派生项。实测那张华为 HEIC 是 `aux=0` + `compat=tmap` + 有 `iref`，旧逻辑误判为「无增益图」 | 摘要多两行：`aux=N（无 aux 型辅助图）`、`compat-tmap=… tmap项=… iref=…` 及关系列表 |
| **0.1.13** | **14** | 见 `git log` | 修 `iref` 的 ID 宽度取值（应由 iref 自身 FullBox version 决定，而非每个 reference box 首字节——后者是 from_id 高字节）；摘要补 `tmap id` 列表 | 摘要多一行 `tmap id: #NN(大小)`，可据此对上 `dimg` 关系定位主图/增益图 |
| **0.1.14** | **15** | 见 `git log` | **HEIC grid 重封**：`HeifGridRewriter` 解析 grid/dimg/iloc/ipma，把 4 块增益图重封成最小 HEIF 交给系统 `ImageDecoder`（不碰 HEVC、不加 .so）。选图时日志多一段 `[2c/5]` | 界面不变；HEIC 日志新增 `grid #NN 2x2 输出 … ← 增益图候选`、`重封图解码：…` |
| **0.1.17** | **18** | 见 `git log` | 新增 **`faithful`（保真）档**：maxLift 1.05、几乎不提亮，只保留高光细节 | 档位下拉多第 4 项「保真（faithful：不提亮，只留高光细节）」 |
| **0.1.18** | **19** | 见 `git log` | **修「烘焙强度不生效」**：strength 改为缩放「HDR 相对 SDR 的增量」（此前只乘增益，被色调映射压平，0.30 与 1.00 输出几乎相同）；`maxLift` 退回安全垫（1.25）；「导出原图」对 JPEG 也可用 | 预览状态行多一段 `亮度 x.xx`；导出日志多一行「输出亮度 … 上限 …」 |
| **0.1.19** | **20** | 见 `git log` | 三路审查修复：①「导出原图」在无增益图/解析失败分支也可用（字节一到就启用）；② 导出加**代次令牌**，换图后旧线程不再点亮按钮/回写旧图；③ 换图清 `parsed/pvBase/pvGain`；④ 导出部分失败仍点亮已落盘的出口；⑤ Kotlin 裁切率重复计数修掉；⑥ Kotlin LTM 输入与 Python 一样先 clip 到 [0,1] | 界面无变化；导出失败时「发送/保存」不再一律变灰 |
| **0.1.20** | **21** | 见 `git log` | 复审补漏：① 产物先落局部变量、gen 校验通过才提交字段（旧线程不会再把旧图写回）；② 导出开始即清空上一轮 `bakedFile/hdrFile`；③ 新增 `loadGen`，快速连续选图时作废前一次解析结果；④ 裁切率改为**按单通道**统计（亮度口径恒 0，是死指标） | 无界面变化；`裁切 x%` 数字开始有意义 |
| **0.1.21** | **22** | 见 `git log` | 「导出原图」改存**原件**字节（此前系统转码成功时会导出转码后的 JPEG）；按钮文案改为「导出（长边 2048，超出才缩）」「导出原图（选位置，任何格式都可用）」 | 界面文案变化 |
| **0.1.22** | **23** | 见 `git log` | **修「烘焙图比原图灰」**：源图 ICC 是 Display P3，此前丢掉 ICC 后系统按 sRGB 解释，实测平均饱和度 60.2 而正确值 69.0（-8.8 点）。现在两端都先转 sRGB 再烘焙（Python `to_srgb()` / Kotlin `inPreferredColorSpace=sRGB`） | 无界面变化；烘焙图色彩恢复正常 |

### 11.3 一眼判定手机上装的是哪版

从下往上判，命中哪个就是哪个：

| 看到这个 | 版本 |
|---|---|
| 摘要出现 `compat-tmap=` / `tmap项=` / `iref=` 三行 | **0.1.12** |
| 「查看全部日志（全屏）」按钮 | **0.1.11** |
| 「清理导出残留」按钮 | **0.1.10** |
| 「导出日志（选位置）」按钮 | **0.1.9** |
| 日志出现「hdrgm 参数取自增益图自身 XMP」或「⚠ 增益图与 probe 均未提供」 | **0.1.8** |
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

**功能已经跑通，剩下的全是「观感」和「对外实测」。**

**已闭环（2026-10-24，v0.1.16）**

- 真机全链路：真实华为样本 → 解析 → 采样解码 → 烘焙 → 几何同步 → 封装 → 自检 → 导出，
  产物 841 KB，经微信传回电脑后 Python 独立校验 **20/20 全 PASS**（`docs/02` §2.9）。
  `GainMapMax=2.148445` 与源一致，证明 v0.1.8 的「读增益图自身 XMP」修复在真机生效。
- 烘焙算法 v3（§10.2）：按 SDR 亮度分流 + `maxLift` 封顶，整体亮度 +2.1%、纯白 0%、验收 5/5。
- HEIC 增益图搞清楚了（§3.5）：`tmap` 派生项引用「主图 grid + 增益图 grid（4 块 hvc1）」，
  不是 aux。自研提取成本高，但系统转 JPEG 会写成 ISO Ultra HDR，**可绕行**。

**还没做（按优先级）**

1. **用户确认 0.1.22 观感** —— 最新反馈是「发灰、层次少」。其中**发灰已定位并修好**（v0.1.22：
   源图 ICC 是 Display P3，此前丢掉后系统按 sRGB 解释，平均饱和度 60.2 而正确值 69.0）。
   **「层次少」还没修**：烘焙链路抬了暗部（p5 25.7→26.7）、压平高光（软膝收敛 0.96），
   下一步要先把「局部对比/高光层次」做成可计算指标，再动参数（见 `docs/07-testing-strategy.md`）。
   **别再直接调 `maxLift`/`strength`** —— 那两个已有回归守着，乱调只会回到"过曝/太亮"。
2. **微信三路径实测**（§3.3）：不勾原图时会不会剥掉增益图。
3. **接收端能否显示 HDR**：需要第二台支持 Ultra HDR 的设备。

**环境铁律（花了半晚查清，别再试）**

容器写出的文件，宿主机 `hdc` **一律读不到**：MediaStore 落在 `/data/virt_service/rgm_hmos`（无权限），
SAF 的两个目标位置（「下载内容」「CLS-AL30」）也都是容器内部存储，`hdc smode` 在零售版被禁用。
取日志靠**全屏日志 + 截图**（v0.1.11 起），取产物靠**App 内分享到微信**（`.dat` 用 XOR `0x7B` 还原）。

---

## 14. 新会话启动（直接复制这段给 AI）

> 你在 `c:/Users/ugnam/Workspace/code/hdr-vivid-ultrahdr`（私有仓库 `git@github.com:freakkid/hdr-vivid-ultrahdr.git`，分支 `main`）。
> 先读 `HANDOVER.md`（§0 接手 / §10.2 烘焙算法 / §12 总结 / §13 发版），再读 `docs/07-testing-strategy.md`（测试策略）。
>
> 硬性约定：
> 1. **改任何代码先跑 `python scripts/verify_bake.py`**（7 步全绿才算过）；打包用 `--apk`，
>    自检没过它会自动跳过构建。`scripts/ship.sh` 提交前也会跑同一套，失败即 `exit 5`。
> 2. **不要为了跑通而放宽阈值**；要改阈值必须在 `docs/07-testing-strategy.md` 里说明理由。
> 3. Kotlin 侧可以在本机真跑：`tests/android_parity/` 用 kotlinc + 两个桩执行真实的
>    `HdrBaker.kt` / `LocalToneMapping.kt` / `HeifGridRewriter.kt`。缺 kotlinc 时脚本会 SKIP（不算通过）。
> 4. 本机：`bash` 是 WSL 存根（不可用），用 `C:/Program Files/Git/bin/bash.exe`；
>    JDK 17 在 `C:/Users/ugnam/.workbuddy/binaries/jdk/17`，kotlinc 在 `~/.workbuddy/binaries/kotlinc/kotlinc/bin/kotlinc.bat`。
> 5. 真机只有一台 HarmonyOS NEXT（CLS-AL30），`hdc` 在 `C:/DevEco/.../toolchains/hdc.exe`；
>    **装机这一步只能人工**，不要把时间浪费在自动化安装上。
>
> 当前待办（按优先级）：
> - P0：装 0.1.22 确认「发灰」已修；
> - P0：把「层次少」量化（局部对比/高光分位距），补进 `tests/acceptance/accept.py` 并加下限；
> - P1：`docs/07-testing-strategy.md` §2 的七个假绿点；
> - P2：JVM 化 `HeifInspector`/`MetadataValidator`/`UltraHdrPackager`/`HdrSourceParser`。

**2026-10-24（v0.1.15）烘焙算法 v2 —— 用验收数据逼出来的修正**：

用户反馈「整体太亮」。`tests/acceptance/` 量化确认：中位亮度 117 → 141（+24）。
根因不是参数：**增益图全图都有值**（中位 86/255），只要 `strength > 0`，中间调就必然被提亮——
`strength→0` 只会连 HDR 一起丢掉，数学上救不回来。必须**按 SDR 亮度分流**。

| 修改 | 理由 |
|---|---|
| 以 SDR 主图亮度为基准，`smoothstep(0.35, 0.90)` 加权 | 手机显示 HDR 时参考白仍对应 SDR 1.0 → 暗部/中间调观感必须与 SDR 一致，只有亮部才采信 HDR |
| 每像素封顶 `min(target, sdr × 1.12)` | 分桶实测 192~223 档被推 +8.5%，向 255 靠后掉色彩与层次（观感=发白）；封顶后降到 +4.6% |
| 软膝收敛到 **0.96** 而非 1.0 | 收敛到 1.0 时 0.9~1.0 挤在 0.99+，大片纯白、高光丢层次（实测比源多 3.6 个百分点纯白） |
| 去掉线性对比度 1.06 | 它把接近白的高光直接推成 255 |
| LTM 的 log2 上限改为 `log2(0.96)` | 同上，LTM 内部原本收敛到线性 1.0 |

结果：验收 **5/5 PASS**（中位差 +0.0、纯白 0.00%、暗部 1.00×、SSIM 0.945~0.992）。
Python 与 Kotlin 同步改，回归 48 项全过。

**一条环境铁律**（花了半晚查清，别再试）：容器写出的文件，宿主机 `hdc` **一律读不到**——
MediaStore 落在 `/data/virt_service/rgm_hmos`（无权限），SAF 的两个目标位置也都是容器内部存储，
`hdc smode` 在零售版被禁用。取日志/取产物只能靠**屏幕截图**或**App 内分享到宿主机微信**。

---

## 13. 发版与分发（2026-10-24 新增）

### 13.1 为什么突然要管签名

此前一直发 `app-debug.apk`。debug keystore 是**每台机器各自随机生成**的（`~/.android/debug.keystore`），
本项目有两台开发机（§1.3-A/B），换机器构建签名就变。后果：

- 用户无法覆盖安装（`INSTALL_FAILED_UPDATE_INCOMPATIBLE`）
- Obtainium / F-Droid 的自动更新**全部失效**
- 签名一旦对外发布就**永久锁死**，想改只能换包名，等于换一个 App

所以第一次公开发布前必须切到固定 keystore 的 release 包。

### 13.2 一次性准备（做完立刻备份）

```bash
KEYDIR="$HOME/android-keys"        # 仓库外；两台机器各一份，私有云/U 盘同步
mkdir -p "$KEYDIR"
keytool -genkeypair -v \
  -keystore "$KEYDIR/hdr-release.jks" \
  -alias hdr -keyalg RSA -keysize 2048 -validity 10000 \
  -storepass '口令' -keypass '口令' \
  -dname "CN=HDR Tools, OU=Personal, O=Personal, L=CN, ST=CN, C=CN"
```

写 `src/android/keystore.properties`（已 gitignore，`*.jks` 也已 gitignore）：

```properties
storeFile=/绝对路径/hdr-release.jks
storePassword=口令
keyAlias=hdr
keyPassword=口令
```

> ⚠ **keystore 丢了 = 永远无法更新已发布的应用**。备份三份，至少一份离线。
> ⚠ 从 debug 包切到 release 包，**现有设备必须先卸载再装**（签名不同，无法覆盖）。

`build.gradle.kts` 是**条件式**读取：`keystore.properties` 不存在时不创建 release 签名配置，
`assembleDebug` 与之前完全一致，不会因为别人机器上没这个文件而构建失败。

### 13.3 日常发版

```bash
python scripts/release.py --bump      # patch+1 → 构建 → 生成说明 → 建 Release
python scripts/release.py --dry-run   # 只自检 + 预览发布说明（不构建不发布）
```

脚本做的事（顺序即检查顺序，**所有检查都在构建前**，构建要几分钟）：

| 步骤 | 说明 |
|---|---|
| 前置检查 | 已跟踪文件无未提交改动、`keystore.properties` 存在、HEAD 已 push、tag 未被占用、`gh` 已登录 |
| `--bump` | versionName patch +1、versionCode +1，并 `git commit`（不 push，交给 `scripts/ship.sh`） |
| 构建 | `gradlew assembleRelease`（Windows 走 `gradlew.bat`） |
| 校验 | APK 存在、大小 ≥ 0.5 MB、`keytool -printcert` 打印签名指纹（可确认是不是那把固定的 key） |
| 说明 | 无历史 tag → 一页项目简介（首个公开版本）；有 tag → 按 Conventional Commits 分组的 changelog，自动附 Obtainium 安装指引 + versionCode + SHA-256 |
| 发布 | `gh release create --latest`，APK 重命名为 `hdr-vivid-ultrahdr-<版本>.apk` |

`gh` 没装：`winget install --id GitHub.cli`（Windows）/ `brew install gh`（macOS），然后 `gh auth login`。

**别忘了**：发版后把新版本补进 §11.2 对照表，否则后人又要靠截屏猜版本。

### 13.4 分发与推广（全程零成本）

不上 App Store、不买开发者账号的前提下，Android 侧的分发成本是 0：

| 渠道 | 成本 | 说明 |
|---|---|---|
| **GitHub Release** | 0 | 主入口，`scripts/release.py` 直接产出 |
| **Obtainium** | 0 | 用户填仓库地址即可自动更新，弥补 APK 分发的最大短板 |
| **酷安** | 0 | 主战场，发帖直接挂 Release 链接 |
| **F-Droid** | 0 | 要求开源，本项目满足 |
| 华为应用市场 | 0 | 但**需要软著**（个人可申请，官费 0，周期 1–3 月） |
| Google Play | $25 一次性 | 新个人号需 20 人封闭测试 14 天，暂缓 |
| App Store | $99/年 + 中国区 ICP 备案 | **暂不做**：HDR Vivid 是华为标准，iPhone 用户手里没有源文件，iOS 端需求近乎为零 |

推广原则：**推「问题的解法」而不是「我做了个 App」**。标题写成「HDR Vivid 照片发出去就变灰怎么办」，
比「我的 App 上线了」有效十倍；截图必须是「发灰 / 正常」的对比。


