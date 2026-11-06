"""Mentés / visszaállítás / BCD.

2026-10-12: a DISM, pnputil, robocopy, bcdboot kilépési kódja is a verdikt része, és a gyors
visszaállítási pont nem "✅", ha a Windows elutasította.
2026-11-04 (a Mentés és Visszaállítás nézet újraírása, explicit user decision): három
mentés-fajta (third-party / a Windows saját driverei / mindkettő), a visszaállítás minden
mentés-formátumot felismer és driverenként választható, a kézi törlés előtt mentés +
visszaállítási pont. Valódi rendszerhez semmi nem nyúl: a futtató és a Popen-es
`_stream_cmd` csonkolva, minden útvonal temp mappában (lásd tests/__init__.py)."""
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import tests
from tests.catalog_stub import quiet
from app import backup_core, bcd_core, wu_core
from app.gui import drivers as gdrv, backup as gbackup

INF_MICRON = (
    '[Version]\nSignature="$WINDOWS NT$"\nClass=SCSIAdapter\nProvider=%Mfg%\n'
    'DriverVer=08/03/2021,2.1.19.0\n[Manufacturer]\n%Mfg%=Models,NTamd64\n'
    '[Models.NTamd64]\n%Desc%=Inst, PCI\\VEN_1344&DEV_5410\n'
    '[Strings]\nMfg="Micron"\nDesc="Micron NVMe Controller"\n')


def _res(stdout='', rc=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=rc)


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class _Tmp(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_windows(self, name='img'):
        """Egy csonk Windows: egy third-party csomag (oem5.inf = a mappája INF-jének másolata),
        egy Windows-csomag, egy AZONOS MÉRETŰ, de más tartalmú csomag, és egy mappa nélküli oem."""
        root = os.path.join(self.tmp, name)
        win = os.path.join(root, 'Windows')
        repo = os.path.join(win, 'System32', 'DriverStore', 'FileRepository')
        _w(os.path.join(repo, 'mtinvme.inf_amd64_aaa', 'mtinvme.inf'), INF_MICRON)
        _w(os.path.join(repo, 'mtinvme.inf_amd64_aaa', 'mtinvme.sys'), 'SYS')
        _w(os.path.join(win, 'INF', 'oem5.inf'), INF_MICRON)
        _w(os.path.join(repo, 'twin.inf_amd64_ccc', 'twin.inf'), 'X' * len(INF_MICRON))
        _w(os.path.join(repo, 'usbstor.inf_amd64_bbb', 'usbstor.inf'), '[Version]\nClass=USB\n')
        _w(os.path.join(win, 'INF', 'usbstor.inf'), '[Version]\nClass=USB\n')
        _w(os.path.join(win, 'INF', 'oem9.inf'), 'orphan')
        _w(os.path.join(win, 'System32', 'config', 'SYSTEM'), 'hive')
        return root


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


class StoreSplitTest(_Tmp):
    """Mérve 2026-11-04: az oemNN.inf a csomag INF-jének BÁJTRA azonos másolata (58/58)."""
    def test_oem_copy_identifies_third_party(self):
        win = os.path.join(self.make_windows(), 'Windows')
        self.assertEqual(backup_core.third_party_store_folders(win), {'oem5.inf': 'mtinvme.inf_amd64_aaa'})

    def test_unreadable_store_is_none_not_empty(self):
        self.assertIsNone(backup_core.third_party_store_folders(os.path.join(self.tmp, 'nincs')))


class BackupModesTest(_Tmp):
    def _backup(self, mode, export_rc=0, free=10 ** 12):
        root = self.make_windows()
        dest = os.path.join(self.tmp, 'dest')
        seen = []

        def fake_stream(cmd, log, check_cancel, si, nw):
            seen.append(cmd)
            out = cmd[-1].split(':', 1)[1]
            if export_rc == 0:
                _w(os.path.join(out, 'mtinvme.inf_amd64_aaa', 'mtinvme.inf'), INF_MICRON)
            return export_rc, False
        with mock.patch.object(backup_core, '_stream_cmd', side_effect=fake_stream), \
                mock.patch.object(backup_core.shutil, 'disk_usage', return_value=SimpleNamespace(free=free)):
            r = backup_core.backup_drivers(lambda *a, **k: _res(), lambda m: None, None, dest, root, mode)
        return r, seen, root

    def test_windows_mode_leaves_out_third_party(self):
        r, seen, _root = self._backup('windows')
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(seen, [])                                   # DISM nem kell hozzá
        repo = os.path.join(r['folder'], 'Windows', 'FileRepository')
        self.assertEqual(sorted(os.listdir(repo)), ['twin.inf_amd64_ccc', 'usbstor.inf_amd64_bbb'])
        self.assertEqual(os.listdir(os.path.join(r['folder'], 'Windows', 'INF')), ['usbstor.inf'])
        self.assertFalse(os.path.isdir(os.path.join(r['folder'], 'ThirdParty')))

    def test_thirdparty_mode_exports_from_offline_image(self):
        r, seen, root = self._backup('thirdparty')
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['tp_count'], 1)
        self.assertIn('/English', seen[0])
        self.assertIn(f'/Image:{root}\\', seen[0])
        self.assertFalse(os.path.isdir(os.path.join(r['folder'], 'Windows')))

    def test_failed_export_is_partial_not_ok(self):
        r, _seen, _root = self._backup('thirdparty', export_rc=50)
        self.assertEqual(r['status'], 'partial')
        self.assertTrue(r['problems'])

    def test_all_mode_has_both_parts_and_is_restorable(self):
        r, _seen, _root = self._backup('all')
        info = backup_core.inspect_backup(r['folder'])
        self.assertTrue(info['ok'])
        self.assertEqual([p['inf'] for p in info['packages']], ['mtinvme.inf'])
        self.assertEqual(info['windows']['count'], 2)
        self.assertTrue(info['kind'].startswith('Teljes mentés'))

    def test_no_space_creates_nothing(self):
        r, _seen, _root = self._backup('all', free=1)
        self.assertEqual(r['status'], 'no_space')
        self.assertIsNone(r['folder'])
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, 'dest')) and os.listdir(os.path.join(self.tmp, 'dest')))


class InspectTest(_Tmp):
    def test_package_facts_from_inf(self):
        _w(os.path.join(self.tmp, 'm', 'oem116', 'mtinvme.inf'), INF_MICRON)
        p = backup_core.inspect_backup(os.path.join(self.tmp, 'm'))['packages'][0]
        self.assertEqual((p['provider'], p['cls'], p['version'], p['date'], p['name']),
                         ('Micron', 'SCSIAdapter', '2.1.19.0', '08/03/2021', 'Micron NVMe Controller'))

    def test_legacy_full_export_keeps_inbox_out_of_packages(self):
        root = os.path.join(self.tmp, 'DriverVarázsló_FullExport_x')
        _w(os.path.join(root, 'mtinvme.inf_amd64_aaa', 'mtinvme.inf'), INF_MICRON)
        _w(os.path.join(root, '_Windows_Inbox_Drivers', 'usbstor.inf_amd64_b', 'usbstor.inf'), 'x')
        _w(os.path.join(root, '_Windows_Inbox_INF', 'usbstor.inf'), 'x')
        info = backup_core.inspect_backup(root)
        self.assertEqual([p['inf'] for p in info['packages']], ['mtinvme.inf'])
        self.assertEqual(info['windows']['count'], 1)
        self.assertFalse(info['tp_root_clean'])
        self.assertEqual(info['kind'], 'Régi DriverVarázsló teljes mentés')

    def test_legacy_wim_extract(self):
        root = os.path.join(self.tmp, 'Windows_Gyari_Alap_Driverek_x')
        _w(os.path.join(root, 'FileRepository', 'usbstor.inf_amd64_b', 'usbstor.inf'), 'x')
        _w(os.path.join(root, 'INF', 'usbstor.inf'), 'x')
        info = backup_core.inspect_backup(root)
        self.assertEqual(info['packages'], [])
        self.assertIn('ISO/WIM', info['kind'])

    def test_fix_and_manual_backups_are_named(self):
        fix = os.path.join(self.tmp, 'DriverVarazslo', 'driver_backup')
        _w(os.path.join(fix, 'oem116', 'mtinvme.inf'), INF_MICRON)
        _w(os.path.join(fix, 'INFO.txt'), 'DriverVarázsló - driver-mentés\nKészült: 2026-11-03 10:00:00\n')
        info = backup_core.inspect_backup(fix)
        self.assertIn('1 kattintásos fix', info['kind'])
        self.assertEqual(info['created'], '2026-11-03 10:00:00')
        kezi = os.path.join(self.tmp, 'DriverVarazslo', 'driver_backup_kezi', '2026-11-04_10-00-00')
        _w(os.path.join(kezi, 'oem116', 'mtinvme.inf'), INF_MICRON)
        self.assertIn('kézi törlés', backup_core.inspect_backup(kezi)['kind'])

    def test_inf_inside_a_package_is_not_a_separate_package(self):
        root = os.path.join(self.tmp, 'pk')
        _w(os.path.join(root, 'igfx.inf_amd64_a', 'igfx.inf'), INF_MICRON)
        _w(os.path.join(root, 'igfx.inf_amd64_a', 'sub', 'ext.inf'), INF_MICRON)
        self.assertEqual(len(backup_core.inspect_backup(root)['packages']), 1)

    def test_single_package_folder(self):
        root = os.path.join(self.tmp, 'oem116')
        _w(os.path.join(root, 'mtinvme.inf'), INF_MICRON)
        info = backup_core.inspect_backup(root)
        self.assertEqual(len(info['packages']), 1)
        self.assertEqual(info['packages'][0]['dir'], os.path.normpath(root))

    def test_empty_or_missing_folder_is_an_error(self):
        os.makedirs(os.path.join(self.tmp, 'ures'))
        for p in (os.path.join(self.tmp, 'ures'), os.path.join(self.tmp, 'nincs')):
            info = backup_core.inspect_backup(p)
            self.assertFalse(info['ok'])
            self.assertTrue(info['error'])


class RestoreTest(_Tmp):
    def _v1(self, n=2, windows=True):
        root = os.path.join(self.tmp, 'mentes')
        for i in range(n):
            _w(os.path.join(root, 'ThirdParty', f'pkg{i}.inf_amd64_{i}', f'pkg{i}.inf'), INF_MICRON)
        if windows:
            _w(os.path.join(root, 'Windows', 'FileRepository', 'usbstor.inf_amd64_b', 'usbstor.inf'), 'x')
            _w(os.path.join(root, 'Windows', 'INF', 'usbstor.inf'), 'x')
        return root

    def _restore(self, source, target=None, rc=0, robo_rc=1, bcd=True, **kw):
        streams, runs = [], []

        def fake_stream(cmd, log, check_cancel, si, nw):
            streams.append(cmd)
            return rc, False

        def run(cmd, **k):
            runs.append(cmd)
            return _res('', robo_rc if cmd[0] == 'robocopy' else 0)
        with mock.patch.object(backup_core, '_stream_cmd', side_effect=fake_stream), \
                mock.patch.object(backup_core.bcd_core, 'repair_bcd', return_value=bcd) as rb, \
                mock.patch.object(backup_core.time, 'sleep'):
            r = backup_core.restore_drivers(run, lambda m: None, None, None, None, source, target, **kw)
        return r, streams, runs, rb

    def test_online_all_is_one_pnputil_call(self):
        src = self._v1()
        r, streams, runs, _ = self._restore(src)
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0][:2], ['pnputil', '/add-driver'])
        self.assertIn('/subdirs', streams[0])
        self.assertTrue(streams[0][2].startswith(os.path.join(src, 'ThirdParty')))
        robo = [c for c in runs if c[0] == 'robocopy']
        self.assertEqual(len(robo), 2)
        for c in robo:
            for flag in ('/ZB', '/XC', '/XN', '/XO'):
                self.assertIn(flag, c)                         # csak a hiányzó fájlok
        self.assertIn('oem*.inf', robo[1])
        self.assertEqual(r['windows'], 'copied')
        self.assertIn(['pnputil', '/scan-devices'], runs)

    def test_online_subset_only_the_chosen_packages(self):
        src = self._v1(n=3)
        chosen = [os.path.join(src, 'ThirdParty', 'pkg1.inf_amd64_1')]
        r, streams, _runs, _ = self._restore(src, package_dirs=chosen, include_windows=False)
        self.assertEqual(r['tp_done'], 1)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0][2], os.path.join(chosen[0], '*.inf'))
        self.assertNotIn('/subdirs', streams[0])

    def test_legacy_root_never_feeds_inbox_to_pnputil(self):
        root = os.path.join(self.tmp, 'old')
        _w(os.path.join(root, 'mtinvme.inf_amd64_aaa', 'mtinvme.inf'), INF_MICRON)
        _w(os.path.join(root, '_Windows_Inbox_Drivers', 'usbstor.inf_amd64_b', 'usbstor.inf'), 'x')
        _r, streams, _runs, _ = self._restore(root)
        self.assertEqual(len(streams), 1)
        self.assertNotIn('/subdirs', streams[0])
        self.assertNotIn('_Windows_Inbox', streams[0][2])

    def test_offline_uses_dism_bcd_and_rescan(self):
        target = self.make_windows('dead')
        src = self._v1()
        r, streams, runs, rb = self._restore(src, target=target + '\\Windows')
        self.assertEqual(r['status'], 'ok')
        self.assertTrue(streams[0][0] == 'dism' and f'/Image:{target}\\' in streams[0])
        self.assertIn('/Recurse', streams[0])
        rb.assert_called_once()
        self.assertTrue(r['boot'])
        self.assertTrue(any(os.path.join(target, 'Windows', 'INF') in c for c in runs if c[0] == 'robocopy'))
        self.assertTrue(os.path.isfile(os.path.join(target, 'ProgramData', 'Microsoft', 'Windows', 'Start Menu',
                                                    'Programs', 'Startup', 'auto_pnputil_scan.bat')))

    def test_offline_target_without_windows_does_nothing(self):
        r, streams, runs, rb = self._restore(self._v1(), target=os.path.join(self.tmp, 'ures'))
        self.assertEqual(r['status'], 'fail')
        self.assertEqual((streams, runs), ([], []))
        rb.assert_not_called()

    def test_exit_codes_are_the_verdict(self):
        src = self._v1(windows=False)
        self.assertEqual(self._restore(src, rc=1)[0]['status'], 'errors')
        self.assertEqual(self._restore(src, rc=259)[0]['status'], 'ok')     # nem volt mit telepíteni
        self.assertEqual(self._restore(src, rc=3010)[0]['status'], 'ok')
        target = self.make_windows('dead')
        self.assertEqual(self._restore(src, target=target, rc=5)[0]['status'], 'errors')
        self.assertEqual(self._restore(src, target=target, bcd=False)[0]['status'], 'errors')
        self.assertEqual(self._restore(self._v1(), robo_rc=8)[0]['windows'], 'failed')


class StreamCmdDrainTest(unittest.TestCase):
    """Megszakítás után is ki kell olvasni a kimenetet: a régi `break` + `wait()` egy megtelt
    csőnél holtpontra futott (a gyerekfolyamat írásra várt, mi a kilépésére)."""
    def test_output_is_drained_after_cancel(self):
        quiet()
        lines = iter(['egy\n', 'ketto\n', 'harom\n'])
        proc = mock.MagicMock()
        proc.stdout = lines
        proc.returncode = 0
        got = []
        with mock.patch.object(backup_core.subprocess, 'Popen', return_value=proc):
            rc, cancelled = tests.REAL_STREAM_CMD(['dism'], got.append, lambda: bool(got), None, None)
        self.assertTrue(cancelled)
        self.assertEqual(list(lines), [])                    # minden sor kiolvasva
        self.assertEqual(got[0], 'egy')
        self.assertNotIn('ketto', got)
        proc.wait.assert_called_once()


class _DelHost(gdrv.GuiDriversMixin):
    _cancel_flag = False
    _si = _nw = None

    def __init__(self, target=None):
        self.target_os_path = target
        self.events, self.rp, self.order = [], [], []
        self._run = lambda cmd, **k: self.order.append(cmd) or _res()

    def emit(self, event, payload=None):
        self.events.append((event, payload))

    def _safe_thread(self, _name, fn):
        fn()

    def _check_cancel(self):
        return False

    def _create_restore_point_sync(self, task_id='autofix', label='AutoFix'):
        self.order.append('RP')
        self.rp.append((task_id, label))

    def logs(self):
        return '\n'.join((p or {}).get('log', '') for e, p in self.events if isinstance(p, dict))


class ManualDeleteBackupTest(unittest.TestCase):
    """Explicit user decision, 2026-11-04: a Driverek kezelése nézet minden törlés előtt ment
    és visszaállítási pontot készít; a mentés hibája nem állítja meg a törlést (1. elv)."""
    def setUp(self):
        quiet()
        self.root = backup_core.manual_backup_root()
        self.assertTrue(self.root.startswith(tests.TEST_APP_DATA))     # sosem a valódi C:\DriverVarazslo
        shutil.rmtree(self.root, ignore_errors=True)

    def test_online_saves_the_chosen_packages_then_restore_point_then_deletes(self):
        h = _DelHost()
        with mock.patch.object(gdrv, 'export_drivers_backup', return_value=(1, 5.0, 1.0)) as exp, \
                mock.patch.object(gdrv.win32, 'driver_store_folders', return_value={}), \
                mock.patch.object(gdrv.time, 'sleep'), \
                mock.patch.object(gdrv.drivers_core, 'delete_driver_package',
                                  side_effect=lambda *a, **k: h.order.append('DEL') or _res('Driver package deleted successfully.')):
            h.delete_drivers(['oem116.inf'])
        args, kw = exp.call_args
        self.assertEqual(args[1], [{'published': 'oem116.inf'}])
        self.assertTrue(kw['dest'].startswith(self.root))
        self.assertIn('KÉZI', kw['purpose'])
        self.assertEqual(h.rp, [('delete', 'kézi driver-törlés')])
        self.assertLess(h.order.index('RP'), h.order.index('DEL'))     # előbb mentés + pont, aztán törlés
        done = [p for e, p in h.events if e == 'task_complete'][0]
        self.assertIn(kw['dest'], done['status'])

    def test_failed_backup_does_not_stop_the_delete(self):
        h = _DelHost()
        with mock.patch.object(gdrv, 'export_drivers_backup', return_value=(0, 0, 0)), \
                mock.patch.object(gdrv.win32, 'driver_store_folders', return_value={}), \
                mock.patch.object(gdrv.time, 'sleep'), \
                mock.patch.object(gdrv.drivers_core, 'delete_driver_package', return_value=_res('ok')) as dele:
            h.delete_drivers(['oem116.inf'])
        dele.assert_called_once()
        self.assertIn('NEM készült', h.logs())
        self.assertIn('mentés NEM készült', [p for e, p in h.events if e == 'task_complete'][0]['status'])

    def test_offline_saves_on_the_target_disk_without_restore_point(self):
        h = _DelHost(target='D:\\')
        with mock.patch.object(gdrv.backup_core, 'backup_drivers',
                               return_value={'status': 'ok', 'folder': 'D:\\x'}) as bk, \
                mock.patch.object(gdrv.backup_core, 'prune_manual_backups'):
            self.assertEqual(h._backup_before_delete(['oem116.inf'], 'D:\\'), 'D:\\x')
        self.assertEqual(bk.call_args[0][3], 'D:\\DriverVarazslo\\driver_backup_kezi')
        self.assertEqual(bk.call_args[0][5], 'thirdparty')
        self.assertEqual(h.rp, [])
        self.assertIn('Offline módban', h.logs())

    def test_only_the_newest_backups_are_kept(self):
        for i in range(7):
            d = os.path.join(self.root, f'2026-11-0{i}_00-00-00')
            os.makedirs(d)
            os.utime(d, (time.time() - 1000 + i, time.time() - 1000 + i))
        removed = backup_core.prune_manual_backups(self.root, keep=4)
        self.assertEqual(len(removed), 3)
        self.assertEqual(sorted(os.listdir(self.root)), [f'2026-11-0{i}_00-00-00' for i in range(3, 7)])

    def test_same_backup_function_as_the_fix(self):
        """Egy feladat = egy függvény (explicit user decision, 2026-11-03)."""
        from app.gui import autofix
        self.assertIs(gdrv.export_drivers_backup, wu_core.export_drivers_backup)
        self.assertIs(autofix.export_drivers_backup, wu_core.export_drivers_backup)


class AutoBackupListTest(_Tmp):
    def test_lists_fix_and_manual_backups_newest_first(self):
        base = os.path.join(self.tmp, 'D', 'DriverVarazslo')
        _w(os.path.join(base, 'driver_backup', 'oem1', 'a.inf'), 'x')
        _w(os.path.join(base, 'driver_backup', 'INFO.txt'), 'Készült: 2026-11-01 08:00:00\n')
        _w(os.path.join(base, 'driver_backup_kezi', '2026-11-04_10-00-00', 'oem2', 'b.inf'), 'x')
        _w(os.path.join(base, 'driver_backup_kezi', '2026-11-04_10-00-00', 'INFO.txt'), 'Készült: 2026-11-04 10:00:00\n')
        os.makedirs(os.path.join(base, 'driver_backup_kezi', 'ures'))
        lst = backup_core.list_auto_backups(os.path.join(self.tmp, 'D'))
        self.assertEqual([(b['kind'], b['count']) for b in lst],
                         [('Kézi törlés előtt', 1), ('1 kattintásos fix előtt', 1)])


class WimExtractTest(_Tmp):
    def _extract(self, image_name='install.wim', mount_rc=0):
        image = os.path.join(self.tmp, image_name)
        _w(image, 'WIM')
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            if '/Mount-Image' in cmd and mount_rc == 0:
                mount = next(c for c in cmd if c.startswith('/MountDir:')).split(':', 1)[1]
                repo = os.path.join(mount, 'Windows', 'System32', 'DriverStore', 'FileRepository')
                _w(os.path.join(repo, 'usbstor.inf_amd64_b', 'usbstor.inf'), 'x')
                _w(os.path.join(mount, 'Windows', 'INF', 'usbstor.inf'), 'x')
            if '/Export-Image' in cmd:
                _w(next(c for c in cmd if c.startswith('/DestinationImageFile:')).split(':', 1)[1], 'W')
            if '/Get-WimInfo' in cmd:
                return _res('Name : Windows 10 Pro\nVersion : 10.0.19041\n')
            return _res('', mount_rc if '/Mount-Image' in cmd else 0)
        # SystemDrive = X: (WinPE): a munkamappa a cél lemezére kerül, nem a valódi C:\DV_Temp-be.
        with mock.patch.dict(os.environ, {'SystemDrive': 'X:'}):
            r = backup_core.extract_windows_drivers(run, lambda m: None, None, image,
                                                    os.path.join(self.tmp, 'out'), self.tmp)
        return r, calls

    def test_result_is_a_restorable_windows_backup(self):
        r, calls = self._extract()
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['count'], 1)
        info = backup_core.inspect_backup(r['folder'])
        self.assertTrue(info['ok'] and info['windows'] and info['windows']['count'] == 1)
        self.assertTrue(any('/Unmount-Image' in c for c in calls))
        self.assertEqual(os.listdir(os.path.join(self.tmp, 'DV_Temp')), [])     # a csatolási mappa eltűnt

    def test_esd_is_converted_and_the_temp_wim_removed(self):
        r, calls = self._extract('install.esd')
        self.assertEqual(r['status'], 'ok')
        self.assertTrue(any('/Export-Image' in c for c in calls))
        self.assertEqual(os.listdir(os.path.join(self.tmp, 'DV_Temp')), [])

    def test_failed_mount_is_fail_and_cleans_up(self):
        r, calls = self._extract(mount_rc=5)
        self.assertEqual(r['status'], 'fail')
        self.assertTrue(any('/Cleanup-Wim' in c for c in calls))
        self.assertFalse(any('/Unmount-Image' in c for c in calls))


class GuiGateTest(_Tmp):
    def test_restore_target_check(self):
        h = gbackup.GuiBackupMixin()
        ok = h.check_restore_target(self.make_windows('dead') + '\\Windows')
        self.assertTrue(ok['ok'])
        self.assertTrue(ok['root'].endswith('dead\\'))
        bad = h.check_restore_target(self.tmp)
        self.assertFalse(bad['ok'])
        self.assertIn('nincs Windows', bad['error'])

    def test_cli_has_a_directory_picker(self):
        """2026-11-04-ig a CLI a GUI `_window` dialógusát örökölte: a Mentés menü minden pontja
        AttributeError-ral állt meg (WinPE alatt a CLI a tartalék)."""
        from app.cli import bridge
        b = bridge.CliBridgeMixin()
        with mock.patch.object(bridge.ui, 'ask', return_value='"' + self.tmp + '"'):
            self.assertEqual(b.select_directory('x'), self.tmp)
        with mock.patch.object(bridge.ui, 'ask', return_value=os.path.join(self.tmp, 'nincs')), \
                mock.patch.object(bridge.ui, 'err'):
            self.assertIsNone(b.select_directory('x'))

    def test_old_parallel_backup_code_is_gone(self):
        for name in ('backup_all_drivers', 'run_restore', 'extract_wim', 'force_copy'):
            self.assertFalse(hasattr(backup_core, name), name)
        for name in ('backup_third_party', 'backup_all', 'restore_online', 'restore_offline', '_run_restore'):
            self.assertFalse(hasattr(gbackup.GuiBackupMixin, name), name)


if __name__ == '__main__':
    unittest.main()
