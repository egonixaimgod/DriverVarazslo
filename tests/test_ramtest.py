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


if __name__ == '__main__':
    unittest.main()
