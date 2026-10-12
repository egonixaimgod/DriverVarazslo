"""Az 1 kattintásos fix "nemtudás nem tény" javításai (2026-10-12-i review).

Minden eset a lánc egy olyan pontja, ahol egy ELBUKOTT lekérdezés vagy írás eddig
"✅ kész"-nek / "nincs semmi"-nek látszott. Valódi rendszerhez semmi nem nyúl: a `_run`,
a registry-olvasók és a lánc-állapot mappája (`_app_data_dir`) csonkolva."""
import json
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import drivers_core, wusettings_core
from app.gui import autofix as af
from app.gui.drivers import GuiDriversMixin


def _res(stdout='', rc=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=rc)


class _Chain(af.GuiAutofixMixin):
    """A mixin minimális gazdája: a képernyőre ment sorok az `emitted`-be kerülnek."""

    def __init__(self, run=None, drivers=None, drivers_why=None):
        self._run = run or (lambda *a, **k: _res())
        self._drivers = drivers or []
        self._drivers_why = drivers_why
        self._cancel_flag = False
        self.emitted = []

    def emit(self, event, payload=None):
        self.emitted.append((event, payload))

    def log_lines(self):
        return [p.get('log', '') for e, p in self.emitted if e == 'task_progress' and isinstance(p, dict)]

    def _get_third_party_drivers(self, problems=None):
        if self._drivers_why and problems is not None:
            problems.append(self._drivers_why)
        return list(self._drivers)


class _TempState(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(af, '_app_data_dir', return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)


class PnpQueryTest(unittest.TestCase):
    """2-es pont: az üres/hibás eszköz-lekérdezés None, nem üres lista."""

    def test_empty_and_garbage_is_none(self):
        quiet()
        for out in ('', '   ', 'nem json', '[]'):
            calls = []
            c = _Chain(run=lambda *a, **k: calls.append(1) or _res(out))
            self.assertIsNone(c._query_pnp_devices(), out)
            self.assertEqual(len(calls), 2)          # egy újrapróbálás

    def test_single_device_dict_becomes_list(self):
        quiet()
        c = _Chain(run=lambda *a, **k: _res(json.dumps({'Name': 'X', 'PNPDeviceID': 'PCI\\A'})))
        self.assertEqual(c._query_pnp_devices(), [{'Name': 'X', 'PNPDeviceID': 'PCI\\A'}])

    def test_summary_does_not_claim_clean_machine(self):
        quiet()
        c = _Chain(run=lambda *a, **k: _res(''))
        for name in ('_emit_missing_packages', '_emit_catalog_no_bind', '_emit_fast_startup_note',
                     '_apply_performance_power_plan', '_emit_driver_health'):
            setattr(c, name, mock.MagicMock())
        c._emit_autofix_summary(3)
        text = '\n'.join(c.log_lines())
        self.assertNotIn('Nem maradt hibakódos eszköz', text)
        self.assertIn('NEM futott le', text)
        c._emit_driver_health.assert_not_called()


class DismFailureTest(_TempState):
    """3-as pont: az elbukott DISM-lista nem "nincs third-party driver"."""

    def test_core_reports_unknown_nonzero_exit(self):
        quiet()
        why = []
        self.assertEqual(drivers_core.get_third_party_drivers(lambda *a, **k: _res('', 1460), why), [])
        self.assertTrue(why and '1460' in why[0])
        why = []
        drivers_core.get_third_party_drivers(lambda *a, **k: _res('The operation completed successfully.', 0), why)
        self.assertEqual(why, [])                     # a tényleg üres lista nem hiba

    def test_failed_list_is_not_cached(self):
        quiet()

        class D(GuiDriversMixin):
            pass

        d = D()
        d._run = lambda *a, **k: _res('', 1460)
        first, second = [], []
        d._get_third_party_drivers(first)
        d._get_third_party_drivers(second)
        self.assertTrue(first and second)            # a második hívó is látja a hibát

    def test_delete_phase_stops_without_deleting(self):
        c = _Chain(drivers_why='a DISM 1460 kóddal tért vissza')
        with mock.patch.object(drivers_core, 'delete_driver_package') as dele:
            with self.assertRaises(Exception) as cm:
                c._delete_third_party_sync()
        dele.assert_not_called()
        self.assertIn('NEM indult el', str(cm.exception))
        self.assertNotIn('Nincs third-party driver', '\n'.join(c.log_lines()))

    def test_missing_packages_report_is_skipped(self):
        c = _Chain(drivers_why='hiba')
        c._emit_missing_packages([{'original': 'a.inf'}])
        text = '\n'.join(c.log_lines())
        self.assertIn('ellenőrzés kimarad', text)
        self.assertNotIn('NEM került vissza', text)


class WedgedDeleteTest(_TempState):
    """4-es pont: beragadáskor a "használatban" csomag a következő lábra megy, a sikertelen kiíródik."""

    def test_in_use_kept_failed_reported(self):
        drvs = [{'published': f'oem{i}.inf', 'original': n, 'class': 'X'}
                for i, n in enumerate(('inuse.inf', 'fail.inf', 'stall1.inf', 'stall2.inf', 'rest.inf'), 1)]
        kind = {'oem1.inf': 'inuse', 'oem2.inf': 'fail', 'oem3.inf': 'stall', 'oem4.inf': 'stall'}
        c = _Chain(drivers=drvs)
        with mock.patch.object(af, '_collect_boot_path_protection', return_value=(set(), [], True)), \
                mock.patch.object(af, '_is_boot_path_protected', return_value=False), \
                mock.patch.object(af, '_export_net_driver_backup', return_value=0), \
                mock.patch.object(af, 'export_drivers_backup', return_value=(0, 0, 0)), \
                mock.patch.object(af, 'collect_driver_usage', return_value={}), \
                mock.patch.object(af, 'is_reboot_pending', return_value=False), \
                mock.patch.object(af, 'spawn_failed', return_value=False), \
                mock.patch.object(c, '_get_installed_driver_info', create=True, return_value={}), \
                mock.patch.object(drivers_core, 'delete_driver_package',
                                  side_effect=lambda run, name, timeout=None: SimpleNamespace(
                                      k=kind.get(name, 'ok'), returncode=1)), \
                mock.patch.object(drivers_core, 'delete_stalled', side_effect=lambda r: r.k == 'stall'), \
                mock.patch.object(drivers_core, 'delete_succeeded', side_effect=lambda r: r.k == 'ok'), \
                mock.patch.object(drivers_core, 'delete_blocked_in_use', side_effect=lambda r: r.k == 'inuse'):
            self.assertEqual(c._delete_third_party_sync(), 'wedged')
        pending = [p['published'] for p in c._autofix_stats_get('pending_deletes')]
        self.assertEqual(sorted(pending), ['oem1.inf', 'oem3.inf', 'oem4.inf', 'oem5.inf'])
        self.assertIn('oem2.inf (fail.inf)', '\n'.join(c.log_lines()))


class ResumeTaskTest(_TempState):
    """1-es pont: a folytató feladat verdiktje a visszaolvasott argumentum."""

    def _chain(self, out, rc=0):
        c = _Chain(run=lambda *a, **k: _res(out, rc))
        c._autofix_skip_printers = True
        return c

    def test_readback_ok(self):
        c = self._chain('ARGS=--resume-autofix --skip-printer-drivers\n')
        with mock.patch.object(af, '_app_exe_path', return_value=r'C:\x\dv.exe'), \
                mock.patch.object(af.sys, 'frozen', True, create=True):
            self.assertTrue(c._schedule_autofix_resume('--resume-autofix'))

    def test_failure_raises_or_returns_false(self):
        for out, rc in (('REGHIBA: Access is denied.', 1), ('', 0), ('ARGS=--resume-step1', 0)):
            c = self._chain(out, rc)
            with mock.patch.object(af, '_app_exe_path', return_value=r'C:\x\dv.exe'), \
                    mock.patch.object(af.sys, 'frozen', True, create=True):
                with self.assertRaises(Exception):
                    c._schedule_autofix_resume('--resume-autofix')
                self.assertFalse(c._schedule_autofix_resume('--resume-autofix', required=False))

    def test_generated_ps_parses_args_flag(self):
        seen = []
        c = _Chain(run=lambda cmd, **k: seen.append(cmd) or _res('ARGS=--resume-step1'))
        with mock.patch.object(af, '_app_exe_path', return_value=r'C:\x\dv.exe'), \
                mock.patch.object(af.sys, 'frozen', True, create=True):
            c._schedule_autofix_resume('--resume-step1')
        ps = seen[0][-1]
        self.assertIn('-ErrorAction Stop', ps)
        self.assertIn("Write-Output ('ARGS=' + $t.Actions[0].Arguments)", ps)


class WuReadbackTest(unittest.TestCase):
    """6-os pont: a WU-írások verdiktje a visszaolvasás."""

    def test_driver_policy(self):
        quiet()
        run = lambda *a, **k: _res()
        with mock.patch.object(wusettings_core, '_driver_policy_state', return_value=(True, True)):
            self.assertTrue(wusettings_core.set_wu_driver_policy(run, True))
            self.assertFalse(wusettings_core.set_wu_driver_policy(run, False))
        with mock.patch.object(wusettings_core, '_driver_policy_state', return_value=(False, True)):
            self.assertFalse(wusettings_core.set_wu_driver_policy(run, True))

    def test_resume_wu(self):
        quiet()
        logs = []
        with mock.patch.object(wusettings_core, 'wu_paused_until', return_value='2036-01-01T00:00:00Z'):
            self.assertFalse(wusettings_core.resume_wu(lambda *a, **k: _res(), logs.append))
        self.assertIn('NEM oldódott fel', logs[-1])
        with mock.patch.object(wusettings_core, 'wu_paused_until', return_value=None):
            self.assertTrue(wusettings_core.resume_wu(lambda *a, **k: _res(), logs.append))

    def test_pause_line(self):
        quiet()
        with mock.patch.object(wusettings_core, 'wu_paused_until', return_value=None):
            self.assertIn('NEM íródott be', af._wu_pause_line('✅ ok'))
        with mock.patch.object(wusettings_core, 'wu_paused_until', return_value='2036-01-01T00:00:00Z'):
            self.assertEqual(af._wu_pause_line('✅ ok'), '✅ ok')


class SmallFixesTest(_TempState):
    """7-es és 8-as pont."""

    def test_regressions_always_three_values(self):
        self.assertEqual(_Chain()._fix_driver_regressions({}), (0, 0, 0))

    def test_leg_count_uses_shared_reader(self):
        c = _Chain()
        self.assertEqual(c._autofix_leg_count(), 0)
        c._autofix_stats_add(2)
        c._autofix_stats_add(0)
        self.assertEqual(c._autofix_leg_count(), 2)


if __name__ == '__main__':
    unittest.main()
