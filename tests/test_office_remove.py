"""Office teljes eltávolítása (2026-11-26): a Microsoft Get Help parancssori változata
(GetHelpCmd.exe -S OfficeScrubScenario), `winact_core.remove_office`. Hálózat és valódi
registry nélkül: a letöltés egy helyben gyártott zip, a Programok listája csonk."""
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from app import winact_core as wc
from app.common import CommandResult


class OfficeRemoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dv_officedel_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []

        def fake_download(run_fn, url, dest, **k):
            self.calls.append(('dl', url))
            with zipfile.ZipFile(dest, 'w') as z:
                z.writestr('GetHelpCmd/GetHelpCmd.exe', b'MZ')

        self.patches = [mock.patch.object(wc, '_app_data_dir', lambda: self.tmp),
                        mock.patch.object(wc, 'download_with_cert_fallback', fake_download)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _go(self, code, before, after):
        states = iter([before, after])

        def run(cmd, **k):
            self.calls.append(('run', cmd))
            return CommandResult(code, 'kimenet')
        return wc.remove_office(run, lambda m: None, installed=lambda: next(states))

    def test_a_microsoft_eszkozet_minden_verziora_futtatja(self):
        ok, _ = self._go(0, ['Microsoft Office Professional Plus 2019 - hu-hu'], [])
        self.assertTrue(ok)
        self.assertEqual(self.calls[0], ('dl', wc.GETHELP_URL))
        cmd = self.calls[1][1]
        self.assertTrue(cmd[0].lower().endswith('gethelpcmd.exe'))
        self.assertEqual(cmd[1:], ['-S', 'OfficeScrubScenario', '-AcceptEula', '-OfficeVersion', 'All'])

    def test_ha_fent_maradt_nem_kesz_akkor_sem_ha_0_a_kod(self):
        # 4. elv: a verdikt a visszaolvasás, nem az eszköz kódja.
        ok, text = self._go(0, ['Microsoft Publisher 2019 - en-us'], ['Microsoft Publisher 2019 - en-us'])
        self.assertFalse(ok)
        self.assertIn('Publisher', text)

    def test_nyitott_office_kodjat_kimondja(self):
        ok, text = self._go(6, ['Microsoft 365'], ['Microsoft 365'])
        self.assertFalse(ok)
        self.assertIn('nyitva van', text)

    def test_nincs_office(self):
        ok, text = self._go(68, [], [])
        self.assertTrue(ok)
        self.assertIn('nincs Office', text)

    def test_nem_zip_letoltes_hiba(self):
        def bad_download(run_fn, url, dest, **k):
            with open(dest, 'w') as f:
                f.write('<html>')
        with mock.patch.object(wc, 'download_with_cert_fallback', bad_download):
            with self.assertRaises(RuntimeError):
                self._go(0, ['Microsoft 365'], [])


class InstalledOfficeTest(unittest.TestCase):
    def test_a_fejlesztoi_gep_valodi_bejegyzesei(self):
        # 2026-11-26, a fejlesztői gép Uninstall-kulcsából szó szerint (Click-to-Run).
        uninst = ('"C:\\Program Files\\Common Files\\Microsoft Shared\\ClickToRun\\OfficeClickToRun.exe" '
                  'scenario=install scenariosubtype=ARP sourcetype=None productstoremove=Proplus2019Retail'
                  '.16_hu-hu_x-none culture=hu-hu version.16=16.0')
        self.assertIn('officeclicktorun.exe', uninst.lower())
        self.assertTrue(wc._OFFICE_MSI_KEY.match('{90160000-0011-0000-0000-0000000FF1CE}'))
        self.assertTrue(wc._OFFICE_MSI_KEY.match('{90160000-008C-0000-1000-0000000FF1CE}'))
        self.assertFalse(wc._OFFICE_MSI_KEY.match('{12345678-0011-0000-0000-0000000FF1CE}'))

    def test_eles_olvasas_nem_dob(self):
        # Csak olvas; a fejlesztői gépen lista jön vissza (üres is lehet).
        self.assertIsInstance(wc.installed_office(), list)


if __name__ == '__main__':
    unittest.main()
