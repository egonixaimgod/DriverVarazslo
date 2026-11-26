"""2026-11-26: a programhoz tartozó driverek felismerése (app/drvowner_core.py) és a hatása a
besorolásra, a gép-térképre és így a fix „Szoftveres driverek megtartása” pipájára.

Terepi eset: ASUS, az eSzignó kártyaolvasó-drivere a Driverek nézetben „Nem használt” volt,
és a fix pipája sem védte volna; a naplókban ugyanez: Lenovo 20W1 (2026-11-23, Build 419)
`oem29.inf -> nodev:security: USB Smart Card Reader (+7 típus) | A gépben nincs hozzá tartozó
eszköz` (gemccid.inf, a 193 „nem használt” között). A setupapi-szakaszok a fejlesztői gép
valódi naplójából vannak, szó szerint (csak a tesztet nem érintő sorok maradtak ki)."""
import os
import unittest

import tests  # noqa: F401
from app import driverusage_core as du, drvowner_core as do, machinemap_core as mm

PF = os.path.normcase(r'C:\Program Files')
ROOTS = [PF, os.path.normcase(r'C:\Program Files (x86)'), os.path.normcase(r'C:\ProgramData')]

# A fejlesztői gép setupapi.dev.log-jából (2026-10-12, ViGEm Bus Driver telepítője).
SEC_PROGRAM = r""">>>  [Device Install (DiInstallDriver) - C:\Program Files\Nefarius Software Solutions\ViGEm Bus Driver\ViGEmBus.inf]
>>>  Section start 2026/10/12 01:36:13.335
      cmd: "C:\Program Files\Nefarius Software Solutions\ViGEm Bus Driver\nefconw.exe" --install-driver --inf-path "C:\Program Files\Nefarius Software Solutions\ViGEm Bus Driver\ViGEmBus.inf"
     dvs:      {Driver Setup Import Driver Package: C:\Program Files\Nefarius Software Solutions\ViGEm Bus Driver\ViGEmBus.inf} 01:36:13.338
     idb:                          Registered driver package 'vigembus.inf_amd64_bde72e7428095b1a' with 'oem4.inf'.
<<<  Section end 2026/10/12 01:36:13.600
"""
# Windows Update (2026-07-24) és a DriverVarázsló saját katalógus-telepítése (2026-07-24).
SEC_WU = r""">>>  [Device Install (Install Windows Update driver) - pci\ven_1022&dev_790b]
>>>  Section start 2026/07/24 06:57:25.250
      cmd: "C:\WINDOWS\uus\AMD64\wuaucltcore.exe" /DeploymentHandlerFullPath \\?\C:\Windows\UUS\AMD64\UpdateDeploy.dll /ClassId a920bcc2-a28c-4b25-b20e-8b15c6900533 /RunHandlerComServer
     dvs: {Driver Setup Import Driver Package: C:\WINDOWS\SoftwareDistribution\Download\Install\SMBUSamd.inf} 06:57:25.252
     idb:                     Registered driver package 'smbusamd.inf_amd64_246caabd058be11f' with 'oem3.inf'.
<<<  Section end 2026/07/24 06:57:26.000
"""
SEC_SELF = r""">>>  [Driver Install (DrvSetupInstallDriver) - C:\DV_Temp\driverdoktor_wu\drv_ext_1\rt640x64.inf]
>>>  Section start 2026/07/24 06:59:34.153
      cmd: pnputil /add-driver C:\DV_Temp\driverdoktor_wu\drv_ext_1\*.inf /subdirs /install
     dvs: {Driver Setup Import Driver Package: C:\DV_Temp\driverdoktor_wu\drv_ext_1\rt640x64.inf} 06:59:34.365
     idb:                     Registered driver package 'rt640x64.inf_amd64_700e80654c4b351c' with 'oem20.inf'.
<<<  Section end 2026/07/24 06:59:35.000
"""

VIGEM_DIR = os.path.normcase(r'C:\Program Files\Nefarius Software Solutions\ViGEm Bus Driver')


def _isdir_only(*dirs):
    alive = {os.path.normcase(d) for d in dirs}
    return lambda p: os.path.normcase(p) in alive


class SetupapiParse(unittest.TestCase):
    def test_szakaszok_a_mappa_szerint(self):
        regs = do.parse_setupapi_registrations(SEC_WU + SEC_PROGRAM + SEC_SELF)
        self.assertEqual(set(regs), {'vigembus.inf_amd64_bde72e7428095b1a',
                                     'smbusamd.inf_amd64_246caabd058be11f',
                                     'rt640x64.inf_amd64_700e80654c4b351c'})
        r = regs['vigembus.inf_amd64_bde72e7428095b1a']
        self.assertEqual(r['pub'], 'oem4.inf')
        self.assertTrue(r['src'].endswith(r'ViGEm Bus Driver\ViGEmBus.inf'))
        self.assertIn('nefconw.exe', r['cmd'])

    def test_regisztracio_nelkuli_szakasz_nem_szamit(self):
        # Egy már meglévő csomag újratelepítése (nincs „Registered driver package”) nem mond
        # semmit a származásáról.
        sec = SEC_PROGRAM.replace("Registered driver package 'vigembus.inf_amd64_bde72e7428095b1a' with 'oem4.inf'.",
                                  'Driver package already imported.')
        self.assertEqual(do.parse_setupapi_registrations(sec), {})

    def test_kesobbi_regisztracio_nyer(self):
        later = SEC_SELF.replace("'rt640x64.inf_amd64_700e80654c4b351c' with 'oem20.inf'",
                                 "'vigembus.inf_amd64_bde72e7428095b1a' with 'oem9.inf'")
        regs = do.parse_setupapi_registrations(SEC_PROGRAM + later)
        self.assertEqual(regs['vigembus.inf_amd64_bde72e7428095b1a']['pub'], 'oem9.inf')


class OriginClassify(unittest.TestCase):
    REGS = do.parse_setupapi_registrations(SEC_WU + SEC_PROGRAM + SEC_SELF)

    def classify(self, folder, isdir, prog=None, lister=lambda p: None):
        return do.classify_origin(self.REGS[folder], prog or [], ROOTS, isdir, lister)

    def test_program_mappajabol(self):
        k, name, ev = self.classify('vigembus.inf_amd64_bde72e7428095b1a', _isdir_only(VIGEM_DIR),
                                    [(VIGEM_DIR, 'ViGEm Bus Driver')])
        self.assertEqual((k, name), ('program', 'ViGEm Bus Driver'))
        self.assertIn('ViGEm Bus Driver', ev)

    def test_eltavolitott_program_nem_szamit(self):
        # A program mappája már nincs meg (eltávolították) -> nem „programé”.
        k, name, _ = self.classify('vigembus.inf_amd64_bde72e7428095b1a', _isdir_only())
        self.assertEqual((k, name), ('gone', None))

    def test_windows_update_es_sajat_telepites_nem_program(self):
        everything = lambda p: True
        self.assertEqual(self.classify('smbusamd.inf_amd64_246caabd058be11f', everything)[0], 'wu')
        self.assertEqual(self.classify('rt640x64.inf_amd64_700e80654c4b351c', everything)[0], 'self')

    def test_ideiglenes_mappa_nem_bizonyitek(self):
        rec = {'src': r'C:\Users\x\AppData\Local\Temp\7zS1234\gemccid.inf', 'cmd': 'dpinst_amd64.exe /q'}
        self.assertEqual(do.classify_origin(rec, [], ROOTS, lambda p: True)[0], 'temp')

    def test_store_alkalmazas_admin_nelkul_nem_tudjuk(self):
        # Mérve 2026-11-26: a WindowsApps admin nélkül nem listázható; a glob ezt üresnek
        # vette, és a GamingServices „már nincs fent” lett.
        rec = {'src': r'C:\Program Files\WindowsApps\Microsoft.GamingServices_38.117.27001.0_x64__8wekyb3d8bbwe\drivers\gameflt.inf',
               'cmd': r'"C:\Program Files\WindowsApps\Microsoft.GamingServices_38.117.27001.0_x64__8wekyb3d8bbwe\GamingServices.exe"'}
        self.assertEqual(do.classify_origin(rec, [], ROOTS, lambda p: True, lambda p: None)[0], 'unknown')
        newer = ['Microsoft.GamingServices_39.118.30001.0_x64__8wekyb3d8bbwe']
        k, name, _ = do.classify_origin(rec, [], ROOTS, lambda p: True, lambda p: newer)
        self.assertEqual((k, name), ('program', 'Microsoft.GamingServices'))
        self.assertEqual(do.classify_origin(rec, [], ROOTS, lambda p: True, lambda p: [])[0], 'gone')

    def test_program_files_mappa_uninstall_nelkul(self):
        rec = {'src': r'C:\Program Files\XboxService\x64\synthgpdriver.inf', 'cmd': 'pnputil /add-driver x'}
        k, name, _ = do.classify_origin(rec, [], ROOTS, _isdir_only(r'C:\Program Files\XboxService'))
        self.assertEqual((k, name), ('program', 'xboxservice'))


class Owners(unittest.TestCase):
    REGS = do.parse_setupapi_registrations(SEC_WU + SEC_PROGRAM + SEC_SELF)

    def test_ujrahasznositott_oem_szam_nem_teveszt_meg(self):
        # A kulcs a DriverStore-mappa: ha az oem4.inf ma egy MÁSIK csomag, nincs gazdája.
        pk = [{'published': 'oem4.inf', 'original': 'heci.inf', 'folder': 'heci.inf_amd64_0000'}]
        owners, origins = do.find_package_owners(pk, self.REGS, [(VIGEM_DIR, 'ViGEm Bus Driver')],
                                                 roots=ROOTS, isdir=lambda p: True)
        self.assertEqual(owners, {})
        self.assertEqual(origins['oem4.inf'], 'no-record')

    def test_mappa_tartalek_es_a_wu_nem_irhato_felul(self):
        pk = [{'published': 'oem29.inf', 'original': 'gemccid.inf', 'folder': 'gemccid.inf_amd64_1111'},
              {'published': 'oem3.inf', 'original': 'SMBUSamd.inf', 'folder': 'smbusamd.inf_amd64_246caabd058be11f'}]
        hits = {'gemccid.inf': (os.path.normcase(r'C:\Program Files\eSzigno\driver'), 'e-Szignó'),
                'smbusamd.inf': (os.path.normcase(r'C:\Program Files\Valami'), 'Valami')}
        owners, _ = do.find_package_owners(pk, self.REGS, [], hits, roots=ROOTS, isdir=lambda p: True)
        self.assertEqual(owners['oem29.inf']['program'], 'e-Szignó')
        self.assertEqual(owners['oem29.inf']['origin'], 'folder')
        self.assertNotIn('oem3.inf', owners)        # Windows Update hozta: erősebb bizonyíték

    def test_olvashatatlan_naplo_nemtudas(self):
        self.assertIsNone(do.read_setupapi_registrations(inf_dir=os.path.join(os.path.dirname(__file__), 'nincs_ilyen')))


class ProgramDirs(unittest.TestCase):
    def test_csak_letezo_mappa_program_gyoker_alatt(self):
        entries = [
            {'name': 'ViGEm Bus Driver', 'location': VIGEM_DIR + '\\', 'icon': '', 'uninstall': ''},
            {'name': 'MSI-s', 'location': '', 'icon': '', 'uninstall': 'MsiExec.exe /X{1234}'},
            {'name': 'Gyökér', 'location': PF, 'icon': '', 'uninstall': ''},
            {'name': 'Eltávolított', 'location': r'C:\Program Files\Regi', 'icon': '', 'uninstall': ''},
            {'name': 'Ikonból', 'location': '', 'icon': r'"C:\Program Files\eSzigno\eszigno.exe",0', 'uninstall': ''},
        ]
        dirs = do.program_dirs(entries, ROOTS, _isdir_only(VIGEM_DIR, r'C:\Program Files\eSzigno', PF))
        self.assertEqual(dirs, [(VIGEM_DIR, 'ViGEm Bus Driver'),
                                (os.path.normcase(r'C:\Program Files\eSzigno'), 'Ikonból')])


# --- A terepi eset: Lenovo 20W1, gemccid.inf (a napló szövegéből) -----------------------
GEM_FACTS = {'services': [], 'sys_files': [], 'inf_class': 'SmartCardReader', 'hwid_count': 8, 'buses': ['USB'], 'usb_vids': ['08E6'],
             'hwids': ['USB\\VID_08E6&PID_3437', 'USB\\VID_08E6&PID_3438'],
             'descriptions': ['USB Smart Card Reader'] + ['USB Smart Card Reader %d' % i for i in range(7)]}
GEM_PKG = [{'published': 'oem29.inf', 'original': 'gemccid.inf', 'class': 'SmartCardReader'}]


class UsageAndMap(unittest.TestCase):
    RAW = {'devices': [], 'services': [], 'filters': [], 'printers': []}
    OWN = {'oem29.inf': {'program': 'e-Szignó', 'evidence': 'teszt', 'origin': 'folder'}}

    def test_gazdatlan_marad_nem_hasznalt(self):
        u = du.build_usage(self.RAW, {'oem29.inf': GEM_FACTS}, ['oem29.inf'])
        self.assertEqual(u['oem29.inf']['state'], du.USAGE_UNUSED)
        m = mm.build_machine_map([], GEM_PKG, {'oem29.inf': GEM_FACTS}, u)
        self.assertEqual(m['packages']['oem29.inf']['slot'], 'nodev:security')   # a terepi napló szerint

    def test_programhoz_tartozik_es_szoftveres_kategoria(self):
        u = du.build_usage(self.RAW, {'oem29.inf': GEM_FACTS}, ['oem29.inf'], owners=self.OWN)
        e = u['oem29.inf']
        self.assertEqual(e['state'], du.USAGE_PROGRAM)
        self.assertEqual(e['program'], 'e-Szignó')
        self.assertIn('e-Szignó', e['summary'])
        self.assertTrue(any('e-Szignó' in r for r in e['reasons']))
        m = mm.build_machine_map([], GEM_PKG, {'oem29.inf': GEM_FACTS}, u)
        info = m['packages']['oem29.inf']
        # A fix „Szoftveres driverek megtartása” pipája ezt a kategóriát védi (GUI + CLI).
        self.assertEqual((info['slot'], info['what_kind']), ('software', 'program'))
        self.assertIn('e-Szignó', info['why'])

    def test_hasznalatban_levo_allapota_nem_valtozik(self):
        raw = {'devices': [], 'filters': [], 'printers': [],
               'services': [{'name': 'gemccid', 'state': 'Running', 'start': 'Demand', 'file': 'gemccid.sys',
                             'repo': 'gemccid.inf', 'dir': 'gemccid.inf_amd64_1111'}]}
        facts = {'oem29.inf': dict(GEM_FACTS, services=['gemccid'], sys_files=['gemccid.sys'])}
        u = du.build_usage(raw, facts, ['oem29.inf'], owners=self.OWN)
        self.assertEqual(u['oem29.inf']['state'], du.USAGE_ACTIVE)

    def test_a_gep_sajat_hardvere_nem_programe(self):
        # Egy gyártói chipset-telepítő is a Program Files-ból importálhat: az hardver, törlődik.
        facts = {'oem50.inf': {'services': [], 'sys_files': [], 'inf_class': 'System', 'hwid_count': 1, 'buses': ['PCI'],
                               'hwids': ['PCI\\VEN_8086&DEV_A123'],
                               'descriptions': ['Intel(R) 100 Series/C230 Series Chipset Family SMBus - A123']}}
        own = {'oem50.inf': {'program': 'Intel Chipset Device Software', 'evidence': 't', 'origin': 'setupapi'}}
        u = du.build_usage(self.RAW, facts, ['oem50.inf'], owners=own)
        m = mm.build_machine_map([], [{'published': 'oem50.inf', 'class': 'System'}], facts, u)
        slot = m['packages']['oem50.inf']['slot']
        self.assertTrue(slot.startswith('nodev:'), slot)
        self.assertIn(slot.split(':', 1)[1], mm.PLATFORM_FUNCS)

    def test_kihuzott_kulso_eszkoz_programe_jelenlevo_nem(self):
        ext = '{6a2b3c4d-0000-0000-0000-112233445566}'
        def gem_node(present):
            return {'id': 'USB\\VID_08E6&PID_3437\\5&1', 'cls': 'SmartCardReader', 'desc': 'USB Smart Card Reader',
                    'friendly': '', 'inf': 'oem29.inf', 'present': present, 'parent': '', 'container': ext,
                    'hwids': ['USB\\VID_08E6&PID_3437'], 'compat': [], 'busdesc': ''}
        facts = {'oem29.inf': GEM_FACTS}
        for present, want in ((False, 'software'), (True, None)):
            u = du.build_usage(self.RAW, facts, ['oem29.inf'], owners=self.OWN)
            m = mm.build_machine_map([gem_node(present)], GEM_PKG, facts, u)
            slot = m['packages']['oem29.inf']['slot']
            if want:
                self.assertEqual(slot, want)
            else:
                self.assertNotEqual(slot, 'software')   # bedugott olvasó: hardver, a fix cseréli


if __name__ == '__main__':
    unittest.main()
