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

    def _backup_before_delete(self, names, target=None):
        # A törlés előtti mentésnek saját tesztje van (test_backup_truth.ManualDeleteBackupTest).
        return None


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


class StoreGenTest(unittest.TestCase):
    """2026-10-15, terepi napló (Lenovo X1 Yoga, Build 382): a fix utolsó lábának elején
    betöltött 106-os lista a képernyőn maradt, pedig a záró takarítás utána 38 csomagot
    törölt. A lista a betöltés ELŐTTI DriverStore-számlálót hordozza; ha közben (vagy utána)
    módosul a tár, a felület ebből látja, hogy újra kell tölteni."""

    def test_list_carries_generation_read_before_query(self):
        quiet()
        h = _Host()
        h.invalidate_driver_cache()                      # egy korábbi telepítés
        gen0 = h.driver_store_gen()

        def online(problems=None):
            h.invalidate_driver_cache()                  # törlés a lekérdezés KÖZBEN
            return [{'published': 'oem40.inf'}]

        threads = []
        real_thread = threading.Thread

        def capture(*a, **k):
            t = real_thread(*a, **k)
            threads.append(t)
            return t

        with mock.patch.object(h, '_get_third_party_drivers', side_effect=online), \
                mock.patch.object(gdrv.threading, 'Thread', side_effect=capture):
            h.load_drivers()
            threads[0].join(5)
        loaded = [p for e, p in h.events if e == 'drivers_loaded']
        self.assertEqual(loaded[0]['store_gen'], gen0)
        self.assertNotEqual(h.driver_store_gen(), loaded[0]['store_gen'])   # -> a felület újratölt

    def test_every_store_mutation_bumps_generation(self):
        h = _Host()
        g = h.driver_store_gen()
        for cmd in ('pnputil /delete-driver oem40.inf', 'pnputil /add-driver x.inf /install'):
            self.assertTrue(drivers_core.mutates_driver_store(cmd))
            h.invalidate_driver_cache()                  # a `_run` ezt hívja minden ilyen parancs után
        self.assertEqual(h.driver_store_gen(), g + 2)


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


class _SyncThread:
    """A betöltő szál helyett: a `start()` azonnal lefuttatja a munkát."""
    def __init__(self, target=None, **_k):
        self._fn = target

    def start(self):
        self._fn()


class ViewOwnedTargetTest(unittest.TestCase):
    """2026-11-05, explicit user decision: a felső „Jelenlegi rendszer / Másik lemez” sáv
    megszűnt. A GUI-ban nincs globális cél: a Driverek nézet és a Mentés nézet a saját
    választóján dönt, és a lemezt minden hívás paraméterként viszi - így egy D:-re váltott
    Driverek nézet nem teszi „offline”-ná a WU-, aktiválás- stb. nézeteket."""

    def test_list_request_carries_the_views_disk(self):
        quiet()
        h = _Host()                                   # globális cél: nincs
        with mock.patch.object(h, '_get_offline_drivers', return_value=[{'published': 'oem4.inf'}]) as off, \
                mock.patch.object(h, '_get_third_party_drivers') as live, \
                mock.patch.object(gdrv.threading, 'Thread', _SyncThread):
            h.load_drivers('D:\\')
        live.assert_not_called()
        self.assertEqual(off.call_args[0][1], 'D:\\')
        loaded = [p for e, p in h.events if e == 'drivers_loaded']
        self.assertEqual(loaded[0]['target'], 'D:\\')
        self.assertIsNone(h.target_os_path)           # a nézet választása nem globális

    def test_delete_runs_on_the_lists_disk(self):
        quiet()
        h = _Host()
        seen = []
        h._backup_before_delete = lambda names, target=None: seen.append(target)
        with mock.patch.object(drivers_core, 'delete_driver_package', return_value=_res('', 0)) as dele, \
                mock.patch.object(gdrv.time, 'sleep'):
            h.delete_drivers(['oem4.inf'], list_target='D:\\')
        self.assertEqual(dele.call_args[0][2], 'D:\\')
        self.assertIsNone(dele.call_args[1]['timeout'])          # offline DISM: nincs pnputil-korlát
        self.assertEqual(seen, ['D:\\'])                           # a mentés is arról a lemezről

    def test_live_list_deletes_live(self):
        quiet()
        h = _Host()
        with mock.patch.object(drivers_core, 'delete_driver_package', return_value=_res('', 0)) as dele, \
                mock.patch.object(gdrv.time, 'sleep'):
            h.delete_drivers(['oem4.inf'], list_target='')
        self.assertIsNone(dele.call_args[0][2])
        self.assertEqual(dele.call_args[1]['timeout'], drivers_core.DELETE_DRIVER_TIMEOUT)

    def test_usage_is_unknown_for_another_windows(self):
        out = _Host().get_driver_usage([{'published': 'oem4.inf'}], 'D:\\')
        self.assertTrue(out['offline'])

    def test_backup_source_is_the_views_choice(self):
        from app.gui import backup as gbk

        class B(gbk.GuiBackupMixin):
            target_os_path = None
            _si = _nw = None

            def __init__(self):
                self.events = []
                self._run = lambda *a, **k: _res()

            def emit(self, e, p=None):
                self.events.append((e, p))

            def _safe_thread(self, _n, fn):
                fn()

            def _check_cancel(self):
                return False

        b = B()
        with mock.patch.object(gbk.backup_core, 'store_overview', return_value=None) as so, \
                mock.patch.object(gbk.backup_core, 'list_auto_backups', return_value=[]), \
                mock.patch.object(gbk.backup_core, 'describe_source', return_value='D:\\'):
            ov = b.get_backup_overview('D:\\')
        self.assertEqual(so.call_args[0][0], 'D:\\')
        self.assertTrue(ov['offline'])
        with mock.patch.object(gbk.backup_core, 'backup_drivers',
                               return_value={'status': 'ok', 'size_mb': 1, 'folder': 'E:\\m', 'problems': []}) as bd:
            b.backup_drivers('thirdparty', 'E:\\', 'D:\\')
        self.assertEqual(bd.call_args[0][4], 'D:\\')              # forrás: a választott Windows
        with mock.patch.object(gbk.backup_core, 'list_auto_backups', return_value=[{'path': 'x'}]) as la:
            self.assertEqual(b.list_auto_backups('D:\\'), [{'path': 'x'}])
        self.assertEqual(la.call_args[0][0], 'D:\\')

    def test_ui_has_no_global_target(self):
        import os
        import re
        html = open(os.path.join(os.path.dirname(__file__), '..', 'ui.html'), encoding='utf-8').read()
        script = html[html.index('<script>'):]
        for gone in ('class="target-bar"', 'apply_target_os', 'reset_target_os', 'change_target_os'):
            self.assertNotIn(gone, html if gone.startswith('class=') else script, gone)
        self.assertRegex(script, r'api\(\)\.load_drivers\(currentTargetPath')
        self.assertRegex(script, r'api\(\)\.backup_drivers\(bkMode, null, bkSrc')
        self.assertRegex(script, r'api\(\)\.get_driver_usage\(drivers, driversTarget')
        self.assertEqual(len(re.findall(r'id="(drv-sys|bk-src)"', html)), 2)


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
