package com.finn.hdr

import java.io.File
import kotlin.system.exitProcess

/**
 * JVM 对照测试入口（由 tests/android_parity/run_jvm_parity.py 调用）。
 *
 * 四个模式分别驱动**真实的** UltraHdrPackager / MetadataValidator /
 * HeifInspector / HdrSourceParser，结果以「TAB 分隔的行式文本」输出，
 * 由 Python 侧解析并与 Python 实现互检。不使用任何 JSON 库，避免额外依赖。
 *
 *   pack <primary> <gainmap> <out> gmMin gmMax gamma offSdr offHdr capMin capMax
 *       → UltraHdrPackager.pack 写出文件 + MetadataValidator 自检
 *   validate <jpeg>
 *       → MetadataValidator.validate
 *   heif <file>
 *       → HeifInspector.summarize
 *   parse <jpeg>
 *       → HdrSourceParser.parse
 *
 * 任何业务异常都以非零码退出（3 = FAIL），stderr 带原因。
 */
private fun die(msg: String): Nothing {
    System.err.println(msg)
    exitProcess(3)
}

private fun fmt(v: Float): String = String.format(java.util.Locale.US, "%.6f", v.toDouble())

private fun readBytes(path: String): ByteArray {
    val f = File(path)
    if (!f.isFile) die("文件不存在: $path")
    return f.readBytes()
}

private fun printChecks(jpeg: ByteArray) {
    val rep = MetadataValidator.validate(jpeg)
    for (c in rep.checks) println("CHECK\t${c.id}\t${if (c.ok) 1 else 0}\t${c.detail}")
    println("REPORT\tok=${if (rep.ok) 1 else 0}\tcount=${rep.checks.size}")
}

fun main(args: Array<String>) {
    if (args.isEmpty()) die("用法: <pack|validate|heif|parse> ...")
    when (args[0]) {
        "pack" -> {
            if (args.size < 10) die("pack 参数不足: pack <primary> <gainmap> <out> <7 个 meta 数值>")
            val primary = readBytes(args[1])
            val gainmap = readBytes(args[2])
            val d = Array(7) { args[4 + it].toDouble() }
            val meta = UltraHdrPackager.Meta(
                gainMapMin = d[0], gainMapMax = d[1], gamma = d[2],
                offsetSdr = d[3], offsetHdr = d[4],
                hdrCapacityMin = d[5], hdrCapacityMax = d[6],
            )
            val (bytes, layout) = UltraHdrPackager.pack(primary, gainmap, meta)
            File(args[3]).writeBytes(bytes)
            println(
                "LAYOUT\tprimaryLen=${layout.primaryLen}\tgainMapLen=${layout.gainMapLen}" +
                    "\tgainMapOffset=${layout.gainMapOffset}\ttotalLen=${layout.totalLen}"
            )
            printChecks(bytes)
        }
        "validate" -> {
            if (args.size < 2) die("validate 参数不足")
            printChecks(readBytes(args[1]))
        }
        "heif" -> {
            if (args.size < 2) die("heif 参数不足")
            val s = HeifInspector.summarize(readBytes(args[1]))
            println("TOP\t${s.topBoxes.joinToString(",")}")
            println("BRAND\t${s.brand}")
            println("COMPAT\t${s.compat.joinToString(",")}")
            for (itm in s.items) println("ITEM\t${itm.id}\t${itm.type}\t${itm.name}\t${itm.size}")
            for (u in s.auxUrns) println("AUX\t$u")
            println("PROPS\t${s.props.joinToString(",")}")
            for (r in s.refs) println("REF\t$r")
            s.primaryItem?.let { println("PRIMARY\t${it.id}\t${it.size}") }
            s.largestSecondary?.let { println("SECONDARY\t${it.id}\t${it.size}") }
            println("TMAPITEM\t${if (s.hasTmapItem) 1 else 0}")
            println("DIMGREF\t${if (s.hasDimgRef) 1 else 0}")
            s.error?.let { println("ERR\t$it") }
        }
        "parse" -> {
            if (args.size < 2) die("parse 参数不足")
            val src = HdrSourceParser.parse(readBytes(args[1]))
            println("CONTAINER\t${src.container.name}")
            println("MAKE\t${src.exifMake ?: ""}")
            println("GAINMAP\t${src.gainMapJpeg?.size ?: -1}")
            println("NEEDSBOOST\t${if (src.needsBoostRecompute) 1 else 0}")
            println("ISO\t${if (src.isoMeta != null) 1 else 0}")
            val m = src.meta
            if (m == null) {
                println("META\tnone")
            } else {
                println(
                    "META\tmin=${fmt(m.minContentBoost)}\tmax=${fmt(m.maxContentBoost)}" +
                        "\tgamma=${fmt(m.gamma)}\toffSdr=${fmt(m.offsetSdr)}\toffHdr=${fmt(m.offsetHdr)}" +
                        "\tcapMin=${fmt(m.hdrCapacityMin)}\tcapMax=${fmt(m.hdrCapacityMax)}"
                )
            }
            for (n in src.notes) println("NOTE\t$n")
        }
        else -> die("未知模式 ${args[0]}")
    }
}
