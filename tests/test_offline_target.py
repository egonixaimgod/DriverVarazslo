"""Offline mód: egy MÁSIK lemez driverlistája nem jelenhet meg, és abból nem lehet törölni
(2026-10-12, terepi napló, Build 372: az induláskori C:-lista a D:-re váltás után ért be,
"OFFLINE MÓD (D:\\)" alatt jelent meg, és a technikus abból törölt a D:\\ képen - egy másik
Windowsban ugyanaz az oemNN.inf egészen más driver). Hálózat/DISM nincs: a lekérdezések
csonkolva, a betöltő szál szinkron."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import drivers_core
from app.gui import drivers as gdrv

# A terepi Error 183 magyarul (így jött /English nélkül) és angolul (így jön most).
DISM_183_EN = ('Deployment Image Servicing and Management tool\nVersion: 10.0.19041.3636\n\n'
               'Error: 183\n\nThe specified image is currently being serviced by another DISM operation.\n')


def _res(stdout='', rc=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=rc)


class _Host(gdrv.GuiDriversMixin):
    _cancel_flag = False

    def __init__(self):
        self.target_os_path = None
        self.events = []
        self._run = lambda *a, **k: _res()

    def emit(self, event, payload=None):
        self.events.append((event, payload))

    def _safe_thread(self, _name, fn):
        fn()


class StaleListTest(unittest.TestCase):
    def test_list_from_before_target_switch_is_dropped(self):
        quiet()
        h = _Host()
        gate = threading.Event()

        def slow_online(problems=None):
            gate.wait(5)                     # a C:-lista lassú...
            return [{'published': 'oem190.inf'}]

        threads = []
        real_thread = threading.Thread

        def capture(*a, **k):
            t = real_thread(*a, **k)
            threads.append(t)
            return t

        with mock.patch.object(h, '_get_third_party_drivers', side_effect=slow_online), \
                mock.patch.object(h, '_get_offline_drivers', return_value=[{'published': 'oem4.inf'}]), \
                mock.patch.object(gdrv.threading, 'Thread', side_effect=capture):
            h.load_drivers()                 # induláskor: élő rendszer
            h.target_os_path = 'D:\\'        # ...közben D:-re vált
            h.load_drivers()
            threads[1].join(5)
            gate.set()                       # ...és csak MOST ér be a C:-lista
            threads[0].join(5)
        loaded = [p for e, p in h.events if e == 'drivers_loaded']
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]['target'], 'D:\\')
        self.assertEqual([d['published'] for d in loaded[0]['drivers']], ['oem4.inf'])


class DeleteTargetGuardTest(unittest.TestCase):
    def test_delete_from_other_disk_list_is_refused(self):
        quiet()
        h = _Host()
        h.target_os_path = 'D:\\'
        with mock.patch.object(drivers_core, 'delete_driver_package') as dele:
            h.delete_drivers(['oem190.inf'], list_target='')          # a lista az élő rendszeré volt
        dele.assert_not_called()
        self.assertTrue(any(e == 'toast' and 'MÁSIK lemez' in p['message'] for e, p in h.events))

    def test_matching_disk_deletes(self):
        quiet()
        h = _Host()
        h.target_os_path = 'D:\\'
        with mock.patch.object(drivers_core, 'delete_driver_package', return_value=_res('', 0)) as dele, \
                mock.patch.object(gdrv.time, 'sleep'):
            h.delete_drivers(['oem4.inf'], list_target='D:\\')
        dele.assert_called_once()


class OfflineDismDeleteTest(unittest.TestCase):
    def test_english_and_busy_retry(self):
        quiet()
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            return _res(DISM_183_EN, 183) if len(calls) == 1 else _res('The operation completed successfully.', 0)

        with mock.patch.object(drivers_core.time, 'sleep'):
            res = drivers_core.delete_driver_package(run, 'oem4.inf', 'D:\\')
        self.assertIn('/English', calls[0])
        self.assertEqual(len(calls), 2)                               # a foglaltságot kivárta
        self.assertTrue(drivers_core.delete_succeeded(res))

    def test_failure_text_is_the_reason_not_the_header(self):
        why = drivers_core.delete_failure_text(_res(DISM_183_EN, 183))
        self.assertIn('MÁSIK DISM', why)
        self.assertNotIn('Deployment Image', why)


if __name__ == '__main__':
    unittest.main()
