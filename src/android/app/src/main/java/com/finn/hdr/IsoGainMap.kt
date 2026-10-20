package com.finn.hdr

/**
 * ISO 21496-1 增益图元数据（JPEG APP2 二进制形式）解析。
 *
 * 与 XMP GContainer 是两套并存的元数据载体。实测华为设备产出的文件可以
 * **只有 ISO 载体、完全没有 XMP**——只认 XMP 的解析器会把这类文件误判为
 * 「无增益图」，表现就是用户以为「传输把增益图弄丢了」，其实文件完好。
 *
 * 字节布局（实测 89 字节净荷）：
 *   sig(28) + header(5) + 7 × fraction(8)
 * fraction = 大端 i32 分子 + u32 分母（实测分母 1_000_000）。
 *
 * header 5 字节的语义与字段顺序尚未与标准原文逐字比对，因此解析后必须做
 * 语义合理性校验，不通过则按实测布局回退，绝不硬用可疑值。
 */
object IsoGainMap {

    val SIG: ByteArray = "urn:iso:std:iso:ts:21496:-1\u0000".toByteArray(Charsets.US_ASCII)
    private const val HEADER_LEN = 5
    private const val FRACTION_COUNT = 7
    private const val FRACTION_SIZE = 8

    data class Meta(
        val rawValues: List<Double> = emptyList(),
        val minContentBoost: Double = 1.0,
        val maxContentBoost: Double = 4.0,
        val gamma: Double = 1.0,
        val offsetSdr: Double = 1.0 / 64.0,
        val offsetHdr: Double = 1.0 / 64.0,
        val hdrCapacityMin: Double = 1.0,
        val hdrCapacityMax: Double = 4.0,
        val notes: MutableList<String> = mutableListOf(),
    ) {
        fun isPlausible(): Boolean {
            if (gamma !in 0.1..4.0) return false
            if (maxContentBoost < 1.0) return false
            if (minContentBoost <= 0.0 || minContentBoost > maxContentBoost) return false
            if (hdrCapacityMax < hdrCapacityMin) return false
            return true
        }

        /** 线性域 → hdrgm 的 log2 域。 */
        fun toLog2(): Map<String, Double> {
            fun lg(v: Double) = if (v > 0.0) Math.log(v) / Math.log(2.0) else 0.0
            return linkedMapOf(
                "GainMapMin" to lg(minContentBoost),
                "GainMapMax" to lg(maxContentBoost),
                "Gamma" to gamma,
                "OffsetSDR" to offsetSdr,
                "OffsetHDR" to offsetHdr,
                "HDRCapacityMin" to lg(hdrCapacityMin),
                "HDRCapacityMax" to lg(hdrCapacityMax),
            )
        }
    }

    fun startsWithSig(b: ByteArray, at: Int): Boolean {
        if (at + SIG.size > b.size) return false
        for (i in SIG.indices) if (b[at + i] != SIG[i]) return false
        return true
    }

    /**
     * @param start APP2 净荷起点
     * @param end   APP2 净荷终点（不含）
     * @return null 表示只含 version 块（参数在别处），或格式不符
     */
    fun parse(b: ByteArray, start: Int, end: Int): Meta? {
        if (!startsWithSig(b, start)) return null
        val bodyStart = start + SIG.size
        val bodyLen = end - bodyStart
        if (bodyLen < HEADER_LEN + FRACTION_SIZE) return null

        val values = ArrayList<Double>(FRACTION_COUNT)
        for (k in 0 until FRACTION_COUNT) {
            val p = bodyStart + HEADER_LEN + k * FRACTION_SIZE
            if (p + FRACTION_SIZE > end) return null
            val num = readI32(b, p)
            val den = readU32(b, p + 4)
            values.add(if (den == 0L) 0.0 else num.toDouble() / den.toDouble())
        }

        var meta = Meta(rawValues = values.map { round6(it) })
        // 先按标准顺序赋值：min, max, gamma, offset_sdr, offset_hdr, cap_min, cap_max
        meta = meta.copy(
            minContentBoost = values[0], maxContentBoost = values[1], gamma = values[2],
            offsetSdr = values[3], offsetHdr = values[4],
            hdrCapacityMin = values[5], hdrCapacityMax = values[6],
        )
        if (meta.isPlausible()) return meta

        // 实测样本 [0, 2.148, 0, 2.169, 1.0, 0, 0]：前两位显然是 log2 的 min/max，
        // 第 5 位才是 1.0（默认 gamma）。按实测布局回退，gamma 保守取 1.0。
        val notes = mutableListOf(
            "字段顺序与标准假设不一致，按实测布局回退（原始值: " +
                values.joinToString(",") { fmt(it) } + "）；gamma 保守取 1.0"
        )
        val v0 = values[0]
        val v1 = values[1]
        val (minBoost, maxBoost) = if (v0 >= 0.0 && v0 < 8.0 && v1 > v0 && v1 <= 8.0) {
            Math.pow(2.0, v0) to Math.pow(2.0, v1)
        } else {
            1.0 to 4.0
        }
        return Meta(
            rawValues = values.map { round6(it) },
            minContentBoost = minBoost,
            maxContentBoost = maxBoost,
            gamma = 1.0,
            offsetSdr = 1.0 / 64.0,
            offsetHdr = 1.0 / 64.0,
            hdrCapacityMin = minBoost,
            hdrCapacityMax = maxBoost,
            notes = notes,
        )
    }

    private fun readI32(b: ByteArray, at: Int): Int =
        ((b[at].toInt() and 0xFF) shl 24) or ((b[at + 1].toInt() and 0xFF) shl 16) or
            ((b[at + 2].toInt() and 0xFF) shl 8) or (b[at + 3].toInt() and 0xFF)

    private fun readU32(b: ByteArray, at: Int): Long =
        readI32(b, at).toLong() and 0xFFFFFFFFL

    private fun round6(v: Double) = Math.round(v * 1_000_000.0) / 1_000_000.0

    private fun fmt(v: Double) = String.format(java.util.Locale.US, "%.4f", v)
}
