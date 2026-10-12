"""A 2. teljes review-kör (2026-10-12) javításai: időzítés (elavult eredmény), nemtudás
nem tény (olvashatatlan INF, elbukott visszaolvasás), hamis siker (HTML-válasz, BitLocker),
és a stressz-teszt energiazára (minimum/maximum a tényleges érték helyett).
Valódi rendszerhez semmi nem nyúl: minden futtató, registry és hálózat csonkolva."""
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import benchmark_core, bitlocker_core, driverusage_core, logupload_core, storeapps_core
from app.gui import display as gdisp, stress as gstress


def _res(stdout='', rc=0, stderr=''):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


# A fejlesztői gépen élőben mért `powercfg /query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE` alakja,
# laptopos értékekkel (AC 600 mp, DC 300 mp).
POWERCFG_VIDEOIDLE = """Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)
  Subgroup GUID: 7516b95f-f776-4464-8c53-06167f40cc99  (Display)
    Power Setting GUID: 3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e  (Turn off display after)
      Minimum Possible Setting: 0x00000000
      Maximum Possible Setting: 0xffffffff
      Possible Settings increment: 0x00000001
      Possible Settings units: Seconds
    Current AC Power Setting Index: 0x00000258
    Current DC Power Setting Index: 0x0000012c
"""


class DisplayStaleTest(unittest.TestCase):
    def test_older_read_is_not_emitted(self):
        quiet()
        gate = threading.Event()
        calls = {'n': 0}

        class D(gdisp.GuiDisplayMixin):
            def __init__(self):
                self.events = []

            def emit(self, ev, p=None):
                self.events.append((ev, p))

            def _collect_display_state(self):
                calls['n'] += 1
                if calls['n'] == 1:
                    gate.wait(5)                    # a nézet-megnyitáskori, lassú beolvasás
                    return {'displays': [{'index': 0, 'hdr': {'enabled': False}}], 'acm_guard': False}
                return {'displays': [{'index': 0, 'hdr': {'enabled': True}}], 'acm_guard': False}

        d = D()
        t = threading.Thread(target=d._emit_display_state)
        t.start()
        while calls['n'] < 1:
            pass
        d._emit_display_state()                     # a HDR-kapcsolás utáni friss beolvasás
        gate.set()
        t.join(5)
        shown = [p for e, p in d.events if e == 'display_info']
        self.assertEqual(len(shown), 1)
        self.assertTrue(shown[0]['displays'][0]['hdr']['enabled'])


class UsageUnreadableInfTest(unittest.TestCase):
    def test_unreadable_inf_is_unknown_not_unused(self):
        quiet()
        facts = driverusage_core.read_inf_facts(['oem5.inf', 'oem6.inf'], inf_dir='X:\\nincs',
                                                reader=lambda p: '' if p.endswith('oem5.inf') else
                                                '[Version]\nClass=System\n')
        usage = driverusage_core.build_usage({'devices': [], 'services': [], 'filters': []}, facts,
                                             ['oem5.inf', 'oem6.inf'])
        self.assertEqual(usage['oem5.inf']['state'], driverusage_core.USAGE_UNKNOWN)
        self.assertEqual(usage['oem6.inf']['state'], driverusage_core.USAGE_UNUSED)


class StoreAppsReadbackTest(unittest.TestCase):
    def test_failed_final_readback_is_not_failure(self):
        quiet()
        logs = []
        req = {'nvidiacorp.nvidiacontrolpanel_56jybvy8sckqj':
               {'pfn': 'NVIDIACorp.NVIDIAControlPanel_56jybvy8sckqj', 'min_version': '8.1', 'infs': ['oem5.inf']}}
        with mock.patch.object(storeapps_core, 'collect_required_store_apps', return_value=req), \
                mock.patch.object(storeapps_core, 'installed_store_apps', side_effect=[{}, None]), \
                mock.patch.object(storeapps_core, 'lookup_store_product', return_value=('9NF8H0H7WMLT', 'NVIDIA')), \
                mock.patch.object(storeapps_core, 'install_via_winget', return_value=('ok', '')):
            rep = storeapps_core.sync_driver_store_apps(lambda *a, **k: _res(), logs.append, ['oem5.inf'],
                                                        lambda u: '')
        self.assertEqual(rep['failed'], [])
        self.assertTrue(rep['unverified'])
        self.assertTrue(any('nem ellenőrizhető' in m for m in logs))
        self.assertFalse(any('NINCS fent' in m for m in logs))


class HtmlResponseTest(unittest.TestCase):
    HTML = '<!DOCTYPE html><html><body>Script function not found: doPost</body></html>'

    def test_log_upload(self):
        quiet()
        with mock.patch.object(logupload_core, 'log_paths', return_value=['x']), \
                mock.patch.object(logupload_core, 'pack_logs', return_value=('YmxvYg==', {'raw': 10, 'packed': 5, 'files': 1})), \
                mock.patch.object(logupload_core, 'machine_label', return_value='PC'):
            ok, msg = logupload_core.upload_logs(lambda *a, **k: _res(), 'https://x', http=lambda *a: self.HTML)
            self.assertFalse(ok)
            ok2, _ = logupload_core.upload_logs(lambda *a, **k: _res(), 'https://x', http=lambda *a: '{"ok": true}')
            self.assertTrue(ok2)

    def test_benchmark_upload(self):
        quiet()
        with mock.patch.object(benchmark_core, 'resolve_endpoint', return_value='https://x'), \
                mock.patch.object(benchmark_core, '_http_request', return_value=self.HTML):
            with self.assertRaises(Exception):
                benchmark_core.upload_result(lambda *a, **k: _res(), {'a': 1})

    def test_tls_eof_uses_schannel_fallback(self):
        quiet()
        err = urllib.error.URLError('[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol')
        with mock.patch('urllib.request.urlopen', side_effect=err), \
                mock.patch.object(benchmark_core, '_http_via_powershell', return_value='{"ok":true}') as ps:
            self.assertEqual(benchmark_core._http_request(lambda *a, **k: _res(), 'https://x'), '{"ok":true}')
        ps.assert_called_once()


class StressPowerTest(unittest.TestCase):
    class S(gstress.GuiStressMixin):
        def __init__(self, out):
            self.cmds = []
            self._out = out

        def _run(self, cmd, **k):
            self.cmds.append(cmd)
            return _res(self._out)

    def test_reads_ac_dc_not_min_max(self):
        quiet()
        self.assertEqual(self.S(POWERCFG_VIDEOIDLE)._query_power_setting('SUB_VIDEO', 'VIDEOIDLE'), (600, 300))

    def test_buggy_old_backup_is_not_restored(self):
        quiet()
        s = self.S('')
        vals = [('SUB_VIDEO_VIDEOIDLE_AC', 0, 4), ('SUB_VIDEO_VIDEOIDLE_DC', 0xFFFFFFFF, 4)]
        key = mock.MagicMock()
        key.__enter__.return_value = key
        with mock.patch.object(gstress.winreg, 'OpenKey', return_value=key), \
                mock.patch.object(gstress.winreg, 'EnumValue', side_effect=vals + [OSError()]), \
                mock.patch.object(gstress.winreg, 'DeleteKey'):
            s._restore_power_after_stress()
        self.assertFalse([c for c in s.cmds if 'setacvalueindex' in c or '/setacvalueindex' in c])


class BitLockerTest(unittest.TestCase):
    def test_unchanged_state_is_failure(self):
        quiet()
        outs = iter([_res('', 0), _res('Titkosítva (Aktív)')])
        ok, why = bitlocker_core.disable_bitlocker(lambda *a, **k: next(outs))
        self.assertFalse(ok)
        self.assertIn('nem változott', why)

    def test_decryption_started_is_success(self):
        quiet()
        outs = iter([_res('', 0), _res('Dekódolás folyamatban (98%)')])
        self.assertEqual(bitlocker_core.disable_bitlocker(lambda *a, **k: next(outs)), (True, ''))


class LogPeriodFilterTest(unittest.TestCase):
    """2026-10-12: a napló-letöltés időszak-szűrője. A dátum a futás mappájából / fájlnevéből
    jön; dátum nélküli sor csak az 'Összes'-be kerül (nem állítjuk, hogy belefér)."""

    def test_period_bounds(self):
        import datetime as dt
        today = dt.date(2026, 10, 12)
        rows = [
            {'folder': '2026-10-12 15-02 - build373 - PC1 - LENOVO 20KGS4032P', 'name': 'a.log.gz'},
            {'folder': 'build300 - PC2 - Dell - 2026-10-06 09-00', 'name': 'b.log.gz'},   # régi alak
            {'folder': 'x', 'name': '2026-09-13_10-00-00_PC3_build360.log'},             # fájlnévből
            {'folder': '2025-10-13 08-00 - build200 - PC4 - HP', 'name': 'c.log.gz'},
            {'folder': '2025-10-12 08-00 - build200 - PC5 - HP', 'name': 'd.log.gz'},
            {'folder': 'nincs datum', 'name': 'e.log.gz', 'date': 'tegnap'},
        ]
        f = lambda p: [r['folder'] for r in logupload_core.filter_rows_by_period(rows, p, today)[0]]
        self.assertEqual(len(f('today')), 1)
        self.assertEqual(len(f('7')), 2)            # 10-06 még benne (ma + 6 nap)
        self.assertEqual(len(f('30')), 3)           # 09-13 a 30. nap
        self.assertEqual(len(f('365')), 4)          # 2025-10-13 benne, 2025-10-12 kívül
        self.assertEqual(len(f('all')), 6)
        self.assertEqual(logupload_core.filter_rows_by_period(rows, '7', today)[1], 1)
        self.assertEqual(len(f('bogus')), 6)        # ismeretlen időszak = mind


if __name__ == '__main__':
    unittest.main()
