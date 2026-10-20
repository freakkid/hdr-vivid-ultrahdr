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

from uhdr import gainmap_ops, jpegseg, mpf, pipeline, synthetic, validator, vivid_source, writer, xmp  # noqa: E402


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


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.path.dirname(__file__), "_tmp")
        os.makedirs(cls.tmp, exist_ok=True)
        cls.src = os.path.join(cls.tmp, "src_cuva.jpg")
        synthetic.write_cuva_sample(cls.src, base_long_edge=1600)
        cls.out = os.path.join(cls.tmp, "out_uhdr.jpg")
        cls.result = pipeline.export(cls.src, cls.out, pipeline.ExportOptions(long_edge=1024))
        cls.data = open(cls.out, "rb").read()

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
