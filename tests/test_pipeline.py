"""回归测试：容器结构、几何一致性、端到端导出、负向检出。

运行：
  python -m unittest discover -s tests -v       （在 src/python 目录下）
"""
from __future__ import annotations

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "python"))

from PIL import Image  # noqa: E402

from uhdr import gainmap_ops, iso21496, jpegseg, mpf, pipeline, synthetic, validator, vivid_source, writer, xmp  # noqa: E402


def _make_pair(bw=1200, bh=900, div=4):
    base = Image.new("RGB", (bw, bh), (120, 140, 160))
    gm = Image.new("L", (bw // div, bh // div), 128)
    return base, gm


class TestJpegSeg(unittest.TestCase):
    def test_find_images_in_cuva(self):
        data = synthetic.make_cuva_sample(1200)
        images = jpegseg.find_all_images(data)
        self.assertGreaterEqual(len(images), 2)
        segs = jpegseg.header_segments_of(data, *images[1])
        self.assertEqual(segs[0].marker, 0xFFE5, "CUVA 副图首段应为 APP5")

    def test_image_end_matches_eoi(self):
        data = synthetic.make_cuva_sample(800)
        images = jpegseg.find_all_images(data)
        for s, e in images:
            self.assertEqual(data[e - 2:e], b"\xff\xd9")

    def test_last_image_eoi_at_file_end(self):
        """最后一张图的 EOI 正好在文件末尾——这是真机上最容易踩的边界。

        Kotlin 版曾用 `pos + 3 < size` 作为循环条件，导致末尾 2 字节的 FFD9
        读不到，副图被静默丢弃（表现为「增益图丢失」）。
        """
        data = synthetic.make_cuva_sample(600)
        images = jpegseg.find_all_images(data)
        self.assertEqual(images[-1][1], len(data), "最后一张图的区间终点应等于文件长度")


    def test_padding_bytes_before_marker(self):
        """JPEG 允许 marker 前出现任意多个 0xFF 填充字节。

        早期实现遇到 `FF FF` 时前进 2 字节，会跳过真正 marker 的起始位，
        把后续字节当长度读，最终抛「段边界错位」并把合法文件判成非法 JPEG。
        """
        data = synthetic.make_cuva_sample(600)
        start, end = jpegseg.find_all_images(data)[0]
        blob = data[:start + 2] + b"\xff\xff\xff" + data[start + 2:]
        segs = jpegseg.header_segments_of(blob, start, end + 3)
        self.assertTrue(segs, "填充字节之后应仍能解析出段")
        self.assertTrue(all(s.marker != 0xFFFF for s in segs), "填充字节不应被当成独立段")


class TestMpf(unittest.TestCase):
    def test_roundtrip(self):
        entries = [mpf.MpEntry(mpf.ATTR_PRIMARY, 1000, 0), mpf.MpEntry(0, 200, 800)]
        blob = mpf.build_mpf(entries)
        parsed = mpf.parse_mpf(blob)
        self.assertEqual(parsed.count, 2)
        self.assertEqual(parsed.entries[0].size, 1000)
        self.assertEqual(parsed.entries[1].offset, 800)
        self.assertEqual(parsed.entries[0].attribute, mpf.ATTR_PRIMARY)


class TestXmp(unittest.TestCase):
    def test_roundtrip(self):
        d = xmp.ContainerDirectory([
            xmp.ContainerItem("image/jpeg", "Primary", 5000, 0),
            xmp.ContainerItem("image/jpeg", "GainMap", 700, 5000),
        ])
        meta = xmp.GainMapMeta(gain_map_max=2.5, hdr_capacity_max=2.5)
        text = xmp.build_xmp(d, meta)
        d2, raw = xmp.parse_xmp(text)
        self.assertEqual(len(d2.items), 2)
        self.assertEqual(d2.by_semantic("GainMap").length, 700)
        self.assertEqual(d2.by_semantic("GainMap").offset, 5000)
        self.assertAlmostEqual(xmp.meta_from_dict(raw).gain_map_max, 2.5)

    def test_meta_validation(self):
        bad = xmp.GainMapMeta(gamma=0.0)
        self.assertTrue(bad.validate(), "gamma=0 必须被判为非法")


class TestGeometry(unittest.TestCase):
    def test_scale_keeps_ratio(self):
        base, gm = _make_pair(3000, 2250, 4)
        b2, g2, plan = gainmap_ops.resize_pair(base, gm, 2048)
        self.assertEqual(max(b2.size), 2048)
        geo = gainmap_ops.GainMapGeometry.of(b2, g2)
        self.assertTrue(geo.is_consistent(1e-9), f"比例漂移: {geo}")
        self.assertEqual(g2.size, (512, 384))

    def test_no_upscale(self):
        base, gm = _make_pair(800, 600, 4)
        b2, _g2, plan = gainmap_ops.resize_pair(base, gm, 2048)
        self.assertEqual(b2.size, (800, 600))
        self.assertTrue(plan.warnings)

    def test_crop_maps_coordinates(self):
        base, gm = _make_pair(1000, 800, 4)
        b2, g2, _ = gainmap_ops.crop_pair(base, gm, (100, 100, 600, 500))
        self.assertEqual(b2.size, (500, 400))
        self.assertEqual(g2.size, (125, 100))
        self.assertTrue(gainmap_ops.GainMapGeometry.of(b2, g2).is_consistent(0.03))

    def test_rotate_90(self):
        base, gm = _make_pair(1000, 500, 4)
        b2, g2, _ = gainmap_ops.rotate_pair(base, gm, 90)
        self.assertEqual(b2.size, (500, 1000))
        self.assertEqual(g2.size, (125, 250))

    def test_mismatched_ratio_raises(self):
        base = Image.new("RGB", (1000, 1000))
        gm = Image.new("L", (400, 100))
        with self.assertRaises(ValueError):
            gainmap_ops.assert_consistent(base, gm)


class TestIso21496(unittest.TestCase):
    """ISO 21496-1 APP2 是 XMP 之外的第二套元数据载体。

    真实教训：华为设备产出的文件可以只有 ISO 载体、完全没有 XMP，
    只认 XMP 的解析器会把这类文件误判成「无增益图」。
    """

    def test_roundtrip_standard_order(self):
        meta = iso21496.IsoGainMapMeta(
            min_content_boost=1.0, max_content_boost=4.0, gamma=1.0,
            offset_sdr=1 / 64, offset_hdr=1 / 64,
            hdr_capacity_min=1.0, hdr_capacity_max=4.0,
        )
        payload = iso21496.build_app2(meta)
        self.assertTrue(payload.startswith(iso21496.ISO_SIG))
        back = iso21496.parse_app2(payload)
        self.assertIsNotNone(back)
        self.assertAlmostEqual(back.max_content_boost, 4.0, places=2)
        self.assertAlmostEqual(back.gamma, 1.0, places=3)
        self.assertTrue(back.is_plausible())

    def test_version_only_block_returns_none(self):
        """主图侧只写 version 块时，不应被当成完整元数据。"""
        meta = iso21496.IsoGainMapMeta()
        payload = iso21496.build_app2(meta, version_block_only=True)
        self.assertIsNone(iso21496.parse_app2(payload))

    def test_measured_layout_fallback(self):
        """实测华为样本 [0, 2.148, 0, 2.169, 1.0, 0, 0] 触发回退，且 gamma 保守为 1.0。"""
        import struct
        body = bytearray(b"\x00\x00\x00\x00\x40")
        for v in (0.0, 2.148445, 0.0, 2.169925, 1.0, 0.0, 0.0):
            num, den = int(v * 1_000_000), 1_000_000
            body += struct.pack(">iI", num, den)
        meta = iso21496.parse_app2(iso21496.ISO_SIG + bytes(body))
        self.assertIsNotNone(meta)
        self.assertEqual(meta.gamma, 1.0, "gamma 不应被误猜为 2.148")
        self.assertGreater(meta.max_content_boost, 4.0)
        self.assertLess(meta.max_content_boost, 5.0)
        self.assertTrue(meta.is_plausible())

    def test_log2_conversion(self):
        meta = iso21496.IsoGainMapMeta(min_content_boost=1.0, max_content_boost=4.0,
                                       gamma=1.0, hdr_capacity_min=1.0, hdr_capacity_max=4.0)
        d = meta.to_log2_dict()
        self.assertAlmostEqual(d["GainMapMax"], 2.0, places=3)
        self.assertAlmostEqual(d["GainMapMin"], 0.0, places=3)

    def test_truncated_body_degrades_to_none(self):
        """声明了分段但实际被截断的畸形 APP2 必须降级为「无参数」，不能抛异常。

        门槛若只校验 header+1 个 fraction（13 字节），body 落在 13~60 之间会走进
        循环并抛 ValueError，把整条导出链路打断——这是第三方文件才能触发的路径。
        """
        truncated = iso21496.ISO_SIG + b"\x00\x00\x00\x00\x40" + b"\x00" * 12
        self.assertIsNone(iso21496.parse_app2(truncated))


class TestSourceDetection(unittest.TestCase):
    """判定优先级：ISO 证据必须压过 _cuva 之类弱证据。"""

    def test_iso_beats_cuva_marker(self):
        base = synthetic.make_cuva_sample(900)
        # 合成源本身含 _cuva 标记，但结构上是 APP5 → 应判 CUVA
        src = vivid_source.parse_hdr_source(base)
        self.assertEqual(src.container, "cuva")

    def test_plain_jpeg(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (120, 90), (90, 120, 150)).save(buf, "JPEG", quality=90)
        src = vivid_source.parse_hdr_source(buf.getvalue())
        self.assertEqual(src.container, "plain_jpeg")
        self.assertFalse(src.has_gainmap)


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.path.dirname(__file__), "_tmp")
        os.makedirs(cls.tmp, exist_ok=True)
        cls.src = os.path.join(cls.tmp, "src_cuva.jpg")
        synthetic.write_cuva_sample(cls.src, base_long_edge=1600)
        cls.out = os.path.join(cls.tmp, "out_uhdr.jpg")
        cls.result = pipeline.export(cls.src, cls.out, pipeline.ExportOptions(long_edge=1024))
        with open(cls.out, "rb") as f:
            cls.data = f.read()

    def test_export_ok(self):
        self.assertTrue(self.result.ok, self.result.validation)
        self.assertEqual(self.result.source_container, "cuva")

    def test_all_checks_pass(self):
        rep = validator.validate_bytes(self.data)
        failed = [c.check_id for c in rep.checks if not c.ok and c.level == "ERROR"]
        self.assertEqual(failed, [])

    def test_gainmap_carries_full_hdrgm(self):
        """完整 hdrgm 参数必须写在增益图自身 XMP（主图只留 Version）。"""
        images = jpegseg.find_all_images(self.data)
        gm_start = images[1][0]
        seg = jpegseg.find_segment(self.data, gm_start, 0xFFE1)
        self.assertIsNotNone(seg, "增益图缺少 XMP APP1 段")
        payload = self.data[seg.payload_start:seg.payload_end]
        self.assertTrue(payload.startswith(xmp.XMP_APP1_SIG))
        xml_text = payload[len(xmp.XMP_APP1_SIG):].decode("utf-8")
        _, raw = xmp.parse_xmp(xml_text)
        for field in ("GainMapMin", "GainMapMax", "Gamma", "OffsetSDR",
                      "OffsetHDR", "HDRCapacityMin", "HDRCapacityMax"):
            self.assertIn(field, raw, f"增益图 XMP 缺少 hdrgm:{field}")

    def test_primary_keeps_only_version(self):
        seg = jpegseg.find_segment(self.data, 0, 0xFFE1)
        xml_text = self.data[seg.payload_start + len(xmp.XMP_APP1_SIG):seg.payload_end].decode("utf-8")
        _, raw = xmp.parse_xmp(xml_text)
        self.assertIn("Version", raw)
        self.assertNotIn("GainMapMax", raw, "主图侧不应重复承载完整参数")

    def test_sdr_readable(self):
        """普通解码器应能把导出文件当普通 JPEG 读取（SDR 兼容底线）。"""
        img = Image.open(io.BytesIO(self.data))
        img.load()
        self.assertEqual(max(img.size), 1024)

    def test_primary_is_first_image(self):
        images = jpegseg.find_all_images(self.data)
        primary = Image.open(io.BytesIO(self.data[images[0][0]:images[0][1]]))
        primary.load()
        self.assertEqual(max(primary.size), 1024)

    def test_gainmap_decodable(self):
        images = jpegseg.find_all_images(self.data)
        gm = Image.open(io.BytesIO(self.data[images[1][0]:images[1][1]]))
        gm.load()
        self.assertIn(gm.mode, ("L", "RGB"))
        self.assertLess(gm.size[0], primary_width := Image.open(io.BytesIO(self.data[images[0][0]:images[0][1]])).size[0])

    # ---- 负向测试：自检器必须能检出破坏 ----

    def test_detect_truncated_gainmap(self):
        broken = self.data[:-100]
        rep = validator.validate_bytes(broken)
        self.assertFalse(rep.ok, "截断增益图必须被检出")
        self.assertTrue(any(not c.ok and c.check_id.startswith("offset.gainmap") for c in rep.checks)
                        or any(not c.ok and c.check_id == "file.soi_eoi" for c in rep.checks))

    def test_detect_bad_mpf_offset(self):
        seg2 = None
        for seg in jpegseg.iter_header_segments(self.data, 0):
            if seg.marker == 0xFFE2:
                seg2 = seg
                break
        self.assertIsNotNone(seg2)
        blob = bytearray(self.data)
        # MPEntry 数据区起点 = TIFF 头 + 62；第 1 项 offset 字段再偏移 16 + 8
        entry1_off = seg2.payload_start + 4 + 62 + 16 + 8
        blob[entry1_off:entry1_off + 4] = (0xDEAD).to_bytes(4, "big")
        rep = validator.validate_bytes(bytes(blob))
        self.assertFalse(rep.ok, "MPF 偏移被篡改必须被检出")
        self.assertTrue(any(not c.ok and "soi" in c.check_id for c in rep.checks))

    def test_detect_missing_xmp(self):
        stripped = writer.strip_container_segments(self.data)
        rep = validator.validate_bytes(stripped)
        self.assertFalse(rep.ok)
        ids = [c.check_id for c in rep.checks if not c.ok]
        self.assertIn("meta.xmp_present", ids)

    def test_xmp_found_even_if_exif_comes_first(self):
        """相机/厂商文件通常 EXIF APP1 在 XMP APP1 之前。

        自检器若取「第一个 APP1」，会把完全正确的文件判成「缺少 XMP」。
        我们自己的产物把 XMP 写最前，所以这个缺陷在自有产物上永远测不出来。
        """
        fake_exif = b"\xff\xe1\x00\x08Exif\x00\x00"
        blob = self.data[:2] + fake_exif + self.data[2:]
        rep = validator.validate_bytes(blob)
        hit = [c for c in rep.checks if c.check_id == "meta.xmp_present"]
        self.assertTrue(hit and hit[0].ok, "EXIF 在 XMP 之前时被误判为缺 XMP")

    def test_unverified_semantics_is_reported(self):
        """CUVA 源没有 hdrgm 参数：结构可以 ok，但语义必须明确标注为未验证。

        否则调用方只看到 ok=True，会把它当成「增益量已校准」的 HDR 文件。
        """
        self.assertFalse(self.result.semantics_verified)
        self.assertIn("语义未验证", self.result.warnings[0],
                      "该警告必须置顶，不能被几何提示淹没")


class TestRoundTrip(unittest.TestCase):
    """导出后再解析，增益参数必须原样保留。

    回归用例：解析端此前用「主图 XMP」覆盖参数，而主图按设计只写
    hdrgm:Version 占位，导致 4.43x 静默退化为默认 4.00x。
    """

    def test_export_then_parse_keeps_gain(self):
        import os
        import tempfile

        from uhdr import pipeline, vivid_source

        # 用一个非默认值，才能区分「读到了真参数」还是「回退默认 2.0」
        want = 2.148
        data = synthetic.make_cuva_sample(640)

        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.jpg")
            with open(src_path, "wb") as f:
                f.write(data)
            out = os.path.join(d, "rt.jpg")
            res = pipeline.export(
                src_path, out,
                pipeline.ExportOptions(long_edge=512, gain_map_max_log2=want),
            )
            self.assertTrue(res.ok, "导出失败")

            again = vivid_source.parse_file(out)
            self.assertIsNotNone(again.meta, "回读没有得到增益参数")
            self.assertAlmostEqual(
                again.meta.gain_map_max, want, places=2,
                msg=f"往返后增益上限变化: {want:.4f} -> {again.meta.gain_map_max:.4f}",
            )
            self.assertTrue(
                any("增益图" in n for n in again.notes),
                f"参数来源未标注为增益图: {again.notes}",
            )


if __name__ == "__main__":
    unittest.main()
