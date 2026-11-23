"""Operációs rendszer / Windows Update gombok (2026-11-03, explicit user decision).

- Egyik gomb sem indít frissítés-keresést (a régi „Szolgáltatások újraindítása”
  `wuauclt /detectnow` + `UsoClient StartScan`-t futtatott, ami letöltötte a szemetet).
- A verdikt a visszaolvasott UX\\Settings + házirend (ezt használja a WU és a Beállítások
  oldal). A vezénylő PolicyState-je NEM verdikt: 2026-11-03-án terepen (ASUS, 30 napos szünet)
  a Windows a szünetet mutatta és betartotta, a PolicyState mégis újraindítások után is a
  régit őrizte, és a rá épülő „még nem vette át” kiírás hamis volt.
- Újraindítás csak a program SAJÁT okából (zárolt gyorsítótár, beragadt szolgáltatás,
  winsock); akkor 10 mp-es visszaszámlálás + befejező feladat, újraindítás után nincs újabb
  újraindítás, csak őszinte jelentés. A Windows saját újraindítás-jelzője csak tájékoztatás.
"""
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import wusettings_core as wc
from app.gui import wu as guiwu


def _res(out='', rc=0):
    return SimpleNamespace(stdout=out, stderr='', returncode=rc)


STOP_OUT = "STOP|UsoSvc|OK\nSTOP|wuauserv|KILLED|1234|Stopped\nSTOP|bits|ALREADY\nSTOP|cryptsvc|OK\nDEPS|AppIDSvc\n"
PURGE_OK = {'ok': True, 'moved': 3, 'locked': [], 'bg_delete': True}
START_OUT = ("START|cryptsvc|Running\nSTART|bits|Running\nSTART|wuauserv|Running\n"
             "START|UsoSvc|Running\nSTART|AppIDSvc|Running\n")


class ParseAndStateTest(unittest.TestCase):
    def test_parse_service_lines(self):
        p = wc.parse_service_lines(STOP_OUT + "STOPERR|bits|Access denied\n")
        self.assertEqual(p['stop']['wuauserv'], 'KILLED')
        self.assertEqual(p['deps'], ['AppIDSvc'])
        self.assertEqual(p['errors'], ['bits: Access denied'])

    def test_allapot_leiras_a_beallitottbol(self):
        self.assertEqual(wc.describe_state({'paused_until': '2026-12-03T16:00:00Z', 'exclude_drivers': True}),
                         'szüneteltetve 2026-12-03-ig · driver-frissítés LETILTVA')
        self.assertEqual(wc.describe_state({'paused_until': None, 'exclude_drivers': False}),
                         'NINCS szüneteltetve · driver-frissítés engedélyezve')

    def test_rebooted_since(self):
        self.assertTrue(wc.rebooted_since(1000.0, now=5000.0, uptime=100.0))
        self.assertFalse(wc.rebooted_since(4950.0, now=5000.0, uptime=100.0))
        self.assertIsNone(wc.rebooted_since(None, now=5000.0, uptime=100.0))

    def test_pause_end_extend(self):
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(days=20)).strftime(wc._ISO_FMT)
        with mock.patch.object(wc, 'wu_paused_until', return_value=future):
            ext = wc.pause_end_for(7, extend=True)
            fresh = wc.pause_end_for(7, extend=False)
        self.assertGreater(ext - fresh, timedelta(days=19))


class RepairTest(unittest.TestCase):
    def setUp(self):
        quiet()
        self.cmds = []

    def _run(self, cmd, **k):
        self.cmds.append(cmd if isinstance(cmd, str) else ' '.join(map(str, cmd)))
        txt = self.cmds[-1]
        if 'Stop-Service' in txt:
            return _res(STOP_OUT)
        if 'Start-Service' in txt:
            return _res(START_OUT)
        return _res()

    def test_soha_nem_keres_es_a_fuggoket_visszainditja(self):
        with mock.patch.object(wc, 'purge_software_distribution', return_value=PURGE_OK):
            rep = wc.repair_wu(self._run, lambda m: None)
        joined = '\n'.join(self.cmds)
        for bad in ('StartScan', 'detectnow', 'ScanInstallWait', 'StartDownload'):
            self.assertNotIn(bad, joined)
        self.assertIn("'AppIDSvc'", joined)       # a leállított függő visszaindul
        self.assertTrue(rep['services_ok'])
        self.assertEqual(rep['reboot_reasons'], [])

    def test_letiltott_szolgaltatas_problema(self):
        out = START_OUT.replace('START|wuauserv|Running', 'START|wuauserv|DISABLED')
        run = lambda cmd, **k: _res(out if 'Start-Service' in str(cmd) else STOP_OUT)
        with mock.patch.object(wc, 'purge_software_distribution', return_value=PURGE_OK):
            rep = wc.repair_wu(run, lambda m: None)
        self.assertFalse(rep['services_ok'])
        self.assertTrue(any('LE VAN TILTVA' in p for p in rep['problems']))

    def test_valasz_nelkul_nem_hamis_siker(self):
        with mock.patch.object(wc, 'purge_software_distribution', return_value=PURGE_OK):
            rep = wc.repair_wu(lambda *a, **k: _res(''), lambda m: None)
        self.assertFalse(rep['services_ok'])


class PurgeTest(unittest.TestCase):
    """Terepi hiba (2026-11-03, ASUS Vivobook, Win10): 735 729 fájl, 260 karakternél hosszabb
    útvonalak - a fájlonkénti törlés/ütemezés ~40 óra lett volna. Most: félretétel + háttér-rd."""

    def setUp(self):
        quiet()
        self.base = tempfile.mkdtemp()
        self.root = os.path.join(self.base, 'SoftwareDistribution')
        deep = os.path.join(self.root, 'Download', 'x' * 60)
        for i in range(4):                       # > 260 karakteres útvonal
            deep = os.path.join(deep, 'amd64_microsoft-windows-e..ent-winrt.resources_' + str(i) * 20)
        os.makedirs('\\\\?\\' + deep)
        for j in range(50):
            open('\\\\?\\' + os.path.join(deep, f'f{j}.mui'), 'w').close()
        os.makedirs(os.path.join(self.root, 'DataStore'))
        open(os.path.join(self.root, 'ReportingEvents.log'), 'w').close()
        self.cmds = []

    def tearDown(self):
        import subprocess
        subprocess.run(['cmd', '/c', 'rd', '/s', '/q', '\\\\?\\' + self.base], capture_output=True)

    def _run(self, cmd, **k):
        self.cmds.append(cmd[-1] if isinstance(cmd, list) else cmd)
        return _res('OK\n')

    def test_felretesz_es_hatterben_torol(self):
        r = wc.purge_software_distribution(self._run, root=self.root)
        self.assertTrue(r['ok'])
        self.assertEqual(r['moved'], 3)
        self.assertEqual(os.listdir(self.root), [])          # a WU üres gyorsítótárat lát
        self.assertTrue(r['bg_delete'])
        self.assertIn('rd /s /q "\\\\?\\' + self.root + wc.WU_TRASH_SUFFIX + '"', self.cmds[0])
        self.assertIn('-WindowStyle Hidden', self.cmds[0])

    def test_zarolt_elem_bent_marad_es_ujrainditasi_ok(self):
        real = os.rename

        def ren(a, b):
            if a.endswith('DataStore'):
                raise PermissionError(32, 'used by another process')
            real(a, b)
        with mock.patch.object(wc.os, 'rename', side_effect=ren):
            r = wc.purge_software_distribution(self._run, root=self.root)
        self.assertFalse(r['ok'])
        self.assertEqual(r['locked'], ['DataStore'])
        self.assertEqual(r['moved'], 2)

    def test_indulaskor_folytatja_a_felbehagyott_torlest(self):
        self.assertIsNone(wc.resume_trash_delete(self._run, root=self.root))   # nincs mit
        os.makedirs(self.root + wc.WU_TRASH_SUFFIX)
        self.assertTrue(wc.resume_trash_delete(self._run, root=self.root))
        self.assertIn('rd /s /q', self.cmds[-1])

    def test_a_fix_lanca_is_az_uj_kiuritest_hivja(self):
        import inspect
        from app.gui import autofix
        src = inspect.getsource(autofix)
        self.assertIn('purge_software_distribution(', src)
        self.assertNotIn('clear_software_distribution(', src)

    def test_hatter_torles_bukasa_nem_hamis_siker(self):
        r = wc.purge_software_distribution(lambda *a, **k: _res('', 1), root=self.root)
        self.assertFalse(r['bg_delete'])

    def test_hiba_utan_is_visszaindulnak_a_szolgaltatasok(self):
        cmds = []
        run = lambda cmd, **k: (cmds.append(str(cmd)), _res(START_OUT if 'Start-Service' in str(cmd) else STOP_OUT))[1]
        with mock.patch.object(wc, 'purge_software_distribution', side_effect=RuntimeError('x')):
            with self.assertRaises(RuntimeError):
                wc.repair_wu(run, lambda m: None)
        self.assertTrue(any('Start-Service' in c for c in cmds))

    def test_nincs_fajlonkenti_torles_es_registry_utemezes(self):
        import inspect
        src = inspect.getsource(wc.repair_wu) + inspect.getsource(wc._repair_cleanup) + \
            inspect.getsource(wc.purge_software_distribution)
        for bad in ('MoveFileEx', 'Remove-Item', 'clear_software_distribution('):
            self.assertNotIn(bad, src)
        purge = inspect.getsource(wc.purge_software_distribution)
        for bad in ('rmtree', 'os.walk', 'os.remove'):
            self.assertNotIn(bad, purge)


class _Wu(guiwu.GuiWuMixin):
    target_os_path = None

    def __init__(self):
        self.events, self.cmds = [], []
        self._cancel_flag = False

    def emit(self, ev, data=None):
        self.events.append((ev, data or {}))

    def _run(self, cmd, **k):
        self.cmds.append(cmd)
        return _res(f'ARGS={guiwu.WU_FINISH_FLAG}\n')

    def _safe_thread(self, name, fn):
        fn()

    def statuses(self):
        return [d.get('status') for e, d in self.events if e == 'task_complete']

    def shutdowns(self):
        return [c for c in self.cmds if isinstance(c, list) and c[:1] == ['shutdown']]


OK_REP = {'services_ok': True, 'problems': [], 'cache': {'ok': True}, 'reboot_reasons': []}


LOCKED_REP = dict(OK_REP, reboot_reasons=['a WU-gyorsítótár egy része zárolva (DataStore)'])


class ConcludeTest(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.object(wc, '_pending_path', return_value=os.path.join(self.tmp, 'wu_pending.json'))
        p.start()
        self.addCleanup(p.stop)
        for name, val in (('wu_reboot_flags', []), ('desired_state', {'paused_until': '2026-12-03T16:00:00Z',
                                                                       'exclude_drivers': True})):
            q = mock.patch.object(wc, name, return_value=val)
            q.start()
            self.addCleanup(q.stop)
        s = mock.patch.object(guiwu.time, 'sleep', lambda s: None)
        s.start()
        self.addCleanup(s.stop)

    def _eff(self, paused):
        return mock.patch.object(wc, 'read_effective_state',
                                 return_value={'paused_until': paused, 'exclude_drivers': True})

    def test_kesz_ujrainditas_nelkul(self):
        w = _Wu()
        with self._eff('2026-12-03T16:00:00Z'):
            w._wu_conclude('pause_wu', 'X', OK_REP, finishing=False)
        self.assertEqual(w.statuses()[-1], '✅ Kész · szüneteltetve 2026-12-03-ig · driver-frissítés LETILTVA')
        self.assertEqual(w.shutdowns(), [])
        self.assertIsNone(wc.load_pending())

    def test_asus_eset_elavult_policystate_nem_hamis_hiba(self):
        # Terep 2026-11-03: 30 napos szünet, a Windows betartja, a PolicyState a régit őrzi.
        w = _Wu()
        with self._eff('2026-11-17T16:08:32Z'):
            w._wu_conclude('pause_wu', 'X', OK_REP, finishing=False)
        self.assertTrue(w.statuses()[-1].startswith('✅'))
        self.assertEqual(w.shutdowns(), [])
        self.assertNotIn('nem vette át', ' '.join(str(d) for _, d in w.events))

    def test_windows_ujrainditas_jelzo_csak_tajekoztatas(self):
        w = _Wu()
        with mock.patch.object(wc, 'wu_reboot_flags', return_value=['a Windows Update újraindítást vár (RebootRequired)']), \
                self._eff(None):
            w._wu_conclude('pause_wu', 'X', OK_REP, finishing=False)
        self.assertEqual(w.shutdowns(), [])
        self.assertTrue(w.statuses()[-1].startswith('✅'))

    def test_sajat_ok_ujrainditas_es_befejezo_feladat(self):
        w = _Wu()
        with self._eff(None):
            w._wu_conclude('pause_wu', 'X', LOCKED_REP, finishing=False)
        self.assertEqual(len(w.shutdowns()), 1)
        self.assertIsNotNone(wc.load_pending())
        ps = next(c[-1] for c in w.cmds if isinstance(c, list) and 'Register-ScheduledTask' in c[-1])
        self.assertIn(guiwu.WU_FINISH_TASK, ps)
        self.assertIn('-AllowStartIfOnBatteries', ps)
        self.assertNotIn('✅', ' '.join(w.statuses()))

    def test_megszakitas_nem_indit_ujra(self):
        w = _Wu()
        w._cancel_flag = True
        with self._eff(None):
            w._wu_conclude('pause_wu', 'X', LOCKED_REP, finishing=False)
        self.assertEqual(w.shutdowns(), [])
        self.assertIsNotNone(wc.load_pending())   # a következő újraindítás után fejeződik be

    def test_regisztracio_bukas_nem_indit_ujra(self):
        w = _Wu()
        w._run = lambda cmd, **k: (w.cmds.append(cmd), _res('REGHIBA: Access denied', 1))[1]
        with self._eff(None):
            w._wu_conclude('pause_wu', 'X', LOCKED_REP, finishing=False)
        self.assertEqual(w.shutdowns(), [])
        self.assertTrue(w.statuses()[-1].startswith('⚠️'))

    def test_befejezes_utan_nincs_ujabb_ujrainditas(self):
        w = _Wu()
        wc.save_pending('X', {'paused_until': '2026-12-03T16:00:00Z', 'exclude_drivers': True})
        with self._eff(None):
            w._wu_conclude('wu_finish', 'X', LOCKED_REP, finishing=True)
        self.assertEqual(w.shutdowns(), [])
        self.assertTrue(w.statuses()[-1].startswith('⚠️'))
        self.assertIsNone(wc.load_pending())


class PauseInputTest(unittest.TestCase):
    def test_rossz_napszam_elutasitva(self):
        quiet()
        for bad in (0, -5, 3651, 'abc', None):
            w = _Wu()
            w.pause_wu(bad)
            self.assertEqual(w.events[0][0], 'toast', bad)
            self.assertEqual(w.cmds, [])


class EgyFuggvenyTest(unittest.TestCase):
    """2026-11-03 (explicit user decision): egy WU-feladatnak EGY függvénye van, és a fix,
    a gombok, a CLI és a driverkeresés is azt hívja - nem élhet mellette régi másolat."""

    def _app_sources(self):
        out = {}
        for d, _dirs, files in os.walk(os.path.join(tests.ROOT, 'app')):
            for f in files:
                if f.endswith('.py'):
                    p = os.path.join(d, f)
                    code = [ln for ln in open(p, encoding='utf-8').read().splitlines()
                            if not ln.lstrip().startswith('#')]
                    out[os.path.relpath(p, tests.ROOT)] = '\n'.join(code)
        return out

    def test_nincs_regi_parhuzamos_megoldas(self):
        for path, src in self._app_sources().items():
            for old in ('WU_PAUSE_REMOVE_PS', 'WU_STOP_SERVICES_PS', 'build_wu_pause_ps',
                        'clear_software_distribution', 'detectnow', 'StartScan', 'MoveFileEx'):
                self.assertNotIn(old, src, f'{old} ismét megjelent: {path}')
            if path != os.path.join('app', 'wusettings_core.py'):
                # WU-szolgáltatást és WU-szünet registryt csak a közös mag kezel.
                self.assertNotIn('Stop-Service wuauserv', src, path)
                self.assertNotIn("'PauseUpdatesExpiryTime'", src, path)

    def test_a_fix_a_kozos_fuggvenyeket_hivja(self):
        import inspect
        from app.gui import autofix, hwscan
        af_src = inspect.getsource(autofix)
        for fn in ('pause_for_days(', 'stop_wu_services(', 'purge_software_distribution(',
                   'set_wu_driver_policy(', 'resume_wu('):
            self.assertIn(fn, af_src)
        self.assertIn('restart_wu_services(', inspect.getsource(hwscan))

    def test_set_wu_pause_a_kozos_fuggvenyeken_megy(self):
        quiet()
        run = lambda *a, **k: _res()
        with mock.patch.object(wc, 'pause_for_days', return_value='2036-01-01T00:00:00Z') as p, \
                mock.patch.object(wc, 'set_wu_driver_policy', return_value=True) as pol:
            self.assertTrue(wc.set_wu_pause(run, True))
        p.assert_called_once_with(wc.AUTOFIX_WU_PAUSE_DAYS, extend=False)
        pol.assert_called_once_with(run, disabled=True)
        with mock.patch.object(wc, 'remove_pause', return_value=True) as rp, \
                mock.patch.object(wc, 'set_wu_driver_policy', return_value=True):
            self.assertTrue(wc.set_wu_pause(run, False))
        rp.assert_called_once()
        # Bukott beírás nem hamis siker.
        with mock.patch.object(wc, 'pause_for_days', return_value=None), \
                mock.patch.object(wc, 'set_wu_driver_policy', return_value=True):
            self.assertFalse(wc.set_wu_pause(run, True))

    def test_pause_for_days_a_kozos_irora_megy(self):
        written = {}
        with mock.patch.object(wc, '_reg_write', side_effect=lambda sub, n, v, t: written.__setitem__(n, v)), \
                mock.patch.object(wc, 'wu_paused_until', side_effect=lambda: written.get('PauseUpdatesExpiryTime')):
            end = wc.pause_for_days(10, extend=False)
        self.assertEqual(set(written), set(wc.WU_PAUSE_VALUE_NAMES))
        self.assertEqual(end, written['PauseQualityUpdatesEndTime'])


class QuickButtonsTest(unittest.TestCase):
    """2026-11-23 (explicit user decision): a négy WU-gyorsgomb MIND teljes resetet csinál
    (catroot2, DLL-ek, winsock), és csak a kért beállításhoz nyúl."""

    def _press(self, method, policy_ok=True, pause_end='2036-11-21T10:00:00Z'):
        quiet()
        calls = []
        rec = lambda name, ret: (lambda *a, **k: (calls.append((name, a[1:] if name == 'policy' else a, k)), ret)[1])
        w = _Wu()
        with mock.patch.object(wc, 'set_wu_driver_policy', rec('policy', policy_ok)), \
                mock.patch.object(wc, 'pause_for_days', rec('pause', pause_end)), \
                mock.patch.object(wc, 'remove_pause', rec('unpause', True)), \
                mock.patch.object(wc, 'desired_state', return_value={}), \
                mock.patch.object(wc, 'repair_wu', lambda run, log, full_reset=False: (calls.append(('repair', (), {'full_reset': full_reset})), OK_REP)[1]), \
                mock.patch.object(guiwu.GuiWuMixin, '_wu_conclude', lambda *a, **k: None):
            getattr(w, method)()
        return calls, w

    def test_mind_a_negy_teljes_reset(self):
        for m in ('disable_wu', 'enable_wu', 'restart_wu', 'disable_pause_wu'):
            calls, _w = self._press(m)
            self.assertEqual([c for c in calls if c[0] == 'repair'], [('repair', (), {'full_reset': True})], m)

    def test_csak_a_kert_beallitashoz_nyul(self):
        names = lambda calls: [(c[0], c[2].get('disabled')) if c[0] == 'policy' else
                               (c[0], c[1]) if c[0] == 'pause' else (c[0],) for c in calls if c[0] != 'repair']
        self.assertEqual(names(self._press('disable_wu')[0]), [('policy', True)])
        self.assertEqual(names(self._press('enable_wu')[0]), [('unpause',), ('policy', False)])
        self.assertEqual(names(self._press('restart_wu')[0]), [])
        self.assertEqual(names(self._press('disable_pause_wu')[0]),
                         [('policy', True), ('pause', (wc.WU_PAUSE_MAX_DAYS, False))])

    def test_bukott_driver_tiltas_utan_nincs_szunet_es_reset(self):
        calls, w = self._press('disable_pause_wu', policy_ok=False)
        self.assertEqual([c[0] for c in calls], ['policy'])
        self.assertTrue(w.statuses()[-1].startswith('❌'))

    def test_bukott_szunet_nem_hamis_siker(self):
        calls, w = self._press('disable_pause_wu', pause_end=None)
        self.assertNotIn('repair', [c[0] for c in calls])
        self.assertTrue(w.statuses()[-1].startswith('❌'))


class TempCleanDefaultsTest(unittest.TestCase):
    """2026-11-23 (explicit user decision): alapból a két TEMP, a WU-gyorsítótár és a Lomtár
    van bepipálva, minden más opcionális - a GUI és a CLI (közös definíció) ugyanígy."""

    def test_alapbol_bepipalt_kategoriak(self):
        import re
        from app import tempclean_core as tc
        defs = [(k, d) for k, _l, _p, _s, d in tc._temp_clean_category_defs('C:')] + \
               [(k, d) for k, _l, d in tc.SPECIAL_CATEGORIES]
        on = {k for k, d in defs if d}
        self.assertEqual(on, {'user_temp', 'windows_temp', 'wu_cache', 'recycle_bin'})
        html = open(os.path.join(os.path.dirname(__file__), '..', 'ui.html'), encoding='utf-8').read()
        checked = set(re.findall(r'<input type="checkbox" id="tc-([a-z-]+)" checked', html))
        self.assertEqual({c.replace('-', '_') for c in checked}, on)


class TempCleanLongPathTest(unittest.TestCase):
    """A Rendszer karbantartás WU-gyorsítótár törlője ugyanabba a 260 karakteres falba futott
    volna (ASUS, LongPathsEnabled nélkül: WinError 3). Most `\\\\?\\` előtaggal töröl/mér."""

    def test_hosszu_utvonal_torlodik_es_merheto(self):
        quiet()
        from app import tempclean_core as tc
        base = tempfile.mkdtemp()
        try:
            deep = base
            for i in range(5):
                deep = os.path.join(deep, 'amd64_microsoft-windows-e..ent-winrt.resources_' + str(i) * 20)
            os.makedirs('\\\\?\\' + deep)
            for j in range(20):
                with open('\\\\?\\' + os.path.join(deep, f'f{j}.mui'), 'w') as f:
                    f.write('x')
            self.assertGreater(len(os.path.join(deep, 'f0.mui')), 260)
            self.assertEqual(tc.folder_size(base), (20, 20))
            seen = []
            real = os.remove
            with mock.patch.object(tc.os, 'remove', side_effect=lambda p: (seen.append(p), real(p))[1]):
                freed, removed, failed = tc._clean_folder_contents(base)
            self.assertEqual((freed, removed, failed), (20, 20, 0))
            self.assertTrue(seen and all(p.startswith('\\\\?\\') for p in seen))
            self.assertEqual(os.listdir(base), [])
        finally:
            import subprocess
            subprocess.run(['cmd', '/c', 'rd', '/s', '/q', '\\\\?\\' + base], capture_output=True)


class Win11UpgradeTest(unittest.TestCase):
    """2026-11-23 (explicit user decision): „Win 11 frissítés javítása” gomb (terep: ThinkPad
    T14, Win 10 -> 11, 0x80070002). 10 év WU-szünet (*„ha megint ráindítok … telepítési
    segédből is akkor megint 2 helyen fog menni párhuzamosan”*) -> teljes reset ->
    telepítő-maradék, DISM, sfc -> újraindítás. Csak a két telepítő-mappát törli (*„nehogy
    leformazzuk a gépet”*), és egy újraindítás előtti hiba nem tűnhet el a befejező jelentésből."""

    def setUp(self):
        quiet()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _press(self, pause_end='2036-11-21T10:00:00Z', *args):
        calls = []
        w = _Wu()
        with mock.patch.object(wc, 'remove_pause', lambda: calls.append('unpause')), \
                mock.patch.object(wc, 'set_wu_driver_policy', lambda *a, **k: calls.append('policy')), \
                mock.patch.object(wc, 'pause_for_days', lambda *a, **k: (calls.append(('pause',) + a), pause_end)[1]), \
                mock.patch.object(wc, 'repair_wu', lambda run, log, full_reset=False: (calls.append(('repair', full_reset)), OK_REP)[1]), \
                mock.patch.object(wc, 'repair_feature_update',
                                  lambda run, log, check_cancel=None, deep=False: (calls.append(('feature', deep)), [])[1]), \
                mock.patch.object(guiwu.GuiWuMixin, '_wu_conclude', lambda *a, **k: None):
            w.fix_win11_upgrade(*args)
        return calls, w

    def test_elobb_10_ev_szunet_aztan_reset_driverhez_nem_nyul(self):
        calls, _w = self._press()
        self.assertEqual(calls, [('pause', wc.WU_PAUSE_MAX_DAYS, False), ('repair', True), ('feature', False)])

    def test_dism_sfc_csak_keresre(self):
        # 2026-11-23 (explicit user decision): alapból csak szünet + reset + maradék (pár perc).
        self.assertEqual(self._press('2036-11-21T10:00:00Z', True)[0][-1], ('feature', True))
        ran = []
        with mock.patch.object(wc, 'remove_upgrade_leftovers', lambda run, log: (ran.append('left'), [])[1]), \
                mock.patch.object(wc, 'dism_restore_health', lambda run, log: (ran.append('dism'), [])[1]), \
                mock.patch.object(wc, 'run_sfc', lambda run, log: (ran.append('sfc'), [])[1]):
            wc.repair_feature_update(None, lambda m: None)
            self.assertEqual(ran, ['left'])
            ran.clear()
            wc.repair_feature_update(None, lambda m: None, deep=True)
            self.assertEqual(ran, ['left', 'dism', 'sfc'])

    def test_bukott_szunet_utan_semmi_nem_fut(self):
        calls, w = self._press(pause_end=None)
        self.assertEqual(calls, [('pause', wc.WU_PAUSE_MAX_DAYS, False)])
        self.assertTrue(w.statuses()[-1].startswith('❌'))

    def _leftover_run(self, deletes=True):
        cmds = []

        def run(cmd, **k):
            cmds.append(cmd)
            if deletes and isinstance(cmd, str) and cmd.startswith('rd '):
                shutil.rmtree(cmd.split('"')[1][4:], ignore_errors=True)
            return _res()
        return run, cmds

    def _make(self, *names):
        for n in names:
            os.makedirs(os.path.join(self.root, n, 'Sources'))
            with open(os.path.join(self.root, n, 'Sources', 'install.esd'), 'w') as f:
                f.write('x')

    def test_csak_a_ket_telepito_mappat_torli_visszaolvasva(self):
        self._make('$WINDOWS.~BT', '$WINDOWS.~WS', 'Users', 'Windows.old')
        run, cmds = self._leftover_run()
        logs = []
        self.assertEqual(wc.remove_upgrade_leftovers(run, logs.append, sys_drive=self.root), [])
        self.assertEqual(sorted(os.listdir(self.root)), ['Users', 'Windows.old'])
        targets = {c.split('"')[1] for c in cmds if isinstance(c, str)}
        self.assertEqual(targets, {'\\\\?\\' + os.path.join(self.root, n) for n in wc.UPGRADE_LEFTOVER_DIRS})
        self.assertEqual(sum('✅' in m for m in logs), 2)

    def test_megmaradt_mappa_jog_atvetel_utan_sem_hamis_siker(self):
        self._make('$WINDOWS.~BT')
        run, cmds = self._leftover_run(deletes=False)
        logs = []
        problems = wc.remove_upgrade_leftovers(run, logs.append, sys_drive=self.root)
        self.assertEqual(len(problems), 1)
        self.assertNotIn('✅', ' '.join(logs))
        lists = [c for c in cmds if isinstance(c, list)]
        self.assertEqual([c[0] for c in lists], ['takeown', 'icacls'])
        self.assertNotIn('/D', lists[0])   # a válaszbetű nyelvfüggő

    def test_nincs_maradek_nincs_parancs(self):
        run, cmds = self._leftover_run()
        self.assertEqual(wc.remove_upgrade_leftovers(run, lambda m: None, sys_drive=self.root), [])
        self.assertEqual(cmds, [])

    def test_dism_verdikt(self):
        from app.common import CMD_TIMEOUT_RETURNCODE
        ok_out = '[==========================100.0%==========================]\r\nThe restore operation completed successfully.\r\nThe operation completed successfully.\r\n'
        bad_out = ('[===   5.0%   ]\r\n\r\nError: 0x800f081f\r\n\r\nThe source files could not be found.\r\n'
                   'Use the "Source" option to specify the location of the files.\r\n'
                   'The DISM log file can be found at C:\\Windows\\Logs\\DISM\\dism.log\r\n')
        for out, rc, want in ((ok_out, 0, None), ('', 0, 'kód 0'), (bad_out, 2, '0x800f081f'),
                              ('', CMD_TIMEOUT_RETURNCODE, 'időtúllépés')):
            seen = []
            p = wc.dism_restore_health(lambda cmd, **k: (seen.append(cmd), _res(out, rc))[1], lambda m: None)
            self.assertIn('/RestoreHealth', seen[0])
            if want is None:
                self.assertEqual(p, [])
            else:
                self.assertEqual(len(p), 1)
                self.assertIn(want, p[0])
        self.assertIn('could not be found', wc.dism_restore_health(lambda c, **k: _res(bad_out, 2), lambda m: None)[0])

    def test_sfc_utf16_es_nincs_sajat_pipa(self):
        said = 'A Windows Erőforrásvédelem nem talált integritássértést.'
        out = f'\r\r\nRendszervizsgálat indítása.\r\r\nEllenőrzés: 37% kész.\rEllenőrzés: 100% kész.\r\r\n\r\r\n{said}\r\r\n'
        kw, logs = {}, []
        p = wc.run_sfc(lambda cmd, **k: (kw.update(k), _res(out, 0))[1], logs.append)
        self.assertEqual(kw.get('encoding'), 'utf-16-le')
        self.assertEqual(p, [])
        self.assertIn(said, logs[-1])
        self.assertNotIn('✅', ' '.join(logs))
        p = wc.run_sfc(lambda cmd, **k: _res(out, 1), lambda m: None)
        self.assertEqual(len(p), 1)
        self.assertIn(said, p[0])
        self.assertEqual(len(wc.run_sfc(lambda cmd, **k: _res('', 0), lambda m: None)), 1)

    def test_megszakitas_utan_a_hatralevo_lepes_kimarad(self):
        flags = iter([False, True, True])
        ran = []
        with mock.patch.object(wc, 'remove_upgrade_leftovers', lambda run, log: (ran.append('left'), [])[1]), \
                mock.patch.object(wc, 'dism_restore_health', lambda run, log: (ran.append('dism'), [])[1]), \
                mock.patch.object(wc, 'run_sfc', lambda run, log: (ran.append('sfc'), [])[1]):
            p = wc.repair_feature_update(None, lambda m: None, check_cancel=lambda: next(flags), deep=True)
        self.assertEqual(ran, ['left'])
        self.assertEqual(len(p), 2)

    def test_ujrainditas_elotti_hiba_a_befejezo_jelentesben(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        w = _Wu()
        with mock.patch.object(wc, '_pending_path', return_value=os.path.join(tmp, 'wu_pending.json')), \
                mock.patch.object(wc, 'wu_reboot_flags', return_value=[]), \
                mock.patch.object(wc, 'desired_state', return_value={'paused_until': '2036-11-21T10:00:00Z', 'exclude_drivers': True}), \
                mock.patch.object(wc, 'read_effective_state', return_value={}), \
                mock.patch.object(wc, 'pause_for_days', return_value='2036-11-21T10:00:00Z'), \
                mock.patch.object(wc, 'reassert_pending', return_value=True), \
                mock.patch.object(wc, 'repair_feature_update',
                                  lambda run, log, check_cancel=None, deep=False: ['a DISM /RestoreHealth nem sikerült: Error: 0x800f081f']), \
                mock.patch.object(guiwu.time, 'sleep', lambda s: None):
            with mock.patch.object(wc, 'repair_wu', return_value=LOCKED_REP):
                w.fix_win11_upgrade(True)
            self.assertEqual(len(w.shutdowns()), 1)
            self.assertEqual(wc.load_pending()['carry'], ['a DISM /RestoreHealth nem sikerült: Error: 0x800f081f'])
            # a befejező lépés ezt a szünetet írja vissza, ha újraindítás után elveszett
            self.assertEqual(wc.load_pending()['paused_until'], '2036-11-21T10:00:00Z')
            with mock.patch.object(wc, 'repair_wu', return_value=OK_REP):
                w.finish_wu_after_reboot()
        self.assertEqual(len(w.shutdowns()), 1)
        self.assertTrue(w.statuses()[-1].startswith('⚠️'), w.statuses()[-1])
        self.assertIn('0x800f081f', w.statuses()[-1])
        self.assertIn('szüneteltetve 2036-11-21-ig', w.statuses()[-1])
        self.assertEqual(OK_REP['problems'], [])   # a közös csonk nem szennyeződött

    def test_gui_es_cli_is_hivja(self):
        html = open(os.path.join(tests.ROOT, 'ui.html'), encoding='utf-8').read()
        self.assertIn('onclick="fixWin11Upgrade()"', html)
        self.assertIn('api().fix_win11_upgrade(deep)', html)
        self.assertIn('<input type="checkbox" id="w11-deep">', html)    # alapból NINCS bepipálva
        cli = open(os.path.join(tests.ROOT, 'app', 'cli', 'menu.py'), encoding='utf-8').read()
        self.assertIn('api.fix_win11_upgrade(deep)', cli)


if __name__ == '__main__':
    unittest.main()
