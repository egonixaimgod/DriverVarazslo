"""A tömeges stressz-teszt memória-terve (2026-10-30): a Prime95 Blend és a Linpack együtt
fut; a fejlesztői gépen (64 GB, 55,6 GB szabad) a Linpack a 30 GB-os opciót kapta, a Blend
pedig ~56 GB-ot kért, és a Linpack "not enough memory"-val leállt."""
import os
import shutil
import tempfile
import unittest

import tests  # noqa: F401
from app.gui.stress import GuiStressMixin
from app.stress_defs import STRESS_TOOLS_BULK


class F(GuiStressMixin):
    def __init__(self, total, avail):
        self._ram = (total, avail)

    def _get_ram_gb(self):
        return self._ram


class MemoryPlan(unittest.TestCase):
    def test_terepi_eset_64gb(self):
        lin, prime = F(64, 55.6)._plan_stress_memory()
        self.assertEqual(lin, 6)                         # 14 GB, nem a 30 GB-os 7-es
        self.assertEqual(prime, int((55.6 - 3.0 - 14) * 1024))
        self.assertLess(14 + prime / 1024 + 1.5, 55.6)   # a kettő együtt elfér

    def test_kis_gep(self):
        lin, prime = F(8, 5.0)._plan_stress_memory()
        self.assertEqual(lin, 1)                         # 2 GB
        self.assertEqual(prime, 512)                     # a minimum

    def test_ismeretlen_szabad_memoria(self):
        lin, prime = F(16, None)._plan_stress_memory()
        self.assertIsNone(prime)                         # a Prime95 saját alapértéke marad

    def test_linpack_a_prime95_elott_indul(self):
        self.assertLess(STRESS_TOOLS_BULK.index('linpack'), STRESS_TOOLS_BULK.index('prime95'))


class PrimeTxt(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.exe = os.path.join(self.d, 'prime95.exe')
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_letrehozza_es_felulirja(self):
        api = F(64, 55.6)
        api._set_prime95_torture_mem(self.exe, 1000)
        with open(os.path.join(self.d, 'prime.txt'), 'rb') as f:
            self.assertEqual(f.read(), b'TortureMem=1000\r\n')
        with open(os.path.join(self.d, 'prime.txt'), 'w', newline='') as f:
            f.write('TortureHyperthreading=1\r\nTortureMem=56934\r\nTortureTime=6\r\n')
        api._set_prime95_torture_mem(self.exe, 2000)
        with open(os.path.join(self.d, 'prime.txt'), 'rb') as f:
            self.assertEqual(f.read(), b'TortureHyperthreading=1\r\nTortureTime=6\r\nTortureMem=2000\r\n')


if __name__ == '__main__':
    unittest.main()
