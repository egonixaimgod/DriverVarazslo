"""Office teljes eltávolítása (2026-11-26): a Microsoft Get Help parancssori változata
(GetHelpCmd.exe -S OfficeScrubScenario), utána a Windows saját eltávolítója a maradékra
(`winact_core.remove_office`). Hálózat és valódi registry nélkül."""
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from app import winact_core as wc
from app.common import CommandResult

C2R = ('"C:\\Program Files\\Common Files\\Microsoft Shared\\ClickToRun\\OfficeClickToRun.exe" '
       'scenario=install scenariosubtype=ARP sourcetype=None productstoremove=Proplus2019Retail'
       '.16_hu-hu_x-none culture=hu-hu version.16=16.0')
# Terep, Lenovo 20Q1, Build 426: ennyi volt fent.
LENOVO = ['Microsoft Office LTSC Professional Plus 2024 - hu-hu', 'Microsoft Project Standard 2016 - hu-hu',
          'Microsoft Visio Standard 2016 - hu-hu', 'Office 16 Click-to-Run Extensibility Component',
          'Office 16 Click-to-Run Licensing Component']
# Terep: a -OfficeVersion All-ra a GetHelpCmd súgót írt és 0-val lépett ki.
USAGE = 'Starting Microsoft GetHelp Command Line Tool\nError: Invalid command line arguments\nUsage: ...'


class FakeMachine:
    """A gép Office-listája; a `run` a hívott eltávolító szerint fogyasztja."""
    def __init__(self, items, gethelp_removes=0, native_works=True, gethelp_out='', code=0):
        self.items = list(items)
        self.calls, self.gethelp_removes, self.native_works = [], gethelp_removes, native_works
        self.gethelp_out, self.code = gethelp_out, code

    def installed(self):
        return [(n, f'{C2R} DisplayLevel=False' if 'Component' not in n else 'msiexec /x {X} /qn')
                for n in self.items]

    def run(self, cmd, **k):
        self.calls.append(cmd)
        if isinstance(cmd, list):  # GetHelpCmd
            del self.items[:self.gethelp_removes]
            return CommandResult(self.code, self.gethelp_out)
        if self.native_works:
            # a Click-to-Run termék a hozzá tartozó komponenseket is viszi (az utolsóval)
            self.items = [n for n in self.items if n not in cmd and not (
                'Component' in n and not any('Component' not in m for m in self.items if m not in cmd))]
        return CommandResult(0)


class OfficeRemoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dv_officedel_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

        def fake_download(run_fn, url, dest, **k):
            self.assertEqual(url, wc.GETHELP_URL)
            with zipfile.ZipFile(dest, 'w') as z:
                z.writestr('GetHelpCmd/GetHelpCmd.exe', b'MZ')

        for p in (mock.patch.object(wc, '_app_data_dir', lambda: self.tmp),
                  mock.patch.object(wc, 'download_with_cert_fallback', fake_download)):
            p.start()
            self.addCleanup(p.stop)

    def _go(self, m):
        # A FakeMachine a natív parancsban a név alapján dönt: a név legyen a parancsban.
        orig = m.installed
        m.installed = lambda: [(n, f'{c} REM {n}') for n, c in orig()]
        return wc.remove_office(m.run, lambda s: None, installed=m.installed)

    def test_csak_a_sugo_szerinti_kapcsolok(self):
        self.assertEqual(wc.GETHELP_ARGS, ['-S', 'OfficeScrubScenario', '-AcceptEula'])

    def test_lenovo_gethelp_semmit_nem_csinal_a_tartalek_leviszi(self):
        m = FakeMachine(LENOVO, gethelp_removes=0, gethelp_out=USAGE)
        ok, text = self._go(m)
        self.assertTrue(ok, text)
        self.assertEqual(sum(isinstance(c, list) for c in m.calls), 1)  # nem haladt: nincs 2. kör
        self.assertTrue(any('DisplayLevel=False' in c for c in m.calls if isinstance(c, str)))

    def test_gethelp_ujrafut_amig_fogy(self):
        m = FakeMachine(['A 2024', 'B 2016', 'C 2013'], gethelp_removes=1)
        ok, _ = self._go(m)
        self.assertTrue(ok)
        self.assertEqual(sum(isinstance(c, list) for c in m.calls), 3)
        self.assertFalse(any(isinstance(c, str) for c in m.calls))  # tartalék nem kellett

    def test_ha_fent_maradt_nem_kesz_akkor_sem_ha_0_a_kod(self):
        m = FakeMachine(['Microsoft Publisher 2019 - en-us'], native_works=False)
        ok, text = self._go(m)
        self.assertFalse(ok)
        self.assertIn('Publisher', text)

    def test_nyitott_office_kodjat_kimondja(self):
        ok, text = self._go(FakeMachine(['Microsoft 365'], native_works=False, code=6))
        self.assertFalse(ok)
        self.assertIn('nyitva van', text)

    def test_nincs_office(self):
        m = FakeMachine([])
        ok, text = self._go(m)
        self.assertTrue(ok)
        self.assertIn('nincs Office', text)
        self.assertEqual(m.calls, [])

    def test_nem_zip_letoltes_hiba(self):
        def bad_download(run_fn, url, dest, **k):
            with open(dest, 'w') as f:
                f.write('<html>')
        with mock.patch.object(wc, 'download_with_cert_fallback', bad_download):
            with self.assertRaises(RuntimeError):
                self._go(FakeMachine(['Microsoft 365']))


class OfficeEntryTest(unittest.TestCase):
    def test_click_to_run_a_fejlesztoi_gep_valodi_sorabol(self):
        name, cmd = wc.office_entry('Proplus2019Retail - hu-hu',
                                    'Microsoft Office Professional Plus 2019 - hu-hu', C2R)
        self.assertEqual(cmd, C2R + ' DisplayLevel=False')

    def test_msi(self):
        self.assertEqual(wc.office_entry('{90160000-008C-0000-1000-0000000FF1CE}',
                                         'Office 16 Click-to-Run Extensibility Component', ''),
                         ('Office 16 Click-to-Run Extensibility Component',
                          'msiexec /x {90160000-008C-0000-1000-0000000FF1CE} /qn /norestart'))
        self.assertIsNone(wc.office_entry('{12345678-0011-0000-0000-0000000FF1CE}', 'X', ''))
        self.assertIsNone(wc.office_entry('Valami', 'Nem Office', 'msiexec /x {...}'))

    def test_eles_olvasas_nem_dob(self):
        # Csak olvas; a fejlesztői gépen lista jön vissza (üres is lehet).
        self.assertIsInstance(wc.installed_office(), list)


if __name__ == '__main__':
    unittest.main()
