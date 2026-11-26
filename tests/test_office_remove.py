"""Office teljes eltávolítása (2026-11-26): a Microsoft SetupProd_OffScrub.exe-je, az exébe
csomagolva, a `remove_office` gombbal indítva (app/gui/winact.py)."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import ROOT
from app.gui import winact
from app.gui.winact import GuiWinActMixin


class F(GuiWinActMixin):
    target_os_path = None


class OfficeRemoveTest(unittest.TestCase):
    def test_az_exe_bekerul_a_buildbe(self):
        # Nélküle a kiadott exében a gomb csak „nem található” hibát adna.
        self.assertTrue(os.path.isfile(os.path.join(ROOT, winact.OFFSCRUB_EXE)))
        with open(os.path.join(ROOT, 'DriverVarazslo.spec'), encoding='utf-8') as f:
            self.assertIn(f"('{winact.OFFSCRUB_EXE}', '.')", f.read())

    def _run(self, rc):
        tmp = tempfile.mkdtemp(prefix='dv_officedel_')
        self.addCleanup(shutil.rmtree, tmp, True)
        popen = mock.MagicMock()
        popen.return_value.wait.return_value = rc
        with mock.patch.object(winact, '_app_data_dir', lambda: tmp), \
                mock.patch.object(winact.subprocess, 'Popen', popen):
            res = F().remove_office()
        return tmp, popen, res

    def test_kimasol_es_starttal_sztringkent_indit(self):
        tmp, popen, res = self._run(0)
        dst = os.path.join(tmp, winact.OFFSCRUB_EXE)
        self.assertTrue(res['success'])
        self.assertTrue(os.path.isfile(dst))
        # `cmd /c start` sztringként: a program bezárása (cleanup_zombies) ne lője ki.
        self.assertEqual(popen.call_args[0][0], f'cmd /c start "" "{dst}"')

    def test_bukott_inditas_nem_siker(self):
        _, _, res = self._run(1)
        self.assertFalse(res['success'])

    def test_offline_modban_nem_indit(self):
        f = F()
        f.target_os_path = 'D:\\'
        with mock.patch.object(winact.subprocess, 'Popen') as popen:
            self.assertFalse(f.remove_office()['success'])
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
