"""A vírusirtó okozta akadás (2026-10-15, Lenovo 20LE, Build 382): minden `pnputil
/export-driver` 300 mp-ig lógott, a fix 2 órán át állt a törlés előtt. A vírusirtó
eltávolítása után a fix azonnal továbbment.

Ellenőrzi: (1) egy beragadt export után egyetlen további export sem indul (net + teljes
mentés + Wi-Fi együtt); (2) a vírusirtó-verdikt három szintje + a nem eldönthető eset;
(3) a diagnózis egy beragadt PowerShellre sem dob. Valódi rendszerhez semmi nem nyúl."""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import avcheck_core as av
from app import wu_core
from app.common import CMD_TIMEOUT_RETURNCODE


def setUpModule():
    quiet()


def _res(rc=0, out=''):
    return SimpleNamespace(returncode=rc, stdout=out, stderr='')


class ExportGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.object(wu_core, '_app_data_dir', return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_first_timeout_stops_every_later_export(self):
        calls = []

        def run(cmd, timeout=None):
            calls.append((cmd, timeout))
            return _res(CMD_TIMEOUT_RETURNCODE)

        net = [{'published': f'oem{i}.inf', 'original': 'e1d68x64.inf', 'class': 'Net'} for i in range(8)]
        allpk = net + [{'published': f'oem{i}.inf', 'class': 'System'} for i in range(20, 174)]
        guard = {}
        self.assertEqual(wu_core._export_net_driver_backup(run, net, guard=guard), 0)
        ok, _mb, _s = wu_core.export_drivers_backup(run, allpk, guard=guard)
        wu_core.export_wifi_driver_backup(run, [{'published': 'oem93.inf'}], guard=guard)
        self.assertEqual(ok, 0)
        self.assertEqual(len(calls), 1, 'a beragadás után nem indulhat újabb export')
        self.assertEqual(calls[0][1], wu_core.EXPORT_DRIVER_TIMEOUT)
        self.assertEqual(guard['hung'], 'oem0.inf')

    def test_healthy_exports_all_run(self):
        calls = []
        guard = {}
        net = [{'published': f'oem{i}.inf', 'class': 'Net'} for i in range(3)]
        n = wu_core._export_net_driver_backup(lambda c, timeout=None: calls.append(c) or _res(0), net, guard=guard)
        self.assertEqual(n, 3)
        self.assertEqual(len(calls), 3)
        self.assertNotIn('hung', guard)


class VerdictTest(unittest.TestCase):
    DEF_ON = {'Name': 'Windows Defender', 'State': 397568}      # 0x061100 - BE
    DEF_OFF = {'Name': 'Windows Defender', 'State': 393472}     # 0x060100 - KI
    ESET_ON = {'Name': 'ESET Security', 'State': 266240}        # 0x041000 - BE

    def test_defender_event_about_us_is_proof(self):
        ev = {'Id': 1123, 'Time': '2026-10-15 14:30:00',
              'Msg': 'Controlled Folder Access blocked C:\\Windows\\System32\\pnputil.exe from making changes'}
        r = av.evaluate({'Products': [self.DEF_ON], 'ProductsOk': True,
                         'Events': [ev], 'EventsOk': True, 'Procs': []})
        self.assertEqual(r['verdict'], 'defender')
        self.assertIn('BLOKKOLJA', av.report_lines(r, 'a driver-mentést')[0])

    def test_unrelated_defender_event_is_not_proof(self):
        ev = {'Id': 1116, 'Msg': 'Észlelt fenyegetés: C:\\Users\\x\\Downloads\\crack.exe'}
        r = av.evaluate({'Products': [self.DEF_ON], 'ProductsOk': True,
                         'Events': ev, 'EventsOk': True, 'Procs': []})
        self.assertEqual(r['verdict'], 'none')

    def test_third_party_av_is_probable_not_proven(self):
        r = av.evaluate({'Products': [self.DEF_OFF, self.ESET_ON], 'ProductsOk': True,
                         'Events': [], 'EventsOk': True, 'Procs': []})
        self.assertEqual(r['verdict'], 'thirdparty')
        lines = av.report_lines(r, 'a driver-mentést')
        self.assertIn('VALÓSZÍNŰLEG', lines[0])
        self.assertIn('ESET Security', lines[0])
        self.assertIn('nem bizonyított', lines[1])

    def test_disabled_third_party_av_does_not_count(self):
        off = {'Name': 'ESET Security', 'State': 262144}         # 0x040000 - KI
        r = av.evaluate({'Products': [self.DEF_ON, off], 'ProductsOk': True,
                         'Events': [], 'EventsOk': True, 'Procs': {'Name': 'drvinst', 'Id': 4, 'AgeS': 3600}})
        self.assertEqual(r['verdict'], 'none')
        lines = av.report_lines(r, 'a driver-mentést')
        self.assertIn('NEM A VÍRUSIRTÓ', lines[0])
        self.assertIn('drvinst', lines[1])

    def test_failed_query_is_unknown_not_none(self):
        self.assertEqual(av.evaluate(None)['verdict'], 'unknown')
        r = av.evaluate({'Products': [self.DEF_ON], 'ProductsOk': True, 'Events': [], 'EventsOk': False})
        self.assertEqual(r['verdict'], 'unknown')
        self.assertIn('Nem eldönthető', av.report_lines(r, 'x')[0])

    def test_diagnose_survives_hung_powershell(self):
        with mock.patch.object(av, '_log_setupapi_tail'):
            r, lines = av.diagnose_hang(lambda c, timeout=None: _res(CMD_TIMEOUT_RETURNCODE), 'a driver-mentést')
        self.assertEqual(r['verdict'], 'unknown')
        self.assertTrue(lines)

    def test_diagnose_parses_powershell_json(self):
        out = json.dumps({'Products': self.ESET_ON, 'ProductsOk': True, 'Events': [], 'EventsOk': True, 'Procs': []})
        with mock.patch.object(av, '_log_setupapi_tail'):
            r, _ = av.diagnose_hang(lambda c, timeout=None: _res(0, out), 'a driver-mentést')
        self.assertEqual(r['verdict'], 'thirdparty')

    def test_script_has_no_placeholders_left(self):
        s = av._script()
        self.assertNotIn('%', s.replace('%{', ''))
        self.assertIn('1123', s)


if __name__ == '__main__':
    unittest.main()
