"""烘焙档位回归：`faithful`（保真）档不得比源 SDR 更亮。

背景：`local` 档曾在真机上被连续否定「太亮」。根因不是 strength，而是
「增益图全图有值 + LTM 抬暗部」把中间调一起提亮。修法是逐像素封顶
`min(target, sdr × max_lift)`，`faithful` 档把上限收到 1.05。

本测试用合成 CUVA 样本，断言「faithful 与源的差距 <= local 与源的差距」，
防止以后调参把封顶调没了。
"""

import io
import os
import tempfile
import unittest

import numpy as np
from PIL import Image

from uhdr import bake, ltm, synthetic, vivid_source


class TestProfileParams(unittest.TestCase):
    def test_off_disables_ltm(self):
        self.assertIsNone(ltm.params_for_profile("off"))

    def test_faithful_params_are_conservative(self):
        p = ltm.params_for_profile("faithful")
        self.assertIsNotNone(p)
        # range_compress 越接近 1 越不抬暗部；detail_gain 越小局部对比越弱
        self.assertGreaterEqual(p.range_compress, 0.95)
        self.assertLessEqual(p.detail_gain, 1.12)


class TestFaithfulNotBrighter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "synth_cuva.jpg")
        synthetic.write_cuva_sample(path, base_long_edge=480)
        cls.src = vivid_source.parse_file(path)
        cls.sdr = Image.open(io.BytesIO(cls.src.primary_jpeg)).convert("L")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _median(self, profile: str) -> float:
        data, _stats = bake.bake(
            self.src,
            bake.BakeOptions(long_edge=480, profile=profile, strength=0.40),
        )
        out = Image.open(io.BytesIO(data)).convert("L")
        if out.size != self.sdr.size:
            out = out.resize(self.sdr.size, Image.Resampling.LANCZOS)
        return float(np.median(np.asarray(out)))

    def test_faithful_closer_to_source_than_local(self):
        sdr_med = float(np.median(np.asarray(self.sdr)))
        d_local = abs(self._median("local") - sdr_med)
        d_faithful = abs(self._median("faithful") - sdr_med)
        self.assertLessEqual(
            d_faithful, d_local + 1.0,
            f"faithful 偏离源 {d_faithful:.1f}，local 偏离 {d_local:.1f}：封顶没生效？",
        )

    def test_faithful_lift_bounded(self):
        sdr_med = float(np.median(np.asarray(self.sdr)))
        med = self._median("faithful")
        # 中位最多 +5%（1.05 是线性域上限，转成 8bit 后的余量放宽到 +6 级）
        self.assertLessEqual(med, sdr_med * 1.05 + 6,
                             f"faithful 中位 {med:.1f} 超出源 {sdr_med:.1f} 的 1.05 倍")


class TestStrengthTakesEffect(unittest.TestCase):
    """strength 必须真的改变输出，否则滑杆看起来「不生效」。

    历史：v0.1.17 之前 strength 只乘增益，而增益差异被色调映射压平 →
    0.30 与 1.00 的预览几乎一样。v0.1.18 改成缩放「HDR 相对 SDR 的增量」
    （target = sdr + (blended - sdr) * strength），本测试守住这条语义。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "synth_strength.jpg")
        synthetic.write_cuva_sample(path, base_long_edge=480)
        cls.src = vivid_source.parse_file(path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _mean(self, strength: float, profile: str = "local") -> float:
        # 合成样本偏中间调，默认权重 (0.35/0.90) 下几乎没有像素落到「采信 HDR」区，
        # 任何参数都测不出差异。这里把权重拉开到全域，专门验证「增量随 strength 缩放」。
        data, _ = bake.bake(
            self.src,
            bake.BakeOptions(long_edge=480, profile=profile, strength=strength,
                             blend_start=0.0, blend_end=0.05),
        )
        out = Image.open(io.BytesIO(data)).convert("L")
        return float(np.mean(np.asarray(out)))

    def test_high_strength_is_brighter_than_low(self):
        low = self._mean(0.35)
        high = self._mean(0.90)
        self.assertGreater(
            high, low + 1.5,
            f"strength 0.90 均值 {high:.1f} 与 0.35 的 {low:.1f} 几乎一样：提亮上限没随强度缩放",
        )


if __name__ == "__main__":
    unittest.main()
