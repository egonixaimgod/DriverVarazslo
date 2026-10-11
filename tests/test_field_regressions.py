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


class StagedAlreadyBound(unittest.TestCase):
    """2026-10-11, Sound Blaster Play! 4: a WU-ág 3 mp-cel korábban már rákötötte a csomagot,
    a katalógus-ág pnputil-ja ezt ki is mondta ("up-to-date on device: <ez az eszköz>"), a
    program mégis "a KÖTÉS hiányzik"-ot írt és újrakötést ajánlott (a keresés-kori jelzőből)."""

    SB = r'USB\VID_041E&PID_3274&MI_00'
    INF = ('[Version]\nClass=MEDIA\n[Manufacturer]\n%M%=C,NTamd64\n[C.NTamd64]\n'
           '%D%=I,USB\\VID_041E&PID_3274&MI_00\n[Strings]\nM="Creative"\nD="SB Play 4"\n')
    # a terepi napló kimenete szó szerint
    BOUND = ('Microsoft PnP Utility\n\nAdding driver package:  ksusbaud.inf\n'
             'Driver package added successfully. (Already exists in the system)\n'
             'Published Name:         oem3.inf\n'
             'Driver package is up-to-date on device: USB\\VID_041E&PID_3274&MI_00\\7&887c350&4&0000\n\n'
             'Total driver packages:  1\nAdded driver packages:  0')
    NOT_BOUND = BOUND.replace('Driver package is up-to-date on device: '
                              'USB\\VID_041E&PID_3274&MI_00\\7&887c350&4&0000\n', '')

    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self._sd = os.environ.get('SystemDrive')
        os.environ['SystemDrive'] = self.tmp
        import urllib.request
        self._uo = urllib.request.urlopen

    def tearDown(self):
        import urllib.request
        urllib.request.urlopen = self._uo
        if self._sd is None:
            os.environ.pop('SystemDrive', None)
        else:
            os.environ['SystemDrive'] = self._sd
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, pnputil_out):
        import urllib.request
        from tests import test_catalog_install as tci
        stub = tci.InstallStub(self.tmp, {'https://x/sb.cab': self.INF}, {})
        stub._run_add_driver = lambda cmd: CommandResult(259, pnputil_out, '')

        def fake_urlopen(req, context=None, timeout=None):
            stub.cur_url = req.full_url
            return tci._Resp(b'CAB')
        urllib.request.urlopen = fake_urlopen
        drv = {'name': 'Sound Blaster Play! 4', 'cat': 'x', 'hwid': self.SB + '&REV_2200',
               'pnp_id': self.SB + r'\7&887C350&4&0000', 'url': 'https://x/sb.cab', 'cat_guid': 'g',
               'wu_title': 'Creative Technology Ltd. - MEDIA - 1.16.4.26', 'wu_date': '2021-01-05',
               'alt_candidates': [], 'inbox_now': True, 'generic_replace': True,
               'all_hwids': [self.SB + '&REV_2200', self.SB], 'installed_inf': 'wdma_usb.inf'}
        stub._install_catalog_sync([drv], task_id='t')
        return stub

    def test_mar_rakotott_csomag_naprakesz(self):
        stub = self._run(self.BOUND)
        text = '\n'.join(stub.log)
        self.assertIn('már naprakész', text)
        self.assertNotIn('KÖTÉS hiányzik', text)
        self.assertEqual(stub._catalog_staged_nobind, [])

    def test_nem_kotott_csomag_tovabbra_is_jelez(self):
        stub = self._run(self.NOT_BOUND)
        self.assertIn('KÖTÉS hiányzik', '\n'.join(stub.log))
        self.assertEqual(stub._catalog_staged_nobind, ['Sound Blaster Play! 4'])


if __name__ == '__main__':
    unittest.main()
