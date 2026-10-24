"""HEIC grid 解析 / 重封的回归测试。

测试用的是**合成文件**：2x2 增益图 grid + 3x4 主图 grid + 华为式 mime/it35 项。
它验证的是「序列化与偏移自洽」，不能替代真机华为 HEIC 的实测——原件只在
手机/容器里，拿到真文件后必须再跑一次
``PYTHONPATH=src/python python -m uhdr.heif_grid <file> --rewrap <gridID> -o out.heic``。
"""

import os
import subprocess
import sys
import tempfile
import unittest

from uhdr.heif_grid import HeifGrids, box, full_box


# ---------------------------------------------------------------------------
# 合成源文件（独立于被测实现，避免自证）
# ---------------------------------------------------------------------------


def _grid_payload(rows: int, cols: int, w: int, h: int) -> bytes:
    return bytes([0, 0, 0, 0, rows - 1, cols - 1]) + w.to_bytes(2, "big") + h.to_bytes(2, "big")


def _fake_tile(seed: int) -> bytes:
    # 4 字节长度前缀 + NAL 头 + 填充，模拟 hvc1 item 的载荷形状
    return (8).to_bytes(4, "big") + b"\x40\x01" + bytes([seed & 0xFF]) * 40


def _source_items() -> list[tuple[int, str, str, str, bool, bytes]]:
    it: list[tuple[int, str, str, str, bool, bytes]] = []
    # 增益图：2x2 = 4 块
    it.append((42, "grid", "", "", False, _grid_payload(2, 2, 512, 384)))
    for n, iid in enumerate(range(43, 47)):
        it.append((iid, "hvc1", "", "", True, _fake_tile(n)))
    # 主图：3x4 = 12 块
    it.append((50, "grid", "", "", False, _grid_payload(3, 4, 4032, 3024)))
    for n, iid in enumerate(range(51, 63)):
        it.append((iid, "hvc1", "", "", True, _fake_tile(100 + n)))
    # 华为私有参数项：mime 项 + it35
    it.append((47, "mime", "hdr", "application/vnd.huawei.hdr", False, b"HUAWEI" * 100))
    it.append((48, "it35", "", "", False, b"\x01" * 237))
    # 那个 62 字节、以 "tmap" 开头的嵌套项
    it.append((49, "tmap", "", "", False, b"tmap" + bytes(58)))
    return it


def _ispe(w: int, h: int) -> bytes:
    return full_box("ispe", 0, 0, w.to_bytes(4, "big") + h.to_bytes(4, "big"))


def build_source() -> bytes:
    items = _source_items()
    payloads = {i[0]: i[5] for i in items}
    ids = [i[0] for i in items]

    hvcc = full_box("hvcC", 1, 0, bytes(24))
    props = [hvcc, _ispe(512, 384), _ispe(256, 192), _ispe(4032, 3024), _ispe(1008, 756)]
    assoc = {iid: [] for iid in ids}
    assoc[42] = [(2, True)]
    for iid in range(43, 47):
        assoc[iid] = [(1, True), (3, True)]
    assoc[50] = [(4, True)]
    for iid in range(51, 63):
        assoc[iid] = [(1, True), (5, True)]

    ftyp = box("ftyp", b"heic" + bytes(4) + b"mif1heic")

    def meta(offsets: list[int]) -> bytes:
        hdlr = full_box("hdlr", 0, 0, bytes(4) + b"pict" + bytes(12) + b"\x00")
        pitm = full_box("pitm", 0, 0, (50).to_bytes(2, "big"))

        infe_parts = b""
        for iid, typ, name, ctype, hidden, _p in items:
            body = iid.to_bytes(2, "big") + bytes(2) + typ.encode("latin-1")
            body += bytes([len(name)]) + name.encode("utf-8") + b"\x00"
            if typ == "mime":
                body += ctype.encode("utf-8") + b"\x00"
            infe_parts += full_box("infe", 2, 1 if hidden else 0, body)
        iinf = full_box("iinf", 0, 0, len(items).to_bytes(2, "big") + infe_parts)

        def dimg(frm: int, to: list[int]) -> bytes:
            return box("dimg", frm.to_bytes(2, "big") + len(to).to_bytes(2, "big")
                       + b"".join(i.to_bytes(2, "big") for i in to))
        iref = full_box("iref", 0, 0, dimg(42, list(range(43, 47)))
                        + dimg(50, list(range(51, 63)))
                        + box("cdsc", (47).to_bytes(2, "big") + (1).to_bytes(2, "big")
                              + (50).to_bytes(2, "big")))

        ipco = box("ipco", b"".join(props))
        ipma_body = len(ids).to_bytes(4, "big")
        for iid in ids:
            lst = assoc[iid]
            ipma_body += iid.to_bytes(2, "big") + bytes([len(lst)])
            for idx, ess in lst:
                ipma_body += bytes([(idx & 0x7F) | (0x80 if ess else 0)])
        iprp = box("iprp", ipco + full_box("ipma", 0, 0, ipma_body))

        iloc_body = bytes([0x44, 0x00]) + len(ids).to_bytes(2, "big")
        for iid, off in zip(ids, offsets):
            iloc_body += iid.to_bytes(2, "big") + bytes(2) + (1).to_bytes(2, "big")
            iloc_body += off.to_bytes(4, "big") + len(payloads[iid]).to_bytes(4, "big")
        iloc = full_box("iloc", 0, 0, iloc_body)

        return box("meta", bytes(4) + hdlr + pitm + iinf + iref + iprp + iloc)

    probe = meta([0] * len(ids))
    base = len(ftyp) + len(probe) + 8
    offsets: list[int] = []
    pos = base
    for iid in ids:
        offsets.append(pos)
        pos += len(payloads[iid])
    m = meta(offsets)
    assert len(m) == len(probe)
    return ftyp + m + box("mdat", b"".join(payloads[i] for i in ids))


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = build_source()
        cls.heif = HeifGrids(cls.data)

    def test_items(self):
        items = self.heif.items
        self.assertEqual(items[42].item_type, "grid")
        self.assertEqual(items[43].item_type, "hvc1")
        self.assertTrue(items[43].hidden)
        self.assertEqual(items[47].content_type, "application/vnd.huawei.hdr")
        self.assertEqual(items[48].item_type, "it35")
        self.assertEqual(items[48].size, 237)
        self.assertEqual(items[49].item_type, "tmap")
        self.assertEqual(self.heif.pitm, 50)

    def test_item_bytes_match(self):
        src = {i[0]: i[5] for i in _source_items()}
        for iid, payload in src.items():
            self.assertEqual(self.heif.item_bytes(self.heif.items[iid]), payload, f"#{iid}")

    def test_grids(self):
        grids = {g.item_id: g for g in self.heif.grids()}
        self.assertEqual(set(grids), {42, 50})
        g = grids[42]
        self.assertEqual((g.rows, g.cols), (2, 2))
        self.assertEqual((g.out_w, g.out_h), (512, 384))
        self.assertEqual(g.tile_ids, [43, 44, 45, 46])
        self.assertEqual(grids[50].tile_count, 12)

    def test_gain_map_candidate_prefers_4_tiles(self):
        cand = self.heif.gain_map_candidate()
        self.assertIsNotNone(cand)
        self.assertEqual(cand.item_id, 42)

    def test_refs_and_props(self):
        self.assertEqual(self.heif.refs_of("dimg", 42), [43, 44, 45, 46])
        self.assertIn("cdsc", [r.typ for r in self.heif.refs])
        names = [t for t, _ in self.heif.properties_of(43)]
        self.assertEqual(names, ["hvcC", "ispe"])

    def test_summary_mentions_grid_and_mime(self):
        s = self.heif.summary()
        self.assertIn("grid #42 2x2", s)
        self.assertIn("application/vnd.huawei.hdr", s)
        self.assertIn("增益图候选", s)


class TestRewrap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = build_source()
        cls.heif = HeifGrids(cls.src)

    def test_rewrap_gain_map(self):
        blob = self.heif.rewrap(42)
        self.assertGreater(len(blob), 0)
        again = HeifGrids(blob)
        self.assertEqual(again.pitm, 42)
        self.assertEqual(len(again.items), 5, "只应保留 grid + 4 块 tile")
        grids = again.grids()
        self.assertEqual(len(grids), 1)
        self.assertEqual(grids[0].tile_ids, [43, 44, 45, 46])
        self.assertEqual((grids[0].out_w, grids[0].out_h), (512, 384))

    def test_rewrap_keeps_payloads_byte_exact(self):
        blob = self.heif.rewrap(42)
        again = HeifGrids(blob)
        for iid in (43, 44, 45, 46):
            self.assertEqual(again.item_bytes(again.items[iid]),
                             self.heif.item_bytes(self.heif.items[iid]), f"tile #{iid}")
        self.assertEqual(again.item_bytes(again.items[42]),
                         self.heif.item_bytes(self.heif.items[42]), "grid 载荷")

    def test_rewrap_offsets_point_into_mdat(self):
        blob = self.heif.rewrap(42)
        again = HeifGrids(blob)
        mdat = next(b for b in again.top if b.typ == "mdat")
        lo, hi = mdat.payload, mdat.start + mdat.size
        for item in again.items.values():
            for off, length in item.extents:
                self.assertGreaterEqual(off, lo, f"#{item.item_id} 偏移落在 mdat 之前")
                self.assertLessEqual(off + length, hi, f"#{item.item_id} 越出 mdat")

    def test_rewrap_preserves_properties(self):
        blob = self.heif.rewrap(42)
        again = HeifGrids(blob)
        grid_props = [t for t, _ in again.properties_of(42)]
        self.assertIn("ispe", grid_props)
        tile_props = again.properties_of(43)
        self.assertEqual([t for t, _ in tile_props], ["hvcC", "ispe"])
        # hvcC 决定 HEVC 参数集，必须逐字节一致，否则解不出画面
        self.assertEqual(tile_props[0][1], self.heif.properties_of(43)[0][1])

    def test_rewrap_main_grid(self):
        blob = self.heif.rewrap(50)
        again = HeifGrids(blob)
        self.assertEqual(len(again.items), 13)
        self.assertEqual(again.grids()[0].tile_ids, list(range(51, 63)))
        for iid in range(51, 63):
            self.assertEqual(again.item_bytes(again.items[iid]),
                             self.heif.item_bytes(self.heif.items[iid]))

    def test_rewrap_missing_grid_raises(self):
        with self.assertRaises(ValueError):
            self.heif.rewrap(43)


class TestRobustness(unittest.TestCase):
    def test_truncated_does_not_raise(self):
        data = build_source()
        for cut in (40, 200, len(data) // 2):
            try:
                h = HeifGrids(data[:cut])
            except ValueError:
                continue  # 连 ftyp 都不完整，按「不是 ISOBMFF」处理是允许的
            # 截断后允许解析不完整，但不允许崩：摘要必须能正常渲染
            self.assertIn("grid", h.summary())

    def test_non_isobmff_raises(self):
        with self.assertRaises(ValueError):
            HeifGrids(b"\xff\xd8\xff\xe1" + bytes(64))

    def test_broken_dimg_warns(self):
        data = bytearray(build_source())
        # 把增益图 grid 的 dimg 计数从 4 改成 3，解析仍应成功但给出警告
        idx = data.find(b"dimg" + (42).to_bytes(2, "big") + (4).to_bytes(2, "big"))
        self.assertGreater(idx, 0, "没找到 dimg(42) 的计数位置")
        data[idx + 7] = 3   # count 是大端 2 字节，改低字节
        h = HeifGrids(bytes(data))
        grid = {g.item_id: g for g in h.grids()}[42]
        self.assertEqual(grid.tile_count, 3)
        self.assertTrue(any("需要 4 块" in w for w in h.warnings))


class TestCli(unittest.TestCase):
    def test_cli_summary_and_rewrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.heic")
            with open(src, "wb") as f:
                f.write(build_source())
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env = dict(os.environ, PYTHONPATH=os.path.join(root, "src", "python"))
            r = subprocess.run([sys.executable, "-m", "uhdr.heif_grid", src],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("grid #42 2x2", r.stdout)

            out = os.path.join(tmp, "gm.heic")
            r2 = subprocess.run([sys.executable, "-m", "uhdr.heif_grid", src,
                                 "--rewrap", "42", "-o", out],
                                capture_output=True, text=True, env=env)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertGreater(os.path.getsize(out), 0)


if __name__ == "__main__":
    unittest.main()
