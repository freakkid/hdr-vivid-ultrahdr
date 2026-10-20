package com.finn.hdr

import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import java.io.File

/**
 * 最小可用入口：选图 → 解析 → 导出 → 自检 → 以文件分享。
 *
 * 这是给真机验证用的"探针界面"，不是产品 UI。它存在的目的：
 *  1. 证明整条链路在真机上能跑通
 *  2. 把每一步的结构自检结果原样显示出来（而不是只给一个"成功"）
 */
class MainActivity : ComponentActivity() {

    private lateinit var logView: TextView
    private lateinit var shareButton: Button
    private var exportedFile: File? = null

    private val pickImage =
        registerForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
            if (uri != null) handle(uri)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 96, 48, 48)
        }

        val pickButton = Button(this).apply {
            text = "选择 HDR 照片并导出"
            setOnClickListener {
                pickImage.launch(
                    PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                )
            }
        }

        shareButton = Button(this).apply {
            text = "以文件发送（保留 HDR 信息）"
            isEnabled = false
            setOnClickListener {
                val f = exportedFile ?: return@setOnClickListener
                runCatching { ShareGuidance.shareAsFile(this@MainActivity, f) }
                    .onFailure { appendLog("\n分享失败：${it.message}") }
            }
        }

        logView = TextView(this).apply {
            textSize = 11f
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
        }

        val scroll = ScrollView(this).apply { addView(logView) }
        root.addView(pickButton)
        root.addView(shareButton)
        root.addView(
            scroll,
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f)
        )
        setContentView(root)
    }

    private fun handle(uri: Uri) {
        logView.text = "处理中…"
        shareButton.isEnabled = false

        Thread {
            val log = StringBuilder()
            try {
                val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() }
                    ?: error("无法读取文件")
                log.appendLine("源文件：${bytes.size} B")

                val src = HdrSourceParser.parse(bytes)
                log.appendLine("容器：${src.container}")
                log.appendLine("增益图：${if (src.hasGainMap) "有（${src.gainMapJpeg?.size} B）" else "无"}")
                log.appendLine("Make：${src.exifMake ?: "unknown"}")
                src.notes.forEach { log.appendLine("· $it") }

                if (!src.hasGainMap) {
                    log.appendLine("\n该文件没有增益图，只能导出纯 SDR。")
                    publish(log.toString())
                    return@Thread
                }

                val result = UltraHdrExporter().export(
                    src,
                    UltraHdrExporter.Options(longEdge = 2048),
                )
                log.appendLine()
                result.steps.forEach { log.appendLine(it) }
                result.warnings.forEach { log.appendLine("⚠ $it") }

                log.appendLine()
                log.appendLine("— 自检 —")
                result.validation?.checks?.forEach {
                    log.appendLine("${if (it.ok) "✓" else "✗"} ${it.id}  ${it.detail}")
                }

                if (result.ok && result.jpeg != null) {
                    val dir = File(cacheDir, "exports").apply { mkdirs() }
                    val f = File(dir, ShareGuidance.suggestedFileName(System.currentTimeMillis().toString()))
                    f.writeBytes(result.jpeg)
                    exportedFile = f
                    log.appendLine()
                    log.appendLine("已写出：${f.name}（${f.length()} B）")
                    runOnUiThread { shareButton.isEnabled = true }
                } else {
                    log.appendLine()
                    log.appendLine("自检未通过 → 不标记为 HDR，请勿对外宣称已保留 HDR。")
                }
            } catch (t: Throwable) {
                log.appendLine("失败：${t.javaClass.simpleName}: ${t.message}")
                if (t is UnsatisfiedLinkError) {
                    log.appendLine("提示：native 库未编译，当前应已回退纯 Kotlin 封装器。")
                }
            }
            publish(log.toString())
        }.start()
    }

    private fun publish(text: String) = runOnUiThread { logView.text = text }

    private fun appendLog(extra: String) = runOnUiThread { logView.append(extra) }
}
