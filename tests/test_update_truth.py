"""Frissítő és letöltő review-javításai (2026-10-12): csonka letöltés nem "kész", és a
CDN-ről jött RÉGI exe-vel nem cseréljük le a programot (végtelen frissítési kör).
Hálózat nincs: az urlopen és a letöltő csonkolva, minden fájl temp mappába megy."""
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import common, update_core


class _Resp(io.BytesIO):
    def __init__(self, body, length):
        super().__init__(body)
        self.headers = {'Content-Length': str(length)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Tmp(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TruncatedDownloadTest(_Tmp):
    def test_short_body_raises_and_removes_file(self):
        dest = os.path.join(self.tmp, 'x.exe')
        with mock.patch('urllib.request.urlopen', return_value=_Resp(b'MZ' + b'0' * 38, 100)):
            with self.assertRaises(Exception):
                common.download_with_cert_fallback(lambda *a, **k: None, 'https://x/y', dest)
        self.assertFalse(os.path.exists(dest))

    def test_full_body_ok(self):
        dest = os.path.join(self.tmp, 'x.exe')
        with mock.patch('urllib.request.urlopen', return_value=_Resp(b'A' * 100, 100)):
            common.download_with_cert_fallback(lambda *a, **k: None, 'https://x/y', dest)
        self.assertEqual(os.path.getsize(dest), 100)


class StaleExeTest(_Tmp):
    def _stage(self, file_ver, expect=371):
        def fake_dl(run, url, dest, **k):
            with open(dest, 'wb') as f:
                f.write(b'MZ' + b'\0' * 1_100_000)
            return dest

        with mock.patch.object(update_core.common, 'download_with_cert_fallback', side_effect=fake_dl), \
                mock.patch.object(update_core.common, 'file_version', return_value=file_ver), \
                mock.patch.object(update_core.common, '_app_data_dir', return_value=self.tmp), \
                mock.patch('tempfile.gettempdir', return_value=self.tmp):
            return update_core.stage_update(lambda m: None, exe_url='https://x/raw/DriverVarazslo.exe',
                                            expect_build=expect)

    def test_old_exe_from_cdn_is_refused(self):
        with self.assertRaises(RuntimeError) as cm:
            self._stage('1.0.370.0')
        self.assertIn('RÉGI', str(cm.exception))
        self.assertFalse([f for f in os.listdir(self.tmp) if f.endswith('.exe')])   # a régit töröltük

    def test_expected_or_unreadable_version_proceeds(self):
        self.assertTrue(self._stage('1.0.371.0').endswith('.bat'))
        self.assertTrue(self._stage(None).endswith('.bat'))                          # nem olvasható: nem döntünk

    def test_build_from_file_version(self):
        self.assertEqual(update_core._build_from_file_version('1.0.371.0'), 371)
        self.assertIsNone(update_core._build_from_file_version('1.0.0.0'))
        self.assertIsNone(update_core._build_from_file_version(None))


if __name__ == '__main__':
    unittest.main()
