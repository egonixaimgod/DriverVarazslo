"""A törlő funkciók review-javításai (2026-10-12): időkorlát minden pnputil-törlésen,
a közös sikerfeltétel (3010 = siker magyarul is), a force-törlés visszaolvasása, és hogy
egy elbukott lekérdezés vagy félbemaradt szkript ne "✅ kész" legyen.

A törlés SZŰRŐ NÉLKÜLI marad (CLAUDE.md 1. elv) - ezek a tesztek csak a jelentés
őszinteségét és az időkorlátokat fogják. Valódi rendszerhez semmi nem nyúl."""
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import drivers_core, dupdrivers_core, tempclean_core
from app.ghost_core import build_ghost_ps
from app.gui import drivers as gdrv, dupdrivers as gdup, ghost as gghost

# A magyar pnputil 3010-es (újraindítás kell) sikere - se 'deleted', se 'törölve' nincs benne.
HU_3010 = 'Microsoft PnP segédprogram\n\nAz illesztőprogram-csomag törlése sikerült.\nÚjraindítás szükséges.'


def _res(stdout='', rc=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=rc)


class _Host:
    """Közös gazda a GUI-mixinekhez: szinkron szál, a képernyő-sorok listában."""
    target_os_path = None
    _cancel_flag = False
    _si = _nw = None

    def __init__(self, run):
        self._run = run
        self.events = []

    def emit(self, event, payload=None):
        self.events.append((event, payload))

    def _safe_thread(self, _name, fn):
        fn()

    def _check_cancel(self):
        return False

    def logs(self):
        return '\n'.join((p or {}).get('log', '') for e, p in self.events if isinstance(p, dict))


class DismHintTest(unittest.TestCase):
    def test_error_code_needs_number_boundary(self):
        self.assertEqual(drivers_core.dism_failure_reason('Error: 216\nThe image ...'), '')
        self.assertIn('Error: 2', drivers_core.dism_failure_reason('Error: 2\nThe system cannot find'))
        self.assertIn('740', drivers_core.dism_failure_reason('Error: 740'))

    def test_offline_list_reports_unknown_exit(self):
        quiet()
        why = []
        with mock.patch.object(drivers_core, 'filter_phantom_packages', side_effect=lambda d, *_: d):
            drivers_core.get_offline_drivers(lambda *a, **k: _res('', 1460), r'D:\\', problems=why)
        self.assertTrue(why and '1460' in why[0])


class ManualDeleteTest(unittest.TestCase):
    def _delete(self, outcome, offline=False):
        quiet()
        calls = []

        def run(cmd, **k):
            calls.append((cmd, k))
            return outcome

        class D(_Host, gdrv.GuiDriversMixin):
            pass

        d = D(run)
        d.target_os_path = r'D:\\' if offline else None
        d.delete_drivers(['oem7.inf'])
        return d, calls

    def test_online_pnputil_has_timeout_offline_dism_not(self):
        _d, calls = self._delete(_res('', 0))
        self.assertEqual(calls[0][1].get('timeout'), drivers_core.DELETE_DRIVER_TIMEOUT)
        _d, calls = self._delete(_res('', 0), offline=True)
        self.assertIsNone(calls[0][1].get('timeout'))

    def test_failure_names_the_reason(self):
        d, _ = self._delete(_res('Microsoft PnP Utility\n\nFailed to delete driver package: Access is denied.', 5))
        self.assertIn('Access is denied', d.logs())

    def test_stalled_says_reboot(self):
        d, _ = self._delete(_res('', drivers_core.CMD_TIMEOUT_RETURNCODE))
        self.assertIn('indítsd újra', d.logs())


class ThirdPartyOnlyTest(unittest.TestCase):
    """2026-10-13 (explicit user decision): a Windows saját drivereinek listázása/törlése
    sehol nem érhető el - se paraméter, se force-törlés."""
    def test_no_all_drivers_path(self):
        import inspect
        from app.gui.drivers import GuiDriversMixin
        self.assertFalse(hasattr(drivers_core, 'get_all_drivers'))
        self.assertFalse(hasattr(drivers_core, 'force_delete_driver_files'))
        self.assertEqual(list(inspect.signature(GuiDriversMixin.load_drivers).parameters), ['self'])
        self.assertNotIn('list_all', inspect.signature(GuiDriversMixin.delete_drivers).parameters)
        self.assertNotIn('all_drivers', inspect.signature(drivers_core.get_offline_drivers).parameters)

    def test_offline_list_never_asks_for_all(self):
        seen = []
        drivers_core.get_offline_drivers(lambda cmd, **k: seen.append(cmd) or _res(''), r'D:\\')
        self.assertTrue(seen and '/all' not in [c.lower() for c in seen[0]])


class DuplicateDeleteTest(unittest.TestCase):
    def test_hungarian_3010_is_success_no_force(self):
        quiet()
        calls = []

        def run(cmd, **k):
            calls.append((cmd, k))
            return _res(HU_3010, 3010)

        ok, fail, skipped = dupdrivers_core.delete_duplicate_packages(run, lambda m: None, ['oem9.inf'], set())
        self.assertEqual((ok, fail, skipped), (1, 0, 0))
        self.assertEqual(len(calls), 1)                              # nem ment /force kör
        self.assertEqual(calls[0][1].get('timeout'), drivers_core.DELETE_DRIVER_TIMEOUT)

    def test_force_round_still_runs_for_failures(self):
        quiet()
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            return _res('Failed to delete driver package: One or more devices are presently installed '
                        'using the specified INF.', drivers_core.DELETE_IN_USE_RETURNCODE)

        dupdrivers_core.delete_duplicate_packages(run, lambda m: None, ['oem9.inf'], set())
        self.assertIn('/force', calls[-1])                           # nincs új szűrő a törlésben

    def test_failed_list_is_not_nothing_to_clean(self):
        quiet()
        logs = []

        def get_drivers(problems=None):
            problems.append('a DISM 1460 kóddal tért vissza')
            return []

        with mock.patch.object(dupdrivers_core, 'get_active_published_infs') as act:
            dupdrivers_core._cleanup_duplicate_groups(lambda *a, **k: _res(), logs.append, get_drivers)
        act.assert_not_called()
        self.assertIn('nem kérdezhető le', logs[-1])
        self.assertNotIn('Nincs azonos nevű', '\n'.join(logs))

    def test_view_shows_error_not_empty(self):
        quiet()

        class D(_Host, gdup.GuiDupDriversMixin):
            def _get_third_party_drivers(self, problems=None):
                problems.append('hiba')
                return []

        d = D(lambda *a, **k: _res())
        with mock.patch.object(gdup.threading, 'Thread',
                               side_effect=lambda target, **k: SimpleNamespace(start=target)):
            d.list_duplicate_drivers()
        payload = [p for e, p in d.events if e == 'dup_drivers_loaded'][0]
        self.assertIn('nem kérdezhető le', payload.get('error', ''))


class GhostTest(unittest.TestCase):
    def test_script_counts_3010_as_success(self):
        self.assertIn('$code -eq 3010', build_ghost_ps())

    def test_unfinished_script_is_not_done(self):
        quiet()

        class G(_Host, gghost.GuiGhostMixin):
            pass

        g = G(lambda *a, **k: _res())
        proc = SimpleNamespace(stdout=iter(['TOTAL: 3\n', 'RM: X\n', 'OK: X\n']), wait=lambda: None,
                               returncode=1, pid=1)
        with mock.patch.object(gghost.subprocess, 'Popen', return_value=proc):
            g.delete_ghost_devices()
        self.assertNotIn('befejeződött', g.logs())
        self.assertIn('NEM ért végig', g.logs())


class TempServicesTest(unittest.TestCase):
    def test_running_line_before_stop(self):
        quiet()
        seen = []
        tempclean_core.stop_services(lambda cmd, **k: seen.append(cmd[-1]) or _res('RUNNING:bits'), ['bits'])
        self.assertLess(seen[0].index("'RUNNING:'"), seen[0].index('Stop-Service'))


if __name__ == '__main__':
    unittest.main()
