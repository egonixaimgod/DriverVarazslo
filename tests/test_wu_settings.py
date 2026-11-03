"""Operációs rendszer / Windows Update gombok (2026-11-03, explicit user decision).

- Egyik gomb sem indít frissítés-keresést (a régi „Szolgáltatások újraindítása”
  `wuauclt /detectnow` + `UsoClient StartScan`-t futtatott, ami letöltötte a szemetet).
- A kiírás a Windows TÉNYLEGES állapotából (UpdatePolicy\\PolicyState) jön; az eltérés
  kimondva (mérve a fejlesztői gépen: UX\\Settings-ben nincs szünet, a PolicyState 2036-ig
  szüneteltet - a régi kiírás „nincs szünet”-et mondott).
- „✅ Kész” csak egyező állapotra; különben 10 mp-es visszaszámlálás + befejező feladat,
  újraindítás után nincs újabb újraindítás, csak őszinte jelentés.
"""
import os
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
START_OUT = ("START|cryptsvc|Running\nSTART|bits|Running\nSTART|wuauserv|Running\n"
             "START|UsoSvc|Running\nSTART|AppIDSvc|Running\n")


class ParseAndStateTest(unittest.TestCase):
    def test_parse_service_lines(self):
        p = wc.parse_service_lines(STOP_OUT + "STOPERR|bits|Access denied\n")
        self.assertEqual(p['stop']['wuauserv'], 'KILLED')
        self.assertEqual(p['deps'], ['AppIDSvc'])
        self.assertEqual(p['errors'], ['bits: Access denied'])

    def test_mismatch_fejlesztoi_gep_esete(self):
        # Mérve 2026-11-03: beállítva nincs szünet, a Windows 2036-ig szüneteltet.
        m = wc.state_mismatches({'paused_until': None, 'exclude_drivers': True},
                                {'paused_until': '2036-09-29T07:49:29Z', 'exclude_drivers': True})
        self.assertEqual(len(m), 1)
        self.assertIn('2036-09-29', m[0])

    def test_mismatch_esetek(self):
        same = {'paused_until': '2026-11-10T08:00:00Z', 'exclude_drivers': False}
        self.assertEqual(wc.state_mismatches(same, dict(same)), [])
        # Csak a nap számít (a Windows a másodperceket nem feltétlenül tartja meg).
        self.assertEqual(wc.state_mismatches(same, {'paused_until': '2026-11-10T09:30:00Z',
                                                    'exclude_drivers': False}), [])
        self.assertTrue(wc.state_mismatches(same, {'paused_until': None, 'exclude_drivers': False}))
        self.assertTrue(wc.state_mismatches({'paused_until': None, 'exclude_drivers': True},
                                            {'paused_until': None, 'exclude_drivers': False}))
        # Nem olvasható tényleges állapot: nincs eltérés-állítás (a hívó külön jelzi).
        self.assertEqual(wc.state_mismatches(same, None), [])
        # Nem olvasható driver-érték: arról nem állítunk semmit.
        self.assertEqual(wc.state_mismatches({'paused_until': None, 'exclude_drivers': True},
                                             {'paused_until': None, 'exclude_drivers': None}), [])

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
        with mock.patch.object(wc, 'clear_software_distribution',
                               return_value={'ok': True, 'remaining': 0, 'sample': []}):
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
        with mock.patch.object(wc, 'clear_software_distribution',
                               return_value={'ok': True, 'remaining': 0, 'sample': []}):
            rep = wc.repair_wu(run, lambda m: None)
        self.assertFalse(rep['services_ok'])
        self.assertTrue(any('LE VAN TILTVA' in p for p in rep['problems']))

    def test_valasz_nelkul_nem_hamis_siker(self):
        with mock.patch.object(wc, 'clear_software_distribution',
                               return_value={'ok': True, 'remaining': 0, 'sample': []}):
            rep = wc.repair_wu(lambda *a, **k: _res(''), lambda m: None)
        self.assertFalse(rep['services_ok'])


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


OK_REP = {'services_ok': True, 'problems': [], 'cache': {'ok': True}, 'scheduled': 0, 'reboot_reasons': []}


class ConcludeTest(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.object(wc, '_pending_path', return_value=os.path.join(self.tmp, 'wu_pending.json'))
        p.start()
        self.addCleanup(p.stop)
        for name, val in (('wu_reboot_flags', []), ('desired_state', {'paused_until': '2026-11-10T08:00:00Z',
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

    def test_egyezik_kesz_ujrainditas_nelkul(self):
        w = _Wu()
        with self._eff('2026-11-10T08:00:00Z'):
            w._wu_conclude('pause_wu', 'X', OK_REP, finishing=False)
        self.assertTrue(w.statuses()[-1].startswith('✅'))
        self.assertEqual(w.shutdowns(), [])
        self.assertIsNone(wc.load_pending())

    def test_elteres_ujrainditas_es_befejezo_feladat(self):
        w = _Wu()
        with self._eff(None):
            w._wu_conclude('pause_wu', 'X', OK_REP, finishing=False)
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
            w._wu_conclude('pause_wu', 'X', OK_REP, finishing=False)
        self.assertEqual(w.shutdowns(), [])
        self.assertIsNotNone(wc.load_pending())   # a következő újraindítás után fejeződik be

    def test_regisztracio_bukas_nem_indit_ujra(self):
        w = _Wu()
        w._run = lambda cmd, **k: (w.cmds.append(cmd), _res('REGHIBA: Access denied', 1))[1]
        with self._eff(None):
            w._wu_conclude('pause_wu', 'X', OK_REP, finishing=False)
        self.assertEqual(w.shutdowns(), [])
        self.assertTrue(w.statuses()[-1].startswith('⚠️'))

    def test_befejezes_utan_nincs_ujabb_ujrainditas(self):
        w = _Wu()
        wc.save_pending('X', {'paused_until': '2026-11-10T08:00:00Z', 'exclude_drivers': True})
        with self._eff(None):
            w._wu_conclude('wu_finish', 'X', OK_REP, finishing=True)
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


if __name__ == '__main__':
    unittest.main()
