"""A katalógus-döntés (`_catalog_find_driver`) terepi esetei + szabály-fuzz.

Minden eset egy valódi terepi hiba visszatérését fogja meg - a forrás a CLAUDE.md
megfelelő szekciója (dátummal jelölve)."""
import unittest

from tests.catalog_stub import CatalogStub, row, device, gen_scenario, quiet
from app.gui.hwscan import _device_stem
from app.wu_core import catalog_supports_device, is_newer_release


class CatalogFieldCases(unittest.TestCase):
    def setUp(self):
        quiet()

    def test_hp_alc221_sajat_subsys_kulcs_nyer(self):
        # 2026-09-03, HP EliteDesk 800 G2: a gép saját kulcsán nincs újabb -> naprakész;
        # az általános kulcs 2026-os sora MÁS gépgyártóé (a részletlap szerint), nem ajánljuk.
        ids = [r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0221&SUBSYS_103C8054&REV_1000',
               r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0221&SUBSYS_103C8054']
        d = device(ids, [r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0221'], 'Realtek ALC221')
        own = [row('aa01', 'Realtek - MEDIA - 6.0.1.8335', '2017-12-26')]
        gen = [row('bb01', 'Realtek - MEDIA - 6.0.9980.1', '2026-04-20')]
        stub = CatalogStub(rows={ids[0].lower(): own, ids[1].lower(): own, '__base__': gen},
                           supported={'aa01': [ids[1].lower()], 'bb01': [r'hdaudio\func_01&ven_10ec&dev_0221&subsys_103c8266']})
        inst = {d['pnp_id'].upper(): {'version': '6.0.1.8335', 'date': '2017-12-26',
                                      'provider': 'Realtek', 'inf': 'oem7.inf'}}
        self.assertIsNone(stub.find(d, inst))

    def test_alps_csak_osztalykodon_illeszkedo_csomag_kizarva(self):
        # 2026-09-21, HP EliteDesk: az AlpsAlpine csomag a gép SMBus-vezérlőjére CSAK az
        # osztálykódján illeszkedik -> letöltés előtt kizárva, nem ajánljuk fel.
        ids = [r'PCI\VEN_8086&DEV_A123&SUBSYS_8054103C&REV_31', r'PCI\VEN_8086&DEV_A123&SUBSYS_8054103C',
               r'PCI\VEN_8086&DEV_A123&CC_0C0500', r'PCI\VEN_8086&DEV_A123&CC_0C05']
        d = device(ids, [r'PCI\VEN_8086&DEV_A123'], 'Intel SMBus')
        alps = [row('cc01', 'AlpsAlpine - System - 10.4200.1616.141', '2019-03-04')]
        stub = CatalogStub(rows={'__base__': alps},
                           supported={'cc01': ['acpi\\len001c', 'pci\\ven_8086&dev_a123&cc_0c05']})
        inst = {d['pnp_id'].upper(): {'version': '10.1.1.44', 'date': '2016-01-01',
                                      'provider': 'INTEL', 'inf': 'oem52.inf'}}
        self.assertIsNone(stub.find(d, inst))

    def test_amd_smbus_datum_dont_nem_a_verzio(self):
        # 2026-07-27: a 2017-es 5.12.0.38 "nagyobb" verzió, mint a 2025-ös 2.0.0.x - a
        # DÁTUM dönt, tehát a katalógus-csomagot felajánljuk. A gyártó a csupasz VEN&DEV-et
        # deklarálja (2026-09-26: ez kompatibilis azonosító, nem kizárási ok).
        ids = [r'PCI\VEN_1022&DEV_790B&SUBSYS_FFFF1849&REV_61', r'PCI\VEN_1022&DEV_790B&SUBSYS_FFFF1849']
        d = device(ids, [r'PCI\VEN_1022&DEV_790B'], 'AMD SMBus')
        cat = [row('dd01', 'Advanced Micro Devices, Inc System Driver Update (2.0.0.29)', '2026-07-08')]
        stub = CatalogStub(rows={'__base__': cat}, supported={'dd01': ['pci\\ven_1022&dev_790b']})
        inst = {d['pnp_id'].upper(): {'version': '5.12.0.38', 'date': '2017-08-30',
                                      'provider': 'Advanced Micro Devices, Inc', 'inf': 'oem3.inf'}}
        hit = stub.find(d, inst)
        self.assertIsNotNone(hit)
        self.assertIn('2.0.0.29', hit['wu_title'])

    def test_hibakodos_eszkoz_inbox_driveren_felajanlva(self):
        # 2026-09-26: Code 10 + Windows alapdriver -> verzió-összevetés NÉLKÜL felajánljuk.
        ids = [r'PCI\VEN_10EC&DEV_8168&SUBSYS_81681849&REV_15', r'PCI\VEN_10EC&DEV_8168&SUBSYS_81681849']
        d = device(ids, [r'PCI\VEN_10EC&DEV_8168'], 'Realtek NIC', err=10)
        cat = [row('ee01', 'Realtek - Net - 10.1.1.1', '2020-01-01')]
        stub = CatalogStub(rows={'__base__': cat}, supported={'ee01': [ids[1].lower()]})
        inst = {d['pnp_id'].upper(): {'version': '10.0.26100.1', 'date': '2006-06-21',
                                      'provider': 'Microsoft', 'inf': 'netrtwlane.inf'}}
        self.assertIsNotNone(stub.find(d, inst))

    def test_tipuskodra_nem_kerdezunk(self):
        stub = CatalogStub(rows={'__base__': [row('ff01', 'LG - Ports - 1.0', '2020-01-01')]})
        self.assertIsNone(stub.find(device([r'ACPI\PNP0501'], [], 'COM port')))

    def test_acpi_rovid_alak_specifikus_kulcs(self):
        # 2026-09-26: `ACPI\AMDIF030` a katalógus egyetlen indexelt alakja - kérdezni kell.
        d = device([r'ACPI\AMDIF030', r'ACPI\VEN_AMDI&DEV_F030'], [], 'AMD GPIO')
        stub = CatalogStub(rows={'acpi\\amdif030': [row('ab01', 'AMD - System - 3.0.5.0', '2025-11-09')]},
                           supported={'ab01': ['acpi\\amdif030']})
        inst = {d['pnp_id'].upper(): {'version': '2.0.1.0', 'date': '2017-08-29',
                                      'provider': 'Advanced Micro Devices, Inc.', 'inf': 'oem4.inf'}}
        hit = stub.find(d, inst)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['cat_guid'], 'ab01')

    def test_lekerdezesi_hiba_nem_naprakesz(self):
        # 2026-09-26: a hiba NEM "nincs csomag" - a hívó látja, hogy a lekérdezés elhasalt.
        ids = [r'PCI\VEN_10EC&DEV_8168&SUBSYS_81681849&REV_15']
        stub = CatalogStub(rows={'__base__': 'ERR'})
        self.assertIsNone(stub.find(device(ids, [], 'NIC')))
        self.assertTrue(stub._catalog_query_failed and stub._catalog_query_failed[0]['total'])

    def test_no_bind_utan_lejjebb_lep(self):
        # 2026-09-03: a bizonyítottan rossz csomag helyett a KÖVETKEZŐ valódi jelölt jön.
        ids = [r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0897&SUBSYS_18494897&REV_1003']
        # A Windows-alapdriveren futó eszköz gyári cserére jelölt (`generic_ok`) - enélkül
        # a régi verzió-kapu dönt (az inbox driver HID/egér eseteinek szabálya).
        d = device(ids, [], 'ALC897', generic_ok=True)
        rows = [row('r1', 'Realtek - MEDIA - 6.0.10007.1', '2026-06-22'),
                row('r2', 'Realtek - MEDIA - 6.0.9360.1', '2022-01-01')]
        stub = CatalogStub(rows={'__base__': rows})
        bad = {_device_stem(d['pnp_id']): [{'guid': 'r1'}]}
        inst = {d['pnp_id'].upper(): {'version': '10.0.26100.1', 'date': '2006-06-21',
                                      'provider': 'Microsoft', 'inf': 'hdaudio.inf'}}
        hit = stub.find(d, inst, bad)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['cat_guid'], 'r2')


class CatalogCompleteness(unittest.TestCase):
    """2026-09-27: a mintavétel helyett teljes vizsgálat, és mélyítés a "nincs csomag" előtt."""

    def setUp(self):
        quiet()

    def test_mintan_tuli_illo_testver_megmarad(self):
        # Azonos cím+dátum alatt 6 bejegyzés: a képviselő és 3 mintatestvér más gépre
        # szabott, a 6. VISZONT ide való. Eddig a minta verdiktje átszállt rá.
        ids = [r'PCI\VEN_10EC&DEV_8168&SUBSYS_81681849&REV_15', r'PCI\VEN_10EC&DEV_8168&SUBSYS_81681849']
        d = device(ids, [r'PCI\VEN_10EC&DEV_8168'], 'Realtek NIC', generic_ok=True)
        rows = [row(f'g{i}', 'Realtek Driver Update (10.74.1128.2024)', '2024-11-27') for i in range(6)]
        sup = {f'g{i}': ['pci\\ven_10ec&dev_8168&subsys_11111111'] for i in range(5)}
        sup['g5'] = [ids[1].lower()]
        stub = CatalogStub(rows={'__base__': rows}, supported=sup)
        inst = {d['pnp_id'].upper(): {'version': '10.0.26100.1', 'date': '2006-06-21',
                                      'provider': 'Microsoft', 'inf': 'rt640x64.inf'}}
        hit = stub.find(d, inst)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['cat_guid'], 'g5')

    def test_melyites_a_nincs_csomag_elott(self):
        ids = [r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0897&SUBSYS_18494897&REV_1003']
        d = device(ids, [r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0897'], 'ALC897', generic_ok=True)
        felszin = [row(f'o{i}', f'Realtek - MEDIA - 6.0.10{i:03d}.1', '2026-06-22') for i in range(3)]
        mely = felszin + [row('jo', 'Realtek - MEDIA - 6.0.9360.1', '2022-01-01')]
        sup = {f'o{i}': [r'hdaudio\func_01&ven_10ec&dev_0897&subsys_10250001'] for i in range(3)}
        sup['jo'] = [ids[0].lower().rsplit('&rev', 1)[0]]

        class Stub(CatalogStub):
            def _catalog_fetch_rows(s, hwid, ssl_ctx, max_pages=3, deep=False, **kw):
                return list(mely if max_pages > 3 else felszin)

            def _catalog_key_truncated(s, hwid, max_pages=3, deep=False):
                return max_pages <= 3

        stub = Stub(supported=sup)
        inst = {d['pnp_id'].upper(): {'version': '10.0.26100.1', 'date': '2006-06-21',
                                      'provider': 'Microsoft', 'inf': 'hdaudio.inf'}}
        hit = stub.find(d, inst)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['cat_guid'], 'jo')

    def test_gyari_driveren_nincs_melyites(self):
        # Gyári driveren futó eszköznél a régebbi sort a kiadás-kapu úgyis elutasítaná.
        ids = [r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0897&SUBSYS_18494897&REV_1003']
        d = device(ids, [], 'ALC897')
        calls = []

        class Stub(CatalogStub):
            def _catalog_fetch_rows(s, hwid, ssl_ctx, max_pages=3, deep=False, **kw):
                calls.append(max_pages)
                return [row('o1', 'Realtek - MEDIA - 6.0.10001.1', '2026-06-22')]

            def _catalog_key_truncated(s, hwid, max_pages=3, deep=False):
                return True

        stub = Stub(supported={'o1': [r'hdaudio\func_01&ven_10ec&dev_0897&subsys_10250001']})
        inst = {d['pnp_id'].upper(): {'version': '6.0.9000.1', 'date': '2021-01-01',
                                      'provider': 'Realtek', 'inf': 'oem5.inf'}}
        self.assertIsNone(stub.find(d, inst))
        self.assertTrue(all(p <= 3 for p in calls), calls)


class CatalogFuzzInvariants(unittest.TestCase):
    """1500 generált forgatókönyv - a döntés ALAPSZABÁLYAI minden esetben."""

    def setUp(self):
        quiet()

    def test_szabalyok(self):
        for seed in range(1500):
            item, installed, stub, no_bind, sup = gen_scenario(seed)
            queried = set()
            orig = stub._catalog_supported_hwids

            def rec(guid, ctx, _o=orig, _q=queried):
                _q.add(guid)
                return _o(guid, ctx)
            stub._catalog_supported_hwids = rec
            hit = stub.find(item, installed, no_bind)      # 1) sosem dob kivételt
            if not hit:
                continue
            dev_ids = {h.lower() for h in item['all_hwids']}
            bad = {r['guid'] for r in no_bind.get(_device_stem(item['pnp_id']), [])}
            for g in [hit['cat_guid']] + [a[0] for a in hit['alt_candidates']]:
                s = sup.get(g)
                # 2) MEGVIZSGÁLT és bizonyítottan nem ide való csomag sosem nyer és sosem
                #    tartalék (a meg nem vizsgált testvér jelölt maradhat - nemtudás)
                if g in queried and s is not None:
                    self.assertTrue(catalog_supports_device(dev_ids, s)[0], (seed, g, s))
            # 3) ismerten rossz csomag sosem nyer
            self.assertNotIn(hit['cat_guid'], bad, seed)
            # 4) gyári driveren futó, hibátlan eszköznél sosem visszalépés
            inst = installed.get(item['pnp_id'].upper()) or {}
            if inst.get('provider') == 'Realtek' and not item['err_code']:
                title = hit['wu_title'].replace('MS Katalógus: ', '')
                self.assertIsNot(is_newer_release(hit['wu_date'], title, inst['date'], inst['version']),
                                 False, seed)


if __name__ == '__main__':
    unittest.main()
