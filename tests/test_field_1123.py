"""2026-11-23-i terepi napló (Lenovo ThinkPad T14, LENOVO 20W1S6240A, Build 419): a
„Driver-letiltás + teljes reset + 10 év szünet” után a bejelentkezéskor induló
`--wu-finish` futás felülete 2 mp alatt, hibaüzenet nélkül bezárult, a CLI-tartalék pedig
az első nem-ASCII sornál elszállt (szó szerint a naplóból):

    [MAIN] GUI nem indult el sikeresen, CLI mód következik...
    FATÁLIS HIBA: ... File "driver_tool.py", line 710, in <module>
    PermissionError: [WinError 31] Egy rendszerhez csatlakoztatott eszköz nem működik

A 35 mp-cel későbbi kézi indítás hibátlan volt, és befejezte a WU-műveletet."""

import os
import unittest

import tests
from tests.catalog_stub import quiet
from app import common


class _Raw:
    """CONOUT$-csonk: a megadott bájtokra a terepi WinError 31-et dobja."""

    def __init__(self, fail=lambda b: False, chunk=None):
        self.got, self.fail, self.chunk = b'', fail, chunk

    def write(self, data):
        if self.fail(data):
            raise PermissionError(13, 'Egy rendszerhez csatlakoztatott eszköz nem működik', None, 31)
        n = len(data) if self.chunk is None else min(self.chunk, len(data))
        self.got += bytes(data[:n])
        return n

    def isatty(self):
        return True

    def fileno(self):
        return 1


class TolerantConsoleTest(unittest.TestCase):
    LINE = '  ⚠️  GUI nem elérhető - CLI mód automatikusan aktiválva\n'

    def setUp(self):
        quiet()

    def test_nem_ascii_bukas_utan_asciival_megy_tovabb(self):
        raw = _Raw(fail=lambda b: any(c > 127 for c in b))
        w = common.TolerantConsoleWriter(raw)
        w.write('=' * 10 + '\n')
        self.assertEqual(w.write(self.LINE), len(self.LINE))      # a régi kód itt elszállt
        w.write('Menü\n')
        self.assertEqual(raw.got.decode('ascii'),
                         '=' * 10 + '\n' + self.LINE.encode('ascii', 'replace').decode() + 'Menü\n'.encode('ascii', 'replace').decode())

    def test_teljesen_halott_konzol_sem_dob_es_egyszer_naplozza(self):
        import logging
        logging.disable(logging.NOTSET)
        self.addCleanup(quiet)
        w = common.TolerantConsoleWriter(_Raw(fail=lambda b: True))
        with self.assertLogs(level='WARNING') as cm:
            for _ in range(50):
                w.write(self.LINE)
            print('x', file=w)
        self.assertEqual(len(cm.records), 2)       # egy ASCII-váltás + egy „elvész”, nem 50

    def test_reszleges_iras_teljesen_kimegy(self):
        raw = _Raw(chunk=7)
        common.TolerantConsoleWriter(raw).write(self.LINE)
        self.assertEqual(raw.got.decode('utf-8'), self.LINE)

    def test_ensure_console_a_hibaturo_irot_koti_be(self):
        import inspect
        src = inspect.getsource(common.ensure_console)
        self.assertIn("TolerantConsoleWriter(open(name, 'wb', buffering=0))", src)


class LogonGuiRetryTest(unittest.TestCase):
    def test_bejelentkezeskori_inditas_egyszer_ujraprobal(self):
        for flag in ('--wu-finish', '--resume-autofix', '--resume-step1', '--resume-clockfix'):
            self.assertEqual(common.logon_gui_retry_args([flag]), [flag, common.GUI_RETRY_FLAG])
            self.assertIsNone(common.logon_gui_retry_args([flag, common.GUI_RETRY_FLAG]))

    def test_kezi_inditas_nem_probal_ujra(self):
        self.assertIsNone(common.logon_gui_retry_args([]))
        self.assertIsNone(common.logon_gui_retry_args(['--force-gui']))

    def test_a_cli_tartalek_elott_ujraprobal(self):
        src = open(os.path.join(tests.ROOT, 'driver_tool.py'), encoding='utf-8').read()
        retry = src.index('retry_args = logon_gui_retry_args(sys.argv[1:])')
        self.assertLess(retry, src.index('GUI nem elérhető - CLI mód automatikusan aktiválva'))
        self.assertLess(src.index('if gui_failed:'), retry)


if __name__ == '__main__':
    unittest.main()
