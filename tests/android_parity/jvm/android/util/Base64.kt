package android.util

/**
 * JVM 桩：只实现 [UltraHdrPackager] 用到的 `Base64.encodeToString(ByteArray, Int)`。
 *
 * 语义对齐 android.util.Base64 的 NO_WRAP：标准字母表、带 '=' padding、不插换行 ——
 * 与 java.util.Base64.getEncoder() 完全一致。未用到的 decode / URL_SAFE 等
 * 符号一律不实现（按硬性约定：桩只覆盖被引用的符号）。
 */
object Base64 {
    const val DEFAULT = 0
    const val NO_PADDING = 1
    const val NO_WRAP = 2

    fun encodeToString(input: ByteArray, flags: Int): String {
        check(flags == NO_WRAP || flags == DEFAULT || flags == NO_PADDING) { "桩只支持 NO_WRAP/DEFAULT/NO_PADDING" }
        val encoder = java.util.Base64.getEncoder()
        val encoded = encoder.encodeToString(input)
        return if (flags == NO_PADDING) encoded.trimEnd('=') else encoded
    }
}
