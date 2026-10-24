package com.finn.hdr

/**
 * `UltraHdrNative` 的桩：只保留 `GainMapMeta` 数据类。
 *
 * 真实的 `UltraHdrNative` 在类初始化时会 `System.loadLibrary`，JVM 上没有那个 `.so`。
 * 编译器只需要类型存在，所以数值对照用这份桩即可。
 */
object UltraHdrNative {
    data class GainMapMeta(
        val minContentBoost: Float,
        val maxContentBoost: Float,
        val gamma: Float,
        val offsetSdr: Float,
        val offsetHdr: Float,
        val hdrCapacityMin: Float,
        val hdrCapacityMax: Float,
    )
}
