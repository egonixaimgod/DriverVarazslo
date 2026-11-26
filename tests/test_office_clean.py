"""Az Office maradékainak takarítója (`officeclean_core`, 2026-11-26): mappa és registry
törlése a megtartandó részek kímélésével, .pst/.ost soha, és a két jelölőnégyzet hatása.
A registry-tesztek egy eldobható HKCU\\Software\\DV_TEST_* kulcson futnak (admin nélkül írható)."""
import os
import shutil
import tempfile
import unittest
import uuid
import winreg
from unittest import mock

from app.common import CommandResult

from app import officeclean_core as oc


class DirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dv_officeclean_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _mk(self, rel, data='x'):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(data)
        return p

    def test_mindent_torol_de_a_pst_ost_marad(self):
        root = os.path.join(self.tmp, 'Outlook')
        self._mk(r'Outlook\outlprnt')
        pst = self._mk(r'Outlook\regi\archiv.pst')
        oc.delete_dir(root)
        self.assertTrue(os.path.exists(pst))
        self.assertFalse(os.path.exists(os.path.join(root, 'outlprnt')))
        self.assertFalse(oc._dir_left(root, ()))  # csak .pst maradt: ez nem maradék

    def test_megtartott_alut_marad(self):
        root = os.path.join(self.tmp, 'Office')
        lic = self._mk(r'Office\Licenses\5\token.dat')
        lic2 = self._mk(r'Office\16.0\Licensing\a.bin')
        self._mk(r'Office\16.0\MRU\b.dat')
        self._mk(r'Office\cache.dat')
        keep = ('Licenses', os.path.join('16.0', 'Licensing'))
        oc.delete_dir(root, keep)
        self.assertTrue(os.path.exists(lic) and os.path.exists(lic2))
        self.assertFalse(os.path.exists(os.path.join(root, '16.0', 'MRU')))
        self.assertFalse(os.path.exists(os.path.join(root, 'cache.dat')))
        self.assertFalse(oc._dir_left(root, keep))
        self.assertTrue(oc._dir_left(root, ()))  # megtartás nélkül a licenc maradék lenne

    def test_felhasznaloi_fajl_soha_nem_torlodik(self):
        # explicit user decision, 2026-11-26: „word fileokat vagy bármit amit csak office al
        # csináltak” - csak a program mehet. Egy profil Office-mappái, valódi fájlnevekkel.
        prof = os.path.join(self.tmp, 'Users', 'X')
        roam = os.path.join('Users', 'X', 'AppData', 'Roaming', 'Microsoft')
        local = os.path.join('Users', 'X', 'AppData', 'Local', 'Microsoft', 'Office')
        keep = [self._mk(os.path.join(roam, 'Word', 'dijbekero~1.asd')),          # Word helyreállítás
                self._mk(os.path.join(roam, 'Word', 'Díjbekérő 2026.docx')),
                self._mk(os.path.join(roam, 'Word', 'STARTUP', 'sajat.dotm')),
                self._mk(os.path.join(roam, 'Excel', 'XLSTART', 'PERSONAL.XLSB')),    # saját makrók
                self._mk(os.path.join(roam, 'Excel', 'konyveles.xlsx')),
                self._mk(os.path.join(roam, 'Office', 'Recent', 'szamla.pdf.lnk')),
                self._mk(os.path.join(roam, 'Outlook', 'archiv.pst')),
                self._mk(os.path.join(roam, 'Outlook', 'level.msg')),
                self._mk(os.path.join(local, 'UnsavedFiles', 'Dokumentum1.docx')),    # nem mentett
                self._mk(os.path.join(local, '16.0', 'OfficeFileCache', 'x.fsd')),     # OneDrive-ra vár
                self._mk(os.path.join(local, 'kep.png')),
                self._mk(os.path.join(roam, 'UProof', 'CUSTOM.DIC')),                  # saját szótár
                self._mk(os.path.join(roam, 'Office', 'MSO1038.acl')),                 # autojavítás
                self._mk(os.path.join(roam, 'Outlook', 'szabalyok.rwz'))]
        gone = [self._mk(os.path.join(roam, 'Word', 'Normal.settings.bin')),
                self._mk(os.path.join(roam, 'Excel', 'Excel16.xlb')),
                self._mk(os.path.join(local, '16.0', 'MRU', 'cache.dat'))]
        # a dokumentumok mappája egy csomóponton át elérhető: nem követjük
        docs = os.path.join(self.tmp, 'Docs')
        doc = self._mk(os.path.join('Docs', 'fontos.dat'))
        junction = os.path.join(prof, 'AppData', 'Local', 'Microsoft', 'Office', 'link')
        os.system(f'mklink /J "{junction}" "{docs}" >nul 2>&1')

        for path, k in oc.user_dirs(prof, False, False):
            oc.delete_dir(path, k)
        for p in keep + [doc]:
            self.assertTrue(os.path.exists(p), f'TÖRÖLTE: {p}')
        for p in gone:
            self.assertFalse(os.path.exists(p), f'nem törölte: {p}')
        self.assertFalse(any(oc._dir_left(p, k) for p, k in oc.user_dirs(prof, False, False)))

    def test_program_files_alatt_a_program_megy_a_dokumentum_nem(self):
        pf = os.path.join(self.tmp, 'PF')
        tpl = self._mk(os.path.join('PF', 'Microsoft Office', 'root', 'Templates', '1038', 'Normal.dotx'))
        doc = self._mk(os.path.join('PF', 'Microsoft Office', 'ide mentettem.docx'))
        with mock.patch.dict(os.environ, {'ProgramW6432': pf, 'ProgramFiles': pf}):
            self.assertTrue(oc._program_area(tpl))
            oc.delete_dir(os.path.join(pf, 'Microsoft Office'))
        self.assertFalse(os.path.exists(tpl))
        self.assertTrue(os.path.exists(doc))

    def test_a_kapu_dokumentumot_semmikepp_nem_torol(self):
        for ext in ('.docx', '.DOCX', '.xlsx', '.pptx', '.ppt', '.pdf', '.pst', '.msg'):
            p = self._mk('x' + ext)
            with self.assertRaises(PermissionError):
                oc._remove_file(p)
            self.assertTrue(os.path.exists(p))
        # közvetlen fájl-célként (pl. parancsikon helyett) sem
        p = self._mk(os.path.join('Desktop', 'számla.docx'))
        oc.delete_dir(p)
        self.assertTrue(os.path.exists(p))

    def test_csak_a_kapu_torol_fajlt(self):
        # forrás-teszt: a modulban fájlt csak `_remove_file` törölhet
        import inspect
        import re as _re
        src = inspect.getsource(oc)
        body = inspect.getsource(oc._remove_file)
        rest = src.replace(body, '')
        self.assertFalse(_re.search(r'os\.(remove|unlink)\(|shutil\.(rmtree|move)|rmtree|Remove-Item|'
                                    r'del /|rd /s|os\.rename|os\.replace', rest))
        # a shutil csak másol és helyet kérdez (a dokumentum-mentés)
        self.assertEqual(set(_re.findall(r'shutil\.(\w+)', rest)), {'copy2', 'disk_usage'})

    def test_parancsikon_fajl(self):
        lnk = self._mk(r'Programs\Word.lnk')
        self.assertEqual(oc._office_shortcuts(os.path.dirname(lnk)), [(lnk, ())])
        self.assertTrue(oc._dir_left(lnk, ()))
        oc.delete_dir(lnk)
        self.assertFalse(os.path.exists(lnk))

    def test_asztal_csak_office_celu_lnk(self):
        word = self._mk(r'Desktop\Word.lnk', 'C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE')
        self._mk(r'Desktop\Excel.lnk', 'D:\\sajat\\excel_tabla.xlsx')  # saját fájl, csak a neve „Excel”
        with open(os.path.join(self.tmp, 'Desktop', 'Outlook.lnk'), 'wb') as f:
            f.write('C:\\Program Files\\Microsoft Office\\OUTLOOK.EXE'.encode('utf-16-le'))
        self._mk(r'Desktop\Szövegszerkesztő.lnk', 'C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE')
        self._mk(r'Desktop\Chrome.lnk', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe')
        found = sorted(os.path.basename(p) for p, _ in oc._office_lnks_checked(os.path.dirname(word)))
        self.assertEqual(found, ['Outlook.lnk', 'Szövegszerkesztő.lnk', 'Word.lnk'])

    def test_nem_office_parancsikon_marad(self):
        d = os.path.dirname(self._mk(r'Programs\Wordpad.lnk'))
        self._mk(r'Programs\Microsoft Office Tools\x.lnk')
        found = [os.path.basename(p) for p, _ in oc._office_shortcuts(d)]
        self.assertEqual(found, ['Microsoft Office Tools'])


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.base = rf'Software\DV_TEST_{uuid.uuid4().hex}'
        self.addCleanup(lambda: oc.delete_key(winreg.HKEY_CURRENT_USER, self.base))
        for sub in (r'16.0\Common\Licensing', r'16.0\Common\Identity\Ids', r'16.0\Word\Options', 'Outlook'):
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf'{self.base}\{sub}') as k:
                winreg.SetValueEx(k, 'v', 0, winreg.REG_SZ, 'x')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf'{self.base}\16.0\Common') as k:
            winreg.SetValueEx(k, 'common', 0, winreg.REG_SZ, 'x')

    def _exists(self, sub):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf'{self.base}\{sub}'))
            return True
        except OSError:
            return False

    def test_teljes_torles(self):
        errs = oc.delete_key(winreg.HKEY_CURRENT_USER, self.base)
        self.assertEqual(errs, [])
        self.assertFalse(self._exists(''))
        self.assertFalse(oc._key_left(winreg.HKEY_CURRENT_USER, self.base, ()))

    def test_aktivalas_megtartasa(self):
        keep = (r'16.0\Common\Licensing', r'16.0\Common\Identity')
        oc.delete_key(winreg.HKEY_CURRENT_USER, self.base, keep)
        self.assertTrue(self._exists(r'16.0\Common\Licensing'))
        self.assertTrue(self._exists(r'16.0\Common\Identity\Ids'))
        self.assertFalse(self._exists(r'16.0\Word'))
        self.assertFalse(self._exists('Outlook'))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf'{self.base}\16.0\Common') as k:
            self.assertEqual(winreg.QueryInfoKey(k)[1], 0)  # az ős értékei törölve
        self.assertFalse(oc._key_left(winreg.HKEY_CURRENT_USER, self.base, keep))
        self.assertTrue(oc._key_left(winreg.HKEY_CURRENT_USER, self.base, ()))


class CheckboxTest(unittest.TestCase):
    """A két jelölőnégyzet minden kombinációja: mi törlődik és mi marad."""

    def _all(self, ks, ka):
        dirs = [p for p, _ in oc.user_dirs(r'C:\Users\X', ks, ka)]
        dirs += [p for p, _ in oc.machine_dirs(ka)]
        keeps = [k for _, k in oc.user_dirs(r'C:\Users\X', ks, ka)]
        rules = oc.user_key_rules(ks, ka)
        mkeys = oc.machine_keys(ka)
        return dirs, keeps, rules, mkeys

    def test_sablon_alairas_mappa_soha_nem_cel(self):
        for ks in (False, True):
            for ka in (False, True):
                dirs, *_ = self._all(ks, ka)
                self.assertFalse([p for p in dirs if os.path.basename(p).lower() in
                                  ('templates', 'signatures', 'document building blocks', 'documents',
                                   'onedrive', 'downloads')], (ks, ka))

    def test_semmi_nem_marad(self):
        dirs, keeps, rules, mkeys = self._all(False, False)
        self.assertIn(r'C:\Users\X\AppData\Roaming\Microsoft\Outlook', dirs)
        self.assertIn(r'C:\Users\X\AppData\Local\Microsoft\Office', dirs)
        self.assertTrue(all(k == () for k in keeps))
        self.assertEqual(rules, [(r'Software\Microsoft\Office', ())])
        self.assertTrue(any('OfficeSoftwareProtectionPlatform' in p for p, _ in mkeys))
        self.assertTrue(any('OfficeSoftwareProtectionPlatform' in p for p in dirs))
        # az .ost helye soha nem cél
        self.assertFalse(any(p.endswith(r'Local\Microsoft\Outlook') for p in dirs))

    def test_aktivalas_marad(self):
        dirs, keeps, rules, mkeys = self._all(False, True)
        self.assertIn('Licenses', [k for kk in keeps for k in kk])
        self.assertIn(r'16.0\Common\Licensing', rules[0][1])
        self.assertFalse(any('OfficeSoftwareProtectionPlatform' in p for p, _ in mkeys))
        self.assertFalse(any('OfficeSoftwareProtectionPlatform' in p for p in dirs))

    def test_fiokok_maradnak(self):
        dirs, keeps, rules, mkeys = self._all(True, False)
        self.assertFalse(any(r'Roaming\Microsoft\Outlook' in p for p in dirs))
        self.assertIn(r'C:\Users\X\AppData\Local\Microsoft\Office\Licenses', dirs)
        self.assertTrue(all(r'\Common\L' in p or r'\Common\I' in p for p, _ in rules))

    def test_mindketto_marad(self):
        dirs, keeps, rules, mkeys = self._all(True, True)
        self.assertEqual(rules, [])
        self.assertFalse(any(r'\Users\X' in p for p in dirs))


class BackupTest(unittest.TestCase):
    """Dokumentum-mentés az eltávolítás előtt (explicit user decision, 2026-11-26)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dv_officebackup_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prof = os.path.join(self.tmp, 'Users', 'X')
        self.dest = os.path.join(self.tmp, 'DriverVarazslo')

    def _mk(self, rel, data='tartalom'):
        p = os.path.join(self.prof, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(data)
        return p

    def test_menti_a_dokumentumokat_az_eredeti_szerkezettel(self):
        docs = [self._mk(r'Documents\Díjbekérő 2026.docx', 'díjbekérő'),
                self._mk(r'Desktop\könyvelés.xlsx'),
                self._mk(r'Downloads\bemutato.pptx'),
                self._mk(r'Documents\szamla.pdf'),
                self._mk(r'Documents\Outlook-fájlok\archiv.pst'),
                self._mk(r'AppData\Roaming\Microsoft\Templates\sajat.dotx'),
                self._mk(r'AppData\Local\Microsoft\Office\UnsavedFiles\Dokumentum1.docx')]
        skipped = [self._mk(r'AppData\Local\Microsoft\Outlook\posta.ost'),     # szerver-másolat
                   self._mk(r'AppData\Local\Programs\vscode\minta.docx'),      # nem Microsoft AppData
                   self._mk(r'Documents\jegyzet.txt'),
                   self._mk(r'Pictures\kep.jpg')]
        ok, dest = oc.backup_documents(lambda m: None, [self.prof], self.dest)
        self.assertTrue(ok, dest)
        for p in docs:
            copy = os.path.join(dest, os.path.splitdrive(p)[1].lstrip('\\'))
            self.assertTrue(os.path.isfile(copy), copy)
        with open(os.path.join(dest, os.path.splitdrive(docs[0])[1].lstrip('\\')), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'díjbekérő')
        for p in skipped:
            self.assertFalse(os.path.exists(os.path.join(dest, os.path.splitdrive(p)[1].lstrip('\\'))), p)
        self.assertTrue(os.path.isfile(os.path.join(dest, 'INFO.txt')))
        self.assertTrue(all(os.path.exists(p) for p in docs))  # az eredeti is megvan

    def test_nincs_eleg_hely(self):
        self._mk(r'Documents\a.docx')
        with mock.patch.object(oc.shutil, 'disk_usage', return_value=mock.Mock(free=10)):
            ok, text = oc.backup_documents(lambda m: None, [self.prof], self.dest)
        self.assertFalse(ok)
        self.assertIn('Nincs elég hely', text)

    def test_nincs_mit_menteni(self):
        os.makedirs(self.prof)
        ok, text = oc.backup_documents(lambda m: None, [self.prof], self.dest)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.dest))


class OfficeHubTest(unittest.TestCase):
    def test_eltavolit_es_visszaolvas(self):
        calls = []

        def run(cmd, **k):
            calls.append(cmd[-1])
            return CommandResult(0, '0' if '.Count' in cmd[-1] else '')
        self.assertEqual(oc.remove_office_hub(run, lambda m: None), [])
        self.assertIn('Remove-AppxPackage', calls[0])
        self.assertIn('Remove-AppxProvisionedPackage', calls[0])

    def test_ha_fent_maradt(self):
        run = lambda cmd, **k: CommandResult(0, '2' if '.Count' in cmd[-1] else '')
        self.assertTrue(oc.remove_office_hub(run, lambda m: None))

    def test_ha_nem_olvashato(self):
        run = lambda cmd, **k: CommandResult(1, '')
        self.assertTrue(oc.remove_office_hub(run, lambda m: None))


if __name__ == '__main__':
    unittest.main()
