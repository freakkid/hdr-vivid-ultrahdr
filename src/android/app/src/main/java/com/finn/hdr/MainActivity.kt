package com.finn.hdr

import android.graphics.Bitmap
import android.graphics.BitmapFactory
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
import java.io.ByteArrayOutputStream
import java.io.File

/**
 * 探针界面：选图 → 同时产出两种结果。
 *
 *   1. 烘焙图 `*_BAKED.jpg`  —— 普通 JPEG，HDR 效果已压进去，微信/任何设备直接可见
 *   2. Ultra HDR `*_HDR.jpg` —— 保留增益图，仅支持 Ultra HDR 的设备能还原
 *
 * 主按钮给烘焙图（用户的真实诉求），Ultra HDR 作为附赠。
 */
class MainActivity : ComponentActivity() {

    private lateinit var logView: TextView
    private lateinit var shareBaked: Button
    private lateinit var shareHdr: Button
    private var bakedFile: File? = null
    private var hdrFile: File? = null

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
            text = "选择 HDR 照片并处理"
            setOnClickListener {
                pickImage.launch(
                    PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                )
            }
        }

        shareBaked = Button(this).apply {
            text = "发送烘焙图（微信可见 HDR 效果）"
            isEnabled = false
            setOnClickListener { share(suggestedTsf(bakedFile)) }
        }

        shareHdr = Button(this).apply {
            text = "发送 Ultra HDR（需设备支持）"
            isEnabled = false
            setOnClickListener { share(suggestedTsf(hdrFile)) }
        }

        logView = TextView(this).apply {
            textSize = 11f
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
        }

        val scroll = ScrollView(this).apply { addView(logView) }
        root.addView(pickButton)
        root.addView(shareBaked)
        root.addView(shareHdr)
        root.addView(
            scroll,
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f)
        )
        setContentView(root)
    }

    private fun suggestedTsf(f: File?): File? = f?.takeIf { it.exists() }

    private fun share(f: File?) {
        if (f == null) return
        runCatching { ShareGuidance.shareAsFile(this, f) }
            .onFailure { appendLog("\n分享失败：${it.message}") }
    }

    private fun handle(uri: Uri) {
        logView.text = "处理中…"
        shareBaked.isEnabled = false
        shareHdr.isEnabled = false

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

                val dir = File(cacheDir, "exports").apply { mkdirs() }
                val baseName = "IMG_${System.currentTimeMillis()}"

                // ---------- 1) 烘焙：普通 JPEG，HDR 效果已压进去 ----------
                log.appendLine()
                log.appendLine("— 烘焙为普通图 —")
                val base = decodeScaled(src.sdrJpeg, 2048)
                val gm = decodeScaled(src.gainMapJpeg!!, 2048)
                val (baked, stats) = HdrBaker.bake(base, gm, src.meta)
                log.appendLine("尺寸：${stats.outSize.first}x${stats.outSize.second}")
                log.appendLine("HDR 峰值：${"%.2f".format(stats.hdrPeak)}x  "
                    + "白点：${"%.2f".format(stats.whitePoint)}  "
                    + "最大 boost：${"%.2f".format(stats.maxBoost)}x")
                log.appendLine("高光裁切：${"%.2f".format(stats.clippedRatio * 100)}%")

                val bos = ByteArrayOutputStream()
                baked.compress(Bitmap.CompressFormat.JPEG, 95, bos)
                val bakedJpeg = bos.toByteArray()
                val bf = File(dir, "${baseName}_BAKED.jpg")
                bf.writeBytes(bakedJpeg)
                bakedFile = bf
                log.appendLine("已写出：${bf.name}（${bakedJpeg.size / 1024} KB）")

                base.recycle(); gm.recycle(); baked.recycle()

                // ---------- 2) Ultra HDR：保留增益图 ----------
                log.appendLine()
                log.appendLine("— 封装 Ultra HDR —")
                val result = UltraHdrExporter().export(src, UltraHdrExporter.Options(longEdge = 2048))
                result.steps.forEach { log.appendLine(it) }
                result.warnings.forEach { log.appendLine("⚠ $it") }
                result.validation?.checks?.forEach {
                    log.appendLine("${if (it.ok) "✓" else "✗"} ${it.id}  ${it.detail}")
                }
                if (result.ok && result.jpeg != null) {
                    val hf = File(dir, "${baseName}_HDR.jpg")
                    hf.writeBytes(result.jpeg)
                    hdrFile = hf
                    log.appendLine("已写出：${hf.name}（${result.jpeg.size / 1024} KB）")
                } else {
                    log.appendLine("自检未通过 → 不标记为 HDR")
                }

                runOnUiThread {
                    shareBaked.isEnabled = bakedFile?.exists() == true
                    shareHdr.isEnabled = hdrFile?.exists() == true
                }
                log.appendLine()
                log.appendLine("烘焙图可直接用微信发，对方无需支持 HDR。")
            } catch (t: Throwable) {
                log.appendLine("失败：${t.javaClass.simpleName}: ${t.message}")
                if (t is OutOfMemoryError) {
                    log.appendLine("提示：大图解码内存不足，建议先清理后台或降低长边。")
                }
            }
            publish(log.toString())
        }.start()
    }

    /** 按目标长边做采样解码，避免 5760×4320 直接解码耗尽内存。 */
    private fun decodeScaled(bytes: ByteArray, targetLong: Int): Bitmap {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= targetLong) sample *= 2
        val opt = BitmapFactory.Options().apply {
            inSampleSize = sample
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opt)
            ?: error("解码失败（sample=$sample）")
    }

    private fun publish(text: String) = runOnUiThread { logView.text = text }

    private fun appendLog(extra: String) = runOnUiThread { logView.append(extra) }
}
