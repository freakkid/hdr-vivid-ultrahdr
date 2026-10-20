package com.finn.hdr

/**
 * libultrahdr 的 JNI 门面。
 *
 * 生产路径选择说明：
 *  - nativeEncode 走 libultrahdr **API-4**（SDR 压缩流 + 增益图压缩流 + 元数据 → 直接封装），
 *    因为本场景是「转封装 + 几何变换」，不是「从 HDR raw 重算增益图」，API-4 能避免二次
 *    色调映射把画质压掉。
 *  - nativeProbe 走 `uhdr_create_decoder` + `uhdr_dec_probe`，只读容器、不解码全图，
 *    用于拿准确的 hdrgm 参数（比自研 XMP 解析更可信）。
 *
 * 编译要求（CMakeLists）：
 *   add_subdirectory(third_party/libultrahdr)
 *   target_link_libraries(uhdrjni ultrahdr log jnigraphics)
 */
object UltraHdrNative {

    init {
        System.loadLibrary("uhdrjni")
    }

    /** libultrahdr 版本串，用于灰度时定位问题。 */
    external fun nativeVersion(): String

    /**
     * 探测容器：返回 FloatArray(8) = [hasGainMap, minBoost, maxBoost, gamma,
     * offsetSdr, offsetHdr, hdrCapMin, hdrCapMax]；无法解析时返回 null。
     */
    external fun nativeProbe(jpeg: ByteArray): FloatArray?

    /**
     * API-4 封装。sdr / gainmap 必须是**已按目标尺寸重编码**的完整 JPEG 字节流。
     * @return 组装好的 Ultra HDR JPEG；失败返回 null（错误详情见 [lastError]）。
     */
    external fun nativeEncode(
        sdrJpeg: ByteArray,
        gainmapJpeg: ByteArray,
        minBoost: Float,
        maxBoost: Float,
        gamma: Float,
        baseQuality: Int,
        gainmapQuality: Int,
    ): ByteArray?

    external fun lastError(): String

    /** 增益图元数据的强类型视图（Kotlin 侧）。 */
    data class GainMapMeta(
        val minContentBoost: Float,
        val maxContentBoost: Float,
        val gamma: Float,
        val offsetSdr: Float,
        val offsetHdr: Float,
        val hdrCapacityMin: Float,
        val hdrCapacityMax: Float,
    )

    fun probe(jpeg: ByteArray): Pair<Boolean, GainMapMeta?> {
        val arr = nativeProbe(jpeg) ?: return false to null
        if (arr.isEmpty() || arr[0] < 0.5f) return false to null
        return true to GainMapMeta(arr[1], arr[2], arr[3], arr[4], arr[5], arr[6], arr[7])
    }
}
