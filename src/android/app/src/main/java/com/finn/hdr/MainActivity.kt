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
    private lateinit var infoView: TextView
    private lateinit var previewBefore: ImageView
    private lateinit var previewAfter: ImageView
    private lateinit var shareBaked: Button
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

    /** 预览代次：快速拖动滑杆时丢弃过期结果，避免旧图覆盖新图。 */
    @Volatile
    private var previewGen = 0

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
        exportButton.isEnabled = false
        runOnUiThread {
            shareBaked.isEnabled = false
            shareHdr.isEnabled = false
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
                    // 不解码，先把结构读出来：有没有 aux、URN 叫什么，直接决定后续路线
                    appendLog("这是 HEIC/HEIF，当前版本不解码。结构如下：")
                    appendLog(HeifInspector.describe(rawBytes))
                    infoView.text = "这是 HEIC（鸿蒙相机可能默认此格式），本版不支持解码。结构已列在下方日志，请复制发我。"
                    appendLog("→ 请点下方「复制全部日志」把上面结构发我，据此判断是否值得做 HEIC 支持。")
                    return@Thread
                }
                val src = HdrSourceParser.parse(rawBytes)
                parsed = src
                appendLog("容器：${src.container}")
                appendLog("增益图：${if (src.hasGainMap) "有（${src.gainMapJpeg?.size} B）" else "无"}")
                appendLog("Make：${src.exifMake ?: "unknown"}")
                src.notes.forEach { appendLog("· $it") }

                if (!src.hasGainMap) {
                    infoView.text = "这张图没有增益图 —— 它是普通 SDR，不需要转换。"
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
                infoView.text = "载入失败：${t.javaClass.simpleName}"
                appendLog(describe(t))
            }
        }.start()
    }

    // =====================================================================
    // 预览
    // =====================================================================

    private fun schedulePreview(reason: String) {
        if (pvBase == null || pvGain == null) return
        appendLog("[预览] $reason → ${curProfile()} / ${"%.2f".format(curStrength())}")
        Thread { renderPreviewNow() }.start()
    }

    /** 必须在后台线程调用。 */
    private fun renderPreviewNow() {
        val base = pvBase ?: return
        val gain = pvGain ?: return
        val src = parsed ?: return
        val gen = ++previewGen
        val profile = curProfile()
        val strength = curStrength()
        try {
            val (baked, stats) = HdrBaker.bake(
                base, gain, src.meta,
                HdrBaker.Options(profile = profile, strength = strength),
            )
            if (gen != previewGen) {          // 已有更新的请求，丢弃本次
                baked.recycle()
                return
            }
            runOnUiThread {
                previewAfter.setImageBitmap(baked)
                infoView.text = "预览 ${stats.outSize.first}x${stats.outSize.second}  " +
                    "峰值 ${"%.2f".format(stats.hdrPeak)}x  " +
                    "裁切 ${"%.2f".format(stats.clippedRatio * 100)}%  " +
                    "[$profile ${"%.2f".format(strength)}]"
            }
            appendLog("  预览：${stats.outSize.first}x${stats.outSize.second} " +
                "峰值=${"%.2f".format(stats.hdrPeak)}x " +
                "裁切=${"%.2f".format(stats.clippedRatio * 100)}% " +
                "暗部提升=${"%.2f".format(stats.ltmShadowLift)}x")
        } catch (t: Throwable) {
            infoView.text = "预览失败：${t.javaClass.simpleName}"
            appendLog("预览失败\n${describe(t)}")
        }
    }

    // =====================================================================
    // 导出全尺寸
    // =====================================================================

    private fun exportFull() {
        val src = parsed ?: return
        if (!src.hasGainMap) return
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
                    HdrBaker.Options(profile = curProfile(), strength = curStrength()),
                )
                appendLog("尺寸 ${stats.outSize.first}x${stats.outSize.second}  " +
                    "峰值 ${"%.2f".format(stats.hdrPeak)}x  白点 ${"%.2f".format(stats.whitePoint)}  " +
                    "最大 boost ${"%.2f".format(stats.maxBoost)}x  裁切 ${"%.2f".format(stats.clippedRatio * 100)}%")
                if (curProfile() != "off") {
                    appendLog("LTM 半径=${stats.ltmRadiusPx}px  detail=${"%.2f".format(stats.ltmDetailGain)}  " +
                        "暗部提升=${"%.2f".format(stats.ltmShadowLift)}x")
                }

                val quality = if (curProfile() == "wechat") 88 else 95
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
                runOnUiThread {
                    exportButton.isEnabled = true
                    shareBaked.isEnabled = bakedFile?.exists() == true
                    shareHdr.isEnabled = hdrFile?.exists() == true
                    infoView.text = "导出完成：${bf.name}（${bakedJpeg.size / 1024} KB）。可直接发微信。"
                }
            } catch (t: Throwable) {
                infoView.text = "导出失败：${t.javaClass.simpleName}"
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

        val transcoded = try {
            contentResolver.openTypedAssetFileDescriptor(uri, "image/jpeg", null)?.use { afd ->
                afd.createInputStream().use { it.readBytes() }
            }
        } catch (t: Throwable) {
            appendLog("转码尝试异常：${describe(t)}")
            null
        }
        if (transcoded != null && sniffFormat(transcoded).first == "JPEG") {
            return transcoded to "$head\n已由系统转码为 JPEG：${transcoded.size} B（注意：增益图会在此步丢失）"
        }
        return raw to "$head\n⚠ 不是 JPEG，系统也未能转码。若是 HEIC，请在相册「另存/导出为 JPG」后再选。"
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

    private fun appendLog(line: String) = runOnUiThread {
        if (logView.text.isNotEmpty()) logView.append("\n")
        logView.append(line)
    }
}
