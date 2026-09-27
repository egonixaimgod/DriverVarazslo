"""A lánc WU-köre: régi WU-csomag helyett a katalógus újabb kiadása (2026-09-27).

Terepi eset (Build 339, ASRock B450M): a WU 2017-es AMD SMBus 5.12.0.38-at ajánlott, a
katalógusban ugyanabból 2026-os 2.0.0.29 volt."""
import unittest

from tests.catalog_stub import quiet
from app.gui.api import DriverToolApi


class _Chain(DriverToolApi):
    """Csak a halasztás logikája - a katalógus-keresés és a lánc-állapot csonkolva."""

    def __init__(self, hits, stats=None, use_catalog=True):
        self._hits = hits
        self._stats = dict(stats or {})
        self._autofix_use_catalog = use_catalog
        self._cancel_flag = False

    def emit(self, *a, **k):
        pass

    def _catalog_search_collect(self, devs, installed_info=None):
        return [h for h in self._hits if h['hwid'] in {d['id'] for d in devs}]

    def _autofix_stats_get(self, key, default=None):
        return self._stats.get(key, default)

    def _autofix_stats_set(self, key, value):
        self._stats[key] = value


DEV = {'id': r'PCI\VEN_1022&DEV_790B&SUBSYS_FFFF1849&REV_61', 'pnp_id': r'PCI\VEN_1022&DEV_790B\3&1',
       'name': 'AMD SMBus', 'err_code': 0, 'pclass': 'System'}
WU = [{'uid': 'u1', 'title': 'Advanced Micro Devices, Inc - System - 8/30/2017 12:00:00 AM - 5.12.0.38', 'device': DEV}]
# A WUA a DriverClass-t a SAJÁT osztálynevén adja: terepen (Build 343) minden AMD
# chipset-csomagnál 'OtherHardware' volt, nem 'System' - a korábbi teszt 'System'-mel
# futott, ezért nem vette észre, hogy az egyeztetés élesben vak.
WU_BY_UID = {'u1': {'DriverVerDate': '2017-08-30', 'DriverClass': 'OtherHardware'}}
HIT = {'hwid': DEV['id'], 'name': 'AMD SMBus', 'cat_guid': 'g1',
       'wu_title': 'MS Katalógus: Advanced Micro Devices, Inc - System - 2.0.0.29', 'wu_date': '2026-07-08'}


class ChainOrder(unittest.TestCase):
    def setUp(self):
        quiet()

    def test_regi_wu_halasztva_ha_a_katalogusban_ujabb(self):
        c = _Chain([HIT])
        rest, halasztott = c._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertEqual(rest, [])
        self.assertEqual(len(halasztott), 1)
        self.assertEqual(c._stats['wu_deferred_uids'], ['u1'])
        # ugyanazon a lábon a következő WU-kör sem rakja fel mégis
        rest2, _ = c._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertEqual(rest2, [])

    def test_terepi_katalogus_cim_alakkal_is(self):
        # A Build 339-es napló valódi katalógus-címe más alakú, mint a WU-é.
        real = dict(HIT, wu_title='MS Katalógus: Advanced Micro Devices, Inc System Driver Update (2.0.0.29)')
        rest, halasztott = _Chain([real])._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertEqual(rest, [])
        self.assertEqual(len(halasztott), 1)

    def test_build343_mind_az_ot_amd_eset(self):
        # A Build 343-as napló öt valódi címpárja: mind UGYANOLYAN fajta, a katalógusé újabb.
        from app.gui.hwscan import GuiHwScanMixin
        pairs = [
            ('Advanced Micro Devices Inc. - SecurityDevices - 5.17.0.0', '2021-06-11',
             'MS Katalógus: Advanced Micro Devices Inc. SecurityDevices Driver Update (5.46.0.0)', '2026-06-03'),
            ('Advanced Micro Devices, Inc - System - 8/30/2017 12:00:00 AM - 5.12.0.38', '2017-08-30',
             'MS Katalógus: Advanced Micro Devices, Inc System Driver Update (2.0.0.29)', '2026-07-07'),
            ('Advanced Micro Devices, Inc. - System - 8/29/2017 12:00:00 AM - 2.0.1.0', '2017-08-29',
             'MS Katalógus: Advanced Micro Devices, Inc. System Driver Update (3.0.5.0)', '2025-11-09'),
            ('Advanced Micro Devices, Inc - System - 2.2.0.121', '2019-09-29',
             'MS Katalógus: Advanced Micro Devices, Inc System Driver Update (2.2.0.137)', '2026-03-21'),
            ('Advanced Micro Devices - System - 1.0.0.83', '2021-05-18',
             'MS Katalógus: Advanced Micro Devices System Driver Update (1.0.0.90)', '2025-09-08'),
        ]
        for i, (wt, wd, ct, cd) in enumerate(pairs):
            pool = [{'update_id': f'u{i}', 'hwid': 'H', 'wu_title': wt, 'wu_date': wd}]
            hits = [{'hwid': 'H', 'name': 'x', 'cat_guid': f'g{i}', 'wu_title': ct, 'wu_date': cd}]
            out = GuiHwScanMixin._reconcile_wu_catalog(
                pool, hits, {f'u{i}': {'DriverClass': 'OtherHardware'}})
            self.assertEqual([p.get('update_id') for p in out if p.get('update_id')], [], wt)

    def test_mas_fajta_csomag_nem_valtja_ki(self):
        # A WU pl. egy Extension-t ad, a katalógus egy más gyártó/fajta csomagot - mindkettő kell.
        other = dict(HIT, wu_title='MS Katalógus: Realtek Semiconductor Corp. - MEDIA - 6.0.9136.1')
        rest, _ = _Chain([other])._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertEqual(len(rest), 1)

    def test_kesobbi_labon_felmegy_ha_a_katalogus_nem_oldotta_meg(self):
        c = _Chain([HIT], stats={'wu_deferred_uids': ['u1']})     # új láb, új folyamat
        rest, halasztott = c._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertEqual([m['uid'] for m in rest], ['u1'])
        self.assertEqual(halasztott, [])

    def test_hibakodos_eszkoznel_nincs_halasztas(self):
        wu = [dict(WU[0], device=dict(DEV, err_code=10))]
        rest, _ = _Chain([HIT])._defer_wu_to_newer_catalog(wu, WU_BY_UID, {})
        self.assertEqual(len(rest), 1)

    def test_gyors_modban_nincs_halasztas(self):
        rest, _ = _Chain([HIT], use_catalog=False)._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertEqual(len(rest), 1)

    def test_regebbi_katalogus_nem_valtja_ki(self):
        old = dict(HIT, wu_date='2016-01-01', wu_title='MS Katalógus: Advanced Micro Devices, Inc - System - 5.10.0.1')
        rest, _ = _Chain([old])._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertEqual(len(rest), 1)


if __name__ == '__main__':
    unittest.main()
