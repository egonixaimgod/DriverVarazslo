"""Kijelző & Színkezelés: a VCGT-parser és a "néma hamis siker" elleni visszaolvasások
(2026-10-12-i review: a gyári visszaállítás 0 hibát mondott elbukott lépések mellett).

Semmi nem nyúl a valódi registryhez vagy kijelzőhöz: a winreg, a ctypes.windll és a
DisplayConfig-hívások csonkolva."""
import struct
import types
import unittest
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import colormgmt_core as cm, display_core as dc
from app.gui import display as gd


def _icc_with_vcgt(body):
    """Minimális ICC: 128 bájt fejléc + 1 tag (vcgt) + a tag teste."""
    off = 128 + 4 + 12
    return b'\0' * 128 + struct.pack('>I', 1) + struct.pack('>4sII', b'vcgt', off, len(body)) + body


def _table(ch, cnt, esz, values):
    fmt = '>' + ('B' if esz == 1 else 'H') * len(values)
    return b'vcgt' + b'\0' * 4 + struct.pack('>I', 0) + struct.pack('>HHH', ch, cnt, esz) + struct.pack(fmt, *values)


def _formula(gamma):
    g = int(gamma * 65536)
    return b'vcgt' + b'\0' * 4 + struct.pack('>I', 1) + struct.pack('>iii', g, 0, 65536) * 3


class VcgtParserTest(unittest.TestCase):
    def setUp(self):
        quiet()

    def test_linear_table_16bit(self):
        vals = [i * 257 for i in range(256)] * 3
        self.assertEqual(cm.parse_vcgt(_icc_with_vcgt(_table(3, 256, 2, vals))), cm._linear_ramp())

    def test_single_channel_is_replicated(self):
        vals = [255 - i for i in range(256)]
        r = cm.parse_vcgt(_icc_with_vcgt(_table(1, 256, 1, vals)))
        self.assertEqual(r[0], r[1])
        self.assertEqual(r[0], r[2])
        self.assertEqual((r[0][0], r[0][255]), (65535, 0))

    def test_formula_gamma(self):
        self.assertEqual(cm.parse_vcgt(_icc_with_vcgt(_formula(1.0))), cm._linear_ramp())
        r = cm.parse_vcgt(_icc_with_vcgt(_formula(2.2)))
        self.assertAlmostEqual(r[1][128] / 65535, (128 / 255) ** 2.2, places=3)

    def test_no_vcgt_or_truncated(self):
        self.assertIsNone(cm.parse_vcgt(b'\0' * 200))
        self.assertIsNone(cm.parse_vcgt(b'\0' * 50))
        full = _icc_with_vcgt(_table(3, 256, 2, [0] * 768))
        self.assertIsNone(cm.parse_vcgt(full[:150]))      # a tag a fájl végén túlra mutat


class SetGammaReadbackTest(unittest.TestCase):
    """5-ös pont: egy görbe "betöltve" csak akkor, ha a visszaolvasás nem lineáris."""

    def setUp(self):
        quiet()
        w = mock.MagicMock()
        w.gdi32.CreateDCW.return_value = 1
        w.gdi32.SetDeviceGammaRamp.return_value = 1
        p = mock.patch('ctypes.windll', w, create=True)
        p.start()
        self.addCleanup(p.stop)

    def _set(self, ramp, back_linear):
        with mock.patch.object(dc, 'read_gamma_ramp', return_value={'linear': back_linear, 'max_deviation': 0}):
            return cm.set_gamma('\\\\.\\DISPLAY1', ramp, 'teszt')

    def test_curve_ignored_by_driver_is_failure(self):
        curve = cm.parse_vcgt(_icc_with_vcgt(_formula(2.2)))
        ok, msg = self._set(curve, back_linear=True)
        self.assertFalse(ok)
        self.assertIn('lineáris', msg)

    def test_curve_loaded(self):
        curve = cm.parse_vcgt(_icc_with_vcgt(_formula(2.2)))
        self.assertEqual(self._set(curve, back_linear=False), (True, ''))

    def test_linear_overwritten_is_failure(self):
        self.assertFalse(self._set(cm._linear_ramp(), back_linear=False)[0])
        self.assertEqual(self._set(cm._linear_ramp(), back_linear=True), (True, ''))


class SetAcmHdrGuardTest(unittest.TestCase):
    """2-es pont: HDR módban a SET_WCG_STATE(17) nem megy ki."""

    def test_hdr_mode_does_not_call(self):
        quiet()
        aid = types.SimpleNamespace(LowPart=1, HighPart=0)
        u32 = mock.MagicMock()
        with mock.patch.object(dc, 'read_hdr_state', return_value={'wcg_supported': True, 'enabled': True}), \
                mock.patch.object(dc, '_user32', u32):
            ok, st = dc.set_acm(aid, 5, False)
        self.assertFalse(ok)
        self.assertTrue(st['enabled'])
        u32.DisplayConfigSetDeviceInfo.assert_not_called()


class CalibrationReadbackTest(unittest.TestCase):
    """6-os pont: az engedélyezés verdiktje a visszaolvasás (az ICM gyökerében maradt 0)."""

    def _set(self, enabled, reads):
        with mock.patch.object(dc, 'winreg', mock.MagicMock()), \
                mock.patch.object(dc, 'calibration_management', side_effect=reads):
            return dc.set_calibration_management(enabled)

    def test_root_zero_blocks_enable(self):
        quiet()
        self.assertEqual(self._set(True, [0, 0]), (False, 0))
        self.assertEqual(self._set(True, [0, 1]), (True, 0))
        self.assertEqual(self._set(False, [1, 0]), (True, 1))


class FactoryResetErrorsTest(unittest.TestCase):
    """1-es pont: minden elbukott lépés az errors-ba kerül (a GUI ebből dönt a ✅-ról)."""

    DISP = [{'name': 'AOC', 'adapter_low': 1, 'adapter_high': 0, 'target_id': 5, 'gdi_name': 'X',
             'hdr': {'enabled': False, 'wcg_enabled': True}}]
    PLAN = {'files': [], 'printer_files': [], 'calibration': 0, 'acm_guard': True, 'profile_guard': True}

    def _reset(self, good):
        quiet()
        patches = [
            mock.patch.object(cm, 'winreg', mock.MagicMock()),
            mock.patch.object(cm, 'factory_reset_plan', return_value=dict(self.PLAN)),
            mock.patch.object(cm, '_registered_changes', return_value=[]),
            mock.patch.object(cm, '_datastore_changes', return_value=[]),
            mock.patch.object(cm, '_night_light_keys', return_value=[]),
            mock.patch.object(cm, '_color_filter_state', return_value=None),
            mock.patch.object(cm, 'remove_all_associations', return_value=['a']),
            mock.patch.object(cm, 'associations_left', return_value=[] if good else ['HKLM x SDR: p.icm']),
            mock.patch.object(cm, 'remove_guard', return_value=good),
            mock.patch.object(cm, 'reset_gamma', return_value=(good, '' if good else 'hiba')),
            mock.patch.object(dc, 'find_display_target', return_value=(object(), 5)),
            mock.patch.object(dc, 'read_hdr_state', return_value={'enabled': False, 'wcg_enabled': True}),
            mock.patch.object(dc, 'set_acm', return_value=(good, {})),
            mock.patch.object(dc, 'set_calibration_management', return_value=(good, 0)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return cm.factory_reset(lambda *a, **k: None, self.DISP)

    def test_every_failure_is_an_error(self):
        errs = ' | '.join(self._reset(good=False)['errors'])
        for part in ('ACM nem kapcsolható', 'ACM-zár', 'profil-zár', 'profil-társítás',
                     'profil-betöltés nem engedélyezhető', 'gamma nem állítható'):
            self.assertIn(part, errs)

    def test_all_good_no_errors(self):
        self.assertEqual(self._reset(good=True)['errors'], [])


class FreshDisplayIdentityTest(unittest.TestCase):
    """7-es pont: a nézet sorszáma ugyanazt a monitort jelenti, ha közben egy új a többi elé sorolódik."""

    def test_new_monitor_in_front(self):
        quiet()
        a = {'index': 0, 'name': 'A', 'adapter_low': 1, 'adapter_high': 0, 'target_id': 10}
        b = {'index': 1, 'name': 'B', 'adapter_low': 1, 'adapter_high': 0, 'target_id': 20}
        new = {'index': 0, 'name': 'UJ', 'adapter_low': 1, 'adapter_high': 0, 'target_id': 5}

        class F(gd.GuiDisplayMixin):
            def emit(self, *a):
                self.emitted = a

        f = F()
        f._display_ident = {d['index']: gd._ident(d) for d in (a, b)}      # amit a nézet látott
        moved = [new, dict(a, index=1), dict(b, index=2)]
        with mock.patch.object(dc, 'enumerate_displays', return_value=moved):
            self.assertEqual(f._fresh_display(1)['name'], 'B')
            self.assertEqual(f._fresh_display(0)['name'], 'A')
        with mock.patch.object(dc, 'enumerate_displays', return_value=[new]):
            self.assertIsNone(f._fresh_display(1))              # lecsatlakozott -> hiba, nem rossz monitor


if __name__ == '__main__':
    unittest.main()
