"""A piros gomb Prime95-módja (2026-10-30, explicit user decision): Small FFTs, NEM Blend.
A Blend a RAM ~87%-át kérte (`TortureMem`), és a Linpack a fejlesztői gépen (64 GB) kétszer
"not enough memory"-val állt le - egy 14 GB / 40 GB-os elosztással is."""
import unittest

import tests  # noqa: F401
from app.gui.stress import GuiStressMixin
from app.stress_defs import STRESS_CLICK_SEQUENCES


class F(GuiStressMixin):
    pass


class PrimeMode(unittest.TestCase):
    def test_small_ffts_es_nem_blend(self):
        steps = [s for s in STRESS_CLICK_SEQUENCES['prime95'] if isinstance(s, str)]
        self.assertIn('small ffts (tests l1/l2/l3', steps)
        self.assertFalse(any('blend' in s for s in steps))

    def test_linpack_a_teljes_szabad_memoriabol_valaszt(self):
        # a terepi eset: 64 GB, 55,6 GB szabad -> a 30 GB-os 7. opció (Small FFTs mellett elfér)
        self.assertEqual(F()._pick_linpack_ram_option(64, 55.6), 7)
        self.assertEqual(F()._pick_linpack_ram_option(16, 12.0), 5)


if __name__ == '__main__':
    unittest.main()
