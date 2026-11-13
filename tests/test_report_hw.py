"""A riport hardver-listái (2026-11-13, Dell Latitude 5300, Build 414 terepi napló):
- a smartctl a RAID-módú (Intel RST) vezérlő mögötti NVMe-t nem olvasta (rc=2, üres JSON),
  a riportba "Ismeretlen Meghajtó ?" került -> a Windows lemezadataiból pótoljuk;
- két "USB Mobile Monitor Virtual Display" videókártyának látszott -> csak a valódi GPU;
- az akku 60002/60002 mWh = 100%: a technikus kézi batteryreporttal igazolta, hogy valós,
  NEM gyanúsítjuk; a riport a töltési ciklusokat is kiírja."""
import json
import unittest
from types import SimpleNamespace

from app import report_core as rc


def _res(stdout, rc_=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=rc_)


class GpuFilterTest(unittest.TestCase):
    def test_dell_virtual_displays_dropped(self):
        gpus = [
            {"Name": "USB Mobile Monitor Virtual Display", "AdapterRAM": 0,
             "PNPDeviceID": "ROOT\\DISPLAY\\0000", "ConfigManagerErrorCode": 0},
            {"Name": "USB Mobile Monitor Virtual Display", "AdapterRAM": 0,
             "PNPDeviceID": "SWD\\IDDCX\\1", "ConfigManagerErrorCode": 0},
            {"Name": "Intel(R) UHD Graphics 620", "AdapterRAM": 1073741824,
             "PNPDeviceID": "PCI\\VEN_8086&DEV_3EA0&SUBSYS_08E01028&REV_02\\3&11583659&0&10",
             "ConfigManagerErrorCode": 0},
            {"Name": "NVIDIA GeForce (régi, kiszerelt)", "PNPDeviceID": "PCI\\VEN_10DE&DEV_1C82\\4&1",
             "ConfigManagerErrorCode": 45},
        ]
        self.assertEqual([g["Name"] for g in rc.physical_gpus(gpus)], ["Intel(R) UHD Graphics 620"])

    def test_unknown_ids_keep_all(self):
        gpus = [{"Name": "X"}, {"Name": "Y", "PNPDeviceID": ""}]
        self.assertEqual(len(rc.physical_gpus(gpus)), 2)
        self.assertEqual(rc.physical_gpus([]), [])


class SmartFallbackTest(unittest.TestCase):
    SCAN = json.dumps({"devices": [{"name": "/dev/sda", "type": "sat"}, {"name": "/dev/sdb", "type": "ata"}]})
    EMPTY = json.dumps({"smartctl": {"exit_status": 2}, "device": {"name": "/dev/sdb", "type": "ata"}})
    WIN = [{"Number": 0, "Model": "IB-AC703 -U3", "Serial": "000000004BA8", "Media": "SSD", "Bus": "USB",
            "Size": 240057409536, "Health": "Healthy"},
           {"Number": 1, "Model": "NVMe CL1-3D512-Q11 NV", "Serial": "ABC_123", "Media": "SSD", "Bus": "RAID",
            "Size": 512110190592, "Health": "Healthy", "Wear": 3, "Temp": 38, "Hours": 1234}]

    def test_rst_nvme_shows_from_windows(self):
        def run(cmd, **kw):
            return _res(self.SCAN) if "--scan" in cmd else _res(self.EMPTY, 2)
        sysd = {"number": 0, "serial": "000000004BA8", "model": "IB-AC703 -U3", "bus": "USB"}
        excluded = []
        data = rc._collect_smart_data(run, "smartctl.exe", sysd, excluded)
        self.assertEqual(data, [])  # az olvashatatlan /dev/sdb nem lesz "Ismeretlen Meghajtó"
        rows = rc.windows_disk_rows(self.WIN, data, sysd, excluded)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["Name"], "NVMe CL1-3D512-Q11 NV")
        self.assertEqual(r["Type"], "NVMe SSD")
        self.assertEqual(r["Size"], "476.9 GB")
        self.assertEqual(r["Health"], "97%")
        self.assertEqual(r["Hours"], "51 nap 10 óra")
        self.assertEqual(r["Temp"], "38 °C")

    def test_no_duplicate_when_smartctl_read_it(self):
        smart = [{"Name": "Samsung SSD 970", "Serial": "S4EWNX0N", "Size": "465.8 GB"}]
        win = [{"Number": 1, "Model": "Samsung SSD 970 EVO", "Serial": "S4EW_NX0N.", "Bus": "NVMe", "Size": 1}]
        self.assertEqual(rc.windows_disk_rows(win, smart), [])

    def test_without_skip_usb_disk_not_from_windows_and_failed_query(self):
        self.assertEqual(rc.windows_disk_rows([self.WIN[0]], []), [])
        self.assertEqual(rc.windows_disk_rows(None, []), [])
        self.assertIsNone(rc.query_windows_disks(lambda *a, **k: _res("nem json")))
        self.assertEqual(len(rc.query_windows_disks(lambda *a, **k: _res(json.dumps(self.WIN[1])))), 1)

    def test_windows_health_without_wear(self):
        row = rc.windows_disk_rows([{"Number": 2, "Model": "WDC HDD", "Media": "HDD", "Bus": "SATA",
                                     "Size": 10**12, "Health": "Healthy"}], [])[0]
        self.assertEqual((row["Type"], row["Health"], row["Hours"], row["Temp"]),
                         ("SATA HDD", "Rendben (Windows)", "?", "?"))


class BatteryCyclesTest(unittest.TestCase):
    def test_dell_full_equals_design_is_100_no_warning(self):
        # a technikus kézi batteryreporttal ellenőrizte: 60k/60k valós -> nincs gyanúsítás
        bats = rc.parse_batteries('[{"Manufacturer":"BYD","Name":"DELL N2K6254","Design":"60002",'
                                  '"Full":"60002","Cycles":"0"}]')
        html = rc.battery_section_html(bats)
        self.assertIn("100%", html)
        self.assertNotIn("⚠️", html)
        self.assertIn("firmware nem számolja", html)

    def test_cycles_shown(self):
        html = rc.battery_section_html(rc.parse_batteries(
            '[{"Name":"A","Design":"50000","Full":"41000","Cycles":"312"}]'))
        self.assertIn("312", html)
        self.assertIn("nem jelenti", rc.battery_section_html(rc.parse_batteries(
            '[{"Name":"A","Design":"50000","Full":"41000"}]')))


class RamVendorJedecTest(unittest.TestCase):
    """Terep, 2026-11-13, Dell Latitude 5300 (Build 414): a napló szó szerint
    `gyártó=80AD000080AD (Manufacturer='80AD000080AD', PartNumber='HMAA1GS6CJR6N-XN')`.
    A 12 jegyű JEDEC-alak a régi `<= 8` hossz-korlát miatt nyers kódként gyártónév lett."""

    def test_dell_12_jegyu_jedec(self):
        self.assertEqual(rc.ram_vendor('80AD000080AD', 'HMAA1GS6CJR6N-XN'), 'SK Hynix')

    def test_ismeretlen_hosszu_hexa_a_cikkszamra_esik(self):
        self.assertEqual(rc.ram_vendor('859A000000000000', 'M471A1K43DB1-CWE'), 'Samsung')
        self.assertIsNone(rc.ram_vendor('859A00000000', 'XYZ123'))

    def test_valodi_nev_marad(self):
        self.assertEqual(rc.ram_vendor('SK Hynix', 'HMA82GS6DJR8N-VK'), 'SK Hynix')


if __name__ == '__main__':
    unittest.main()
