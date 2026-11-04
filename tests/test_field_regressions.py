"""Terepen egyszer már elromlott döntések regressziós tesztjei (2026-10-11-i audit):
a lánc-állapot atomikus írása, a törlés verdiktje magyar pnputil-szöveggel, a DISM
magyar dátuma és a nyomtató-felismerés két ismert csapdája (ESET "scanner", telefon)."""
import json
import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app.common import CommandResult
from app.drivers_core import delete_succeeded, _dism_date_to_iso
from app.wu_core import identify_printer_packages
from app.gui.autofix import GuiAutofixMixin


class ChainState(unittest.TestCase):
    """autofix_stats.json: egy félbeszakadt írás nem viheti el a lánc memóriáját."""

    def setUp(self):
        logging.disable(logging.NOTSET)   # itt a naplósor maga az ellenőrzött viselkedés
        self.tmp = tempfile.mkdtemp()
        tmp = self.tmp

        class F(GuiAutofixMixin):
            def _autofix_stats_path(self):
                return os.path.join(tmp, 'autofix_stats.json')
        self.f = F()

    def tearDown(self):
        quiet()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kulcsok_megmaradnak_es_nincs_ideiglenes_fajl(self):
        f = self.f
        f._autofix_stats_set('keep_packages', [{'published': 'oem7.inf'}])
        f._autofix_stats_add(3)
        f._autofix_stats_add(2)
        self.assertEqual(f._autofix_stats_get('keep_packages'), [{'published': 'oem7.inf'}])
        self.assertEqual(f._autofix_stats_total_and_clear(), 5)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_csonka_fajl_felreteve_es_jelezve(self):
        f = self.f
        p = f._autofix_stats_path()
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('{"keep_packages": [{"publ')      # BSOD az írás közepén (régi viselkedés)
        with self.assertLogs(level='ERROR'):
            self.assertIsNone(f._autofix_stats_get('keep_packages'))
        self.assertTrue(os.path.exists(p + '.corrupt'))  # a bizonyíték megmarad
        f._autofix_stats_set('legs', [])
        with open(p, encoding='utf-8') as fh:
            self.assertEqual(json.load(fh), {'legs': []})

    def test_iras_kozbeni_hiba_a_regi_allapotot_hagyja(self):
        f = self.f
        f._autofix_stats_set('keep_packages', ['oem7.inf'])
        # az írás elszáll (pl. nem szerializálható érték) - a régi fájl érintetlen
        with self.assertLogs(level='WARNING'):
            f._autofix_stats_set('rossz', object())
        self.assertEqual(f._autofix_stats_get('keep_packages'), ['oem7.inf'])


class DeleteVerdict(unittest.TestCase):
    """A magyar hibaüzenet tartalmazza a 'törl' sikertövet - egyszer hamis sikert adott."""

    def test_magyar_hibaszoveg_nem_siker(self):
        res = CommandResult(1, 'A(z) oem12.inf illesztőprogram-csomag törlése nem sikerült.', '')
        self.assertFalse(delete_succeeded(res))

    def test_3010_siker(self):
        self.assertTrue(delete_succeeded(CommandResult(3010, '', '')))

    def test_ures_kimenet_hibakoddal_nem_siker(self):
        self.assertFalse(delete_succeeded(CommandResult(1, '', '')))


class DismDate(unittest.TestCase):
    """A /English a dátumot NEM angolosítja: magyar gépen `2006. 06. 21.` jön."""

    def test_alakok(self):
        for raw in ('2006. 06. 21.', '2006-06-21', '6/21/2006', '21.06.2006'):
            self.assertEqual(_dism_date_to_iso(raw), '2006-06-21', raw)

    def test_olvashatatlan_ures(self):
        self.assertEqual(_dism_date_to_iso('xyz'), '')


class PrinterDetection(unittest.TestCase):
    """A driverből dönt: az ESET "scanner"-e és egy telefon-driver NEM nyomtató."""

    INFS = {
        'oem1.inf': '[Version]\nClass=Printer\n[Dev]\nUSBPRINT\\HP_LaserJet=VID_03F0&PID_1D17\n',
        'oem2.inf': '[Version]\nClass=USB\n; HP LaserJet USB kompozit\nUSB\\VID_03F0&PID_1D17\n',
        'oem3.inf': '[Version]\nClass=AntiVirus\n; ESET real-time file system scanner\n',
        'oem4.inf': '[Version]\nClass=AndroidUsbDeviceClass\n; SAMSUNG Mobile USB\nUSB\\VID_04E8&PID_6860\n',
        'oem5.inf': '[Version]\nClass=USBDevice\n; WIA scanner driver\n',
    }

    def test_besorolas(self):
        drivers = [{'published': p, 'original': p} for p in self.INFS]
        found = identify_printer_packages(drivers, inf_dir='X', reader=lambda path: self.INFS.get(os.path.basename(path)))
        self.assertEqual(found.get('oem1.inf'), 'osztaly')
        self.assertIn('oem2.inf', found)             # ugyanaz a fizikai eszköz (VID:PID)
        self.assertNotIn('oem3.inf', found)          # vírus-"scanner", nem nyomtató
        self.assertNotIn('oem4.inf', found)          # Samsung telefon: a gyártónév nem dönt
        self.assertEqual(found.get('oem5.inf'), 'kulcsszo')


class StagedAlreadyBound(unittest.TestCase):
    """2026-10-11, Sound Blaster Play! 4: a WU-ág 3 mp-cel korábban már rákötötte a csomagot,
    a katalógus-ág pnputil-ja ezt ki is mondta ("up-to-date on device: <ez az eszköz>"), a
    program mégis "a KÖTÉS hiányzik"-ot írt és újrakötést ajánlott (a keresés-kori jelzőből)."""

    SB = r'USB\VID_041E&PID_3274&MI_00'
    INF = ('[Version]\nClass=MEDIA\n[Manufacturer]\n%M%=C,NTamd64\n[C.NTamd64]\n'
           '%D%=I,USB\\VID_041E&PID_3274&MI_00\n[Strings]\nM="Creative"\nD="SB Play 4"\n')
    # a terepi napló kimenete szó szerint
    BOUND = ('Microsoft PnP Utility\n\nAdding driver package:  ksusbaud.inf\n'
             'Driver package added successfully. (Already exists in the system)\n'
             'Published Name:         oem3.inf\n'
             'Driver package is up-to-date on device: USB\\VID_041E&PID_3274&MI_00\\7&887c350&4&0000\n\n'
             'Total driver packages:  1\nAdded driver packages:  0')
    NOT_BOUND = BOUND.replace('Driver package is up-to-date on device: '
                              'USB\\VID_041E&PID_3274&MI_00\\7&887c350&4&0000\n', '')

    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self._sd = os.environ.get('SystemDrive')
        os.environ['SystemDrive'] = self.tmp
        import urllib.request
        self._uo = urllib.request.urlopen

    def tearDown(self):
        import urllib.request
        urllib.request.urlopen = self._uo
        if self._sd is None:
            os.environ.pop('SystemDrive', None)
        else:
            os.environ['SystemDrive'] = self._sd
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, pnputil_out):
        import urllib.request
        from tests import test_catalog_install as tci
        stub = tci.InstallStub(self.tmp, {'https://x/sb.cab': self.INF}, {})
        stub._run_add_driver = lambda cmd: CommandResult(259, pnputil_out, '')

        def fake_urlopen(req, context=None, timeout=None):
            stub.cur_url = req.full_url
            return tci._Resp(b'CAB')
        urllib.request.urlopen = fake_urlopen
        drv = {'name': 'Sound Blaster Play! 4', 'cat': 'x', 'hwid': self.SB + '&REV_2200',
               'pnp_id': self.SB + r'\7&887C350&4&0000', 'url': 'https://x/sb.cab', 'cat_guid': 'g',
               'wu_title': 'Creative Technology Ltd. - MEDIA - 1.16.4.26', 'wu_date': '2021-01-05',
               'alt_candidates': [], 'inbox_now': True, 'generic_replace': True,
               'all_hwids': [self.SB + '&REV_2200', self.SB], 'installed_inf': 'wdma_usb.inf'}
        stub._install_catalog_sync([drv], task_id='t')
        return stub

    def test_mar_rakotott_csomag_naprakesz(self):
        stub = self._run(self.BOUND)
        text = '\n'.join(stub.log)
        self.assertIn('már naprakész', text)
        self.assertNotIn('KÖTÉS hiányzik', text)
        self.assertEqual(stub._catalog_staged_nobind, [])

    def test_nem_kotott_csomag_tovabbra_is_jelez(self):
        stub = self._run(self.NOT_BOUND)
        self.assertIn('KÖTÉS hiányzik', '\n'.join(stub.log))
        self.assertEqual(stub._catalog_staged_nobind, ['Sound Blaster Play! 4'])


class GhostBindIsNotProof(unittest.TestCase):
    """2026-10-12, Lenovo 20KG (Build 373): a vándorló SSD-n ugyanazon a PCI-helyen más
    gépek szellemeszközei állnak (más SUBSYS). A pnputil ezekre "installed"-et írt, a valódi
    eszközre "up-to-date"-et (a régi driverén maradt) - mégis "✅ telepítve" lett."""

    from app.wu_core import package_bound_to_device_family as bound
    bound = staticmethod(bound)
    # a terepi napló kimenete szó szerint (rövidítve)
    OUT = ('Adding driver package:  e1d.inf\nDriver package added successfully.\n'
           'Published Name:         oem110.inf\n'
           'Driver package installed on device: PCI\\VEN_8086&DEV_15D7&SUBSYS_07A61028&REV_21\\3&11583659&0&FE\n'
           'Driver package installed on device: PCI\\VEN_8086&DEV_15D7&SUBSYS_225A17AA&REV_21\\3&11583659&0&FE\n'
           'Driver package is up-to-date on device: PCI\\VEN_8086&DEV_15D7&SUBSYS_225C17AA&REV_21\\3&11583659&0&FE\n'
           'Added driver packages:  1')
    DEV = {'name': 'Intel(R) Ethernet Connection (4) I219-LM #7',
           'pnp_id': 'PCI\\VEN_8086&DEV_15D7&SUBSYS_225C17AA&REV_21\\3&11583659&0&FE',
           'all_hwids': ['PCI\\VEN_8086&DEV_15D7&SUBSYS_225C17AA&REV_21',
                         'PCI\\VEN_8086&DEV_15D7&SUBSYS_225C17AA', 'PCI\\VEN_8086&DEV_15D7&CC_020000',
                         'PCI\\VEN_8086&DEV_15D7']}

    def test_mas_gep_szellemeszkoze_nem_bizonyitek(self):
        self.assertFalse(self.bound(self.OUT, self.DEV, own_uptodate_counts=False))

    def test_sajat_uptodate_alapbol_tovabbra_is_bizonyitek(self):
        # a Sound Blaster-eset (már rákötött csomag) változatlan
        self.assertTrue(self.bound(self.OUT, self.DEV))

    def test_sajat_installed_bizonyitek(self):
        out = self.OUT.replace('is up-to-date on device', 'installed on device')
        self.assertTrue(self.bound(out, self.DEV, own_uptodate_counts=False))

    def test_usb_gyerek_interfesz_tovabbra_is_bizonyitek(self):
        dev = {'pnp_id': 'USB\\VID_041E&PID_3274\\5&1', 'all_hwids': ['USB\\VID_041E&PID_3274']}
        out = 'Driver package installed on device: USB\\VID_041E&PID_3274&MI_00\\7&887c350&0&0000'
        self.assertTrue(self.bound(out, dev, own_uptodate_counts=False))


class StaleBinding(unittest.TestCase):
    """2026-10-12, Lenovo 20KGS4032P (teszt-SSD egy ASRock B450M után): egy "AMD SMBus"
    szellemeszköz a registryben `oem2.inf`-re mutatott, amit azóta a "Microsoft Print to
    PDF" csomag (prnms009.inf, Printer) kapott meg - a gép-térkép a Chipset alá tette."""

    NODES = [{'id': 'PCI\\VEN_1022&DEV_790B\\3&X', 'present': False, 'cls': 'System',
              'desc': 'AMD SMBus', 'inf': 'oem2.inf', 'hwids': ['PCI\\VEN_1022&DEV_790B']}]
    PKG = [{'published': 'oem2.inf', 'original': 'prnms009.inf', 'class': 'Printer',
            'provider': 'Microsoft', 'version': '10.0.19041.1806', 'date': '2006-06-21'}]

    def test_helper(self):
        from app.driverusage_core import stale_binding
        self.assertTrue(stale_binding('System', 'Printer'))
        self.assertFalse(stale_binding('Printer', 'printer'))
        self.assertFalse(stale_binding('', 'Printer'))           # nem tudjuk -> nem döntünk
        self.assertFalse(stale_binding('System', '%ClassName%'))

    def test_usage_and_map(self):
        from app import driverusage_core as du, machinemap_core as mm
        facts = {'oem2.inf': {'inf_class': 'Printer', 'hwid_count': 0, 'services': [], 'sys_files': []}}
        quiet()
        u = du.build_usage({'devices': du.devices_from_nodes(self.NODES), 'services': [],
                            'filters': []}, facts, ['oem2.inf'])
        m = mm.build_machine_map(self.NODES, self.PKG, facts, usage=u, printer_pubs=['oem2.inf'])
        self.assertNotIn('AMD SMBus', ' '.join(u['oem2.inf'].get('reasons') or []))
        self.assertNotIn('AMD SMBus', m['packages']['oem2.inf']['why'])
        # ellenpróba: egyező osztálynál a kötés marad
        nodes = [dict(self.NODES[0], cls='Printer')]
        m2 = mm.build_machine_map(nodes, self.PKG, facts, printer_pubs=['oem2.inf'])
        self.assertIn('AMD SMBus', m2['packages']['oem2.inf']['why'])


class AbsentGpuUnderPresentPort(unittest.TestCase):
    """2026-11-03, terep (MSI MS-7D69, ASRock A620, ASUS E1504FA): egy kiszerelt videókártya
    szelleme a JELEN LÉVŐ PCIe-port alatt 'board:gpu'-ra került, amihez nincs kártya ->
    „Váratlan hely-kulcs 'board:gpu' - az 'Egyéb' kártyára kerül”. Napló szó szerint:
    `oem20.inf -> board:gpu: NVIDIA GeForce RTX 5060 | ... most NEM csatlakoztatott eszközt`."""

    PORT = {'id': 'PCI\\VEN_1022&DEV_14DB&SUBSYS_14531022&REV_00\\3&2411E6FE&0&09', 'present': True,
            'cls': 'System', 'desc': 'PCI Express-gyökérport', 'inf': 'pci.inf',
            'hwids': ['PCI\\VEN_1022&DEV_14DB&SUBSYS_14531022&REV_00'], 'compat': ['PCI\\CC_060400']}
    GHOST = {'id': 'PCI\\VEN_10DE&DEV_2D05&SUBSYS_88EE1043&REV_A1\\4&1A2B3C4D&0&0009', 'present': False,
             'cls': 'Display', 'desc': 'NVIDIA GeForce RTX 5060', 'inf': 'oem20.inf',
             'hwids': ['PCI\\VEN_10DE&DEV_2D05&SUBSYS_88EE1043&REV_A1'], 'compat': ['PCI\\CC_030000', 'PCI\\CC_0300'],
             'parent': 'PCI\\VEN_1022&DEV_14DB&SUBSYS_14531022&REV_00\\3&2411E6FE&0&09'}

    def test_szellem_gpu_a_nincs_jelen_csoportba(self):
        from app import machinemap_core as mm
        quiet()
        slots, _g = mm.assign_node_slots([self.PORT, self.GHOST])
        self.assertEqual(slots[self.GHOST['id'].lower()][0], 'absent:gpu')

    def test_leallitott_szoftver_komponens_marad_a_szulonel(self):
        from app import machinemap_core as mm
        quiet()
        comp = {'id': 'SWD\\DRIVERENUM\\{ABC}#COMP&1', 'present': False, 'cls': 'SoftwareComponent',
                'desc': 'Szoftver-komponens', 'inf': 'oem7.inf', 'hwids': ['SWC\\VEN_1022'],
                'compat': [], 'parent': self.PORT['id']}
        slots, _g = mm.assign_node_slots([self.PORT, comp])
        self.assertEqual(slots[comp['id'].lower()][0], slots[self.PORT['id'].lower()][0])


class BootChainFromNodes(unittest.TestCase):
    """2026-10-12: a boot-lánc bejárása az eszközfából (a Get-PnpDeviceProperty-s bejárás
    54-133 mp volt). Terepi lánc: Lenovo 20KGS4032P, USB-s teszt-SSD."""

    def test_lenovo_usb_ssd(self):
        from app.wu_core import boot_chain_from_nodes, _collect_boot_path_protection
        ids = ['SCSI\\DISK&VEN_IB-AC703&PROD_-U3\\6&4419B6D&0&000000', 'USB\\VID_174C&PID_55AA\\X1',
               'USB\\ROOT_HUB30\\5&1', 'PCI\\VEN_8086&DEV_9D2F\\3&1', 'ACPI\\PNP0A08\\0',
               'ACPI_HAL\\PNP0C08\\0', 'ROOT\\ACPI_HAL\\0000', 'HTREE\\ROOT\\0']
        infs = ['disk.inf', 'uaspstor.inf', 'usbhub3.inf', 'usbxhci.inf', 'pci.inf', 'acpi.inf', 'hal.inf', '']
        nodes = [{'id': i, 'parent': (ids[k + 1] if k + 1 < len(ids) else ''), 'inf': f, 'cls': 'X',
                  'service': 's', 'desc': '@oem1.inf,%a%;Név' if k == 0 else 'n'}
                 for k, (i, f) in enumerate(zip(ids, infs))]
        nodes.append({'id': 'PCI\\VEN_1022&DEV_790B\\X', 'parent': ids[4], 'inf': 'oem2.inf'})  # nem a láncon
        chain = boot_chain_from_nodes([ids[0].lower()], nodes)   # a Storage-út kisbetűs id-t ad
        self.assertEqual([c['Inf'] for c in chain], infs[:7])   # a HTREE gyökér nem tag
        self.assertEqual(chain[0]['Name'], 'Név')
        self.assertEqual(boot_chain_from_nodes(['NINCS\\ILYEN'], nodes), [])
        quiet()
        run = lambda *a, **k: CommandResult(0, json.dumps({'DiskIds': ids[0]}), '')
        infs_got, _c, det = _collect_boot_path_protection(run, node_fn=lambda: nodes)
        self.assertTrue(det)
        self.assertEqual(infs_got, set())            # csupa Windows-INF: nincs védendő third-party (2026-11-04)
        run_empty = lambda *a, **k: CommandResult(0, json.dumps({'DiskIds': []}), '')
        self.assertFalse(_collect_boot_path_protection(run_empty, node_fn=lambda: nodes)[2])  # fail-safe


class NarrowedBootProtection(unittest.TestCase):
    """2026-11-04, explicit user decision: a fix csak RAID / Intel VMD (vagy ismeretlen)
    vezérlő mögött véd boot-drivert; szabványos NVMe/AHCI-n a Windows saját drivere viszi."""

    def _chain(self, ctrl_inf, ctrl_svc, ctrl_ids, extra=None):
        disk = 'SCSI\\DISK&VEN_NVME\\5&1&0&000000'
        ctrl = 'PCI\\VEN_X\\4&1'
        nodes = [{'id': disk, 'parent': ctrl, 'inf': 'disk.inf', 'service': 'disk', 'cls': 'DiskDrive',
                  'compat': ['GenDisk'], 'desc': 'Lemez'},
                 {'id': ctrl, 'parent': 'PCI\\ROOT\\0', 'inf': ctrl_inf, 'service': ctrl_svc, 'cls': 'SCSIAdapter',
                  'hwids': ctrl_ids[:1], 'compat': ctrl_ids[1:], 'desc': 'Vezérlő'},
                 {'id': 'PCI\\ROOT\\0', 'parent': 'ACPI\\PNP0A08\\0', 'inf': (extra or {}).get('inf', 'pci.inf'),
                  'service': 'pci', 'cls': 'System', 'compat': ['PCI\\CC_0604'], 'desc': 'PCIe-port'},
                 {'id': 'ACPI\\PNP0A08\\0', 'parent': 'HTREE\\ROOT\\0', 'inf': 'acpi.inf', 'service': 'ACPI', 'cls': 'System'}]
        return disk, nodes

    def _collect(self, ctrl_inf, ctrl_svc, ctrl_ids, services=lambda inf: [], extra=None):
        from app.wu_core import _collect_boot_path_protection
        quiet()
        disk, nodes = self._chain(ctrl_inf, ctrl_svc, ctrl_ids, extra)
        run = lambda *a, **k: CommandResult(0, json.dumps({'DiskIds': [disk]}), '')
        return _collect_boot_path_protection(run, node_fn=lambda: nodes, inf_services=services)

    def test_intel_vmd_raid_is_protected(self):
        infs, chain, det = self._collect('oem50.inf', 'iaStorVD',
                                         ['PCI\\VEN_8086&DEV_9A0B&SUBSYS_0A6C1028', 'PCI\\VEN_8086&DEV_9A0B&CC_010400', 'PCI\\CC_0104'],
                                         services=lambda inf: ['iastorvd'])
        self.assertTrue(det)
        self.assertEqual(infs, {'oem50.inf'})
        self.assertEqual(chain[0]['Mode'], 'raid')
        self.assertIn('NEM indul', chain[1]['Verdict']['why'])

    def test_asus_micron_nvme_is_deletable(self):
        # Terepi lánc (ASUS E1504FA, 2026-11-03 napló): "Micron NVMe Storage Controller [SCSIAdapter]
        # INF=oem116.inf szolgáltatás=mtinvme". Az osztálykódot a napló nem írta ki: a szabványos
        # NVMe-osztálykód (CC_010802) itt feltevés, élesben a fix futása dönti el.
        infs, chain, _det = self._collect('oem116.inf', 'mtinvme',
                                          ['PCI\\VEN_1344&DEV_5411', 'PCI\\VEN_1344&DEV_5411&CC_010802', 'PCI\\CC_010802'],
                                          services=lambda inf: ['mtinvme'])
        self.assertEqual(infs, set())
        self.assertEqual(chain[0]['Mode'], 'nvme')
        self.assertIn('stornvme', chain[1]['Verdict']['why'])

    def test_rst_on_ahci_controller_is_deletable(self):
        # Élőben mérve 2026-11-04, fejlesztői gép: Intel 100 Series SATA AHCI Controller, oem11.inf, iaStorA.
        infs, chain, _det = self._collect('oem11.inf', 'iaStorA',
                                          ['PCI\\VEN_8086&DEV_A102&SUBSYS_86941043', 'PCI\\VEN_8086&DEV_A102&CC_010601',
                                           'PCI\\VEN_8086&DEV_A102&CC_0106'], services=lambda inf: ['iastora'])
        self.assertEqual(infs, set())
        self.assertEqual(chain[0]['Mode'], 'ahci')

    def test_chipset_naming_inf_is_deletable_unless_unreadable(self):
        # Terepi lánc (2026-10-xx napló): "Mobile 6th/7th Generation Intel(R) Processor Family I/O PCI
        # Express Root Port #5 - 9D14 [System] INF=oem163.inf szolgáltatás=pci" - csak elnevező INF.
        args = ('stornvme.inf', 'stornvme', ['PCI\\VEN_144D&DEV_A808', 'PCI\\CC_010802'])
        self.assertEqual(self._collect(*args, services=lambda inf: [], extra={'inf': 'oem163.inf'})[0], set())
        self.assertEqual(self._collect(*args, services=lambda inf: None, extra={'inf': 'oem163.inf'})[0],
                         {'oem163.inf'})                         # olvashatatlan INF -> óvatos ág

    def test_unknown_controller_with_own_driver_is_protected(self):
        infs, chain, _det = self._collect('oem77.inf', 'lsi_sas3', ['PCI\\VEN_1000&DEV_0097', 'PCI\\CC_010700'],
                                          services=lambda inf: ['lsi_sas3'])
        self.assertEqual(infs, {'oem77.inf'})
        self.assertEqual(chain[0]['Mode'], 'unknown')

    def test_closing_report_marks_storage_switch_as_intended(self):
        from app.gui import autofix as af
        quiet()

        class H(af.GuiAutofixMixin):
            def __init__(self):
                self.events = []

            def emit(self, e, p=None):
                self.events.append((e, p))

            def _get_installed_driver_info(self):
                return {'PCI\\VEN_1344&DEV_5411\\4&1': {'inf': 'stornvme.inf', 'provider': 'Microsoft'}}

            def _query_pnp_devices(self):
                return [{'pnp_id': 'PCI\\VEN_1344&DEV_5411\\4&1', 'name': 'Micron NVMe Storage Controller'}]
        h = H()
        pre = {'PCI\\VEN_1344&DEV_5411\\4&1': {'inf': 'mtinvme.inf', 'provider': 'Micron', 'version': '2.1.19.0',
                                               'class': 'SCSIAdapter'}}
        with mock.patch.object(af, 'driverstore_package_inf', return_value=None), \
                mock.patch.object(af, '_is_inbox_driver', return_value=True), \
                mock.patch.object(af, '_filter_wu_scan_devices', side_effect=lambda x: x):
            h._fix_driver_regressions(pre)
        self.assertTrue(h._health_lost_vendor[0]['intended'])
        self.assertIn('szándékos', h._health_lost_vendor[0]['why'])

    def test_drivers_view_gets_the_same_verdict(self):
        """A Driverek nézet a fixével KÖZÖS döntést kapja (csak kiírja, nem tilt)."""
        from app.gui import drivers as gdrv
        from app import driverusage_core as duc
        quiet()
        chain = [{'Inf': 'disk.inf', 'Mode': 'raid'},
                 {'Inf': 'oem50.inf', 'Mode': 'raid', 'Verdict': {'protect': True, 'why': 'RAID', 'device': 'VMD'}}]

        class H(gdrv.GuiDriversMixin):
            target_os_path = None
            _run = staticmethod(lambda *a, **k: CommandResult(0, '', ''))
        with mock.patch('app.wu_core._collect_boot_path_protection', return_value=({'oem50.inf'}, chain, True)), \
                mock.patch.object(duc, 'collect_usage_context', return_value=None):
            out = H().get_driver_usage([{'published': 'oem50.inf'}])
        self.assertEqual(out['boot']['mode'], 'raid')
        self.assertTrue(out['boot']['packages']['oem50.inf']['protect'])
        self.assertIn('error', out)                          # a használat elbukott, a boot-adat ettől még megvan


if __name__ == '__main__':
    unittest.main()
