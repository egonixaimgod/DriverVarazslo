"""A Build 344-es terepi napló (2026-09-28, ASRock B450M Pro4, Win10 19045) javításai.

Minden eset a terepen MÉRT adatból épül: a két AMD csomag INF-dekorációja (letöltve és
kicsomagolva), a pnputil szó szerinti kimenete, a naplóban látott óraugrás."""
import os
import shutil
import sys
import tempfile
import unittest

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import wu_core, wusettings_core, driverusage_core as du, powerplan_core as pp
from app.gui import autofix as af
from app.gui.hwscan import GuiHwScanMixin

WIN10 = {'major': 10, 'minor': 0, 'build': 19045, 'arch': 'amd64', 'product_type': 1}
WIN11 = dict(WIN10, build=26100)

SMB_IDS = [r'PCI\VEN_1022&DEV_790B&SUBSYS_FFFF1849&REV_61', r'PCI\VEN_1022&DEV_790B&SUBSYS_FFFF1849',
           r'PCI\VEN_1022&DEV_790B&CC_0C0500', r'PCI\VEN_1022&DEV_790B&CC_0C05']
GPIO_IDS = [r'ACPI\VEN_AMDI&DEV_F030', r'ACPI\AMDIF030', r'*AMDIF030']

# A mért INF-ek lényegi része (a [Manufacturer] és a models-szekció szó szerint).
AMDINTERFACE_2_0_0_29 = (
    "[Version]\nClass=System\nDriverVer=07/08/2026,2.0.0.29\n"
    "[Manufacturer]\n%AMD% = AMD.Mfg, NTX86.10.0...22000, NTX86.10.0...26100, "
    "NTamd64.10.0...22000, NTamd64.10.0...26100 ;comment\n"
    "[AMD.Mfg.NTamd64.10.0...22000]\n%SMB% = Inst, PCI\\VEN_1022&DEV_790B\n"
    "[AMD.Mfg.NTamd64.10.0...26100]\n%SMB% = Inst, PCI\\VEN_1022&DEV_790B\n"
    "[Strings]\nAMD=\"AMD\"\nSMB=\"AMD SMBus\"\n")
AMDGPIO3_3_0_5_0 = (
    "[Version]\nDriverVer=11/10/2025,3.0.5.0000\n[Manufacturer]\n%AMD%=Amd,NTamd64.10.0...22000\n"
    "[Amd.NTamd64.10.0...22000]\n%GPIO.DeviceDesc% = GPIO_Inst,ACPI\\AMDIF030\n")
AMDGPIO3_3_0_3_0 = AMDGPIO3_3_0_5_0.replace('3.0.5.0000', '3.0.3.0000').replace(
    'NTamd64.10.0...22000', 'NTamd64')


def _pkg(tmp, name, text):
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'x.inf'), 'w', encoding='utf-8') as f:
        f.write(text)
    return d


class InfOsApplicable(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merte_esetek_win10(self):
        smb = _pkg(self.tmp, 'smb', AMDINTERFACE_2_0_0_29)
        g5 = _pkg(self.tmp, 'g5', AMDGPIO3_3_0_5_0)
        g3 = _pkg(self.tmp, 'g3', AMDGPIO3_3_0_3_0)
        self.assertIs(wu_core.inf_os_applicable(smb, SMB_IDS, WIN10)[0], False)
        self.assertIs(wu_core.inf_os_applicable(g5, GPIO_IDS, WIN10)[0], False)
        self.assertIs(wu_core.inf_os_applicable(g3, GPIO_IDS, WIN10)[0], True)

    def test_win11_en_mind_jo(self):
        for text, ids in ((AMDINTERFACE_2_0_0_29, SMB_IDS), (AMDGPIO3_3_0_5_0, GPIO_IDS)):
            d = _pkg(self.tmp, 'p' + str(len(os.listdir(self.tmp))), text)
            self.assertIs(wu_core.inf_os_applicable(d, ids, WIN11)[0], True)

    def test_nem_eldontheto_esetek(self):
        # Az eszköz nem szerepel / dekoráció nélküli szekció / nincs [Manufacturer] -> None
        self.assertIsNone(wu_core.inf_os_applicable(
            _pkg(self.tmp, 'a', AMDGPIO3_3_0_5_0), SMB_IDS, WIN10)[0])
        undecorated = "[Manufacturer]\n%A%=Amd\n[Amd]\n%D%=I,ACPI\\AMDIF030\n"
        self.assertIsNone(wu_core.inf_os_applicable(_pkg(self.tmp, 'b', undecorated), GPIO_IDS, WIN10)[0])
        self.assertIsNone(wu_core.inf_os_applicable(_pkg(self.tmp, 'c', "[Version]\n"), GPIO_IDS, WIN10)[0])
        self.assertIsNone(wu_core.inf_os_applicable(self.tmp, [], WIN10)[0])

    def test_vegyes_szekcio_barmelyik_jo_eleg(self):
        mixed = ("[Manufacturer]\n%A%=Amd,NTamd64,NTamd64.10.0...22000\n"
                 "[Amd.NTamd64]\n%D%=I,ACPI\\AMDIF030\n[Amd.NTamd64.10.0...22000]\n%D%=I,ACPI\\AMDIF030\n")
        self.assertIs(wu_core.inf_os_applicable(_pkg(self.tmp, 'm', mixed), GPIO_IDS, WIN10)[0], True)

    def test_dekoracio_szabalyok(self):
        cases = [('NTamd64', True, True), ('NTamd64.10.0...22000', False, True), ('NTx86', False, False),
                 ('NTarm64', False, False), ('NTamd64.6.3', True, True), ('NTamd64.10.0.3', False, False),
                 ('NTamd64.10.0.0x1..19041', True, True), ('NT.10.0...26100', False, True)]
        for dec, w10, w11 in cases:
            self.assertIs(wu_core.inf_decoration_applies(dec, WIN10), w10, dec)
            self.assertIs(wu_core.inf_decoration_applies(dec, WIN11), w11, dec)
        self.assertIsNone(wu_core.inf_decoration_applies('foo', WIN10))


class Reconcile(unittest.TestCase):
    """Más Windowsra címkézett katalógus-csomag nem válthat ki WU-ajánlatot."""
    POOL = [{'update_id': 'u1', 'hwid': 'H',
             'wu_title': 'Advanced Micro Devices, Inc - System - 8/30/2017 12:00:00 AM - 5.12.0.38',
             'wu_date': '2017-08-30'}]
    HIT = {'hwid': 'H', 'name': 'AMD SMBus', 'cat_guid': 'g1', 'wu_date': '2026-07-07',
           'wu_title': 'MS Katalógus: Advanced Micro Devices, Inc System Driver Update (2.0.0.29)'}

    def setUp(self):
        quiet()

    def test_mas_windowsra_cimkezett_nem_valtja_ki(self):
        out = GuiHwScanMixin._reconcile_wu_catalog(self.POOL, [dict(self.HIT, os_label_ok=False)], {})
        self.assertEqual([p.get('update_id') for p in out], ['u1'])

    def test_erre_a_windowsra_cimkezett_kivaltja(self):
        out = GuiHwScanMixin._reconcile_wu_catalog(self.POOL, [dict(self.HIT, os_label_ok=True)], {})
        self.assertEqual([p.get('update_id') for p in out if p.get('update_id')], [])


class NoSourceActionable(unittest.TestCase):
    def test_driver_nelkuli_eszkoz_teendo(self):
        e = {'hwid': SMB_IDS[0], 'pnp_id': SMB_IDS[0] + r'\3&1', 'inbox_now': False, 'no_driver': True}
        self.assertTrue(wu_core.no_source_is_actionable(e))
        self.assertFalse(wu_core.no_source_is_actionable(dict(e, no_driver=False)))


class SoftwareDistribution(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self._sr = os.environ.get('SYSTEMROOT')
        os.environ['SYSTEMROOT'] = self.tmp
        self._rm = shutil.rmtree
        self._sleep = wusettings_core.time.sleep
        wusettings_core.time.sleep = lambda s: None

    def tearDown(self):
        shutil.rmtree = self._rm
        wusettings_core.time.sleep = self._sleep
        os.environ['SYSTEMROOT'] = self._sr or ''
        self._rm(self.tmp, ignore_errors=True)

    def test_zarolt_fajl_nem_hamis_siker(self):
        sd = os.path.join(self.tmp, 'SoftwareDistribution', 'Download', 'x', 'Metadata')
        os.makedirs(sd)
        open(os.path.join(sd, 'UpdateAgent.dll'), 'w').close()

        def locked(path, ignore_errors=False):
            raise PermissionError(5, 'Access is denied')
        shutil.rmtree = locked
        res = wusettings_core.clear_software_distribution(lambda *a, **k: None)
        self.assertFalse(res['ok'])
        self.assertEqual(res['remaining'], 1)
        self.assertTrue(wusettings_core.software_distribution_message(res).startswith('⚠️'))
        logs = []
        wusettings_core.disable_wu_full(lambda *a, **k: None, logs.append)
        self.assertNotIn('✅ Gyorsítótár törölve.', logs)

    def test_sikeres_torles(self):
        os.makedirs(os.path.join(self.tmp, 'SoftwareDistribution', 'DataStore'))
        res = wusettings_core.clear_software_distribution(lambda *a, **k: None)
        self.assertTrue(res['ok'])
        self.assertEqual(wusettings_core.software_distribution_message(res), '✅ Gyorsítótár törölve.')


class UsageSameOriginal(unittest.TestCase):
    """Két azonos eredeti nevű csomag (amdgpio3.inf), azonos .sys - a mappa-hash dönt."""

    def test_csak_a_futo_csomag_hasznalt(self):
        raw = {'devices': [], 'filters': [], 'printers': [],
               'services': [{'name': 'amdgpio3', 'state': 'Running', 'start': 'Boot', 'file': 'amdgpio3.sys',
                             'repo': 'amdgpio3.inf', 'dir': 'amdgpio3.inf_amd64_aaa111'}]}
        facts = {'oem6.inf': {'services': ['amdgpio3'], 'sys_files': ['amdgpio3.sys']},
                 'oem17.inf': {'services': ['amdgpio3'], 'sys_files': ['amdgpio3.sys']}}
        origs = {'oem6.inf': 'amdgpio3.inf', 'oem17.inf': 'amdgpio3.inf'}
        folders = {'oem6.inf': 'amdgpio3.inf_amd64_bbb222', 'oem17.inf': 'amdgpio3.inf_amd64_aaa111'}
        u = du.build_usage(raw, facts, ['oem6.inf', 'oem17.inf'], origs, folders)
        self.assertEqual(u['oem17.inf']['state'], du.USAGE_ACTIVE)
        self.assertNotEqual(u['oem6.inf']['state'], du.USAGE_ACTIVE)
        # mappa-adat nélkül a régi viselkedés (mindkettő "fut") - nem romlik el semmi
        u2 = du.build_usage(raw, facts, ['oem6.inf', 'oem17.inf'], origs)
        self.assertEqual(u2['oem6.inf']['state'], du.USAGE_ACTIVE)


class PowerPlan(unittest.TestCase):
    QUERY = ("Power Setting GUID: 893dee8e  (Processor minimum)\n  Minimum Possible Setting: 0x00000000\n"
             "  Maximum Possible Setting: 0x00000064\n  Possible Settings increment: 0x00000001\n"
             "  Current AC Power Setting Index: 0x00000064\n  Current DC Power Setting Index: 0x00000005\n")

    def setUp(self):
        quiet()

    def test_parse_es_szabaly(self):
        self.assertEqual(pp.parse_setting_indexes(self.QUERY), (100, 5))
        self.assertEqual(pp.parse_setting_indexes(''), (None, None))
        self.assertTrue(pp.keep_current_scheme(pp.ULTIMATE_PERFORMANCE_GUID, None))
        self.assertTrue(pp.keep_current_scheme('11111111-2222-3333-4444-555555555555', 100))
        self.assertFalse(pp.keep_current_scheme('11111111-2222-3333-4444-555555555555', 5))
        self.assertFalse(pp.keep_current_scheme(pp.BALANCED_GUID, 100))
        self.assertFalse(pp.keep_current_scheme(pp.POWER_SAVER_GUID, 100))
        self.assertFalse(pp.keep_current_scheme(pp.HIGH_PERFORMANCE_GUID, 100))

    def _run(self, active_guid, min_ac_hex):
        calls = []

        class R:
            def __init__(s, rc=0, out=''):
                s.returncode, s.stdout, s.stderr = rc, out, ''

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ['powercfg', '/getactivescheme']:
                return R(0, f'Power Scheme GUID: {active_guid}  (Revision - Ultra Performance)')
            if cmd[:2] == ['powercfg', '/query']:
                return R(0, self.QUERY.replace('0x00000064\n  Current DC', f'{min_ac_hex}\n  Current DC'))
            return R(0, '')
        return pp.apply_performance_plan(run), calls

    def test_egyedi_teljesitmeny_sema_marad(self):
        res, calls = self._run('11111111-2222-3333-4444-555555555555', '0x00000064')
        self.assertTrue(res['ok'] and res['kept'])
        self.assertNotIn(['powercfg', '/setactive', pp.HIGH_PERFORMANCE_GUID], calls)
        self.assertIn('processzor minimális állapota', res['applied'])

    def test_beepitett_alap_semak_mindig_lecserelodnek(self):
        # Kiegyensúlyozott és Energiatakarékos: akkor is átállítjuk, ha valaki 100%-ra húzta
        # bennük a processzor minimumát - a beépített alapsémák sosem maradnak.
        for guid in (pp.BALANCED_GUID, pp.POWER_SAVER_GUID):
            res, calls = self._run(guid, '0x00000064')
            self.assertFalse(res['kept'], guid)
            self.assertIn(['powercfg', '/setactive', pp.HIGH_PERFORMANCE_GUID], calls)

    def test_teljesitmenycentrikus_a_reviOS_ertekeivel(self):
        # Kiegyensúlyozottról: a BEÉPÍTETT Teljesítménycentrikus aktiválódik (saját séma NEM
        # jön létre), és a ReviOS rejtett beállításai GUID-dal rákerülnek.
        calls = []

        class R:
            def __init__(s, rc=0, out=''):
                s.returncode, s.stdout, s.stderr = rc, out, ''

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ['powercfg', '/getactivescheme']:
                return R(0, f'Power Scheme GUID: {pp.BALANCED_GUID}  (Kiegyensúlyozott)')
            return R(0, '')
        res = pp.apply_performance_plan(run)
        self.assertTrue(res['ok'])
        self.assertIn(['powercfg', '/setactive', pp.HIGH_PERFORMANCE_GUID], calls)
        self.assertFalse(any(c[:2] == ['powercfg', '/duplicatescheme'] for c in calls))
        self.assertFalse(any(c[:2] == ['powercfg', '/changename'] for c in calls))
        for label in ('magparkolás kikapcsolva', 'magparkolás kikapcsolva (P-magok)',
                      'órajel-emelés azonnal a maximumra', 'órajel-emelés küszöbe 10%',
                      'mély alvóállapot ritkítva (C-state küszöb 100%)',
                      'USB 3 kapcsolat-energiagazdálkodás'):
            self.assertIn(label, res['applied'])
        # AC ÉS DC is (akkumulátoron se legyen lassú)
        self.assertIn(['powercfg', '/setdcvalueindex', 'SCHEME_CURRENT', pp.SUB_PROCESSOR_GUID,
                       '0cc5b647-c1df-4637-891a-dec35c318584', '100'], calls)

    def test_hianyzo_teljesitmenycentrikus_letrejon(self):
        # Egyes OEM-image-eken nincs Nagy teljesítményű séma: a sablonból jön létre.
        calls = []

        class R:
            def __init__(s, rc=0, out=''):
                s.returncode, s.stdout, s.stderr = rc, out, ''

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd == ['powercfg', '/setactive', pp.HIGH_PERFORMANCE_GUID]:
                return R(1)
            if cmd[:2] == ['powercfg', '/duplicatescheme']:
                return R(0, 'Power Scheme GUID: 99999999-8888-7777-6666-555555555555  (Nagy teljesítményű)')
            if cmd[:2] == ['powercfg', '/getactivescheme']:
                return R(0, f'Power Scheme GUID: {pp.BALANCED_GUID}  (Kiegyensúlyozott)')
            return R(0, '')
        res = pp.apply_performance_plan(run)
        self.assertTrue(res['ok'])
        self.assertIn(['powercfg', '/setactive', '99999999-8888-7777-6666-555555555555'], calls)

    def test_visszaolvasas_elteres_nem_siker(self):
        class R:
            def __init__(s, rc=0, out=''):
                s.returncode, s.stdout, s.stderr = rc, out, ''

        def run(cmd, **kw):
            if cmd[:2] == ['powercfg', '/getactivescheme']:
                return R(0, f'Power Scheme GUID: {pp.HIGH_PERFORMANCE_GUID}  (x)')
            if cmd[:2] == ['powercfg', '/qh'] and cmd[-1] == '7b224883-b3cc-4d79-819f-8374152cbe7c':
                # a Windows nem vette át az IDLEPROMOTE-ot: 60 maradt
                return R(0, 'Current AC Power Setting Index: 0x0000003c\nCurrent DC Power Setting Index: 0x0000003c')
            return R(0, '')
        res = pp.apply_performance_plan(run)
        self.assertIn('mély alvóállapot ritkítva (C-state küszöb 100%)', res['failed'])
        self.assertNotIn('mély alvóállapot ritkítva (C-state küszöb 100%)', res['applied'])

    def test_egyedi_takarekos_sema_lecserelodik(self):
        res, calls = self._run('11111111-2222-3333-4444-555555555555', '0x00000005')
        self.assertFalse(res['kept'])
        self.assertIn(['powercfg', '/setactive', pp.HIGH_PERFORMANCE_GUID], calls)


class ChainTimer(unittest.TestCase):
    def test_gap_szabalyok(self):
        self.assertEqual(af.chain_gap_seconds(1000, 1070, 60), (70, False))       # normál
        self.assertEqual(af.chain_gap_seconds(1000, 1000 - 7200, 60), (60, True))  # óra vissza
        self.assertEqual(af.chain_gap_seconds(1000, 1000 + 7200, 60), (60, True))  # óra előre
        self.assertEqual(af.chain_gap_seconds(1000, 1050, None), (50, False))
        self.assertEqual(af.chain_gap_seconds(None, 1050, 40), (40, True))

    def _simulate(self, legs, reboot_s=70, boot_part=55, gap_jumps=None):
        """Egy teljes lánc szimulálása: `legs` = [(munkaidő_mp, óraugrás_mp a láb közben)].
        Minden láb KÜLÖN "folyamat" (új példány, nullázódó monotonic), a lánc-állapot közös.
        Visszatérés: (mért összeg, becsült-e, valós idő)."""
        clock = {'wall': 10_000.0, 'mono': 0.0, 'up': 500.0}
        stats = {}

        class Chain(af.GuiAutofixMixin):
            def _autofix_stats_get(s, k, d=None):
                return stats.get(k, d)

            def _autofix_stats_set(s, k, v):
                stats[k] = v

        import app.win32 as w
        saved = (af.time.time, af.time.monotonic, w.system_uptime_seconds)
        af.time.time = lambda: clock['wall']
        af.time.monotonic = lambda: clock['mono']
        w.system_uptime_seconds = lambda: clock['up']
        real = 0.0
        try:
            quiet()
            c = Chain()
            c._chain_timer_start_new()
            for i, (work, jump) in enumerate(legs):
                if i:
                    c = Chain()                       # új folyamat a reboot után
                    c._chain_timer_leg_begin()
                clock['mono'] += work
                clock['wall'] += work + jump
                real += work
                if i == len(legs) - 1:
                    total, est = af.chain_total_seconds(stats.get('chain_timer'),
                                                        clock['mono'] - c._chain_leg_t0)
                    return total, est, real
                c._chain_timer_leg_end(reboot=True)
                clock['wall'] += reboot_s + (gap_jumps or {}).get(i, 0)   # leállás+POST+boot+logon
                clock['mono'] = 1000.0 * (i + 1)      # új boot: a monotonic máshonnan indul
                clock['up'] = boot_part
                real += reboot_s
        finally:
            af.time.time, af.time.monotonic, w.system_uptime_seconds = saved

    def test_ugras_nelkul_pontos(self):
        total, est, real = self._simulate([(60, 0), (90, 0), (300, 0), (120, 0), (60, 0), (80, 0)])
        self.assertEqual(total, real)
        self.assertFalse(est)

    def test_terepi_lanc_ora_visszaugrik_a_4_labon(self):
        # Build 344: a 4. láb KÖZBEN a rendszeróra 2 órát visszaugrott. A lábon belüli ugrást a
        # monotonic óra nem látja, a láb végi falióra-bélyeg már az ugrott időt rögzíti, így
        # a következő rés is helyes - a mérés PONTOS, nem becslés.
        total, est, real = self._simulate([(60, 0), (90, 0), (300, 0), (120, -7200), (60, 0), (80, 0)])
        self.assertEqual(total, real)
        self.assertFalse(est)

    def test_ugras_ujraindulas_kozben_becsles(self):
        # Ha az óra az újraindulás ALATT ugrik (pl. RTC-időzóna a bootnál), a rés nem fér
        # össze az uptime-mal -> az uptime a rés becslése (a leállás+POST ideje hiányzik).
        total, est, real = self._simulate([(60, 0), (90, 0), (120, 0)], gap_jumps={1: -7200})
        self.assertTrue(est)
        self.assertEqual(real - total, 70 - 55)

    def test_regi_lanc_nincs_meres(self):
        self.assertEqual(af.chain_total_seconds(None, 10), (None, False))


class InstallerOs(unittest.TestCase):
    """A katalógus-telepítő: Win11-only nyertes -> a Win10-en is jó tartalék megy fel;
    driver nélküli eszköz + pnputil no-op -> NEM "már naprakész"."""

    def setUp(self):
        quiet()
        from tests import test_catalog_install as tci
        self.tci = tci
        self.tmp = tempfile.mkdtemp()
        self._sd = os.environ.get('SystemDrive')
        os.environ['SystemDrive'] = self.tmp
        import urllib.request
        self._uo = urllib.request.urlopen
        self._host = wu_core.host_os_signature
        wu_core.host_os_signature = lambda: dict(WIN10)

    def tearDown(self):
        import urllib.request
        urllib.request.urlopen = self._uo
        wu_core.host_os_signature = self._host
        if self._sd is None:
            os.environ.pop('SystemDrive', None)
        else:
            os.environ['SystemDrive'] = self._sd
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, alts, inf_by_url, urls, add_out=None, no_driver=False, bound_after=False):
        import urllib.request
        tci = self.tci
        stub = tci.InstallStub(self.tmp, inf_by_url, urls)
        recorded = []
        stub._no_bind_record = lambda items, *a, **k: recorded.extend(items or [])
        if bound_after:
            # a telepítés után az eszköz a felrakott csomagon fut (a kötés-ellenőrzéshez)
            stub._get_installed_driver_info = lambda: {r'ACPI\AMDIF030\0': {
                'inf': 'oem50.inf', 'version': '3.0.3.0', 'provider': 'AMD', 'date': '2025-09-08'}}
        if add_out is not None:
            stub._run_add_driver = lambda cmd: tci.CommandResult(259, add_out, '')

        def fake_urlopen(req, context=None, timeout=None):
            stub.cur_url = req.full_url
            return tci._Resp(b'CAB')
        urllib.request.urlopen = fake_urlopen
        drv = {'name': 'AMD GPIO Controller', 'cat': 'x', 'hwid': GPIO_IDS[0], 'pnp_id': r'ACPI\AMDIF030\0',
               'url': 'https://x/g5.cab', 'cat_guid': 'g5', 'wu_title': 'MS Katalógus: AMD GPIO (3.0.5.0)',
               'wu_date': '2025-11-09', 'alt_candidates': alts, 'inbox_now': False, 'no_driver': no_driver,
               'all_hwids': list(GPIO_IDS), 'installed_inf': ''}
        res = stub._install_catalog_sync([drv], task_id='t')
        return res, stub, recorded

    def test_win11_only_nyertes_utan_a_tartalek_megy_fel(self):
        res, stub, rec = self._run([('g3', 'AMD GPIO (3.0.3.0)', '2025-09-08')],
                                   {'https://x/g5.cab': AMDGPIO3_3_0_5_0, 'https://x/g3.cab': AMDGPIO3_3_0_3_0},
                                   {'g3': 'https://x/g3.cab'}, no_driver=True, bound_after=True)
        self.assertEqual(res[0], 1, '\n'.join(stub.log))
        self.assertTrue(any('csak újabb Windows-verzióra' in line for line in stub.log))
        self.assertTrue(any(r.get('no_bind_reason') == 'más Windows-verzióra készült (INF TargetOSVersion)'
                            and r.get('no_bind_os_build') == 19045 for r in rec))

    def test_nem_kotott_csomag_nem_feltelepitve_a_mertegben(self):
        # A csonk szerint a telepítés után is driver nélkül van -> a tételes mérlegben NEM
        # állhat "✅ Feltelepítve" alatt.
        res, stub, _rec = self._run([('g3', 'AMD GPIO (3.0.3.0)', '2025-09-08')],
                                    {'https://x/g5.cab': AMDGPIO3_3_0_5_0, 'https://x/g3.cab': AMDGPIO3_3_0_3_0},
                                    {'g3': 'https://x/g3.cab'}, no_driver=True, bound_after=False)
        text = '\n'.join(stub.log)
        self.assertEqual(res[0], 0)
        self.assertNotIn('✅ Feltelepítve', text)
        self.assertIn('A Windows nem rakta fel az eszközre', text)

    def test_minden_jelolt_win11_only(self):
        res, stub, _rec = self._run([], {'https://x/g5.cab': AMDGPIO3_3_0_5_0}, {}, no_driver=True)
        text = '\n'.join(stub.log)
        self.assertIn('mind újabb Windows-verzióra', text)
        self.assertIn('NINCS drivere', text)
        self.assertNotIn('MÁR GYÁRI DRIVER FUT', text)

    def test_driver_nelkuli_no_op_nem_naprakesz(self):
        # Ugyanaz a pnputil-kimenet, amit a terepi napló szó szerint rögzített.
        out = ('Microsoft PnP Utility\n\nAdding driver package:  amdgpio3.inf\nDriver package added '
               'successfully.\nPublished Name:         oem6.inf\nDriver package is up-to-date on device: '
               'ACPI\\AMDIF030\\0\n\nTotal driver packages:  1\nAdded driver packages:  0')
        res, stub, rec = self._run([], {'https://x/g5.cab': AMDGPIO3_3_0_3_0}, {}, add_out=out, no_driver=True)
        text = '\n'.join(stub.log)
        self.assertNotIn('már naprakész', text)
        self.assertIn('NEM rakta fel', text)
        self.assertTrue(any('nem választotta ki' in (r.get('no_bind_reason') or '') for r in rec))


class Relaunch(unittest.TestCase):
    """A WebView2/.NET telepítés utáni újraindítás: nem duplázott argv, saját _MEI mappa."""

    def test_parancs_es_kornyezet(self):
        from app import common
        seen = {}

        class P:
            returncode = 0

            def __init__(s, cmd, **kw):
                seen['cmd'], seen['env'] = cmd, kw.get('env') or {}

            def wait(s, timeout=None):
                return 0
        saved = (common.subprocess.Popen, common.release_app_mutex, getattr(sys, 'frozen', None),
                 sys.executable, os.environ.get('_PYI_APPLICATION_HOME_DIR'))
        common.subprocess.Popen = P
        common.release_app_mutex = lambda: False
        sys.frozen = True
        sys.executable = r'C:\Users\Józsi Béla\Downloads\DriverVarazslo.exe'
        os.environ['_PYI_APPLICATION_HOME_DIR'] = r'C:\Temp\_MEI98802'
        try:
            quiet()
            self.assertTrue(common.relaunch_detached(['--force-gui'], 'teszt'))
        finally:
            common.subprocess.Popen, common.release_app_mutex = saved[0], saved[1]
            if saved[2] is None:
                del sys.frozen
            else:
                sys.frozen = saved[2]
            sys.executable = saved[3]
            if saved[4] is None:
                os.environ.pop('_PYI_APPLICATION_HOME_DIR', None)
            else:
                os.environ['_PYI_APPLICATION_HOME_DIR'] = saved[4]
        self.assertEqual(seen['cmd'],
                         r'cmd /c start "" "C:\Users\Józsi Béla\Downloads\DriverVarazslo.exe" --force-gui')
        self.assertEqual(seen['cmd'].count('DriverVarazslo.exe'), 1)       # nincs duplázott exe
        self.assertNotIn('_PYI_APPLICATION_HOME_DIR', seen['env'])
        self.assertEqual(seen['env'].get('PYINSTALLER_RESET_ENVIRONMENT'), '1')


class GuiGuard(unittest.TestCase):
    def test_elkapott_kivetel_torli_a_jelzot(self):
        from app import common
        tmp = tempfile.mkdtemp()
        saved = common._app_data_dir
        common._app_data_dir = lambda: tmp
        try:
            quiet()
            common.gui_attempt_begin('wv2=1|net=2')
            self.assertTrue(common.gui_crash_check('wv2=1|net=2')[0])
            common.gui_attempt_failed_cleanly('kivétel: teszt')
            self.assertFalse(common.gui_crash_check('wv2=1|net=2')[0])
        finally:
            common._app_data_dir = saved
            shutil.rmtree(tmp, ignore_errors=True)


class NoBindOsBuild(unittest.TestCase):
    def test_mas_buildon_rogzitett_os_bejegyzes_elavul(self):
        import json
        from app.gui import hwscan
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'catalog_no_bind.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([{'pnp': 'A', 'title': 't1', 'os_build': 19045},
                       {'pnp': 'A', 'title': 't2', 'os_build': 26100},
                       {'pnp': 'A', 'title': 't3'}], f)

        class S(GuiHwScanMixin):
            def _no_bind_store_path(s):
                return path
        saved = dict(hwscan._HOST_OS)
        hwscan._HOST_OS.update(build=19045)
        try:
            quiet()
            titles = [t['title'] for t in S()._no_bind_load()]
        finally:
            hwscan._HOST_OS.clear()
            hwscan._HOST_OS.update(saved)
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(titles, ['t1', 't3'])


class DeferLog(unittest.TestCase):
    def test_ugyanazon_a_labon_nincs_felmegy_sor(self):
        import logging
        from tests.test_chain_order import _Chain, WU, WU_BY_UID, HIT
        logging.disable(logging.NOTSET)
        c = _Chain([HIT])
        with self.assertLogs(level='INFO') as cm:
            c._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
            rest, _ = c._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertEqual(rest, [])
        self.assertFalse(any('most FELMEGY' in line for line in cm.output), cm.output)
        c2 = _Chain([HIT], stats={'wu_deferred_uids': ['u1']})      # új láb
        with self.assertLogs(level='INFO') as cm2:
            c2._defer_wu_to_newer_catalog(list(WU), WU_BY_UID, {})
        self.assertTrue(any('most FELMEGY' in line for line in cm2.output))
        quiet()


if __name__ == '__main__':
    unittest.main()
