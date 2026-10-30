"""Szervízprogramok könyvtára (2026-10-30): programonkénti csomagok, GitHub / Drive forrás,
a licences programok soha nem a nyilvános GitHubról, és a letöltő csak a kért csomagot hozza."""
import os
import re
import shutil
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

import tests  # noqa: F401
from app import toollib_core as tl
from app.benchmark_defs import BENCH_TOOLS
from app.gui import stress as stress_mod
from app.gui.stress import GuiStressMixin
from app.ramtest_core import RAMTEST_TOOLS
from app.stress_defs import STRESS_TOOLS

# A Sergei-csomagokból jött, licenckulcsos (vagy nem bizonyítottan ingyenes) programok:
# ezek NEM kerülhetnek a nyilvános GitHub-kiadásba.
LICENSED = {'aida64', 'hdsentinel', 'treesize', 'teracopy', 'totalcmd', 'teamviewer', 'wlu',
            'macrium', 'partitionwizard', 'occt'}
INTERNAL = ['smartctl', 'sumatrapdf', 'hpdriver']


class Catalog(unittest.TestCase):
    def test_minden_programnak_van_csomagja(self):
        for k in list(STRESS_TOOLS) + list(BENCH_TOOLS) + list(RAMTEST_TOOLS) + INTERNAL:
            self.assertIsNotNone(tl.package_for(k), k)

    def test_licences_programok_csak_drive_rol(self):
        drive = {p for p, (src, _) in tl.PACKAGES.items() if src == tl.SOURCE_DRIVE}
        self.assertEqual(drive, LICENSED)

    def test_felulet_drive_jelolese_egyezik(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'ui.html'), encoding='utf-8') as f:
            ui = f.read()
        block = ui[ui.index('const SERVICE_TOOLS = ['):]
        block = block[:block.index('];')]
        keys = re.findall(r"\{key:'(\w+)'", block)
        drive_ui = set(re.findall(r"\{key:'(\w+)'[^\n]*drive:true", block))
        self.assertEqual(set(keys), set(STRESS_TOOLS))
        self.assertEqual(drive_ui, {k for k in keys if tl.package_source(tl.package_for(k)) == tl.SOURCE_DRIVE})

    def test_packages_for_sorrend_es_ismetles(self):
        self.assertEqual(tl.packages_for(['tm5', 'testmem5', 'furmark', 'nincsilyen']), ['testmem5', 'furmark'])


class F(GuiStressMixin):
    def __init__(self):
        self._stresstools_download_lock = threading.Lock()
        self._run = None


def _fake_zip(path, files):
    with zipfile.ZipFile(path, 'w') as z:
        for name in files:
            z.writestr(name, 'x')


class Downloader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, tl.CACHE_DIRNAME)
        self.urls = []
        p1 = mock.patch.object(tl, 'cache_root', lambda temp_dir=None: self.root)
        p2 = mock.patch.object(stress_mod, 'download_with_cert_fallback', self._fake_dl)
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)
        self.api = F()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_dl(self, run, url, dest, **kw):
        self.urls.append(url)
        _fake_zip(dest, ['furmark.exe'] if 'furmark' in url else ['aida64.exe', 'pkey.txt'])

    def test_csak_a_kert_csomag_jon_le_es_cachelodik(self):
        root = self.api._download_stresstools(keys=['furmark'])
        self.assertEqual(root, self.root)
        self.assertEqual(self.urls, [tl.GITHUB_BASE_URL + 'furmark.zip'])
        self.assertTrue(os.path.exists(os.path.join(self.root, 'FurMark', 'furmark.exe')))
        self.assertEqual(self.api._find_stress_tool_exes(root, ['furmark'])['furmark'],
                         os.path.join(self.root, 'FurMark', 'furmark.exe'))
        self.api._download_stresstools(keys=['furmark'])
        self.assertEqual(len(self.urls), 1)          # másodszor nincs letöltés
        self.assertFalse(any(f.endswith('.zip') for f in os.listdir(self.tmp)))   # a zip törölve

    def test_drive_jelszo_nelkul_nem_tolt_le(self):
        self.assertIsNone(self.api._download_stresstools(keys=['aida64']))
        self.assertIn('jelszó', self.api._toollib_last_error)
        self.assertEqual(self.urls, [])

    def test_drive_jelszoval_a_fajl_azonositojaval(self):
        sent = []

        def post(run, url, payload, http=None):
            sent.append(payload)
            return {'ok': True, 'id': 'ABC123'}, ''
        with mock.patch.object(stress_mod.logupload_core, '_post', post):
            root = self.api._download_stresstools(keys=['aida64'], password='titok')
        self.assertEqual(root, self.root)
        self.assertEqual(sent, [{'action': 'toolfile', 'pw': 'titok', 'name': 'aida64.zip'}])
        self.assertEqual(self.urls, [tl.drive_url('ABC123')])
        self.assertTrue(tl.package_ready(self.root, 'aida64'))

    def test_nem_zip_valasz_hiba_es_nincs_jelzo(self):
        def html(run, url, dest, **kw):
            with open(dest, 'w') as f:
                f.write('<html>Sign in</html>')
        with mock.patch.object(stress_mod, 'download_with_cert_fallback', html):
            self.assertIsNone(self.api._download_stresstools(keys=['furmark']))
        self.assertFalse(tl.package_ready(self.root, 'furmark'))
        self.assertIn('nem zip', self.api._toollib_last_error)

    def _post_ok(self, sent):
        def post(run, url, payload, http=None):
            sent.append(payload['pw'])
            return ({'ok': True, 'id': 'ID'}, '') if payload['pw'] == 'jo' else ({}, 'hibás jelszó')
        return post

    def _fake_store(self):
        """A Windows Hitelesítőadat-kezelő csonkja (a teszt ne írjon a valódi tárba)."""
        store = {}
        return store, [mock.patch.object(tl, 'load_password', lambda: store.get('pw', '')),
                       mock.patch.object(tl, 'save_password', lambda p: store.__setitem__('pw', p)),
                       mock.patch.object(tl, 'forget_password', lambda: store.pop('pw', None) or True)]

    def test_jelszo_megjegyzese_es_hasznalata(self):
        sent = []
        store, patches = self._fake_store()
        for p in patches + [mock.patch.object(stress_mod.logupload_core, '_post', self._post_ok(sent))]:
            p.start()
            self.addCleanup(p.stop)
        self.assertIsNone(self.api._download_stresstools(keys=['aida64'], password='rossz'))
        self.assertEqual(store, {})                          # rossz jelszót nem jegyez meg
        self.assertTrue(self.api._download_stresstools(keys=['aida64'], password='jo'))
        self.assertEqual(store, {'pw': 'jo'})
        shutil.rmtree(tl.package_dir(self.root, 'aida64'))
        self.assertTrue(self.api._download_stresstools(keys=['aida64']))   # beírás nélkül
        self.assertEqual(sent[-1], 'jo')
        self.assertTrue(tl.CRED_TARGET)
        self.assertNotIn('DriverVarazslo\\', tl.CRED_TARGET)   # nem fájl a programmappában

    def test_mikor_kell_jelszo(self):
        from app.gui.toolsinstall import GuiToolsInstallMixin

        class T(GuiToolsInstallMixin):
            def _scan_installed_tools(self, keys):
                return {'totalcmd': ('x.exe', 'x')}
        store, patches = self._fake_store()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(tl.package_dir(self.root, 'treesize'))
        tl.mark_package_ready(self.root, 'treesize')            # már letöltve: nem kell
        api = T()
        api._toollib_check_password = lambda pw, key: ((True, '') if pw == 'jo' else (False, 'hibás jelszó'))
        keys = ['cpuz', 'aida64', 'treesize', 'totalcmd']
        self.assertEqual(api.toollib_password_needed(keys), ['AIDA64', 'Total Commander'])
        self.assertEqual(api.toollib_password_needed(keys, launch=True), ['AIDA64'])   # telepített indul
        self.assertEqual(api.toollib_password_needed(['cpuz']), [])                    # ingyenes
        store['pw'] = 'jo'
        self.assertEqual(api.toollib_password_needed(keys), [])                        # jó, megjegyezve
        # Build 400: a megjegyzett jelszó azóta megváltozott -> elfelejti és kérdez
        store['pw'] = 'regi'
        self.assertEqual(api.toollib_password_needed(keys), ['AIDA64', 'Total Commander'])
        self.assertEqual(store, {})
        # a beírt jelszó: rossz -> nem menti; jó -> menti
        self.assertFalse(api.toollib_verify_password('rossz', keys)['ok'])
        self.assertEqual(store, {})
        self.assertTrue(api.toollib_verify_password('jo', keys)['ok'])
        self.assertEqual(store, {'pw': 'jo'})
        # hálózati hibánál nem kérünk új jelszót (az nem segítene)
        api._toollib_check_password = lambda pw, key: (False, 'a szerver válasza nem JSON')
        self.assertEqual(api.toollib_password_needed(keys), [])

    def test_regi_cache_torlodik(self):
        legacy = os.path.join(self.tmp, tl.LEGACY_CACHE_DIRNAME, 'stresstools', 'FurMark')
        os.makedirs(legacy)
        self.api._download_stresstools(keys=['furmark'])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, tl.LEGACY_CACHE_DIRNAME)))


class CallLogRedaction(unittest.TestCase):
    """A hívásnaplóba soha nem kerül jelszó (a napló felmegy a Drive-ra)."""

    def test_jelszo_nem_kerul_a_naplora(self):
        from app import common

        class A:
            def letolt(self, keys, password=''):
                return 'ok'
        f = common._make_logged('A', A.letolt)
        # más tesztek globálisan letiltják a naplózást - erre a tesztre visszakapcsoljuk
        import logging
        prev = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, prev)
        with self.assertLogs(level='INFO') as cm:
            f(A(), ['aida64'], 'titok123')
            f(A(), ['cpuz'], password='titok456')
        txt = '\n'.join(cm.output)
        self.assertNotIn('titok', txt)
        self.assertIn("['aida64'], ***", txt)


if __name__ == '__main__':
    unittest.main()
