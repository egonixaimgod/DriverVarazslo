"""Szervíz programok: a telepített állapot felismerése a Program Files mappából
(nyilvántartás nélkül is) és a kijelöltek törlése (mappa + parancsikon, visszaolvasva)."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app.gui.toolsinstall import GuiToolsInstallMixin


class F(GuiToolsInstallMixin):
    def __init__(self, tmp):
        self.tmp = tmp
        self.events = []

    def emit(self, ev, data):
        self.events.append((ev, data))

    def _tool_install_record_path(self):
        return os.path.join(self.tmp, 'installed_tools.json')


class ToolsInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pf86 = os.path.join(self.tmp, 'pf86')
        self.pf = os.path.join(self.tmp, 'pf')
        self.pub = os.path.join(self.tmp, 'pub')
        for d in (self.pf86, self.pf, os.path.join(self.pub, 'Desktop')):
            os.makedirs(d)
        env = {'ProgramFiles(x86)': self.pf86, 'ProgramFiles': self.pf, 'PUBLIC': self.pub}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.api = F(self.tmp)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, *parts):
        p = os.path.join(*parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'w').close()
        return p

    def test_detects_folder_without_record(self):
        self._touch(self.pf86, 'FurMark', 'FurMark.exe')
        self._touch(self.pf, 'CPUID', 'CPU-Z', 'cpuz_x64.exe')
        self._touch(self.pf, 'Deep', 'a', 'b', 'prime95.exe')  # túl mély: nem számít
        st = self.api.get_service_tools_status()
        self.assertTrue(st['furmark']['installed'])
        self.assertEqual(st['cpuz']['path'], os.path.join(self.pf, 'CPUID', 'CPU-Z'))
        self.assertFalse(st['prime95']['installed'])

    def test_uninstall_removes_folder_and_shortcut_only(self):
        exe = self._touch(self.pf, 'CPUID', 'CPU-Z', 'cpuz_x64.exe')
        other = self._touch(self.pf, 'CPUID', 'HWMonitor', 'hwmonitor.exe')
        lnk = self._touch(self.pub, 'Desktop', 'CPU-Z.lnk')
        found = self.api._scan_installed_tools(['cpuz', 'gpuz'])
        ok, fail, skip = self.api._uninstall_tools_sync(['cpuz', 'gpuz'], found, {})
        self.assertEqual((ok, fail, skip), (1, 0, 1))
        self.assertFalse(os.path.exists(exe))
        self.assertFalse(os.path.exists(lnk))
        self.assertTrue(os.path.exists(other))
        self.assertTrue(os.path.isdir(self.pf))

    def test_refuses_folder_outside_program_files(self):
        exe = self._touch(self.tmp, 'elsewhere', 'furmark.exe')
        ok, fail, _ = self.api._uninstall_tools_sync(['furmark'], {'furmark': (exe, os.path.dirname(exe))}, {})
        self.assertEqual((ok, fail), (0, 1))
        self.assertTrue(os.path.exists(exe))


if __name__ == '__main__':
    unittest.main()
