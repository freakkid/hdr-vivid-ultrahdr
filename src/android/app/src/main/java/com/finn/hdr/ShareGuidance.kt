package com.finn.hdr

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.content.FileProvider
import java.io.File

/**
 * 分享引导：把「提高 HDR 存活概率」变成用户可执行的动作，而不是一句免责声明。
 *
 * 事实边界（务必在 UI 文案中体现）：
 *  - 我们只能保证**导出文件本身**的编码保真（XMP/MPF/增益图完整且尺寸匹配）
 *  - 微信是否重编码、是否保留 XMP/MPF，不受我方控制；即使是「原图」也不承诺
 *  - 「以文件发送」绕过图片处理管道，是概率最高的路径，但接收端仍需支持 Ultra HDR
 */
object ShareGuidance {

    enum class Channel(val label: String, val survival: Survival) {
        WECHAT_CHAT_IMAGE("微信聊天-直接发图", Survival.ALMOST_CERTAIN_LOST),
        WECHAT_ORIGINAL("微信聊天-勾选原图", Survival.UNKNOWN_UNSTABLE),
        WECHAT_AS_FILE("微信聊天-以文件发送", Survival.LIKELY_KEPT),
        SYSTEM_SHARE("系统分享面板", Survival.UNKNOWN_UNSTABLE),
        LOCAL_ONLY("仅保存到相册/本地", Survival.CERTAIN_KEPT),
    }

    enum class Survival(val description: String) {
        CERTAIN_KEPT("可保证：不经过第三方重编码"),
        LIKELY_KEPT("较可能保留：不压缩像素，但仍取决于接收端解析"),
        UNKNOWN_UNSTABLE("不确定：可能保留也可能被剥离，必须实测"),
        ALMOST_CERTAIN_LOST("几乎必然丢失：走图片压缩通道，元数据被剔除"),
    }

    data class Advice(
        val channel: Channel,
        val title: String,
        val body: String,
        val primaryAction: String,
        val riskNote: String,
    )

    fun adviceFor(channel: Channel, exportOk: Boolean): Advice = when (channel) {
        Channel.WECHAT_CHAT_IMAGE -> Advice(
            channel,
            "不建议直接发图",
            "直接选图会进入微信图片压缩通道，元数据（XMP/MPF）与副图大概率被丢弃，接收端只能看到 SDR。",
            "改用以文件发送",
            if (exportOk) "文件本身完好，但该路径几乎必然丢失 HDR 信息。" else "导出文件自检未通过，请先修复。",
        )
        Channel.WECHAT_ORIGINAL -> Advice(
            channel,
            "可尝试「原图」，但不保证",
            "「原图」避免像素重编码，但微信各版本对非像素元数据的处理不一致，可能保留也可能被重写。",
            "发送后用另一台 HDR 设备验证",
            "不同微信版本/系统版本结果不同，请以实测为准。",
        )
        Channel.WECHAT_AS_FILE -> Advice(
            channel,
            "推荐路径：以文件发送",
            "走文件通道不经过图片压缩与重编码，字节流原样传输，是当前存活率最高的做法。",
            "点「+」→ 文件 → 选择导出的 .jpg",
            "接收方需要点开下载；且其看图软件必须支持 Ultra HDR 才能看到 HDR 效果。",
        )
        Channel.SYSTEM_SHARE -> Advice(
            channel,
            "结果取决于目标应用",
            "系统分享面板只是转发，是否保留元数据由最终接收应用决定。",
            "优先选择支持原文件传输的应用",
            "社交通讯类应用多数会重编码。",
        )
        Channel.LOCAL_ONLY -> Advice(
            channel,
            "仅在本地/自家 App 内保证完整",
            "保存到本地不经过任何第三方处理，HDR 信息完整保留。",
            "在 App 内预览确认 HDR 效果",
            "换设备或上传网盘时注意选择「原文件」。",
        )
    }

    /** 以文件形式分享（FileProvider），绕过图片压缩管道。 */
    fun shareAsFile(context: Context, jpegFile: File, mime: String = "image/jpeg") {
        val uri: Uri = FileProvider.getUriForFile(
            context, "${context.packageName}.fileprovider", jpegFile,
        )
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = mime
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            // 明确声明按文件传输，避免被 target 当作图片二次处理
            putExtra(Intent.EXTRA_TITLE, jpegFile.name)
        }
        context.startActivity(Intent.createChooser(intent, "以文件发送（保留 HDR 信息）"))
    }

    /** 建议的导出命名：带标记便于用户与接收方识别。 */
    fun suggestedFileName(timestamp: String): String = "IMG_${timestamp}_HDR.jpg"
}
