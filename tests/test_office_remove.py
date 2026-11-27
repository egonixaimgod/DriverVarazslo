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

    def _go(self, m, leftovers=(), **kw):
        # A FakeMachine a natív parancsban a név alapján dönt: a név legyen a parancsban.
        orig = m.installed
        m.installed = lambda: [(n, f'{c} REM {n}') for n, c in orig()]
        self.clean_calls = []

        def fake_clean(run, log, **k):
            self.clean_calls.append(k)
            return list(leftovers)
        return wc.remove_office(m.run, lambda s: None, installed=m.installed, deep_clean=fake_clean, **kw)

    def test_a_maradek_takaritas_a_kapcsolokkal_fut(self):
        ok, text = self._go(FakeMachine(['Microsoft 365'], gethelp_removes=1),
                            keep_settings=True, keep_activation=False)
        self.assertTrue(ok)
        self.assertEqual(self.clean_calls, [{'keep_settings': True, 'keep_activation': False, 'remove_hub': False,
                                             'protected': []}])
        self.assertIn('fiókok/beállítások megmaradtak', text)

    def test_maradek_nem_kesz(self):
        ok, text = self._go(FakeMachine(['Microsoft 365'], gethelp_removes=1),
                            leftovers=['mappa: C:\\Program Files\\Microsoft Office'])
        self.assertFalse(ok)
        self.assertIn('Microsoft Office', text)

    def test_bukott_mentes_utan_az_office_hoz_nem_nyul(self):
        m = FakeMachine(['Microsoft 365'], gethelp_removes=1)
        orig = m.installed
        m.installed = lambda: [(n, f'{c} REM {n}') for n, c in orig()]
        cleaned = []
        ok, text = wc.remove_office(m.run, lambda s: None, installed=m.installed,
                                    deep_clean=lambda *a, **k: cleaned.append(1) or [],
                                    backup_docs=True, backup=lambda lg: (False, 'Nincs elég hely'))
        self.assertFalse(ok)
        self.assertIn('NEM nyúltam', text)
        self.assertEqual(m.calls, [])
        self.assertEqual(cleaned, [])

    def test_sikeres_mentes_utan_fut_es_kiirja_hova(self):
        m = FakeMachine(['Microsoft 365'], gethelp_removes=1)
        orig = m.installed
        m.installed = lambda: [(n, f'{c} REM {n}') for n, c in orig()]
        ok, text = wc.remove_office(m.run, lambda s: None, installed=m.installed,
                                    deep_clean=lambda *a, **k: [], backup_docs=True,
                                    backup=lambda lg: (True, r'C:\DriverVarazslo\office_dokumentum_mentes\x'))
        self.assertTrue(ok)
        self.assertIn('office_dokumentum_mentes', text)

    def test_valodi_takarito_tesztbol_tiltva(self):
        from app import officeclean_core
        with self.assertRaises(RuntimeError):
            officeclean_core.deep_clean(None, None)

    def test_csak_a_sugo_szerinti_kapcsolok(self):
        self.assertEqual(wc.GETHELP_ARGS, ['-S', 'OfficeScrubScenario', '-AcceptEula'])

    def test_lenovo_gethelp_semmit_nem_csinal_a_tartalek_leviszi(self):
        m = FakeMachine(LENOVO, gethelp_removes=0, gethelp_out=USAGE)
        ok, text = self._go(m)
        self.assertTrue(ok, text)
        # „Invalid command line arguments”-re a kör egyszer -LogFolder nélkül is lefut (2026-11-27:
        # a kapcsoló nincs mérve); utána nem haladt: nincs 2. kör.
        gh = [c for c in m.calls if isinstance(c, list)]
        self.assertEqual(len(gh), 2)
        self.assertIn('-LogFolder', gh[0])
        self.assertNotIn('-LogFolder', gh[1])
        self.assertTrue(any('DisplayLevel=False' in c for c in m.calls if isinstance(c, str)))

    def test_elhasalt_gethelp_nem_var_es_ablakot_zar(self):
        # 2026-11-27, terep (fejlesztői gép + Lenovo Build 428): a GetHelp „background”-ot ír,
        # de a naplója szerint nem talált Office-t -> nincs 5 perces várakozás, jön a tartalék.
        import os
        m = FakeMachine(['Microsoft Office LTSC Professional Plus 2024 - hu-hu'],
                        gethelp_out='Uninstall office is running in the background.')
        orig = m.run

        def run(cmd, **k):
            if isinstance(cmd, list) and 'OfficeScrubScenario' in cmd:
                d = cmd[cmd.index('-LogFolder') + 1]
                with open(os.path.join(d, 'GetHelp.log'), 'w', encoding='utf-8') as f:
                    f.write('Running Office Uninstall. This may take a few minutes...\n'
                            'Office Uninstall failed.  There are no supported Office products installed.\n')
            return orig(cmd, **k)
        m.run = run
        with mock.patch.object(wc, '_wait_background_uninstall',
                               side_effect=AssertionError('nem szabad várni')):
            ok, text = self._go(m)
        self.assertTrue(ok, text)
        self.assertTrue(any(isinstance(c, list) and 'powershell' in c and 'GetHelpElevated' in c[-1]
                            for c in m.calls))                    # a beragadt ablak bezárva
        self.assertTrue(any('DisplayLevel=False' in c for c in m.calls if isinstance(c, str)))

    def test_vedett_kulcs_nem_bukas_de_kimondja(self):
        def clean(run, log, protected=None, **k):
            protected.append(r'SOFTWARE\Microsoft\Office\Common\Smart Tag\Actions\{B7EFF951-E52F-45CC-9EF7-57124F2177CC}')
            return []
        m = FakeMachine(['Microsoft Office LTSC Professional Plus 2024 - hu-hu'], gethelp_removes=1)
        orig = m.installed
        m.installed = lambda: [(n, f'{c} REM {n}') for n, c in orig()]
        ok, text = wc.remove_office(m.run, lambda s: None, installed=m.installed, deep_clean=clean)
        self.assertTrue(ok, text)
        self.assertIn('⚠️', text)
        self.assertIn('Smart Tag', text)
        self.assertNotIn('maradék nélkül', text)

    def test_gethelp_hibasor_a_kepernyorol(self):
        # A felhasználó képernyőképéről, szó szerint.
        self.assertEqual(wc.gethelp_failure_line(['Running Office Uninstall. This may take a few minutes...',
                                                  'Office Uninstall failed.  There are no supported Office '
                                                  'products installed.\nPress any key to exit...']),
                         'Office Uninstall failed.  There are no supported Office products installed.')
        self.assertEqual(wc.gethelp_failure_line(['Scenario completed successfully.']), '')

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
        self.assertIn('nem volt telepített Office', text)
        self.assertEqual(m.calls, [])
        self.assertEqual(len(self.clean_calls), 1)  # a régi maradékot így is takarítja

    def test_nem_zip_letoltes_hiba(self):
        def bad_download(run_fn, url, dest, **k):
            with open(dest, 'w') as f:
                f.write('<html>')
        with mock.patch.object(wc, 'download_with_cert_fallback', bad_download):
            with self.assertRaises(RuntimeError):
                self._go(FakeMachine(['Microsoft 365']))


class BackgroundWaitTest(unittest.TestCase):
    """Terep, Build 427: "Uninstall office is running in the background." - meg kell várni."""

    def _clock(self):
        t = [0.0]
        return t, (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))

    def test_kivarja_amig_elfogy(self):
        t, clock, sleep = self._clock()
        states = iter([['A', 'B'], ['B'], []])
        left = wc._wait_background_uninstall(lambda m: None, lambda: next(states), ['A', 'B'],
                                             sleep=sleep, clock=clock)
        self.assertEqual(left, [])

    def test_hibas_gethelp_nem_var_5_percet(self):
        t, clock, sleep = self._clock()
        left = wc._wait_background_uninstall(lambda m: None, lambda: ['A'], ['A'], sleep=sleep, clock=clock,
                                             failed=lambda: 'There are no supported Office products installed.')
        self.assertEqual(left, ['A'])
        self.assertEqual(t[0], wc.BACKGROUND_POLL_SECS)      # egy kör után vége

    def test_ha_nem_valtozik_feladja(self):
        t, clock, sleep = self._clock()
        left = wc._wait_background_uninstall(lambda m: None, lambda: ['A'], ['A'], sleep=sleep, clock=clock)
        self.assertEqual(left, ['A'])
        self.assertLessEqual(t[0], wc.BACKGROUND_IDLE_SECS + wc.BACKGROUND_POLL_SECS)

    def test_a_remove_office_hatter_uzenetre_var(self):
        tmp = tempfile.mkdtemp(prefix='dv_officedel_')
        self.addCleanup(shutil.rmtree, tmp, True)

        def dl(run_fn, url, dest, **k):
            with zipfile.ZipFile(dest, 'w') as z:
                z.writestr('GetHelpCmd.exe', b'MZ')
        items = ['Microsoft 365']
        polls = []

        def installed():
            polls.append(1)
            if len(polls) > 3:  # a háttérben pár lekérdezés után eltűnik
                items.clear()
            return [(n, 'x') for n in items]

        def run(cmd, **k):
            return CommandResult(0, 'Scenario completed successfully.\nExecutionResultDetails: '
                                    'Uninstall office is running in the background.')
        with mock.patch.object(wc, '_app_data_dir', lambda: tmp), \
                mock.patch.object(wc, 'download_with_cert_fallback', dl), \
                mock.patch.object(wc, 'BACKGROUND_POLL_SECS', 0):
            ok, _ = wc.remove_office(run, lambda m: None, installed=installed,
                                     deep_clean=lambda r, l, **k: [])
        self.assertTrue(ok)


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


class OfficeDesktopShortcuts(unittest.TestCase):
    """2026-11-27: Office-ikonok az asztalra - a Start menü gyári parancsikonjai, változatlan névvel.
    A fájlnevek és célok a fejlesztői gép Start menüjéből (élőben mérve)."""

    def setUp(self):
        import os
        self.start = tempfile.mkdtemp(prefix='dv_officelnk_start_')
        self.desk = tempfile.mkdtemp(prefix='dv_officelnk_desk_')
        self.addCleanup(shutil.rmtree, self.start, True)
        self.addCleanup(shutil.rmtree, self.desk, True)
        os.makedirs(os.path.join(self.start, 'Microsoft Office-eszközök'))

    def _lnk(self, folder, name, target):
        import os
        with open(os.path.join(folder, name), 'wb') as f:      # a .lnk a célt UTF-16-ként is tárolja
            f.write(b'L\x00\x00\x00' + ('C:\\Program Files\\Microsoft Office\\root\\Office16\\' + target)
                    .encode('utf-16-le'))

    def test_csak_a_gyari_programok_valtozatlan_nevvel(self):
        import os
        for n, t in (('Word.lnk', 'WINWORD.EXE'), ('Excel.lnk', 'EXCEL.EXE'), ('Outlook (classic).lnk', 'OUTLOOK.EXE'),
                     ('Project.lnk', 'WINPROJ.EXE'), ('WordCraft.lnk', 'WordCraft.exe')):
            self._lnk(self.start, n, t)
        self._lnk(os.path.join(self.start, 'Microsoft Office-eszközök'), 'Project Server Accounts.lnk', 'WINPROJ.EXE')
        made, already, failed = wc.office_desktop_shortcuts([self.start], [self.desk], self.desk)
        self.assertEqual(sorted(os.listdir(self.desk)),
                         ['Excel.lnk', 'Outlook (classic).lnk', 'Project.lnk', 'Word.lnk'])
        self.assertEqual((already, failed), ([], []))
        # Másodszorra semmi nem duplázódik.
        made2, already2, _ = wc.office_desktop_shortcuts([self.start], [self.desk], self.desk)
        self.assertEqual(made2, [])
        self.assertEqual(sorted(already2), sorted(made))

    def test_ami_mar_kint_van_mas_neven_azt_sem_teszi_ki(self):
        import os
        self._lnk(self.start, 'Word.lnk', 'WINWORD.EXE')
        self._lnk(self.desk, 'Szövegszerkesztő.lnk', 'WINWORD.EXE')
        made, already, _ = wc.office_desktop_shortcuts([self.start], [self.desk], self.desk)
        self.assertEqual((made, already), ([], ['Word']))
        self.assertNotIn('Word.lnk', os.listdir(self.desk))

    def test_nincs_office(self):
        ok, text = wc.office_shortcuts_text(*wc.office_desktop_shortcuts([self.start], [self.desk], self.desk))
        self.assertFalse(ok)
        self.assertIn('Nincs telepített Office', text)


if __name__ == '__main__':
    unittest.main()
