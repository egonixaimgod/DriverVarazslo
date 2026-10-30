"""2026-10-30, élő ellenőrzés a fejlesztői gépen (ASRock B450M, 23 third-party csomag):
a "Készenlétben" kettébontása ("Nincs bedugva" / "Nem fut") és a gép-térkép három rossz
besorolása (Xbox-kiegészítő telefonként, pendrive telefonként, USB-s BT-adapter hálózatiként).
A bemenetek a gép valódi eszközfájából és INF-jeiből vannak."""
import logging
import unittest

import tests  # noqa: F401
from app import driverusage_core as du, machinemap_core as mm

SVC_INF = r"""
[Version]
Class=%s
Provider=ACME
[Manufacturer]
ACME=M,NTamd64
[M.NTamd64]
%s
[Inst.Services]
AddService=acmesvc,0x2,Svc
[Svc]
ServiceBinary=%%12%%\acmesvc.sys
"""


def _usage(cls, models, svc_state='Stopped', filters=()):
    facts = {'oem50.inf': du.parse_inf_facts(SVC_INF % (cls, models))}
    raw = {'devices': [], 'filters': list(filters), 'printers': [], 'nodes': [],
           'services': [{'name': 'acmesvc', 'state': svc_state, 'file': 'acmesvc.sys'}]}
    return du.build_usage(raw, facts, ['oem50.inf'])['oem50.inf']


class StandbyVsIdle(unittest.TestCase):
    def test_hardveres_driver_leallitott_szolgaltatassal_nincs_bedugva(self):
        # A Sony-kontroller csomagja (sie_usb_device.inf): a WINUSB csak bedugáskor indul.
        u = _usage('USBDevice', 'Dev=Inst,USB\\VID_054C&PID_0CE6')
        self.assertEqual(u['state'], du.USAGE_STANDBY)
        self.assertEqual(u['summary'], 'nincs bedugva (szolgáltatás: acmesvc)')

    def test_szoftveres_driver_leallitott_szolgaltatassal_nem_fut(self):
        u = _usage('System', '')
        self.assertEqual(u['state'], du.USAGE_IDLE)
        self.assertEqual(u['summary'], 'leállítva: acmesvc')

    def test_futo_szolgaltatas_hasznalatban(self):
        self.assertEqual(_usage('System', '', svc_state='Running')['state'], du.USAGE_ACTIVE)

    def test_feliratok_es_szamlalo(self):
        self.assertEqual(du.USAGE_LABELS[du.USAGE_STANDBY], 'Nincs bedugva')
        self.assertEqual(du.USAGE_LABELS[du.USAGE_IDLE], 'Nem fut')
        c = du.summarize_counts({'a': {'state': 'idle'}, 'b': {'state': 'standby'}})
        self.assertEqual((c['idle'], c['standby']), (1, 1))


# Az Xbox kontroller-kiegészítő (oem15.inf) gyártókódjai, rövidítve: 78 gyártóból 3
# telefon-gyártó (Apple, Google Stadia, Xiaomi).
XBOX_FACTS = {'inf_class': 'Extension', 'hwid_count': 265, 'buses': ['USB', 'VHF'],
              'descriptions': ['XBOX Synthetic Device Extension', 'XBOX Gamepad Device Extension'],
              'usb_vids': ['045E', '0B05', '1949', '18D1', '054C', '28DE', '2DC8', '2002', '3820',
                           '05AC', '2717', '0E6F', '24C6', '0F0D']}


class PackagePurpose(unittest.TestCase):
    def test_xbox_kiegeszito_nem_telefon(self):
        self.assertEqual(mm.package_purpose(XBOX_FACTS)[0], mm.F_INPUT)

    def test_igazi_telefon_driver_marad_telefon(self):
        f = {'inf_class': 'USB', 'hwid_count': 12, 'buses': ['USB'], 'usb_vids': ['04E8'],
             'descriptions': ['SAMSUNG Mobile USB Composite Device']}
        self.assertEqual(mm.package_purpose(f)[0], mm.F_PHONE)
        f = dict(f, usb_vids=['18D1', '0BB4', '22B8'], descriptions=['Android Bootloader Interface'])
        self.assertEqual(mm.package_purpose(f)[0], mm.F_PHONE)

    def test_gpio_controller_nem_beviteli(self):
        # a "controller" szó önmagában nem beviteli eszköz
        f = {'inf_class': 'System', 'hwid_count': 1, 'buses': ['ACPI'], 'descriptions': ['AMD GPIO Controller']}
        self.assertEqual(mm.package_purpose(f)[0], mm.F_CHIPSET)


# A fejlesztői gép valódi csomópontjai (container-azonosító rövidítve).
PENDRIVE = [
    {'id': 'SWD\\WPDBUSENUM\\{EF97F1D7-D22E-11F1-9F4F-7085C2CE64CF}#0000000000100000', 'present': True,
     'cls': 'WPD', 'friendly': 'SERGEI', 'container': 'c1'},
    {'id': 'USB\\VID_346D&PID_5678\\FC0516D826288', 'present': True, 'cls': 'USB',
     'desc': 'USB Mass Storage Device', 'compat': ['USB\\Class_08&SubClass_06&Prot_50'], 'container': 'c1'},
    {'id': 'USBSTOR\\Disk&Ven_VendorC&Prod_ProductCode&Rev_3.20\\FC0516D826288&0', 'present': True,
     'cls': 'DiskDrive', 'desc': 'VendorC ProductCode USB Device', 'container': 'c1'},
]
BT_ADAPTER = [
    {'id': 'USB\\VID_0B05&PID_190E\\00E04C239987', 'present': True, 'cls': 'Bluetooth',
     'desc': 'ASUS USB-BT500', 'compat': ['USB\\Class_E0&SubClass_01&Prot_01'], 'container': 'c2'},
    {'id': 'BTH\\MS_BTHPAN\\7&DD6F3D2&2&2', 'present': False, 'cls': 'Net',
     'desc': 'Bluetooth Device (Personal Area Network)', 'container': 'c2'},
    {'id': 'BTH\\MS_RFCOMM\\7&DD6F3D2&2&0', 'present': False, 'cls': 'Bluetooth',
     'desc': 'Bluetooth Device (RFCOMM Protocol TDI)', 'container': 'c2'},
]


class ContainerCategory(unittest.TestCase):
    def test_pendrive_kulso_tarolo(self):
        self.assertEqual(mm.container_category(PENDRIVE), mm.F_STORAGE)

    def test_usb_bt_adapter_bluetooth(self):
        self.assertEqual(mm.container_category(BT_ADAPTER), mm.F_BT)

    def test_mtp_telefon_marad_telefon(self):
        phone = [{'id': 'USB\\VID_04E8&PID_6860&MI_00\\6&1', 'present': True, 'cls': 'WPD',
                  'friendly': 'Galaxy S21', 'container': 'c3'}]
        self.assertEqual(mm.container_category(phone), mm.F_PHONE)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    unittest.main()
