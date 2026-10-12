"""Mentés / visszaállítás / BCD review-javításai (2026-10-12): a DISM, pnputil, robocopy,
bcdboot kilépési kódja is a verdikt része, és a gyors visszaállítási pont nem "✅", ha a
Windows elutasította. Valódi rendszerhez semmi nem nyúl: a futtató és a Popen-es
`_stream_cmd` csonkolva, minden útvonal temp mappában."""
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import backup_core, bcd_core


def _res(stdout='', rc=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=rc)


class _Tmp(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)


class QuickRestorePointTest(unittest.TestCase):
    def test_refused_point_is_not_success(self):
        quiet()
        self.assertFalse(backup_core.create_restore_point_quick(
            lambda *a, **k: _res('RPFAIL: A visszaállítási pont nem hozható létre'), 'x'))
        self.assertTrue(backup_core.create_restore_point_quick(lambda *a, **k: _res('RPOK'), 'x'))


class RepairBcdTest(_Tmp):
    def _repair(self, rc):
        os.makedirs(os.path.join(self.tmp, 'Windows'), exist_ok=True)
        with mock.patch.object(bcd_core, '_find_efi_partition', return_value=(None, None, None)):
            return bcd_core.repair_bcd(lambda *a, **k: _res('', rc), lambda m: None, self.tmp)

    def test_all_steps_failed_is_false(self):
        self.assertFalse(self._repair(1))

    def test_bcdboot_ok_is_true(self):
        self.assertTrue(self._repair(0))


class RestoreVerdictTest(_Tmp):
    def _restore(self, online, rc):
        src = os.path.join(self.tmp, 'src')
        os.makedirs(src, exist_ok=True)
        with mock.patch.object(backup_core, '_stream_cmd', return_value=(rc, False)), \
                mock.patch.object(backup_core.bcd_core, 'repair_bcd', return_value=True), \
                mock.patch.object(backup_core.time, 'sleep'):
            return backup_core.run_restore(lambda *a, **k: _res(), lambda m: None, None, None, None,
                                           online, src, None if online else os.path.join(self.tmp, 'tgt'))

    def test_offline_dism_failure_is_errors(self):
        self.assertEqual(self._restore(False, 5), 'errors')
        self.assertEqual(self._restore(False, 0), 'ok')

    def test_online_pnputil_codes(self):
        self.assertEqual(self._restore(True, 1), 'errors')
        self.assertEqual(self._restore(True, 259), 'ok')       # nem volt mit telepíteni
        self.assertEqual(self._restore(True, 3010), 'ok')

    def test_failed_bcd_is_errors(self):
        src = os.path.join(self.tmp, 'src2')
        os.makedirs(src, exist_ok=True)
        with mock.patch.object(backup_core, '_stream_cmd', return_value=(0, False)), \
                mock.patch.object(backup_core.bcd_core, 'repair_bcd', return_value=False):
            self.assertEqual(backup_core.run_restore(lambda *a, **k: _res(), lambda m: None, None, None, None,
                                                     False, src, os.path.join(self.tmp, 'tgt2')), 'errors')


class FullBackupCopyTest(_Tmp):
    def test_robocopy_error_marks_copy_not_ok(self):
        win = os.path.join(self.tmp, 'img')
        os.makedirs(os.path.join(win, 'Windows', 'System32', 'DriverStore', 'FileRepository'))

        def run(cmd, **k):
            return _res('', 16 if cmd[0] == 'robocopy' else 0)

        r = backup_core.backup_all_drivers(run, lambda m: None, None, self.tmp, win)
        self.assertEqual(r['status'], 'ok')
        self.assertFalse(r['copy_ok'])


if __name__ == '__main__':
    unittest.main()
