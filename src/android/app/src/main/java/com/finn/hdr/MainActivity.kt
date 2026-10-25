package com.finn.hdr

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import java.io.ByteArrayOutputStream
import java.io.File

/**
 * 探针界面：选图 → 实时预览烘焙效果 → 需要时导出全尺寸双产物。
 *
 *   1. 烘焙图 `*_BAKED.jpg`  —— 普通 JPEG，HDR 效果已压进去，微信/任何设备直接可见
 *   2. Ultra HDR `*_HDR.jpg` —— 保留增益图，仅支持 Ultra HDR 的设备能还原
 *
 * 设计取向：容器环境（如鸿蒙卓易通）无法连着电脑看 logcat，所以
 * **所有失败都必须能在屏幕上读到**——分阶段标记 + 异常链 + 堆栈前几行 + 一键复制。
 */
class MainActivity : ComponentActivity() {

    // ---- 视图 ----
    private lateinit var logView: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var infoView: TextView
    private lateinit var previewBefore: ImageView
    private lateinit var previewAfter: ImageView
    private lateinit var shareBaked: Button
    private lateinit var saveBaked: Button
    private lateinit var exportRawButton: Button
    private lateinit var exportLogButton: Button
    private lateinit var shareHdr: Button
    private lateinit var exportButton: Button
    private lateinit var profileSpinner: Spinner
    private lateinit var strengthLabel: TextView
    private lateinit var strengthSeek: SeekBar

    // ---- 状态 ----
    private var parsed: HdrSourceParser.HdrSource? = null
    private var pvBase: Bitmap? = null
    private var pvGain: Bitmap? = null
    private var bakedFile: File? = null
    private var hdrFile: File? = null

    /** 选中的原始文件字节。HEIC 走不通时靠它把原件导出去，交给桌面端处理。 */
    private var lastRawBytes: ByteArray? = null

    /** 日志文件：容器里没有 logcat，这是唯一可回溯的记录。 */
    private val logFile: File by lazy { File(filesDir, "hdr-log.txt") }

    /**
     * 预览代次：快速拖动滑杆时丢弃过期结果，避免旧图覆盖新图。
     * 必须是原子自增——`@Volatile Int` 的 `++` 是读改写，并发下两个线程会拿到同一个代次。
     */
    private val previewGen = java.util.concurrent.atomic.AtomicInteger(0)

    /**
     * 导出代次：换图时自增，作废在跑的导出线程。
     * 少了它就会出现「上一张的导出结果把按钮点亮、bakedFile 却指向旧图」。
     */
    private val exportGen = java.util.concurrent.atomic.AtomicInteger(0)

    /**
     * 加载代次：快速连续选图时作废前一次的解析/解码结果。
     * 没有它，先发起的慢加载会在后一次清理之后回写 `parsed`/`pvBase`，造成
     * 「预览是 B、导出却是 A」这类错配。
     */
    private val loadGen = java.util.concurrent.atomic.AtomicInteger(0)

    private val previewEdge = 720     // 预览长边，够看清效果又不吃内存
    private val exportEdge = 2048     // 导出长边（与桌面 Python 默认一致）

    /** 与 Python bake.py 的 profile 语义一致；顺序即 UI 顺序。 */
    private val profiles = listOf(
        "local" to "本地观看（local：局部色调映射）",
        "wechat" to "微信分享（wechat：克制映射+重压缩余量）",
        "off" to "仅全局映射（off：旧行为，A/B 对照）",
        // 嫌「整体偏亮」时用这档：maxLift 1.05，几乎不提亮，只保留高光细节
        "faithful" to "保真（faithful：不提亮，只留高光细节）",
    )

    private val strengthMin = 0.30f
    private val strengthMax = 1.00f

    private fun strengthFromProgress(p: Int): Float =
        strengthMin + (strengthMax - strengthMin) * (p / 100f)

    private fun curProfile() = profiles[profileSpinner.selectedItemPosition].first
    private fun curStrength() = strengthFromProgress(strengthSeek.progress)

    // =====================================================================
    // 选图：三条通道依次退化
    // =====================================================================

    private val pickImage =
        registerForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
            if (uri != null) loadSource(uri) else say("已取消选择")
        }

    private val pickDocument =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) loadSource(uri) else say("已取消选择")
        }

    private val pickContent =
        registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
            if (uri != null) loadSource(uri) else say("已取消选择")
        }

    /**
     * 用 SAF（ACTION_CREATE_DOCUMENT）导出，保存位置由用户/系统 DocumentsUI 指定。
     *
     * 为什么弃用 MediaStore.Downloads：安卓兼容层（卓易通 / anco）里 MediaStore 的后端是
     * **容器私有**的 media 存储，宿主机 `hdc` 完全读不到（`/data/virt_service/rgm_hmos`
     * 连 shell 用户都没有读权限，零售版也无法 `hdc smode` 提权）。
     * SAF 的目标 URI 由系统文件选择器提供，能落到宿主机可见的目录。
     */
    private val saveLogDoc =
        registerForActivityResult(ActivityResultContracts.CreateDocument("text/plain")) { uri ->
            if (uri == null) {
                say("已取消保存")
                return@registerForActivityResult
            }
            val txt = logView.text.toString()
            Thread {
                try {
                    contentResolver.openOutputStream(uri)?.use {
                        it.write(txt.toByteArray(Charsets.UTF_8))
                        it.flush()
                    } ?: error("openOutputStream 返回 null")
                    say("日志已保存（${txt.length} 字）")
                } catch (t: Throwable) {
                    say("保存日志失败：${t.javaClass.simpleName}")
                    appendLog("保存日志失败\n${describe(t)}")
                }
            }.start()
        }

    private val saveRawDoc =
        registerForActivityResult(
            ActivityResultContracts.CreateDocument("application/octet-stream")
        ) { uri ->
            if (uri == null) {
                say("已取消保存")
                return@registerForActivityResult
            }
            val bytes = lastRawBytes
            if (bytes == null || bytes.isEmpty()) {
                say("还没有读到原始文件，请先选图")
                return@registerForActivityResult
            }
            Thread {
                try {
                    contentResolver.openOutputStream(uri)?.use {
                        it.write(bytes)
                        it.flush()
                    } ?: error("openOutputStream 返回 null")
                    say("原图已保存（${bytes.size} B）")
                } catch (t: Throwable) {
                    say("保存原图失败：${t.javaClass.simpleName}")
                    appendLog("保存原图失败\n${describe(t)}")
                }
            }.start()
        }

    /** step: 0=系统相册 1=文件选择器 2=GetContent */
    private fun openPicker(step: Int = 0) {
        val label = when (step) {
            0 -> "系统相册"; 1 -> "文件选择器"; else -> "内容选择器"
        }
        try {
            when (step) {
                0 -> pickImage.launch(
                    PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                )
                1 -> pickDocument.launch(arrayOf("image/*"))
                else -> pickContent.launch("image/*")
            }
        } catch (t: Throwable) {
            infoView.text = "$label 不可用：${t.javaClass.simpleName} ${t.message ?: ""}"
            appendLog("[选图] $label 启动失败\n${describe(t)}")
            if (step < 2) openPicker(step + 1)
        }
    }

    // =====================================================================
    // 界面
    // =====================================================================

    private fun dp(v: Int): Int = Math.round(v * resources.displayMetrics.density)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(24), dp(12), dp(12))
        }

        val pickButton = Button(this).apply {
            text = "选择 HDR 照片"
            setOnClickListener { openPicker(0) }
        }
        val pickFileButton = Button(this).apply {
            text = "用文件管理器选择（相册不可用点这个）"
            setOnClickListener { openPicker(1) }
        }

        previewBefore = ImageView(this).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            setBackgroundColor(Color.parseColor("#22000000"))
            contentDescription = "原图"
        }
        previewAfter = ImageView(this).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            setBackgroundColor(Color.parseColor("#22000000"))
            contentDescription = "烘焙后"
        }
        val previewRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(previewBefore, LinearLayout.LayoutParams(0, dp(150), 1f))
            addView(previewAfter, LinearLayout.LayoutParams(0, dp(150), 1f))
        }

        infoView = TextView(this).apply {
            textSize = 12f
            setTextIsSelectable(true)
            text = "尚未选择照片。左侧=原图，右侧=烘焙后。"
        }

        profileSpinner = Spinner(this).apply {
            adapter = ArrayAdapter(
                this@MainActivity,
                android.R.layout.simple_spinner_dropdown_item,
                profiles.map { it.second },
            )
            onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
                override fun onItemSelected(
                    parent: android.widget.AdapterView<*>?,
                    view: android.view.View?,
                    position: Int,
                    id: Long,
                ) {
                    schedulePreview("切换档位")
                }

                override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
            }
        }

        strengthLabel = TextView(this).apply {
            textSize = 12f
            text = "烘焙强度：0.70（1.0 = 完全按元数据恢复 HDR）"
        }
        strengthSeek = SeekBar(this).apply {
            max = 100
            progress = Math.round((0.70f - strengthMin) / (strengthMax - strengthMin) * 100)
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar, progress: Int, fromUser: Boolean) {
                    strengthLabel.text =
                        "烘焙强度：${"%.2f".format(strengthFromProgress(progress))}" +
                            "（1.0 = 完全按元数据恢复 HDR）"
                    if (fromUser) schedulePreview("调强度")
                }
                override fun onStartTrackingTouch(sb: SeekBar) {}
                override fun onStopTrackingTouch(sb: SeekBar) {}
            })
        }

        exportButton = Button(this).apply {
            text = "导出（长边 $exportEdge，超出才缩）"
            isEnabled = false
            setOnClickListener { exportFull() }
        }

        shareBaked = Button(this).apply {
            text = "发送烘焙图（微信可见 HDR 效果）"
            isEnabled = false
            setOnClickListener { share(bakedFile) }
        }

        shareHdr = Button(this).apply {
            text = "发送 Ultra HDR（需设备支持）"
            isEnabled = false
            setOnClickListener { share(hdrFile) }
        }

        // 只保存烘焙图：HDR 效果已压进像素，任何设备/平台都能直接看到
        saveBaked = Button(this).apply {
            text = "保存烘焙图到本地"
            isEnabled = false
            setOnClickListener { saveBakedToGallery() }
        }

        // HEIC 解不了时的唯一出路：把原件另存到用户指定的位置（容器内 MediaStore 宿主机读不到）
        exportRawButton = Button(this).apply {
            text = "导出原图（选位置，任何格式都可用）"
            isEnabled = false
            setOnClickListener {
                val bytes = lastRawBytes
                if (bytes == null || bytes.isEmpty()) {
                    infoView.text = "还没有读到原始文件，请先选图"
                    return@setOnClickListener
                }
                val fmt = sniffFormat(bytes).first
                val ext = when {
                    fmt.startsWith("ISOBMFF") -> "heic"
                    fmt == "JPEG" -> "jpg"
                    fmt == "PNG" -> "png"
                    else -> "bin"
                }
                saveRawDoc.launch("HDR_RAW_${System.currentTimeMillis()}.$ext")
            }
        }

        // 剪贴板只适合短内容：粘贴端（微信/输入法）对超长文本会静默截断，
        // 用户会以为「复制不全」。所以另给一个写文件的出口。
        val copyLogButton = Button(this).apply {
            text = "复制日志（短）"
            setOnClickListener { copyLogToClipboard() }
        }

        exportLogButton = Button(this).apply {
            text = "导出日志（选位置）"
            setOnClickListener { saveLogDoc.launch("hdr-log-${System.currentTimeMillis()}.txt") }
        }

        // 0.1.8 及之前用 MediaStore 写到容器私有媒体库，宿主机看不到、文件管理器也难找，
        // 只能由写入方自己删。这是一次性清理入口。
        val purgeButton = Button(this).apply {
            text = "清理导出残留"
            setOnClickListener { purgeExports() }
        }

        // 反容器取文件的唯一通道：**屏幕**。宿主机 `hdc snapshot_display` 截得到这里显示的
        // 任何文字，所以把日志全屏展开后，排查者截图即可，不必再折腾 MediaStore/SAF。
        val viewLogButton = Button(this).apply {
            text = "查看全部日志（全屏）"
            setOnClickListener { showLogDialog() }
        }

        logView = TextView(this).apply {
            textSize = 11f
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
        }

        val scroll = ScrollView(this).apply { addView(logView) }
        logScroll = scroll

        root.addView(pickButton)
        root.addView(pickFileButton)
        root.addView(previewRow)
        root.addView(infoView)
        root.addView(profileSpinner)
        root.addView(strengthLabel)
        root.addView(strengthSeek)
        root.addView(exportButton)
        root.addView(shareBaked)
        root.addView(shareHdr)
        root.addView(saveBaked)
        root.addView(exportRawButton)
        // 两个日志出口并排，省一行高度 —— 按钮已经有 10 个，日志区被挤得只剩两行
        val logButtonRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(copyLogButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            addView(exportLogButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        }
        root.addView(logButtonRow)
        val actionRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(purgeButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            addView(viewLogButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        }
        root.addView(actionRow)
        root.addView(
            scroll,
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f)
        )
        setContentView(root)
    }

    // =====================================================================
    // 选图后：解析一次，之后只重算预览
    // =====================================================================

    private fun loadSource(uri: Uri) {
        infoView.text = "读取中…"
        appendLog("\n===== 载入 ${uri.scheme}://${uri.authority} =====")
        bakedFile = null
        hdrFile = null
        lastRawBytes = null
        parsed = null                  // 必须清：否则新图还没解析完时点导出会把上一张再导一遍
        pvBase = null
        pvGain = null
        val lgen = loadGen.incrementAndGet()   // 作废上一次还在跑的加载线程
        previewGen.incrementAndGet()   // 作废上一张图残留的预览任务
        exportGen.incrementAndGet()    // 作废在跑的导出线程（否则旧结果会点亮按钮、内容却是上一张）
        previewHandler.removeCallbacks(previewTask)
        runOnUiThread {
            exportButton.isEnabled = false
            shareBaked.isEnabled = false
            shareHdr.isEnabled = false
            saveBaked.isEnabled = false
            exportRawButton.isEnabled = false
            previewBefore.setImageDrawable(null)
            previewAfter.setImageDrawable(null)
        }

        Thread {
            try {
                appendLog("[1/5] 读取字节")
                val read = readImageBytes(uri)
                val rawBytes = read.effective
                appendLog(read.note)
                appendLog("长度：${rawBytes.size} B")

                // 字节一读到就让「导出原图」可用，早于任何解析：解析失败/无增益图时
                // 这条出口最有用，不能等到后面某个分支才点亮（此前只有 HEIC 与有增益图
                // 的 JPEG 会点亮，其余情况按钮永远是灰的）。
                // 存的是**原件**，不是系统转码后的 JPEG —— 按钮写的是「导出原图」。
                lastRawBytes = read.raw
                runOnUiThread { exportRawButton.isEnabled = true }
                if (lgen != loadGen.get()) return@Thread   // 期间又选了别的图，本次结果作废

                appendLog("[2/5] 解析容器")
                val fmtNow = sniffFormat(rawBytes).first
                if (fmtNow.startsWith("ISOBMFF")) {
                    // 两步走：① 把结构读出来（决定有没有增益图）② 用系统解码器解主图（让用户先看到画面）
                    appendLog("[2a/5] HEIC 结构诊断")
                    // 先打 4~8 行摘要：屏幕上一眼能判断，且短到能粘出去（长日志在粘贴端会被截断）
                    appendLog(HeifInspector.brief(rawBytes))
                    appendLog("—— 以下为完整结构（可跳过，排障才需要）——")
                    appendLog(HeifInspector.describe(rawBytes))

                    // 增益图在这里的形态是 grid（实测华为：4 块拼 2x2），aux/tmap 都看不出来。
                    // 重封成最小 HEIF 交给系统解码器，是目前唯一不碰 HEVC 码流的取法。
                    appendLog("[2c/5] grid 增益图探测（重封试解，不改主流程）")
                    appendLog(HeifGridRewriter.summary(rawBytes))
                    HeifGridRewriter.rewrapGainMapCandidate(rawBytes)?.let { (mini, g) ->
                        appendLog("重封 grid #${g.id}（${g.rows}x${g.cols}，${mini.size} B）")
                        val gm = HeicDecoder.decodeMain(mini, 0)
                        appendLog(
                            "重封图解码：${gm.engine} ${gm.width}x${gm.height} " +
                                if (gm.ok) "成功" else "失败（${gm.note.take(90)}）"
                        )
                    } ?: appendLog("没有可重封的 grid（可能不是 grid 结构）")

                    appendLog("[2b/5] 尝试解码 HEIC 主图（增益图不会被提取）")
                    val dec = HeicDecoder.decodeMain(rawBytes, previewEdge)
                    appendLog("解码通道：${dec.engine}  ${dec.width}x${dec.height}")
                    appendLog(dec.note)
                    runOnUiThread { exportRawButton.isEnabled = true }

                    if (dec.ok) {
                        pvBase = dec.bitmap
                        pvGain = null
                        runOnUiThread {
                            previewBefore.setImageBitmap(dec.bitmap)
                            previewAfter.setImageDrawable(null)
                        }
                        say("HEIC 主图已显示，但增益图未提取 —— 无法烘焙 HDR 效果。")
                        appendLog("→ 结论：主图可显示，但增益图未提取。要烘 HDR 需先确认形态——看上方摘要的 aux / iref / tmap 三项。")
                        appendLog("→ 可点「导出原图」把 .heic 存到相册，用微信发出来在桌面端处理。")
                    } else {
                        say("HEIC 无法解码：容器里缺 HEIF/HEVC 解码器。")
                        appendLog("→ 结构已列出；请点「复制全部日志」发我，同时可用「导出原图」把文件传出来。")
                        appendLog("→ 备选：相册里「另存为 JPG」再选（注意：增益图会在另存时丢失）。")
                    }
                    return@Thread
                }
                val src = HdrSourceParser.parse(rawBytes)
                parsed = src
                appendLog("容器：${src.container}")
                appendLog("增益图：${if (src.hasGainMap) "有（${src.gainMapJpeg?.size} B）" else "无"}")
                appendLog("Make：${src.exifMake ?: "unknown"}")
                src.notes.forEach { appendLog("· $it") }

                if (!src.hasGainMap) {
                    say("这张图没有增益图 —— 它是普通 SDR，不需要转换。")
                    appendLog("→ 无增益图，停止。请选一张 HDR 照片（相机需开启 HDR Vivid / HDR 照片）。")
                    runOnUiThread {
                        try {
                            val b = BitmapFactory.decodeByteArray(rawBytes, 0, rawBytes.size)
                            if (b != null) {
                                previewBefore.setImageBitmap(b)
                                infoView.text = "该图无增益图（已是普通 SDR）。左图为原图。"
                            }
                        } catch (t: Throwable) {
                            appendLog("预览解码失败：${describe(t)}")
                        }
                    }
                    return@Thread
                }

                appendLog("[3/5] 解码预览图（长边 $previewEdge）")
                val base = decodeScaled(src.sdrJpeg, previewEdge)
                val gain = decodeScaled(src.gainMapJpeg!!, previewEdge)
                pvBase = base
                pvGain = gain
                appendLog("主图 ${base.width}x${base.height}  增益图 ${gain.width}x${gain.height}")
                runOnUiThread {
                    previewBefore.setImageBitmap(base)
                    exportButton.isEnabled = true
                    exportRawButton.isEnabled = true
                }

                appendLog("[4/5] 生成预览")
                renderPreviewNow()
                appendLog("[5/5] 就绪：拖滑杆/换档位会即时重算预览；满意后点「导出全尺寸」。")
                appendLog("（发送/保存/写入相册这几个按钮要等「导出全尺寸」完成后才亮，未导出时灰色是正常的）")
            } catch (t: Throwable) {
                parsed = null
                say("载入失败：${t.javaClass.simpleName}")
                appendLog(describe(t))
            }
        }.start()
    }

    // =====================================================================
    // 预览
    // =====================================================================

    private val previewHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val previewTask = Runnable { Thread { renderPreviewNow() }.start() }

    /**
     * 防抖后再重算预览。
     * 滑杆从 0.30 拖到 1.00 会触发约 70 次回调，逐次起线程会让 70 个 bake 并发抢 CPU，
     * 每个都变慢、内存叠加，还拖着主线程处理 70 条日志。
     */
    private fun schedulePreview(reason: String) {
        if (pvBase == null || pvGain == null) return
        previewHandler.removeCallbacks(previewTask)
        previewHandler.postDelayed(previewTask, 160)
    }

    /** 必须在后台线程调用。 */
    private fun renderPreviewNow() {
        val base = pvBase ?: return
        val gain = pvGain ?: return
        val src = parsed ?: return
        val gen = previewGen.incrementAndGet()
        val profile = curProfile()
        val strength = curStrength()
        try {
            val (baked, stats) = HdrBaker.bake(
                base, gain, src.meta,
                HdrBaker.Options(profile = profile, strength = strength),
            )
            if (gen != previewGen.get()) {    // 已有更新的请求，丢弃本次
                baked.recycle()
                return
            }
            runOnUiThread {
                if (gen != previewGen.get()) {   // 入队期间又被更新的请求超过
                    baked.recycle()
                    return@runOnUiThread
                }
                previewAfter.setImageBitmap(baked)
                infoView.text = "预览 ${stats.outSize.first}x${stats.outSize.second}  " +
                    "峰值 ${"%.2f".format(stats.hdrPeak)}x  " +
                    "亮度 ${"%.2f".format(stats.meanLift)}x  " +
                    "裁切 ${"%.2f".format(stats.clippedRatio * 100)}%  " +
                    "[$profile ${"%.2f".format(strength)}]"
            }
            // 预览不写日志：拖动滑杆会连续触发，写日志会把真正的错误挤出视野。
        } catch (t: Throwable) {
            say("预览失败：${t.javaClass.simpleName}")
            appendLog("预览失败\n${describe(t)}")
        }
    }

    // =====================================================================
    // 导出全尺寸
    // =====================================================================

    private fun exportFull() {
        val src = parsed ?: return
        if (!src.hasGainMap) return
        // 必须在主线程快照：导出线程里读 Spinner/SeekBar 会与用户操作并发，
        // 且一次导出内读三次，打印的参数可能和实际执行的不一致
        val profile = curProfile()
        val strength = curStrength()
        val gen = exportGen.get()      // 换图会自增，旧线程据此放弃回写 UI
        bakedFile = null               // 清掉上一轮产物：否则本次失败时按钮会亮在旧文件上
        hdrFile = null
        previewGen.incrementAndGet()   // 导出期间不再接受预览结果
        exportButton.isEnabled = false
        infoView.text = "导出中…"

        Thread {
            // 产物先落在局部变量里，只有 gen 校验通过才提交到字段 ——
            // 否则旧线程会把上一张的文件「写回」新图的界面状态
            var localBaked: File? = null
            var localHdr: File? = null
            try {
                appendLog("\n===== 导出全尺寸（长边 $exportEdge）=====")
                val dir = File(cacheDir, "exports").apply { mkdirs() }
                val baseName = "IMG_${System.currentTimeMillis()}"

                appendLog("[1/4] 解码全尺寸")
                val base = decodeScaled(src.sdrJpeg, exportEdge)
                val gain = decodeScaled(src.gainMapJpeg!!, exportEdge)
                appendLog("主图 ${base.width}x${base.height}  增益图 ${gain.width}x${gain.height}")

                appendLog("[2/4] 烘焙")
                val (baked, stats) = HdrBaker.bake(
                    base, gain, src.meta,
                    HdrBaker.Options(profile = profile, strength = strength),
                )
                appendLog("尺寸 ${stats.outSize.first}x${stats.outSize.second}  " +
                    "峰值 ${"%.2f".format(stats.hdrPeak)}x  白点 ${"%.2f".format(stats.whitePoint)}  " +
                    "最大 boost ${"%.2f".format(stats.maxBoost)}x  裁切 ${"%.2f".format(stats.clippedRatio * 100)}%")
                if (stats.targetMode == "display") {
                    appendLog("输出亮度 ${"%.3f".format(stats.meanLift)}x（源 SDR = 1.000）" +
                        "目标模式 display：log 域几何插值，不逐像素封顶 —— " +
                        "这个数随 strength 变化才说明参数生效")
                } else {
                    appendLog("输出亮度 ${"%.3f".format(stats.meanLift)}x（源 SDR = 1.000，上限 " +
                        "${"%.2f".format(stats.liftCap)}x）目标模式 sdr（锚回底图）—— " +
                        "这个数随 strength 变化才说明参数生效")
                }
                if (profile != "off") {
                    appendLog("LTM 半径=${stats.ltmRadiusPx}px  detail=${"%.2f".format(stats.ltmDetailGain)}  " +
                        "暗部提升=${"%.2f".format(stats.ltmShadowLift)}x")
                }

                val quality = if (profile == "wechat") 88 else 95
                val bos = ByteArrayOutputStream()
                baked.compress(Bitmap.CompressFormat.JPEG, quality, bos)
                val bakedJpeg = bos.toByteArray()
                val bf = File(dir, "${baseName}_BAKED.jpg")
                bf.writeBytes(bakedJpeg)
                localBaked = bf
                appendLog("已写出 ${bf.name}（${bakedJpeg.size / 1024} KB, q=$quality）")
                base.recycle(); gain.recycle(); baked.recycle()

                appendLog("[3/4] 封装 Ultra HDR")
                val result = UltraHdrExporter().export(src, UltraHdrExporter.Options(longEdge = exportEdge))
                result.steps.forEach { appendLog(it) }
                result.warnings.forEach { appendLog("⚠ $it") }
                result.validation?.checks?.forEach {
                    appendLog("${if (it.ok) "✓" else "✗"} ${it.id}  ${it.detail}")
                }
                if (result.ok && result.jpeg != null) {
                    val hf = File(dir, "${baseName}_HDR.jpg")
                    hf.writeBytes(result.jpeg)
                    localHdr = hf
                    appendLog("已写出 ${hf.name}（${result.jpeg.size / 1024} KB）")
                } else {
                    appendLog("自检未通过 → 不标记为 HDR")
                }

                appendLog("[4/4] 完成")
                pruneExports(dir)
                runOnUiThread {
                    if (gen != exportGen.get()) return@runOnUiThread   // 已换图，丢弃旧结果
                    bakedFile = localBaked
                    hdrFile = localHdr
                    exportButton.isEnabled = true
                    shareBaked.isEnabled = localBaked?.exists() == true
                    shareHdr.isEnabled = localHdr?.exists() == true
                    saveBaked.isEnabled = localBaked?.exists() == true
                    infoView.text = "导出完成：${bf.name}（${bakedJpeg.size / 1024} KB）。可保存到相册或用微信发送。"
                }
            } catch (t: Throwable) {
                say("导出失败：${t.javaClass.simpleName}")
                appendLog(describe(t))
                runOnUiThread {
                    if (gen != exportGen.get()) return@runOnUiThread
                    bakedFile = localBaked
                    hdrFile = localHdr
                    exportButton.isEnabled = true
                    // 烘焙图可能已经落盘（后续阶段才失败），不能因为异常就把出口全关掉
                    shareBaked.isEnabled = localBaked?.exists() == true
                    saveBaked.isEnabled = localBaked?.exists() == true
                    shareHdr.isEnabled = localHdr?.exists() == true
                }
            }
        }.start()
    }

    // =====================================================================
    // 工具
    // =====================================================================

    /** 异常链 + 堆栈前几行，屏幕上看到的就是能直接定位的信息。 */
    private fun describe(t: Throwable): String {
        val sb = StringBuilder()
        sb.append("失败：${t.javaClass.name}\n  message=${t.message}\n")
        var cause = t.cause
        var depth = 0
        while (cause != null && depth < 3) {
            sb.append("  ← ${cause.javaClass.name}: ${cause.message}\n")
            cause = cause.cause
            depth++
        }
        t.stackTrace.take(6).forEach { sb.append("  at $it\n") }
        if (t is OutOfMemoryError) sb.append("  提示：内存不足，可先清理后台再试\n")
        return sb.toString()
    }

    /** 依据魔数判断真实格式——扩展名不可信（实测有 .heic 其实是 JPEG 的情况）。 */
    private fun sniffFormat(b: ByteArray): Pair<String, String> {
        if (b.isEmpty()) return "空文件" to ""
        val head = b.copyOfRange(0, minOf(12, b.size))
        val hex = head.joinToString(" ") { "%02X".format(it) }
        val ascii = { at: Int, len: Int ->
            if (b.size >= at + len) String(b, at, len, Charsets.US_ASCII) else ""
        }
        val name = when {
            b.size >= 3 && b[0] == 0xFF.toByte() && b[1] == 0xD8.toByte() && b[2] == 0xFF.toByte() -> "JPEG"
            ascii(4, 4) == "ftyp" -> "ISOBMFF(${ascii(8, 4)})"
            b.size >= 4 && b[0] == 0x89.toByte() && b[1] == 0x50.toByte() -> "PNG"
            ascii(0, 4) == "RIFF" -> "RIFF/WebP"
            else -> "未知"
        }
        return name to hex
    }

    /**
     * 一次读取的两个版本：
     *  - [raw]       文件原件字节（「导出原图」必须用它，否则 HEIC 会导出成转码后的 JPEG）
     *  - [effective] 实际用于解析/解码的字节（系统转码成功时是 JPEG）
     */
    private data class ReadResult(val raw: ByteArray, val effective: ByteArray, val note: String)

    /** 读图并尽量取到 JPEG 字节；HEIC 等由系统转码。 */
    private fun readImageBytes(uri: Uri): ReadResult {
        val raw = contentResolver.openInputStream(uri)?.use { it.readBytes() }
            ?: error("无法读取文件（openInputStream 返回 null）")
        val (name, hex) = sniffFormat(raw)
        val head = "格式=$name  头部=$hex"
        if (name == "JPEG") return ReadResult(raw, raw, head)

        // 非 JPEG：试一次让系统转成 JPEG。失败原因要分开报，否则无法判断是
        // 「provider 不支持该转换」还是「根本没有解码器」。
        var transcoded: ByteArray? = null
        try {
            val afd = contentResolver.openTypedAssetFileDescriptor(uri, "image/jpeg", null)
            if (afd == null) {
                appendLog("系统转码：openTypedAssetFileDescriptor 返回 null（provider 不支持转 image/jpeg）")
            } else {
                afd.use { a -> a.createInputStream().use { transcoded = it.readBytes() } }
                appendLog("系统转码：返回 ${transcoded?.size ?: 0} B")
            }
        } catch (t: Throwable) {
            appendLog("系统转码异常：${describe(t)}")
        }
        val tc = transcoded
        if (tc != null && tc.isNotEmpty()) {
            val tf = sniffFormat(tc).first
            if (tf == "JPEG") {
                return ReadResult(
                    raw, tc,
                    "$head\n已由系统转码为 JPEG：${tc.size} B（注意：增益图会在此步丢失；" +
                        "「导出原图」仍导出原件 ${raw.size} B）",
                )
            }
            appendLog("系统转码返回的仍不是 JPEG（$tf），放弃该通道")
        }
        return ReadResult(raw, raw, "$head\n⚠ 不是 JPEG，系统转码也不可用。下面先读结构、再试本地解码。")
    }

    /** 按目标长边做采样解码，避免 5760×4320 直接解码耗尽内存。 */
    private fun decodeScaled(bytes: ByteArray, targetLong: Int): Bitmap {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) {
            error("图片头解码失败（宽高=${bounds.outWidth}x${bounds.outHeight}）")
        }
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= targetLong) sample *= 2
        val opt = BitmapFactory.Options().apply {
            inSampleSize = sample
            inPreferredConfig = Bitmap.Config.ARGB_8888
            // 源图 ICC 常是 Display P3；不在这里转成 sRGB，丢掉 ICC 后系统会按 sRGB 解释，
            // 平均饱和度实测掉 8.8 个点（观感「烘焙图比原图灰」）。minSdk=26 支持该字段。
            inPreferredColorSpace = android.graphics.ColorSpace.get(android.graphics.ColorSpace.Named.SRGB)
        }
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opt)
            ?: error("解码失败（sample=$sample, 源=${bounds.outWidth}x${bounds.outHeight}）")
    }

    /** 只保留最近若干份导出，避免 cacheDir 无限增长。 */
    private fun pruneExports(dir: File, keep: Int = 10) {
        runCatching {
            val files = dir.listFiles()?.sortedByDescending { it.lastModified() } ?: return
            files.drop(keep).forEach { it.delete() }
        }
    }

    /**
     * 只把**烘焙图**（`_BAKED.jpg`）保存到相册。
     *
     * 烘焙图是「HDR 效果已经压进像素」的那张，任何设备、任何平台（含微信）都能直接看到效果，
     * 所以只需要这一张。走两段式提交（IS_PENDING 1 → 写完 → 置 0），避免相册扫到半截文件。
     */
    private fun saveBakedToGallery() {
        val f = bakedFile
        if (f == null || !f.exists()) {
            say("还没有烘焙图，请先点「导出全尺寸」")
            return
        }
        if (android.os.Build.VERSION.SDK_INT < 29) {
            say("系统低于 Android 10，暂不支持直接写相册（请用「发送烘焙图」）")
            return
        }
        say("保存烘焙图中…")
        Thread {
            try {
                val values = android.content.ContentValues().apply {
                    put(android.provider.MediaStore.MediaColumns.DISPLAY_NAME, f.name)
                    put(android.provider.MediaStore.MediaColumns.MIME_TYPE, "image/jpeg")
                    put(android.provider.MediaStore.MediaColumns.RELATIVE_PATH, "Pictures/HDRBake")
                    put(android.provider.MediaStore.MediaColumns.IS_PENDING, 1)
                }
                val uri = contentResolver.insert(
                    android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values
                ) ?: error("MediaStore.insert 返回 null")
                contentResolver.openOutputStream(uri)?.use { out ->
                    f.inputStream().use { it.copyTo(out) }
                    out.flush()
                } ?: error("openOutputStream 返回 null")
                values.clear()
                values.put(android.provider.MediaStore.MediaColumns.IS_PENDING, 0)
                contentResolver.update(uri, values, null, null)

                val msg = "烘焙图已保存：相册/Pictures/HDRBake/${f.name}（${f.length() / 1024} KB）"
                say(msg)
                appendLog(msg)
            } catch (t: Throwable) {
                say("保存烘焙图失败：${t.javaClass.simpleName}")
                appendLog("保存烘焙图失败\n${describe(t)}")
            }
        }.start()
    }

    /**
     * 清理历史导出留在**容器私有媒体库**里的文件。
     *
     * 为什么必须由 App 删：0.1.8 及之前走 MediaStore，写进的是 anco 容器的私有媒体库
     * （`/data/virt_service/rgm_hmos/...`）。宿主机 `hdc` 无权限读取，容器内的文件管理器
     * 也未必能定位。Android 10+ 允许应用删除自己创建的媒体条目，无需任何存储权限。
     */
    private fun purgeExports() {
        infoView.text = "清理中…"
        Thread {
            var deleted = 0
            var failed = 0
            val notes = StringBuilder()

            if (android.os.Build.VERSION.SDK_INT >= 29) {
                val relPath = android.provider.MediaStore.MediaColumns.RELATIVE_PATH
                val targets = listOf(
                    android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI to
                        listOf("Download/HDRLog%", "Download/HDRRaw%"),
                    android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI to
                        listOf("Pictures/HDRBake%"),
                )
                for ((collection, patterns) in targets) {
                    for (p in patterns) {
                        try {
                            deleted += contentResolver.delete(collection, "$relPath LIKE ?", arrayOf(p))
                        } catch (t: Throwable) {
                            failed++
                            notes.append("· $p: ${t.javaClass.simpleName}\n")
                        }
                    }
                }
            } else {
                notes.append("· 系统低于 Android 10，无 MediaStore 条目\n")
            }

            // 导出缓存（cacheDir/exports），本来只保留最近 10 份
            runCatching {
                File(cacheDir, "exports").listFiles()?.forEach {
                    if (it.delete()) deleted++ else failed++
                }
            }.onFailure { notes.append("· 缓存目录: ${it.javaClass.simpleName}\n") }

            val msg = "清理完成：删除 $deleted 个文件" +
                if (failed > 0) "，$failed 个失败（可能属于其他应用）" else ""
            say(msg)
            appendLog(msg)
            if (notes.isNotEmpty()) appendLog(notes.toString().trimEnd())
        }.start()
    }

    /**
     * 全屏查看完整日志。
     *
     * 为什么需要它：容器里写出的文件宿主机一律读不到（MediaStore 与 SAF 两条路都实测过），
     * 但 `hdc snapshot_display` 能截到屏幕上的任何内容——**屏幕是唯一可靠的跨容器通道**。
     * 日志区本身只剩两行高，看不到 `HEIC 摘要` 的 aux / iloc，所以给一个全屏可滚动视图。
     * 摘要排在日志最前面，打开即可见。
     */
    private fun showLogDialog() {
        val txt = logView.text.toString()
        if (txt.isBlank()) {
            say("日志还是空的，请先选一张图")
            return
        }
        val tv = TextView(this).apply {
            text = txt
            textSize = 11f
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
            setPadding(dp(12), dp(8), dp(12), dp(8))
        }
        val sv = ScrollView(this).apply { addView(tv) }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(sv, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(460)
            ))
        }
        android.app.AlertDialog.Builder(this)
            .setTitle("完整日志（${txt.length} 字，可滚动）")
            .setView(box)
            .setPositiveButton("关闭", null)
            .setNeutralButton("复制") { _, _ -> copyLogToClipboard() }
            .show()
    }

    /** 复制日志到剪贴板（按钮与全屏对话框共用）。 */
    private fun copyLogToClipboard() {
        val txt = logView.text.toString()
        if (txt.isEmpty()) {
            say("日志还是空的，请先选一张图")
            return
        }
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("hdr-log", txt))
        say(
            if (txt.length > 3000) {
                "已复制 ${txt.length} 字，但**粘贴端可能截断**；建议改用「查看全部日志」直接看屏幕"
            } else {
                "日志已复制（${txt.length} 字）"
            }
        )
    }

    private fun share(f: File?) {
        if (f == null) {
            infoView.text = "还没有可发送的文件，请先导出"
            return
        }
        runCatching { ShareGuidance.shareAsFile(this, f) }
            .onFailure {
                infoView.text = "分享失败：${it.javaClass.simpleName}"
                appendLog("分享失败\n${describe(it)}")
            }
    }

    private fun say(msg: String) = runOnUiThread { infoView.text = msg }

    /**
     * 追加日志，并**同步落盘**。
     *
     * 为什么必须落盘：容器环境（鸿蒙卓易通）连不上 logcat，一旦 App 被杀或崩溃，
     * 屏幕上和剪贴板里的内容就全没了。日志文件是唯一可回溯的证据。
     */
    private fun appendLog(line: String) = runOnUiThread {
        if (logView.text.isNotEmpty()) logView.append("\n")
        logView.append(line)
        // 自动滚到底：日志多了以后，不滚动的话用户永远停在最上面几行
        logScroll.post { logScroll.fullScroll(android.view.View.FOCUS_DOWN) }
        runCatching { logFile.appendText(line + "\n") }
    }
}
