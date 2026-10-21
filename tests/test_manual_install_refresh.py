"""A kézi telepítés gyorsítása és a lista frissítése, plusz a gépenkénti napló (2026-10-21,
terepi napló: Lenovo T440s, Build 392).

- A WU-telepítő először a helyi gyorsítótárból keres (online 68 mp volt a T440s-en), és
  csak hiány vagy letöltési hiba esetén online.
- A sikeres telepítés után nem jöhet „a Windows Update már telepítettként látja” sor
  (az eszköznevet vetettük össze a csomagcímmel).
- A felület tételesen megkapja, melyik sor települt (`hw_install_result`), a pool-elem
  `install_state`-et kap (a CLI ebből olvas).
- A vándorló SSD naplója gépenként külön fájl: fejléc-sor, félrerakás, törlés.

Valódi rendszerhez semmi nem nyúl: Popen, `_app_data_dir` és a firmware-olvasó csonkolva."""
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from tests.test_search_truth import _Host, _res
from app import logupload_core as lc
from app import win32
from app.gui import hwscan as hs
from app.wu_core import _build_wu_install_ps

GPU = {'update_id': 'b5b088d8', 'name': 'Általános Microsoft-videokártya',
       'wu_title': 'Intel Corporation driver update for Intel(R) HD Graphics Family'}


class _S(_Host, hs.GuiHwScanMixin):
    def _get_third_party_drivers(self, problems=None):
        return []


def _install(pool, lines):
    quiet()
    s = _S(lambda *a, **k: _res())
    s.hw_updates_pool = pool
    proc = SimpleNamespace(stdout=iter(lines), wait=lambda timeout=None: None, returncode=0, pid=1)
    with mock.patch.object(hs.subprocess, 'Popen', return_value=proc):
        out = s._install_wu_api_sync_inner(pool)
    return s, out


class WuInstallItemsTest(unittest.TestCase):
    def test_success_is_not_reported_as_already_installed(self):
        # A terepi sorrend szó szerint (T440s): FOUND, TOTAL, DLONE, INSTONE, OK, DONE.
        t = GPU['wu_title']
        s, (ok, fail, _c) = _install([dict(GPU)], [
            'SEARCH: Driver frissítések keresése...\n', f'FOUND: {t}\n', 'TOTAL: 1\n',
            f'DLONE: 1/1 {t}\n', f'INSTONE: 1/1 {t}\n', f'OK: {t}\n', 'DONE: Sikeres=1, Sikertelen=0\n'])
        self.assertEqual((ok, fail), (1, 0))
        self.assertNotIn('már telepítettként látja', s.logs())
        self.assertEqual(s._wu_ok_titles, {t})

    def test_unoffered_title_is_already(self):
        s, _ = _install([dict(GPU)], ['SKIP: Más csomag\n', 'EMPTY: Nem található egyező driver.\n'])
        self.assertIn('már telepítettként látja', s.logs())
        self.assertEqual(s._wu_already_titles, {GPU['wu_title']})

    def test_broken_script_claims_nothing(self):
        s, _ = _install([dict(GPU)], ['INIT: x\n'])
        self.assertNotIn('már telepítettként látja', s.logs())
        self.assertEqual(s._wu_already_titles, set())

    def test_report_marks_pool_and_emits_indices(self):
        quiet()
        wu_ok, wu_fail, cat_ok, cat_stuck = (dict(GPU), dict(GPU, wu_title='Másik', update_id='x'),
                                             {'name': 'Hang', 'url': 'u'}, {'name': 'NIC', 'url': 'v'})
        s = _S(lambda *a, **k: _res())
        s.hw_updates_pool = [wu_fail, wu_ok, cat_ok, cat_stuck]
        s._wu_ok_titles, s._wu_already_titles = {GPU['wu_title']}, set()
        s._catalog_item_states = {id(cat_ok): 'already'}
        s._report_hw_install_items([0, 1, 2, 3], [wu_fail, wu_ok])
        ev = [p for e, p in s.events if e == 'hw_install_result']
        self.assertEqual(ev, [{'items': {'1': 'ok', '2': 'already'}}])
        self.assertEqual([d.get('install_state') for d in s.hw_updates_pool], [None, 'ok', 'already', None])


class WuInstallScriptTest(unittest.TestCase):
    def test_cache_first_only_for_uid_filter(self):
        ps = _build_wu_install_ps(target_uids=['a', 'a'])
        self.assertIn('$Searcher.Online = $online', ps)
        self.assertIn('Find-Drivers $false', ps)
        # A letöltési hiba utáni online tartalék is benne van.
        self.assertIn('online újrakeresés', ps)

    @unittest.skipUnless(os.name == 'nt', 'PowerShell-tokenizáló csak Windowson')
    def test_script_tokenizes(self):
        for kw in ({'target_uids': ['a']}, {'target_hwids': ['PCI\\VEN_8086']}, {'match_system_devices': True}):
            ps = _build_wu_install_ps(**kw)
            chk = ("$e=$null; [void][System.Management.Automation.PSParser]::Tokenize("
                   "[IO.File]::ReadAllText($env:DV_PS), [ref]$e); $e.Count")
            with tempfile.NamedTemporaryFile('w', suffix='.ps1', delete=False, encoding='utf-8-sig') as f:
                f.write(ps)
            try:
                out = subprocess.run(['powershell', '-NoProfile', '-Command', chk], capture_output=True,
                                     text=True, env=dict(os.environ, DV_PS=f.name), timeout=60)
            finally:
                os.remove(f.name)
            self.assertEqual(out.stdout.strip(), '0', (kw, out.stdout, out.stderr))


def _smbios(*structs):
    """Nyers RSMB-blob: 8 bájt fejléc + struktúrák (típus, formázott rész, sztringek)."""
    body = b''
    for stype, formatted, strings in structs:
        head = struct.pack('<BBH', stype, 4 + len(formatted), 0) + formatted
        tail = (b'\x00'.join(s.encode() for s in strings) + b'\x00\x00') if strings else b'\x00\x00'
        body += head + tail
    return struct.pack('<BBBBI', 0, 3, 0, 0, len(body)) + body


class SmbiosTest(unittest.TestCase):
    def test_parse_serials(self):
        uuid = bytes(range(16))
        sys1 = bytes([1, 2, 3, 4]) + uuid           # gyártó, termék, verzió, SN sztring-indexek
        board = bytes([1, 2, 3, 4])
        raw = _smbios((0, b'\x00' * 14, ['bios']),
                      (1, sys1, ['LENOVO', '20ARS21Y0B', 'ThinkPad T440s', 'PB0ABCDE']),
                      (2, board, ['LENOVO', '20ARS21Y0B', 'x', 'L1HF']),
                      (127, b'', []))
        self.assertEqual(win32.parse_smbios_serials(raw),
                         {'system_serial': 'PB0ABCDE', 'system_uuid': uuid.hex(), 'board_serial': 'L1HF'})

    def test_garbage_and_empty_uuid(self):
        self.assertEqual(win32.parse_smbios_serials(b'\x01\x02'),
                         {'system_serial': '', 'system_uuid': '', 'board_serial': ''})
        raw = _smbios((1, bytes([0, 0, 0, 0]) + b'\xff' * 16, []))
        self.assertEqual(win32.parse_smbios_serials(raw)['system_uuid'], '')


HW = {'SystemManufacturer': 'LENOVO', 'SystemProductName': '20ARS21Y0B',
      'BaseBoardManufacturer': 'LENOVO', 'BaseBoardProduct': '20ARS21Y0B'}


class MachineIdentityTest(unittest.TestCase):
    def test_same_model_different_serial(self):
        k1, l1 = lc.machine_log_identity(HW, {'system_serial': 'PB0AAAAA'})
        k2, _ = lc.machine_log_identity(HW, {'system_serial': 'PB0BBBBB'})
        self.assertNotEqual(k1, k2)
        self.assertEqual(l1, 'LENOVO 20ARS21Y0B · SN PB0AAAAA')
        self.assertEqual(lc.machine_log_identity(HW, {'system_serial': 'PB0AAAAA'})[0], k1)

    def test_assembled_pc_uses_board(self):
        hw = {'SystemManufacturer': 'To Be Filled By O.E.M.', 'SystemProductName': 'To Be Filled By O.E.M.',
              'BaseBoardManufacturer': 'ASRock', 'BaseBoardProduct': 'B450M Pro4'}
        key, label = lc.machine_log_identity(hw, {'system_serial': 'To Be Filled By O.E.M.',
                                                  'board_serial': 'M80-ABC'})
        self.assertTrue(key)
        self.assertEqual(label, 'ASRock B450M Pro4 · SN M80-ABC')

    def test_nothing_known_is_none(self):
        self.assertEqual(lc.machine_log_identity({}, {})[0], None)


class SeparateLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, lc.LOG_BASENAME)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, first_line, name=None):
        with open(name or self.base, 'w', encoding='utf-8') as f:
            f.write('﻿' + (first_line + '\n' if first_line else '') + '2026-10-21 [INFO] x\n')

    def _archived(self):
        root = os.path.join(self.tmp, lc.OTHER_MACHINES_SUBDIR)
        return sorted(os.listdir(root)) if os.path.isdir(root) else []

    def test_same_machine_stays(self):
        self._write(lc.log_header_line('aaa111', 'LENOVO X'))
        self.assertEqual(lc.separate_other_machine_log(self.tmp, 'aaa111')[0], None)
        self.assertTrue(os.path.isfile(self.base))
        self.assertEqual(self._archived(), [])

    def test_other_machine_moved_with_rotated(self):
        self._write(lc.log_header_line('bbb222', 'HP EliteDesk · SN CZC1'))
        self._write('', self.base + '.1')
        dest, label, errors = lc.separate_other_machine_log(self.tmp, 'aaa111')
        self.assertEqual((label, errors), ('HP EliteDesk · SN CZC1', []))
        self.assertFalse(os.path.exists(self.base))
        self.assertEqual(sorted(os.listdir(dest)), [lc.LOG_BASENAME, lc.LOG_BASENAME + '.1'])
        self.assertTrue(self._archived()[0].endswith(' - HP EliteDesk · SN CZC1'))

    def test_legacy_log_without_header_moved(self):
        self._write('')
        dest, label, _ = lc.separate_other_machine_log(self.tmp, 'aaa111')
        self.assertTrue(dest)
        self.assertIn('gépazonosító nélkül', label)

    def test_unknown_either_side_does_nothing(self):
        self._write(lc.log_header_line(None, 'ismeretlen gép'))
        self.assertEqual(lc.separate_other_machine_log(self.tmp, 'aaa111')[0], None)
        self._write(lc.log_header_line('bbb222', 'X'))
        self.assertEqual(lc.separate_other_machine_log(self.tmp, None)[0], None)
        self.assertTrue(os.path.isfile(self.base))

    def test_locked_main_file_moves_nothing(self):
        self._write(lc.log_header_line('bbb222', 'X'))
        self._write('', self.base + '.1')
        real_move = shutil.move

        def move(src, dst):
            if src == self.base:
                raise PermissionError('foglalt')
            return real_move(src, dst)
        with mock.patch.object(shutil, 'move', side_effect=move):
            dest, _l, errors = lc.separate_other_machine_log(self.tmp, 'aaa111')
        self.assertIsNone(dest)
        self.assertTrue(errors)
        self.assertTrue(os.path.isfile(self.base + '.1'))
        self.assertEqual(os.listdir(os.path.join(self.tmp, lc.OTHER_MACHINES_SUBDIR)), [])

    def test_clear_logs_takes_archived_machines_too(self):
        quiet()
        self._write(lc.log_header_line('bbb222', 'X'))
        lc.separate_other_machine_log(self.tmp, 'aaa111')
        self._write(lc.log_header_line('aaa111', 'Y'))
        with mock.patch.object(lc, '_app_data_dir', return_value=self.tmp):
            removed, _freed, errors = lc.clear_logs()
        self.assertEqual((removed, errors), (2, []))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, lc.OTHER_MACHINES_SUBDIR)))


if __name__ == '__main__':
    unittest.main()
