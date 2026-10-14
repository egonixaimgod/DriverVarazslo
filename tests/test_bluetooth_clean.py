"""Bluetooth tiszta lap (2026-10-14): a kiválasztás, a SYSTEM-eredmény olvasása és hogy a
kör őszinte legyen (a rádió MINDIG visszakapcsol, a ✅ csak visszaolvasott üres állapotra jár).

A bemenet a felhasználó gépének eszközfájából van (ASUS USB-BT500, két "Forcell Master Pro"
párosítás, PS VR2 / DualShock kontroller). Valódi rendszerhez semmi nem nyúl: a `run`, az
eszközfa és a SYSTEM-lépés csonk."""
import unittest
from types import SimpleNamespace

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import bluetooth_core as bt
from app.common import CMD_TIMEOUT_RETURNCODE

RADIO_C = '0c39aaad-6a84-59ef-8fad-b138d5d3c9a1'
FORCELL_C = '644e56a0-81d7-563b-b549-254fc63202e8'
DS4_C = 'c28a6dab-c6d1-51a4-b393-e8d8cc79f5f9'
PC_C = '00000000-0000-0000-ffff-ffffffffffff'

RADIO = 'USB\\VID_0B05&PID_190E\\00E04C239987'


def _tree():
    return [
        {'id': RADIO, 'present': True, 'problem': 0, 'cls': 'Bluetooth', 'friendly': 'ASUS USB-BT500', 'container': RADIO_C},
        {'id': 'BTH\\MS_RFCOMM\\7&dd6f3d2&2&0', 'present': False, 'cls': 'Bluetooth', 'container': RADIO_C},
        {'id': 'BTHENUM\\Dev_3B9ED7071C80\\8&ae0cc96&4&BluetoothDevice_3B9ED7071C80', 'present': False,
         'cls': 'Bluetooth', 'friendly': 'Forcell Master Pro', 'container': FORCELL_C},
        {'id': 'BTHHFENUM\\BthHFPAudio\\9&27af0095&0&97', 'present': False, 'cls': 'MEDIA', 'container': FORCELL_C},
        {'id': 'BTHENUM\\{00001124-0000-1000-8000-00805f9b34fb}_VID&0002054c_PID&09cc\\8&ae0cc96&4&8430951E43C0_C00000',
         'present': False, 'cls': 'HIDClass', 'container': DS4_C},
        # A BT-kontroller HID-gyereke NEM BT-enumerátoros - csak a konténer köti hozzá.
        {'id': 'HID\\{00001124-0000-1000-8000-00805f9b34fb}_VID&0002054c_PID&09cc\\9&1d1fc358&2&0000',
         'present': False, 'cls': 'HIDClass', 'desc': 'HID-compliant game controller', 'container': DS4_C},
        # A gép saját eszköze és egy USB-s billentyűzet: nem BT, maradnak.
        {'id': 'PCI\\VEN_8086&DEV_7A70', 'present': True, 'cls': 'System', 'container': PC_C},
        {'id': 'USB\\VID_3434&PID_0430\\6&1', 'present': True, 'cls': 'Keyboard', 'container': 'aaaa'},
    ]


def _res(rc=0, out=''):
    return SimpleNamespace(returncode=rc, stdout=out, stderr='')


class _Sim:
    """Állapotos csonk: a /remove-device tényleg eltávolít a fából."""
    def __init__(self, remove_rc=0, stuck=False):
        self.nodes = _tree()
        self.calls = []
        self.remove_rc = remove_rc
        self.stuck = stuck

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:2] == ['pnputil', '/remove-device']:
            if self.stuck:
                return _res(CMD_TIMEOUT_RETURNCODE)
            if self.remove_rc == 0:
                self.nodes = [n for n in self.nodes if n['id'] != cmd[2]]
            return _res(self.remove_rc)
        if cmd[0] == 'powershell' and 'Stop-Service' in cmd[-1]:
            return _res(0, 'STOPPED:bthserv\n')
        return _res(0)

    def enum(self):
        return [dict(n) for n in self.nodes]

    def pnp(self, verb):
        return [c[2] for c in self.calls if c[:2] == ['pnputil', verb]]


def _sys_ok(run, log):
    return {'deleted': ['Devices\\3b9ed7071c80 | Forcell Master Pro'], 'errors': [], 'left': 0, 'done': True}


class SelectTest(unittest.TestCase):
    def test_radio_and_its_container_stay_bt_children_go(self):
        radios, targets = bt.select_bluetooth_nodes(_tree())
        ids = [n['id'] for n in targets]
        self.assertEqual([r['id'] for r in radios], [RADIO])
        self.assertNotIn(RADIO, ids)                       # a rádió konténere nem bontható ki
        self.assertIn('BTH\\MS_RFCOMM\\7&dd6f3d2&2&0', ids)  # de a protokoll-csomópont megy
        self.assertTrue(any(i.startswith('HID\\') for i in ids))  # konténer szerinti gyerek
        self.assertFalse(any(i.startswith(('PCI\\', 'USB\\')) for i in ids))
        self.assertTrue(ids[0].startswith('HID\\'))        # gyerek a szülő előtt
        self.assertEqual(len(ids), 5)


class ResultParseTest(unittest.TestCase):
    def test_no_done_is_unknown_not_zero(self):
        self.assertIsNone(bt.parse_clear_result('DELETED: 3\nLEFT: 0\n')['left'])
        self.assertIsNone(bt.parse_clear_result('')['left'])

    def test_normal(self):
        r = bt.parse_clear_result('﻿DEL: Devices\\d52c42b1345b | Forcell Master Pro\nDELETED: 1\nLEFT: 0\nDONE\n')
        self.assertEqual(r['left'], 0)
        self.assertEqual(r['deleted'], ['Devices\\d52c42b1345b | Forcell Master Pro'])


class FlowTest(unittest.TestCase):
    def _go(self, sim, **kw):
        quiet()
        return bt.clear_bluetooth(sim.run, lambda s: None, sim.enum, system_clear=kw.pop('system_clear', _sys_ok), **kw)

    def test_happy_path_is_ok_and_radio_back(self):
        sim = _Sim()
        rep = self._go(sim)
        self.assertTrue(rep['ok'], rep)
        self.assertEqual(sim.pnp('/disable-device'), [RADIO])
        self.assertEqual(sim.pnp('/enable-device'), [RADIO])
        self.assertTrue(any('Start-Service' in c[-1] for c in sim.calls if c[0] == 'powershell'))

    def test_cancel_still_reenables_radio(self):
        sim = _Sim()
        rep = self._go(sim, check_cancel=lambda: True)
        self.assertTrue(rep['cancelled'])
        self.assertFalse(rep['ok'])
        self.assertEqual(sim.pnp('/enable-device'), [RADIO])
        self.assertEqual(sim.pnp('/remove-device'), [])

    def test_left_nodes_not_ok(self):
        rep = self._go(_Sim(remove_rc=1))
        self.assertFalse(rep['ok'])
        self.assertEqual(len(rep['left_nodes']), 5)

    def test_unverified_pairings_not_ok(self):
        rep = self._go(_Sim(), system_clear=lambda r, l: {'deleted': [], 'errors': ['x'], 'left': None, 'done': False})
        self.assertFalse(rep['ok'])

    def test_stuck_aborts_after_three(self):
        sim = _Sim(stuck=True)
        rep = self._go(sim)
        self.assertTrue(rep['stuck'])
        self.assertEqual(len(sim.pnp('/remove-device')), bt.BT_MAX_STUCK)
        self.assertEqual(sim.pnp('/enable-device'), [RADIO])

    def test_disabled_radio_left_alone(self):
        sim = _Sim()
        sim.nodes[0]['problem'] = bt.CM_PROB_DISABLED
        self._go(sim)
        self.assertEqual(sim.pnp('/disable-device'), [])
        self.assertEqual(sim.pnp('/enable-device'), [])


class GeneratedPsTest(unittest.TestCase):
    def test_no_control_chars(self):
        self.assertFalse([c for c in bt.CLEAR_PAIRINGS_PS if ord(c) < 32 and c not in '\n'])


if __name__ == '__main__':
    unittest.main()
