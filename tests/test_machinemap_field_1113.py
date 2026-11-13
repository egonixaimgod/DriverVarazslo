"""2026-11-13: a Driverek nézet besorolásának javításai 25 terepi gépfutás naplójából
(Dell Latitude 5300/E5470/5490/7490, Lenovo 20KGS/20LES/20ARS, HP EliteDesk/ProDesk, ASUS
E1504FA, MSI MS-7D69, ASRock) és a fejlesztői gép élő eszközfájából. Minden eset bemenete
terepi/élő adat; ahol csak a név volt a naplóban, az azonosító a gép INF-jéből jön."""
import unittest

import tests  # noqa: F401
from app import driverusage_core as du, machinemap_core as mm

PC = mm.PC_CONTAINER_ID


def node(id_, cls='', desc='', inf='', present=True, parent='', container=PC, hwids=None, compat=None,
         busdesc=''):
    return {'id': id_, 'cls': cls, 'desc': desc, 'friendly': '', 'inf': inf, 'present': present,
            'parent': parent, 'container': container, 'hwids': hwids or [], 'compat': compat or [],
            'busdesc': busdesc}


class StaleBindingByHwid(unittest.TestCase):
    """Azonos osztályon belül az azonosító dönt (csak nem jelenlévő eszközre)."""
    # A fejlesztői gép élő adata: a nem jelenlévő "AMD PCI" (DEV_148A) az oem18.inf-re mutat,
    # amit ma az AMD Interface 2.0.0.29 visel - abban DEV_148A már nincs.
    AMD_IFACE = ['PCI\\VEN_1022&DEV_14AC', 'PCI\\VEN_1022&DEV_790B', 'ACPI\\AMDI0104', 'ACPI\\PCCINTAA']
    GHOST = ['PCI\\VEN_1022&DEV_148A&SUBSYS_148A1022&REV_00', 'PCI\\VEN_1022&DEV_148A&CC_1300']

    def test_nem_jelenlevo_idegen_azonositoval_elavult(self):
        self.assertTrue(du.stale_binding('System', 'System', self.GHOST, self.AMD_IFACE, present=False))

    def test_jelenlevo_eszkozre_nem_dont(self):
        self.assertFalse(du.stale_binding('System', 'System', self.GHOST, self.AMD_IFACE, present=True))

    def test_illeszkedo_azonosito_nem_elavult(self):
        ids = ['PCI\\VEN_1022&DEV_790B&SUBSYS_FFFF1849&REV_61', 'PCI\\VEN_1022&DEV_790B']
        self.assertFalse(du.stale_binding('System', 'System', ids, self.AMD_IFACE, present=False))

    def test_nemtudas_nem_dont(self):
        self.assertFalse(du.stale_binding('System', 'System', [], self.AMD_IFACE, present=False))
        self.assertFalse(du.stale_binding('System', 'System', self.GHOST, [], present=False))
        # fel nem oldott %token% az INF-ben (pl. a Sony sie_usb_device.inf) = nem tudjuk
        self.assertFalse(du.stale_binding('USB', 'USB', self.GHOST, ['USB\\%SIE_DEVICE_0_ID%'],
                                          present=False))

    def test_regi_osztaly_szabaly_valtozatlan(self):
        self.assertTrue(du.stale_binding('System', 'Printer'))

    def test_terkep_es_hasznalat_nem_koti_a_szellemet(self):
        # Terep (Dell 5300 teszt-SSD): az appleusbvhci.inf (USB) egy másik gép "Thunderbolt(TM)
        # Controller - 9A1B" szellemén ült, a térkép az USB-vezérlők közé tette.
        ghost = node('PCI\\VEN_8086&DEV_9A1B&SUBSYS_0A1B1028&REV_01\\3&11583659&0&68', 'USB',
                     'Thunderbolt(TM) Controller - 9A1B', 'oem90.inf', present=False,
                     hwids=['PCI\\VEN_8086&DEV_9A1B&SUBSYS_0A1B1028&REV_01', 'PCI\\VEN_8086&DEV_9A1B'],
                     compat=['PCI\\VEN_8086&CC_0C0340', 'PCI\\CC_0C03'])
        facts = {'oem90.inf': {'inf_class': 'USB', 'hwids': ['USB\\APPLE_VHCI'], 'hwid_count': 1,
                               'descriptions': ['Apple Mobile Device USB VHCI'], 'buses': ['USB'],
                               'services': [], 'sys_files': []}}
        pkgs = [{'published': 'oem90.inf', 'original': 'appleusbvhci.inf', 'class': 'USB'}]
        m = mm.build_machine_map([ghost], pkgs, facts, {})
        self.assertFalse(m['packages']['oem90.inf']['slot'].startswith('absent:usb'))
        raw = {'devices': du.devices_from_nodes([ghost]), 'services': [], 'filters': [], 'printers': [],
               'nodes': [ghost]}
        u = du.build_usage(raw, facts, ['oem90.inf'])['oem90.inf']
        self.assertNotIn('Thunderbolt', ' '.join(u.get('reasons') or []))


class NameKeywords(unittest.TestCase):
    def test_serial_io_host_controller_chipset(self):
        for d in ('Intel(R) Serial IO GPIO Host Controller - 3450',
                  'Mobile Intel(R) Processor Family I/O Serial IO I2C Host Controller - 3446'):
            f = {'inf_class': 'System', 'hwid_count': 3, 'buses': ['PCI'], 'descriptions': [d]}
            self.assertEqual(mm.package_purpose(f)[0], mm.F_CHIPSET, d)

    def test_valodi_usb_vezerlo_marad(self):
        for d in ('Intel(R) USB 3.1 eXtensible Host Controller - 1.10', 'Standard Enhanced PCI to USB Host Controller',
                  'USB2 Enhanced Host Controller'):
            f = {'inf_class': 'System', 'hwid_count': 3, 'buses': ['PCI'], 'descriptions': [d]}
            self.assertEqual(mm.package_purpose(f)[0], mm.F_USB, d)


class PurposeWithoutDevice(unittest.TestCase):
    def test_netservice_szoftveres(self):
        f = {'inf_class': 'NetService', 'hwid_count': 1, 'buses': ['FT_FSFILTER'],
             'descriptions': ['FortiClient NDIS 6.3 Packet Filter Driver']}
        self.assertEqual(mm.package_purpose(f)[0], mm.F_SOFTWARE)

    def test_csak_gyoker_azonosito_szoftveres(self):
        f = {'inf_class': 'DellInstrumentation', 'hwid_count': 1, 'buses': ['ROOT'],
             'hwids': ['ROOT\\DELLINSTRUMENTATION'], 'descriptions': ['DellInstrumentation Device']}
        self.assertEqual(mm.package_purpose(f)[0], mm.F_SOFTWARE)

    def test_hidclass_beviteli(self):
        f = {'inf_class': 'HIDClass', 'hwid_count': 2, 'buses': ['USB'], 'usb_vids': ['046D'],
             'hwids': ['USB\\VID_046D&PID_C52B'], 'descriptions': ['Logitech Unifying USB receiver']}
        self.assertEqual(mm.package_purpose(f)[0], mm.F_INPUT)

    def test_akku_szenzor_cpu_kartyat_kap(self):
        # Terep: "Egyéb – nodev:sensor" / "nodev:battery" (a NODEV_ORDER-ből hiányoztak).
        for cls, func in (('Sensor', mm.F_SENSOR), ('Battery', mm.F_BATTERY), ('Processor', mm.F_CPU)):
            facts = {'oem1.inf': {'inf_class': cls, 'hwid_count': 1, 'buses': ['ACPI'],
                                  'hwids': ['ACPI\\X0001'], 'descriptions': ['x']}}
            m = mm.build_machine_map([], [{'published': 'oem1.inf', 'class': cls}], facts, {})
            self.assertEqual(m['packages']['oem1.inf']['slot'], 'nodev:' + func)
            card = next(s for s in m['slots'] if s['key'] == 'nodev:' + func)
            self.assertEqual(card['title'], mm.FUNC_META[func][1])   # nem "Egyéb"


class IntelSstAudio(unittest.TestCase):
    """A terepi naplók valódi INTELAUDIO-azonosítói."""

    def _purpose(self, hwids, desc, cls='MEDIA'):
        text = '[Version]\nClass=%s\n[Manufacturer]\nX=M\n[M]\n%s\n' % (
            cls, '\n'.join('"%s"=Inst,%s' % (desc, h) for h in hwids))
        return mm.package_purpose(du.parse_inf_facts(text))[0]

    def test_kijelzo_hang_grafika(self):
        self.assertEqual(self._purpose(['INTELAUDIO\\FUNC_01&VEN_8086&DEV_280B'], 'Intel(R) Display Audio'),
                         mm.F_GPU)

    def test_realtek_sst_hang(self):
        self.assertEqual(self._purpose(['INTELAUDIO\\FUNC_01&VEN_10EC&DEV_0256'], 'Realtek Audio'), mm.F_AUDIO)

    def test_sst_mikrofon_hang(self):
        self.assertEqual(self._purpose(
            ['INTELAUDIO\\CTLR_DEV_9D71&LINKTYPE_02&DEVTYPE_01&VEN_8086&DEV_AE20'],
            'Intel(R) Smart Sound Technology (Intel(R) SST) Microphone Array'), mm.F_AUDIO)


class NodePlacement(unittest.TestCase):
    def _slot(self, nodes, nid):
        slots, _g = mm.assign_node_slots(nodes, 'laptop')
        return slots[nid.lower()][0]

    def test_mediatek_bt_nem_telefon(self):
        n = node('USB\\VID_0E8D&PID_0616\\000000000', 'Bluetooth', 'RZ616 Bluetooth(R) Adapter',
                 compat=['USB\\Class_E0&SubClass_01&Prot_01', 'USB\\Class_E0'])
        self.assertEqual(self._slot([n], n['id']), 'board:' + mm.F_BT)

    def test_synaptics_root_touchpad_beepitett(self):
        n = node('ROOT\\SYNHIDMINI\\0000', 'HIDClass', 'Synaptics HID-Compliant Touch pad Device',
                 'oem18.inf', hwids=['ROOT\\VID_06CB&PID_000F'])
        self.assertEqual(self._slot([n], n['id']), 'board:' + mm.F_INPUT)

    def test_virtualis_eszkoz_marad_szoftveres(self):
        for desc, cls in (('Nefarius Virtual Gamepad Emulation Bus', 'System'),
                          ('Fn and function keys', 'System'), ('WireGuard Tunnel', 'Net')):
            n = node('ROOT\\SYSTEM\\0002', cls, desc, 'oem84.inf')
            self.assertEqual(self._slot([n], n['id']), 'software', desc)

    def test_halozati_szkenner_nyomtato_kartya(self):
        n = node('ROOT\\IMAGE\\0000', 'Image', 'HP CLJM477 Scan Driver', 'oem40.inf')
        self.assertTrue(self._slot([n], n['id']).startswith('ext:scanner-'))
        facts = {'oem40.inf': {'inf_class': 'Image', 'hwids': ['ROOT\\HPCLJM477'], 'hwid_count': 1}}
        m = mm.build_machine_map([n], [{'published': 'oem40.inf', 'class': 'Image'}], facts, {})
        card = next(s for s in m['slots'] if s['key'] == m['packages']['oem40.inf']['slot'])
        self.assertEqual((card['section'], card['category']), ('external', mm.F_PRINTER))

    def test_nem_jelenlevo_komponens_a_szulo_csoportjaba(self):
        # Terep: "Intel(R) Graphics Control Panel" (SWC) egy kiszerelt Intel GPU alatt -> Egyéb volt.
        gpu = node('PCI\\VEN_8086&DEV_5917&SUBSYS_225C17AA&REV_07\\3&11583659&0&10', 'Display',
                   'Intel(R) UHD Graphics 620', present=False, compat=['PCI\\CC_030000', 'PCI\\CC_0300'])
        swc = node('SWD\\DRIVERENUM\\{a}#GfxCPL&1&2&0', 'SoftwareComponent', 'Intel(R) Graphics Control Panel',
                   'oem61.inf', present=False, parent=gpu['id'])
        self.assertEqual(self._slot([gpu, swc], swc['id']), 'absent:' + mm.F_GPU)

    def test_voice_clarity_a_hanghoz(self):
        # A fejlesztői gép élő eszközfája: SWD\DRIVERENUM ... #VocaEffectPack, szülője SWD\COMPUTER.
        comp = node('SWD\\COMPUTER\\MFG_X&FAM_X&PROD_X&SKU_X', 'Computer', 'X', parent='HTREE\\ROOT\\0')
        root = node('HTREE\\ROOT\\0')
        vc = node('SWD\\DRIVERENUM\\{96bedf2c}#VocaEffectPack&1&37fb5493&0', 'AudioProcessingObject',
                  'Voice Clarity', 'oem11.inf', parent=comp['id'])
        self.assertEqual(self._slot([root, comp, vc], vc['id']), 'board:' + mm.F_AUDIO)

    def test_extension_inf_a_jelenlevo_eszkoznel(self):
        # Terep (Dell 5300): az Intel grafika-kiegészítő (Extension) az iGPU helyett
        # "nincs hozzá eszköz" lett, miközben a használat-oszlop szerint az iGPU tölti be.
        gpu = node('PCI\\VEN_8086&DEV_3EA0&SUBSYS_08B71028&REV_02\\3&11583659&0&10', 'Display',
                   'Intel(R) UHD Graphics 620', 'oem60.inf',
                   hwids=['PCI\\VEN_8086&DEV_3EA0&SUBSYS_08B71028&REV_02', 'PCI\\VEN_8086&DEV_3EA0&SUBSYS_08B71028'],
                   compat=['PCI\\VEN_8086&CC_030000', 'PCI\\CC_030000', 'PCI\\CC_0300'])
        facts = {'oem60.inf': {'inf_class': 'Display', 'hwids': ['PCI\\VEN_8086&DEV_3EA0'], 'hwid_count': 1},
                 'oem81.inf': {'inf_class': 'Extension', 'hwids': ['PCI\\VEN_8086&DEV_3EA0&SUBSYS_08B71028'],
                               'hwid_count': 1, 'descriptions': ['Intel(R) HD Graphics 610']}}
        pkgs = [{'published': 'oem60.inf', 'class': 'Display'}, {'published': 'oem81.inf', 'class': 'Extension'}]
        m = mm.build_machine_map([gpu], pkgs, facts, {})
        self.assertEqual(m['packages']['oem81.inf']['slot'], m['packages']['oem60.inf']['slot'])
        self.assertEqual(m['packages']['oem81.inf']['what_kind'], 'extension')


class CardTitles(unittest.TestCase):
    def test_driver_nelkuli_eszkoz_neve(self):
        # Terep: a Bluetooth-kártya címe "USB\VID_8087&PID_0A2B\5&1683e25f&0&6" volt.
        self.assertEqual(mm.node_name(node('USB\\VID_8087&PID_0A2B\\5&1683e25f&0&6')),
                         'Ismeretlen eszköz (USB\\VID_8087&PID_0A2B)')
        self.assertEqual(mm.node_name(node('USB\\VID_8087&PID_0A2B\\5&1&0&6', busdesc='Intel Bluetooth')),
                         'Intel Bluetooth')

    def test_halozat_cime_a_fizikai_kartya(self):
        wfd = node('{5D624F94-8850-40C3-A3FA-A4FD2080BAF3}\\VWIFIMP_WFD\\5&1', 'Net',
                   'Microsoft Wi-Fi Direct Virtual Adapter #3')
        wifi = node('PCI\\VEN_8086&DEV_9DF0\\3&1&0&A3', 'Net', 'Intel(R) Wireless-AC 9560 160MHz')
        heads = mm._headline_nodes(mm.F_NET, [wfd, wifi])
        self.assertEqual([mm.node_name(h) for h in heads], ['Intel(R) Wireless-AC 9560 160MHz'])

    def test_rgb_vezerlo_sajat_kategoria(self):
        # Terep: "Beviteli eszköz | MYSTIC LIGHT" (MSI MS-7D69), "LED Controller" (ASRock A620).
        for nm in ('MYSTIC LIGHT', 'LED Controller', 'AURA LED Controller'):
            hid = node('USB\\VID_1462&PID_7D69\\A02021', 'HIDClass', nm, container='c2',
                       compat=['USB\\Class_03&SubClass_00&Prot_00', 'USB\\Class_03'])
            self.assertEqual(mm.container_category([hid]), mm.F_LIGHT, nm)
        kb = node('USB\\VID_1B1C&PID_1B13\\1', 'Keyboard', 'Corsair K70 RGB', container='c3',
                  compat=['USB\\Class_03&SubClass_01&Prot_01', 'USB\\Class_03'])
        self.assertEqual(mm.container_category([kb]), mm.F_INPUT)    # RGB-s billentyűzet beviteli marad

    def test_ps_kontroller_beviteli(self):
        hid = node('USB\\VID_054C&PID_0CE6&MI_03\\1', 'HIDClass', 'Wireless Controller', container='c4',
                   compat=['USB\\Class_03&SubClass_00&Prot_00', 'USB\\Class_03'])
        aud = node('USB\\VID_054C&PID_0CE6&MI_00\\1', 'MEDIA', 'Wireless Controller', container='c4',
                   compat=['USB\\Class_01&SubClass_01&Prot_00', 'USB\\Class_01'])
        self.assertEqual(mm.container_category([aud, hid]), mm.F_INPUT)
        # egy valódi headset (hang + HID gombok) hang marad
        hs = node('USB\\VID_1395&PID_0300&MI_03\\1', 'HIDClass', 'EPOS GSP 370', container='c5',
                  compat=['USB\\Class_03'])
        hsa = node('USB\\VID_1395&PID_0300&MI_00\\1', 'MEDIA', 'EPOS GSP 370', container='c5',
                   compat=['USB\\Class_01&SubClass_01&Prot_00', 'USB\\Class_01'])
        self.assertEqual(mm.container_category([hsa, hs]), mm.F_AUDIO)

    def test_monitor_neve_az_edid_nev(self):
        mon = node('DISPLAY\\AOC2702\\5&1', 'Monitor', 'Generic Monitor (AG276QZD2)', container='c1')
        aud = node('SWD\\MMDEVAPI\\{0.0.0.00000000}.{abcdef}', 'AudioEndpoint',
                   'AG276QZD2 (NVIDIA High Definition Audio)', container='c1')
        self.assertEqual(mm.container_name([mon, aud]), 'AG276QZD2')


if __name__ == '__main__':
    unittest.main()
