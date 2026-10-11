"""Terepen egyszer már elromlott döntések regressziós tesztjei (2026-10-11-i audit):
a lánc-állapot atomikus írása, a törlés verdiktje magyar pnputil-szöveggel, a DISM
magyar dátuma és a nyomtató-felismerés két ismert csapdája (ESET "scanner", telefon)."""
import json
import logging
import os
import shutil
import tempfile
import unittest

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app.common import CommandResult
from app.drivers_core import delete_succeeded, _dism_date_to_iso
from app.wu_core import identify_printer_packages
from app.gui.autofix import GuiAutofixMixin


class ChainState(unittest.TestCase):
    """autofix_stats.json: egy félbeszakadt írás nem viheti el a lánc memóriáját."""

    def setUp(self):
        logging.disable(logging.NOTSET)   # itt a naplósor maga az ellenőrzött viselkedés
        self.tmp = tempfile.mkdtemp()
        tmp = self.tmp

        class F(GuiAutofixMixin):
            def _autofix_stats_path(self):
                return os.path.join(tmp, 'autofix_stats.json')
        self.f = F()

    def tearDown(self):
        quiet()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kulcsok_megmaradnak_es_nincs_ideiglenes_fajl(self):
        f = self.f
        f._autofix_stats_set('keep_packages', [{'published': 'oem7.inf'}])
        f._autofix_stats_add(3)
        f._autofix_stats_add(2)
        self.assertEqual(f._autofix_stats_get('keep_packages'), [{'published': 'oem7.inf'}])
        self.assertEqual(f._autofix_stats_total_and_clear(), 5)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_csonka_fajl_felreteve_es_jelezve(self):
        f = self.f
        p = f._autofix_stats_path()
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('{"keep_packages": [{"publ')      # BSOD az írás közepén (régi viselkedés)
        with self.assertLogs(level='ERROR'):
            self.assertIsNone(f._autofix_stats_get('keep_packages'))
        self.assertTrue(os.path.exists(p + '.corrupt'))  # a bizonyíték megmarad
        f._autofix_stats_set('legs', [])
        with open(p, encoding='utf-8') as fh:
            self.assertEqual(json.load(fh), {'legs': []})

    def test_iras_kozbeni_hiba_a_regi_allapotot_hagyja(self):
        f = self.f
        f._autofix_stats_set('keep_packages', ['oem7.inf'])
        # az írás elszáll (pl. nem szerializálható érték) - a régi fájl érintetlen
        with self.assertLogs(level='WARNING'):
            f._autofix_stats_set('rossz', object())
        self.assertEqual(f._autofix_stats_get('keep_packages'), ['oem7.inf'])


class DeleteVerdict(unittest.TestCase):
    """A magyar hibaüzenet tartalmazza a 'törl' sikertövet - egyszer hamis sikert adott."""

    def test_magyar_hibaszoveg_nem_siker(self):
        res = CommandResult(1, 'A(z) oem12.inf illesztőprogram-csomag törlése nem sikerült.', '')
        self.assertFalse(delete_succeeded(res))

    def test_3010_siker(self):
        self.assertTrue(delete_succeeded(CommandResult(3010, '', '')))

    def test_ures_kimenet_hibakoddal_nem_siker(self):
        self.assertFalse(delete_succeeded(CommandResult(1, '', '')))


class DismDate(unittest.TestCase):
    """A /English a dátumot NEM angolosítja: magyar gépen `2006. 06. 21.` jön."""

    def test_alakok(self):
        for raw in ('2006. 06. 21.', '2006-06-21', '6/21/2006', '21.06.2006'):
            self.assertEqual(_dism_date_to_iso(raw), '2006-06-21', raw)

    def test_olvashatatlan_ures(self):
        self.assertEqual(_dism_date_to_iso('xyz'), '')


class PrinterDetection(unittest.TestCase):
    """A driverből dönt: az ESET "scanner"-e és egy telefon-driver NEM nyomtató."""

    INFS = {
        'oem1.inf': '[Version]\nClass=Printer\n[Dev]\nUSBPRINT\\HP_LaserJet=VID_03F0&PID_1D17\n',
        'oem2.inf': '[Version]\nClass=USB\n; HP LaserJet USB kompozit\nUSB\\VID_03F0&PID_1D17\n',
        'oem3.inf': '[Version]\nClass=AntiVirus\n; ESET real-time file system scanner\n',
        'oem4.inf': '[Version]\nClass=AndroidUsbDeviceClass\n; SAMSUNG Mobile USB\nUSB\\VID_04E8&PID_6860\n',
        'oem5.inf': '[Version]\nClass=USBDevice\n; WIA scanner driver\n',
    }

    def test_besorolas(self):
        drivers = [{'published': p, 'original': p} for p in self.INFS]
        found = identify_printer_packages(drivers, inf_dir='X', reader=lambda path: self.INFS.get(os.path.basename(path)))
        self.assertEqual(found.get('oem1.inf'), 'osztaly')
        self.assertIn('oem2.inf', found)             # ugyanaz a fizikai eszköz (VID:PID)
        self.assertNotIn('oem3.inf', found)          # vírus-"scanner", nem nyomtató
        self.assertNotIn('oem4.inf', found)          # Samsung telefon: a gyártónév nem dönt
        self.assertEqual(found.get('oem5.inf'), 'kulcsszo')


if __name__ == '__main__':
    unittest.main()
