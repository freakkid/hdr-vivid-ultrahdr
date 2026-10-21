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

    private val previewEdge = 720     // 预览长边，够看清效果又不吃内存
    private val exportEdge = 2048     // 导出长边（与桌面 Python 默认一致）

    /** 与 Python bake.py 的 profile 语义一致；顺序即 UI 顺序。 */
    private val profiles = listOf(
        "local" to "本地观看（local：局部色调映射）",
        "wechat" to "微信分享（wechat：克制映射+重压缩余量）",
        "off" to "仅全局映射（off：旧行为，A/B 对照）",
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
            text = "导出全尺寸（$exportEdge 长边）"
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

        // 容器里 ACTION_SEND 常被裁剪，把文件落到系统相册是最稳的出路
        saveBaked = Button(this).apply {
            text = "保存烘焙图到相册"
            isEnabled = false
            setOnClickListener { saveToGallery(bakedFile) }
        }

        // HEIC 解不了时的唯一出路：把原件导到相册，用户自己用微信传出去
        exportRawButton = Button(this).apply {
            text = "导出原图（HEIC 无增益图时用）"
            isEnabled = false
            setOnClickListener { exportRawFile() }
        }

        val copyLogButton = Button(this).apply {
            text = "复制全部日志"
            setOnClickListener {
                val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("hdr-log", logView.text))
                infoView.text = "日志已复制到剪贴板（共 ${logView.text.length} 字）"
            }
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
        root.addView(copyLogButton)
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
        previewGen.incrementAndGet()   // 作废上一张图残留的预览任务
        previewHandler.removeCallbacks(previewTask)
        exportButton.isEnabled = false
        runOnUiThread {
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
                val (rawBytes, formatNote) = readImageBytes(uri)
                appendLog(formatNote)
                appendLog("长度：${rawBytes.size} B")

                appendLog("[2/5] 解析容器")
                val fmtNow = sniffFormat(rawBytes).first
                if (fmtNow.startsWith("ISOBMFF")) {
                    // 两步走：① 把结构读出来（决定有没有增益图）② 用系统解码器解主图（让用户先看到画面）
                    appendLog("[2a/5] HEIC 结构诊断")
                    appendLog(HeifInspector.describe(rawBytes))
                    lastRawBytes = rawBytes

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
                        appendLog("→ 结论：能看到画面，但烘不出 HDR。若要保住效果需提取增益图（见下方 aux URN）。")
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
                }

                appendLog("[4/5] 生成预览")
                renderPreviewNow()
                appendLog("[5/5] 就绪：拖滑杆/换档位会即时重算预览；满意后点「导出全尺寸」。")
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
        previewGen.incrementAndGet()   // 导出期间不再接受预览结果
        exportButton.isEnabled = false
        infoView.text = "导出中…"

        Thread {
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
                bakedFile = bf
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
                    hdrFile = hf
                    appendLog("已写出 ${hf.name}（${result.jpeg.size / 1024} KB）")
                } else {
                    appendLog("自检未通过 → 不标记为 HDR")
                }

                appendLog("[4/4] 完成")
                pruneExports(dir)
                runOnUiThread {
                    exportButton.isEnabled = true
                    shareBaked.isEnabled = bakedFile?.exists() == true
                    shareHdr.isEnabled = hdrFile?.exists() == true
                    saveBaked.isEnabled = bakedFile?.exists() == true
                    infoView.text = "导出完成：${bf.name}（${bakedJpeg.size / 1024} KB）。可保存到相册或用微信发送。"
                }
            } catch (t: Throwable) {
                say("导出失败：${t.javaClass.simpleName}")
                appendLog(describe(t))
                runOnUiThread { exportButton.isEnabled = true }
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

    /** 读图并尽量取到 JPEG 字节；HEIC 等由系统转码。 */
    private fun readImageBytes(uri: Uri): Pair<ByteArray, String> {
        val raw = contentResolver.openInputStream(uri)?.use { it.readBytes() }
            ?: error("无法读取文件（openInputStream 返回 null）")
        val (name, hex) = sniffFormat(raw)
        val head = "格式=$name  头部=$hex"
        if (name == "JPEG") return raw to head

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
                return tc to "$head\n已由系统转码为 JPEG：${tc.size} B（注意：增益图会在此步丢失）"
            }
            appendLog("系统转码返回的仍不是 JPEG（$tf），放弃该通道")
        }
        return raw to "$head\n⚠ 不是 JPEG，系统转码也不可用。下面先读结构、再试本地解码。"
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
     * 把选中的**原始文件字节**导出到系统「下载」目录。
     *
     * 为什么需要它：鸿蒙相册在受保护媒体库，`hdc` 读不到；容器内 App 私有目录也拿不出来。
     * App 是目前唯一能读到这些字节的地方，所以由 App 负责落盘，
     * 用户再用微信/数据线传出来，桌面端就能用 pillow-heif 彻底分析。
     *
     * 走 `MediaStore.Downloads`（API 29+）：无需存储权限，且文件管理器可见。
     */
    private fun exportRawFile() {
        val bytes = lastRawBytes
        if (bytes == null || bytes.isEmpty()) {
            say("还没有读到原始文件，请先选图")
            return
        }
        if (android.os.Build.VERSION.SDK_INT < 29) {
            say("系统低于 Android 10，暂不支持直接导出（请用「发送」按钮）")
            return
        }
        val (fmt, _) = sniffFormat(bytes)
        val ext = when {
            fmt.startsWith("ISOBMFF") -> "heic"
            fmt == "JPEG" -> "jpg"
            fmt == "PNG" -> "png"
            else -> "bin"
        }
        val mime = when (ext) {
            "heic" -> "image/heic"
            "jpg" -> "image/jpeg"
            "png" -> "image/png"
            else -> "application/octet-stream"
        }
        val name = "HDR_RAW_${System.currentTimeMillis()}.$ext"
        say("导出原图中…")
        Thread {
            try {
                val values = android.content.ContentValues().apply {
                    put(android.provider.MediaStore.MediaColumns.DISPLAY_NAME, name)
                    put(android.provider.MediaStore.MediaColumns.MIME_TYPE, mime)
                    put(android.provider.MediaStore.MediaColumns.RELATIVE_PATH,
                        android.os.Environment.DIRECTORY_DOWNLOADS + "/HDRRaw")
                    put(android.provider.MediaStore.MediaColumns.IS_PENDING, 1)
                }
                val uri = contentResolver.insert(
                    android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, values
                ) ?: error("MediaStore.insert 返回 null（容器可能不支持 Downloads 集合）")
                contentResolver.openOutputStream(uri)?.use { out ->
                    out.write(bytes)
                    out.flush()
                } ?: error("openOutputStream 返回 null")
                values.clear()
                values.put(android.provider.MediaStore.MediaColumns.IS_PENDING, 0)
                contentResolver.update(uri, values, null, null)

                appendLog("已导出原图：$name（${bytes.size} B, $mime）")
                runOnUiThread {
                    infoView.text = "原图已导出到 下载/HDRRaw/$name\n用微信发出来即可在桌面分析"
                }
            } catch (t: Throwable) {
                appendLog("导出原图失败：${describe(t)}")
                runOnUiThread { infoView.text = "导出原图失败：${t.javaClass.simpleName}" }
            }
        }.start()
    }

    /**
     * 保存到系统相册（MediaStore）。
     * 容器里 `ACTION_SEND` 分享面板经常被裁剪，写进相册是更可靠的一条出路：
     * 用户可以从相册直接发微信。
     */
    private fun saveToGallery(f: File?) {
        if (f == null || !f.exists()) {
            infoView.text = "还没有导出的文件，请先点「导出全尺寸」"
            return
        }
        if (android.os.Build.VERSION.SDK_INT < 29) {
            infoView.text = "系统低于 Android 10，暂不支持直接写相册（请用「发送」按钮）"
            appendLog("保存到相册已跳过：SDK=${android.os.Build.VERSION.SDK_INT} < 29")
            return
        }
        infoView.text = "保存到相册中…"
        Thread {
            try {
                val values = android.content.ContentValues().apply {
                    put(android.provider.MediaStore.Images.Media.DISPLAY_NAME, f.name)
                    put(android.provider.MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                    put(android.provider.MediaStore.Images.Media.RELATIVE_PATH, "Pictures/HDRBake")
                }
                val uri = contentResolver.insert(
                    android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values
                ) ?: error("MediaStore.insert 返回 null")
                contentResolver.openOutputStream(uri)?.use { out ->
                    f.inputStream().use { it.copyTo(out) }
                } ?: error("openOutputStream 返回 null")
                appendLog("已保存到相册：Pictures/HDRBake/${f.name}")
                runOnUiThread { infoView.text = "已保存到相册：Pictures/HDRBake/${f.name}" }
            } catch (t: Throwable) {
                appendLog("保存到相册失败\n${describe(t)}")
                runOnUiThread { infoView.text = "保存到相册失败：${t.javaClass.simpleName}（可改用「发送」按钮）" }
            }
        }.start()
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
