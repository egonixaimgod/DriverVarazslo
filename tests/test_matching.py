"""HWID-párosítók: a WU-párosító (`_hwid_matches`), a katalógus-szűrő
(`catalog_supports_device` / `_supported_id_covers`), az újrakötés szigorú párosítója
(`rebind._strict_hwid_match`) és a keresőkulcs-szűrő (`is_specific_hwid`).

A három párosító SZÁNDÉKOSAN különböző szigorúságú - az esetek a CLAUDE.md terepi
leleteiből valók, és azt őrzik, hogy egyik se csússzon át a másik szabályára."""
import unittest

import tests  # noqa: F401
from app.wu_core import _hwid_matches, catalog_supports_device, _supported_id_covers, is_specific_hwid
from app.gui.rebind import _strict_hwid_match


class WuMatcher(unittest.TestCase):
    def test_r9_200_subsys_kozbeekelve(self):
        # 2026-07: a WU `VEN&DEV&REV`, az eszköz `VEN&DEV&SUBSYS&REV` - tag-részhalmaz.
        self.assertTrue(_hwid_matches(r'pci\ven_1002&dev_6811&rev_00',
                                      r'PCI\VEN_1002&DEV_6811&SUBSYS_30001682&REV_00'))

    def test_egy_tagu_nem_illik_mindenre(self):
        self.assertFalse(_hwid_matches(r'PCI\VEN_8086', r'PCI\VEN_8086&DEV_A123&SUBSYS_1&REV_31'))

    def test_col_lanc_nem_reszhalmaz(self):
        # 2026-09-03: a touchpad-kollekció INF-je nem illik a billentyűzet-gyerekre.
        self.assertFalse(_hwid_matches(r'HID\VID_044E&PID_1212&COL02', r'HID\VID_044E&PID_1212&COL02&COL02'))


class CatalogSupports(unittest.TestCase):
    def test_amd_csupasz_ven_dev_tamogatott(self):
        # 2026-09-26: a gyártó a csupasz VEN&DEV-et deklarálja - ez kompatibilis azonosító.
        dev = [r'pci\ven_1022&dev_790b&subsys_ffff1849&rev_61', r'pci\ven_1022&dev_790b&subsys_ffff1849']
        self.assertTrue(catalog_supports_device(dev, ['pci\\ven_1022&dev_790b'])[0])

    def test_mas_gepgyarto_subsys_nem_tamogatott(self):
        # 2026-09-27: a kompatibilis csupasz azonosító ne tegye "támogatottá" egy más
        # SUBSYS-ű OEM-változatot (HP ALC221 vs Clevo/Acer).
        dev = [r'hdaudio\func_01&ven_10ec&dev_0221&subsys_103c8054&rev_1000',
               r'hdaudio\func_01&ven_10ec&dev_0221&subsys_103c8054', r'hdaudio\func_01&ven_10ec&dev_0221']
        self.assertFalse(catalog_supports_device(dev, [r'hdaudio\func_01&ven_10ec&dev_0221&subsys_15581234'])[0])

    def test_csak_osztalykod_kizart(self):
        # 2026-09-21: AlpsAlpine - csak az osztálykódon illeszkedik.
        dev = [r'pci\ven_8086&dev_a123&subsys_8054103c&rev_31', r'pci\ven_8086&dev_a123&cc_0c05']
        ok, cc = catalog_supports_device(dev, ['acpi\\len001c', 'pci\\ven_8086&dev_a123&cc_0c05'])
        self.assertFalse(ok)
        self.assertTrue(cc)

    def test_acpi_egytagu_pontos(self):
        self.assertTrue(_supported_id_covers(r'acpi\amdif030', r'ACPI\AMDIF030'))

    def test_interfesz_csomag_nem_a_szulore(self):
        self.assertFalse(catalog_supports_device([r'usb\vid_1532&pid_00b9&rev_0200'],
                                                 [r'usb\vid_1532&pid_00b9&mi_02'])[0])


class RebindMatcher(unittest.TestCase):
    CASES = [
        (r'USB\VID_1532&PID_00B9&MI_02', r'USB\VID_1532&PID_00B9', False),          # szülő kompat.
        (r'USB\VID_1532&PID_00B9&MI_02', r'USB\VID_1532&PID_00B9&REV_0200&MI_02', True),
        (r'USB\VID_1532&PID_00B9&MI_02', r'USB\VID_1532&PID_00B9&MI_00', False),    # más interfész
        (r'USB\VID_1532&PID_00B9', r'USB\VID_1532&PID_00B9&REV_0200', True),
        (r'ACPI\LEN009B', r'ACPI\LEN009B', True),                                   # T580 tapipad
        (r'PCI\VEN_1002&DEV_6811&REV_00', r'PCI\VEN_1002&DEV_6811&SUBSYS_30001682&REV_00', True),
        (r'HID\VID_044E&PID_1212&COL02', r'HID\VID_044E&PID_1212&COL02&COL04', False),
        (r'HID\VID_044E&PID_1212&MI_00&COL01', r'HID\VID_044E&PID_1212&MI_00&COL01', True),
    ]

    def test_esetek(self):
        for inf, dev, exp in self.CASES:
            self.assertEqual(_strict_hwid_match(inf, dev), exp, (inf, dev))


class SpecificKeys(unittest.TestCase):
    def test_specifikus(self):
        for h in [r'ACPI\VEN_LEN&DEV_009B', r'ACPI\AMDIF030', r'PCI\VEN_8086&DEV_A123',
                  r'USB\VID_046D&PID_C52B', r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0892']:
            self.assertTrue(is_specific_hwid(h), h)

    def test_tipuskod(self):
        for h in [r'ACPI\PNP0501', r'USB\ROOT_HUB30', '*PNP0F13', r'ACPI\VEN_PNP&DEV_0100', r'ACPI\MSFT0101']:
            self.assertFalse(is_specific_hwid(h), h)


if __name__ == '__main__':
    unittest.main()
