"""2026-11-10: kézi napló-feltöltés kóddal (oldalsáv / CLI), és a HP ProDesk 600 G5
Build 411-es naplójából jött két záró-jelentés javítás."""

import unittest

from app import wu_core
from app.gui.logs import GuiLogsMixin


class ManualLogUploadTest(unittest.TestCase):
    """A kódot a Drive dönti el: rossz (vagy nem ellenőrizhető) kódra SEMMI nem megy fel."""

    def _api(self, check):
        class F(GuiLogsMixin):
            def __init__(self):
                self.events, self.uploads, self.checked = [], [], []
            def emit(self, ev, data=None): self.events.append((ev, data))
            def _safe_thread(self, name, fn): fn()
            def _toollib_check_password(self, pw, key):
                self.checked.append(pw)
                return check
            def upload_run_log(self, task, outcome):
                self.uploads.append((task, outcome))
                return True
        return F()

    def _status(self, api):
        return [d['status'] for ev, d in api.events if ev == 'task_complete']

    def test_jo_kod_feltolt(self):
        api = self._api((True, ''))
        api.upload_debug_log(' 1234 ')
        self.assertEqual(api.checked, ['1234'])
        self.assertEqual(len(api.uploads), 1)
        self.assertEqual(self._status(api), ['✅ Napló feltöltve'])

    def test_rossz_kod_nem_tolt_fel(self):
        api = self._api((False, 'hibás jelszó (a szkript LOG_READ_PASSWORD értéke)'))
        api.upload_debug_log('0000')
        self.assertEqual(api.uploads, [])
        self.assertTrue(self._status(api)[0].startswith('❌'))
        self.assertTrue(any('Hibás kód' in (d or {}).get('log', '') for _, d in api.events))

    def test_nem_ellenorizheto_kod_nem_hibas_kod(self):
        api = self._api((False, 'getaddrinfo failed'))
        api.upload_debug_log('1234')
        self.assertEqual(api.uploads, [])
        self.assertFalse(any('Hibás kód' in (d or {}).get('log', '') for _, d in api.events))

    def test_ures_kod(self):
        api = self._api((True, ''))
        api.upload_debug_log('  ')
        self.assertEqual((api.checked, api.uploads), ([], []))


class Build411HpLogTest(unittest.TestCase):
    def test_ucsi_nem_teendo(self):
        self.assertIn('ucmucsiacpiclient.inf', wu_core.HEALTH_REPORT_SKIP_INFS)

    def test_duplikatum_szoveg_nem_iger_ujrainditast(self):
        import inspect
        from app import dupdrivers_core
        src = inspect.getsource(dupdrivers_core)
        self.assertNotIn("újraindítás után törölhető.')", src)


if __name__ == '__main__':
    unittest.main()
