"""RAM TEST mag (app/ramtest_core.py): a három memóriateszt kimenetének értelmezése, a HCI-
felosztás, a WHEA-lekérdezés olvasása és a záró verdikt. A bemenetek ÉLŐ futásokból vannak
(2026-10-26, Ryzen 5 5600, 64 GB): TM5 0.13.1 Log.txt, y-cruncher 0.8.7 kimenet, HCI 7.0 ablakszövegek."""
import datetime
import unittest

from app import ramtest_core as rc

# TM5 0.13.1 Log.txt, élőben (a második futás leállítva).
TM5_LOG_STOPPED = """
-------- TestMem5 0.13.1 2026-10-26 20:10 --------
0:00:00  Configuration: Extreme @ anta777
0:00:00  Testing 4576 MB x 12 (83%) out of 65462 MB
0:01:29  Testing stopped by user
"""
TM5_LOG_RUNNING = """
-------- TestMem5 0.13.1 2026-10-26 20:14 --------
0:00:00  Configuration: Mini @ test
0:00:00  Testing 4592 MB x 12 (84%) out of 65462 MB
"""

# y-cruncher 0.8.7 "stress -M:1G -D:10 -TL:25 VT3", élőben (a vége).
YC_OK = """
  6   Stop on Error:      Enabled
 18  VT3   Vector Transform (v3)         85.3 MiB  AVX2 Float      ----|-----
Iteration: 0  Total Elapsed Time: 0.151 seconds  ( 0.003 minutes )
Running VT3: Passed  Test Speed:  5.33 * 10^09  bits / sec

Iteration: 1  Total Elapsed Time: 10.250 seconds  ( 0.171 minutes )
Running VT3: Passed  Test Speed:  5.35 * 10^09  bits / sec

Test Finished. Waiting for threads to terminate...
"""


class Tm5LogTest(unittest.TestCase):
    def test_live_logs(self):
        self.assertEqual(rc.parse_tm5_log(TM5_LOG_STOPPED)['state'], 'stopped')
        self.assertEqual(rc.parse_tm5_log(TM5_LOG_RUNNING)['state'], 'running')
        self.assertEqual(rc.parse_tm5_log('')['state'], 'empty')

    def test_only_last_run_counts(self):
        old_error = TM5_LOG_RUNNING + "0:03:10  Error in test 4\n"
        self.assertEqual(rc.parse_tm5_log(old_error)['state'], 'errors')
        self.assertEqual(rc.parse_tm5_log(old_error + TM5_LOG_RUNNING)['state'], 'running')

    def test_completed_and_errors(self):
        ok = TM5_LOG_RUNNING + "1:02:00  Testing completed in 1:02:00, no errors.\n"
        self.assertEqual(rc.parse_tm5_log(ok)['state'], 'ok')
        bad = TM5_LOG_RUNNING + "0:10:00  Error in test 4\n1:02:00  Testing completed, detected 3 error(s).\n"
        r = rc.parse_tm5_log(bad)
        self.assertEqual(r['state'], 'errors')
        self.assertEqual(r['errors'], 3)
        crash = TM5_LOG_RUNNING + "0:10:00  One of the testing threads has been crashed.\n"
        self.assertEqual(rc.parse_tm5_log(crash)['state'], 'errors')

    def test_tool_error_is_not_a_ram_error(self):
        t = TM5_LOG_RUNNING + "0:00:01  Failed to allocate memory for testing.\n"
        self.assertEqual(rc.parse_tm5_log(t)['state'], 'tool_error')


class YcruncherTest(unittest.TestCase):
    def test_live_ok(self):
        r = rc.parse_ycruncher_output(YC_OK)
        self.assertEqual((r['passed'], r['errors'], r['finished']), (2, [], True))

    def test_error_line(self):
        r = rc.parse_ycruncher_output(YC_OK.replace('Running VT3: Passed  Test Speed:  5.35',
                                                    'Running VT3: Failed  Errors Detected', 1))
        self.assertEqual(len(r['errors']), 1)

    def test_args(self):
        a = rc.ycruncher_args(900, 50.0)
        self.assertEqual(a[:3], ['skip-warnings', 'pause:-2', 'stress'])
        self.assertIn('-M:35G', a)
        self.assertIn('-TL:900', a)
        self.assertEqual(a[-2:], rc.YC_TESTS)


class HciTest(unittest.TestCase):
    def test_live_texts(self):
        self.assertEqual(rc.parse_hci_status(' [/]   5.1% Coverage, 0 Errors'), (5.1, 0))
        self.assertEqual(rc.parse_hci_status(' [\\]   112.4% Coverage, 2 Errors'), (112.4, 2))
        self.assertIsNone(rc.parse_hci_status('Standard (free) version'))
        self.assertEqual(rc.parse_hci_title_errors('[0 Errors] MemTest'), 0)
        self.assertEqual(rc.parse_hci_title_errors(' [3 errors] MemTest '), 3)
        self.assertIsNone(rc.parse_hci_title_errors('Welcome, New MemTest  User'))

    def test_alloc_failure_and_exit_codes(self):
        # Terepen (Build 396): a 1999-es Static és a felugró ablak címe is ez volt.
        self.assertTrue(rc.hci_alloc_failed('Could not allocate 3397 MB'))
        self.assertFalse(rc.hci_alloc_failed(' [/]   5.1% Coverage, 0 Errors'))
        self.assertFalse(rc.hci_alloc_failed(None))
        # Terepen az y-cruncher: rc=3221225786
        self.assertIn('0xC000013A', rc.describe_exit_code(3221225786))
        self.assertIn('STATUS_CONTROL_C_EXIT', rc.describe_exit_code(-1073741510))
        self.assertIn('ismeretlen', rc.describe_exit_code(42))

    def test_plan_never_exceeds_field_limit(self):
        # Terepen 54.6 GB szabad, 12 szál -> 14 x 3397 MB lett, és a 3397 nem fért bele.
        n, per = rc.hci_plan(54.6 * 1024, 12)
        self.assertLessEqual(per, 2048)

    def test_grid_all_on_screen_no_resize(self):
        area = (0, 0, 2560, 1368)  # a felhasználó munkaterülete, élőben
        w, h = 326, 377            # a HCI-ablak látható kerete, élőben
        for n in (1, 6, 12, 21, 23, 40):
            g = rc.hci_grid(n, w, h, area)
            self.assertEqual(len(g), n)
            for x, y, _r, _c in g:
                self.assertTrue(0 <= x and x + w <= 2560, (n, x))
                self.assertTrue(0 <= y and y + h <= 1368, (n, y))
            if n <= 21:  # 7 x 3 átfedés nélkül elfér
                self.assertEqual(len({(x // w, y // h) for x, y, _r, _c in g}), n)
        g = rc.hci_grid(23, w, h, area)
        self.assertEqual(max(r for *_, r, _c in g) + 1, 4)
        self.assertEqual(max(c for *_, c in g) + 1, 7)

    def test_plan(self):
        n, per = rc.hci_plan(55 * 1024, 12)
        self.assertGreaterEqual(n, 12)
        self.assertLessEqual(per, rc.HCI_MAX_MB_PER_INSTANCE)
        self.assertLessEqual(n * per, 55 * 1024 * rc.HCI_MEM_FRACTION)
        n, per = rc.hci_plan(3000, 16)  # kevés RAM, sok szál: egy példány se legyen apró
        self.assertGreaterEqual(per, rc.HCI_MIN_MB_PER_INSTANCE)
        self.assertIsNone(rc.hci_plan(100, 4))
        self.assertIsNone(rc.hci_plan(None, 4))


class Tm5ConfigTest(unittest.TestCase):
    SRC = ("Memory Test config file v0.02\n\n[Main Section]\nConfig Name=Extreme1\nConfig Author=anta777\n"
           "Tests=16\nTime (%)=500\nCycles=3\n\n[Test0]\nTime (%)=100\n")

    def test_cycles(self):
        t = rc.tm5_config_text(self.SRC, 1)
        self.assertIn('Cycles=1\n', t)
        self.assertNotIn('Cycles=3', t)
        self.assertIn('Config Name=DriverVarazslo', t)
        self.assertIn('[Test0]\nTime (%)=100', t)
        with self.assertRaises(ValueError):
            rc.tm5_config_text('[Main Section]\n', 1)

    def test_crlf_kept(self):
        """Build 396, terepen: az LF-re alakított konfigot a TM5 "nem találta"."""
        src = self.SRC.replace('\n', '\r\n')
        t = rc.tm5_config_text(src, 1)
        self.assertEqual(t.count('\r\n'), src.count('\r\n'))
        self.assertEqual(t.count('\n'), t.count('\r\n'))
        self.assertIn('Config Name=DriverVarazslo\r\n', t)

    def test_real_file_roundtrip(self):
        """A valódi írás/olvasás útja (ramtest._ramtest_tm5): newline=''-vel bájtra megmarad a CRLF."""
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, 'a.cfg'), os.path.join(tmp, 'b.cfg')
            with open(src, 'wb') as f:
                f.write(self.SRC.replace('\n', '\r\n').encode('latin-1'))
            with open(src, 'r', encoding='latin-1', newline='') as f:
                cfg = rc.tm5_config_text(f.read(), 1)
            with open(dst, 'w', encoding='latin-1', newline='') as f:
                f.write(cfg)
            data = open(dst, 'rb').read()
            self.assertEqual(data.count(b'\r\n'), data.count(b'\n'))
            self.assertTrue(data.startswith(b'Memory Test config file v0.02\r\n'))


class WheaTest(unittest.TestCase):
    def test_parse(self):
        out = "EV|19|2026-10-26 20:01:02|Figyelmeztetés|Egy javított hardverhiba történt.\nDONE\n"
        ev = rc.parse_whea_output(out)
        self.assertEqual(ev[0]['id'], 19)
        self.assertEqual(rc.parse_whea_output('DONE'), [])
        self.assertIsNone(rc.parse_whea_output('ERR|Access denied'))
        self.assertIsNone(rc.parse_whea_output(''))  # nem ért végig: nem "nincs esemény"
        self.assertIsNone(rc.parse_whea_output('EV|19|x|y|z'))

    def test_query_has_timestamp(self):
        ps = rc.whea_query_ps(datetime.datetime(2026, 10, 26, 20, 5, 9))
        self.assertIn("'20261026200509'", ps)
        self.assertIn('NoMatchingEventsFound', ps)


class VerdictTest(unittest.TestCase):
    OK = {k: {'state': 'ok'} for k in rc.STEP_NAMES}

    def test_ok_needs_clean_whea(self):
        self.assertTrue(rc.ramtest_verdict(self.OK, [])['ok'])
        v = rc.ramtest_verdict(self.OK, None)
        self.assertFalse(v['ok'])
        self.assertIn('NEM ELDÖNTHETŐ', v['headline'])

    def test_errors(self):
        r = dict(self.OK, tm5={'state': 'errors'}, ycruncher={'state': 'skipped'}, hcimemtest={'state': 'skipped'})
        v = rc.ramtest_verdict(r, [])
        self.assertIn('NEM STABIL', v['headline'])
        self.assertTrue(any('XMP' in a for a in v['advice']))

    def test_whea19_fails_even_if_tests_pass(self):
        v = rc.ramtest_verdict(self.OK, [{'id': 19, 'time': '', 'level': '', 'message': ''}])
        self.assertFalse(v['ok'])
        self.assertTrue(any('SOC' in a for a in v['advice']))

    def test_missing_tool_is_not_success(self):
        r = dict(self.OK, hcimemtest={'state': 'tool_error'})
        self.assertFalse(rc.ramtest_verdict(r, [])['ok'])
        r = dict(self.OK, hcimemtest={'state': 'cancelled'})
        self.assertFalse(rc.ramtest_verdict(r, [])['ok'])


class AsciiToolCopyTest(unittest.TestCase):
    """Build 395, terepen: a TM5 a "...\\DriverVarázsló_Stress\\..." útvonalról nem találta a
    konfigját. Az ékezetes útvonalú programot ASCII mappába másoljuk, az ASCII-t nem."""

    def test_copy_only_non_ascii(self):
        import os
        import tempfile
        from unittest import mock
        import app.gui.stress as stress_mod
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'DriverVarázsló_Stress', 'TestMem5')
            os.makedirs(os.path.join(src, 'bin'))
            for rel in ('TM5.exe', os.path.join('bin', 'x.cfg')):
                open(os.path.join(src, rel), 'w').close()
            appdata = os.path.join(tmp, 'DriverVarazslo')
            api = stress_mod.GuiStressMixin()
            with mock.patch.object(stress_mod, '_app_data_dir', return_value=appdata):
                new = api._ascii_tool_copy(os.path.join(src, 'TM5.exe'))
                self.assertEqual(new, os.path.join(appdata, 'TestMem5', 'TM5.exe'))
                self.assertTrue(new.isascii())
                self.assertTrue(os.path.exists(os.path.join(appdata, 'TestMem5', 'bin', 'x.cfg')))
                self.assertEqual(api._ascii_tool_copy(new), new)  # ASCII: nincs másolás


class RamTestLogUploadTest(unittest.TestCase):
    """A RAM TEST végén MINDIG felmegy a napló: rendes vég és hiba esetén is (2026-10-27)."""

    def _api(self, download_ok):
        from app.gui.ramtest import GuiRamTestMixin

        class F(GuiRamTestMixin):
            def __init__(self):
                self.events, self.uploads = [], []
            def emit(self, ev, data=None): self.events.append(ev)
            def _safe_thread(self, name, fn): fn()
            def _lock_power_for_stress(self): pass
            def _restore_power_after_stress(self): pass
            def _ramtest_whea(self, since): return []
            def _ramtest_kill(self, pids=None, images=None): pass
            def _check_cancel(self): return False
            def _task_dl_progress(self, task): return None
            def _download_stresstools(self, progress=None, keys=(), password=''): return 'X:\\st' if download_ok else None
            def _find_stress_tool_exes(self, d, names): return {}
            def upload_run_log(self, task, outcome): self.uploads.append((task, outcome, list(self.events)))
        return F()

    def test_upload_on_finish_before_complete(self):
        api = self._api(True)
        api.start_ram_test('gyors')
        self.assertEqual(len(api.uploads), 1)
        task, outcome, before = api.uploads[0]
        self.assertEqual(task, 'ramtest')
        self.assertIn('Gyors', outcome)
        self.assertNotIn('task_complete', before)  # a feltöltés sora még a nyitott ablakba megy
        self.assertIn('task_complete', api.events)

    def test_time_box(self):
        """Gyors mód: a keret végén hiba nélkül 'ok', de egy addig talált hiba nyer."""
        import time
        api = self._api(True)
        boxed = lambda: {'state': 'ok', 'detail': 'keret'}
        past = time.monotonic() - 1
        far = time.monotonic() + 3600
        self.assertEqual(api._ramtest_wait(lambda: None, far, None, {'n': 0}, lambda: None,
                                           box_end=past, box_result=boxed)['detail'], 'keret')
        self.assertEqual(api._ramtest_wait(lambda: {'state': 'errors'}, far, None, {'n': 0}, lambda: None,
                                           box_end=past, box_result=boxed)['state'], 'errors')
        self.assertEqual(rc.RAMTEST_MODES['gyors']['box_seconds'] * 3, 27 * 60)  # 3 lépés, kb. 30 perc

    def test_upload_on_error(self):
        api = self._api(False)
        api.start_ram_test('ejszakas')
        self.assertEqual(len(api.uploads), 1)
        self.assertIn('HIBA', api.uploads[0][1])
        self.assertIn('task_error', api.events)


if __name__ == '__main__':
    unittest.main()
