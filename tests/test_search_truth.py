"""Driver-keresés és -telepítés review-javításai (2026-10-12): egy elbukott lekérdezés
vagy félbemaradt szkript ne legyen "✅ kész / 0 eszköz", az időtúllépés-ág éljen, és a kézi
WU-telepítés ugyanúgy utóellenőrizze a hamis [kód=4]-et, mint a lánc.

Valódi rendszerhez semmi nem nyúl: `_run`, Popen, a mappák (`_app_data_dir`, SystemDrive)
csonkolva."""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import wu_core
from app.common import CMD_TIMEOUT_RETURNCODE
from app.gui import hwscan as hs
from app.gui import rebind as rb


def _res(stdout='', rc=0, stderr=''):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


class _Host:
    target_os_path = None
    _cancel_flag = False
    _hw_scanning = False
    _task_busy = None
    _si = _nw = None
    hw_updates_pool = []
    _hw_installed_devs = []

    def __init__(self, run):
        self._run = run
        self.events = []

    def emit(self, event, payload=None):
        self.events.append((event, payload))

    def _check_cancel(self):
        return False

    def _check_internet(self):
        return True

    def logs(self):
        return '\n'.join((p or {}).get('log', '') for e, p in self.events if isinstance(p, dict))


class QueryPnpTest(unittest.TestCase):
    def test_failure_is_none_after_retry(self):
        quiet()
        calls = []
        self.assertIsNone(wu_core.query_pnp_devices(lambda *a, **k: calls.append(1) or _res('')))
        self.assertEqual(len(calls), 2)
        self.assertEqual(wu_core.query_pnp_devices(lambda *a, **k: _res(json.dumps({'Name': 'X'}))),
                         [{'Name': 'X'}])


class ScanTest(unittest.TestCase):
    def test_failed_device_query_is_not_clean_machine(self):
        quiet()

        class S(_Host, hs.GuiHwScanMixin):
            pass

        s = S(lambda *a, **k: _res(''))
        with mock.patch.object(hs.threading, 'Thread',
                               side_effect=lambda target, **k: SimpleNamespace(start=target)), \
                mock.patch.object(hs.time, 'sleep'):
            s.start_hw_scan()
        result = [p for e, p in s.events if e == 'hw_scan_result'][0]
        self.assertIn('nem kérdezhető le', result['sys_info'])
        self.assertNotIn('Kész', result['sys_info'])


class WuSearchTimeoutTest(unittest.TestCase):
    def test_timeout_restarts_services(self):
        quiet()
        cmds = []

        def run(cmd, **k):
            cmds.append(cmd[-1])
            return _res('', CMD_TIMEOUT_RETURNCODE, 'IDŐTÚLLÉPÉS') if len(cmds) == 1 else _res()

        class S(_Host, hs.GuiHwScanMixin):
            pass

        self.assertIsNone(S(run)._search_wu_api_inner())
        self.assertTrue(any('Stop-Service wuauserv' in c for c in cmds[1:]))


class ManualWuInstallTest(unittest.TestCase):
    def _install(self, lines, after):
        quiet()

        class S(_Host, hs.GuiHwScanMixin):
            def __init__(self, run):
                super().__init__(run)
                self.lists = [[], after]

            def _get_third_party_drivers(self, problems=None):
                return self.lists.pop(0) if self.lists else after

        s = S(lambda *a, **k: _res())
        proc = SimpleNamespace(stdout=iter(lines), wait=lambda timeout=None: None, returncode=0, pid=1)
        with mock.patch.object(hs.subprocess, 'Popen', return_value=proc):
            out = s._install_wu_api_sync_inner([{'update_id': 'u1', 'name': 'Intel NIC'}])
        return s, out

    def test_false_code4_is_verified_success(self):
        s, (ok, fail, _c) = self._install(
            ['TOTAL: 1\n', 'FAIL: [kód=4] Intel - Net - 12.19.2.57\n', 'DONE: kész\n'],
            [{'published': 'oem5.inf', 'version': '12.19.2.57'}])
        self.assertEqual((ok, fail), (1, 0))
        self.assertIn('valójában FELKERÜLT', s.logs())

    def test_script_without_done_is_reported(self):
        s, (ok, fail, _c) = self._install(['INIT: x\n'], [])
        self.assertEqual((ok, fail), (0, 0))
        self.assertIn('váratlanul leállt', s.logs())


class PresentHwidsTest(unittest.TestCase):
    def test_failed_query_is_not_cached(self):
        quiet()

        class S(_Host, hs.GuiHwScanMixin):
            pass

        s = S(lambda *a, **k: _res(''))
        self.assertEqual(s._present_hwid_sets({'all_hwids': ['PCI\\A']}), [['PCI\\A']])
        self.assertIsNone(getattr(s, '_present_hwids_cache', None))


class CatalogLedgerTest(unittest.TestCase):
    """Egy váratlan kivétel is bekerül a tételes záró mérlegbe (eddig a tétel "eltűnt")."""

    def test_exception_item_is_listed(self):
        quiet()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        class S(_Host, hs.GuiHwScanMixin):
            pass

        s = S(lambda *a, **k: _res())
        with mock.patch.dict(os.environ, {'SystemDrive': tmp}), \
                mock.patch.object(hs, '_app_data_dir', return_value=tmp):
            ok, fail, _c = s._install_catalog_sync([{'url': 'https://x/y.cab'}])   # nincs 'name' -> KeyError
        self.assertEqual(fail, 1)
        self.assertIn('MI TÖRTÉNT TÉTELESEN', s.logs())
        self.assertIn('váratlan hiba', s.logs())


class RebindTest(unittest.TestCase):
    def test_failed_queries_raise_not_nothing_to_do(self):
        quiet()

        class R(_Host, rb.GuiRebindMixin):
            def _get_installed_driver_info(self):
                return {}

            def _get_third_party_drivers(self, problems=None):
                if problems is not None:
                    problems.append('hiba')
                return []

        with self.assertRaises(Exception):
            R(lambda *a, **k: _res(''))._rebind_sweep('rebind')
        with self.assertRaises(Exception):          # eszközlista van, a DISM-lista bukott
            R(lambda *a, **k: _res(json.dumps([{'Name': 'X', 'PNPDeviceID': 'PCI\\VEN_1&DEV_2\\3',
                                                'PNPClass': 'Net', 'HardwareID': ['PCI\\VEN_1&DEV_2'],
                                                'ConfigManagerErrorCode': 0}])))._rebind_sweep('rebind')


if __name__ == '__main__':
    unittest.main()
