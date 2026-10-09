"""Bolti tábla - akciós ár (2026-10-08). Offline, a valódi sablonnal.

A Word-os mérés (2 oldal, a lábléc-sor y-pozíciója akciónál is 529,15 pt) nem futtatható
offline - azt a CLAUDE.md "Bolti tábla: akciós ár" szakasza írja le. Ez a teszt azt őrzi,
ami a docx XML-jéből eldönthető."""
import io
import os
import re
import unittest
import zipfile

from app import shoplabel_core as sc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, sc.TEMPLATE_FILENAME)
LAPTOP = {'cpu': 'Intel® Core™ i7-10610U', 'ram': '16GB DDR4', 'storage': '256GB SSD',
          'gpu': 'Intel® UHD Graphics', 'display': '14" 1920x1080 60Hz', 'keyboard': 'Magyar',
          'battery': '100%', 'condition': 'Szép állapot!', 'warranty': '6 hónap'}


def machine(kind='laptop', sale=False, price='140000', sale_price='119000', windows=False, fee='15000'):
    return {'kind': kind, 'name': 'Dell Latitude 7410', 'price': price, 'sale': sale,
            'sale_price': sale_price, 'windows': windows, 'windows_fee': fee, 'values': dict(LAPTOP)}


def doc_xml(machines):
    with open(TEMPLATE, 'rb') as f:
        data = sc.fill_template(f.read(), machines)
    return zipfile.ZipFile(io.BytesIO(data)).read('word/document.xml').decode('utf-8')


class SalePureTests(unittest.TestCase):
    def test_price_number(self):
        self.assertEqual(sc.price_number('140 000 Ft'), 140000)
        self.assertEqual(sc.price_number('140.000'), 140000)
        self.assertIsNone(sc.price_number('Érdeklődjön!'))
        self.assertIsNone(sc.price_number(''))

    def test_sale_percent(self):
        self.assertEqual(sc.sale_percent('140000', '119000'), 15)
        self.assertEqual(sc.sale_percent('1249990', '999990'), 20)
        self.assertIsNone(sc.sale_percent('100000', '100000'))     # nem olcsóbb
        self.assertIsNone(sc.sale_percent('100000', '120000'))
        self.assertIsNone(sc.sale_percent('Érdeklődjön', '1000'))  # nem szám

    def test_validate(self):
        self.assertEqual(sc.validate_machines([machine(sale=True), machine()]), [])
        miss = sc.validate_machines([machine(sale=True, sale_price=''), machine()])
        self.assertTrue(any('akciós ár' in m for m in miss), miss)
        miss = sc.validate_machines([machine(sale=True, sale_price='150000'), machine()])
        self.assertTrue(any('nem kisebb' in m for m in miss), miss)
        # nem akciós gépnél egy ott maradt akciós ár nem számít
        self.assertEqual(sc.validate_machines([machine(sale=False, sale_price='999999'), machine()]), [])

    def test_price_label(self):
        self.assertEqual(sc.price_label(machine()), '140 000 Ft')
        self.assertEqual(sc.price_label(machine(sale=True)), '140 000 Ft -> AKCIÓ 119 000 Ft (-15%)')


@unittest.skipUnless(os.path.isfile(TEMPLATE), 'nincs sablon')
class SaleTemplateTests(unittest.TestCase):
    def test_no_sale_unchanged(self):
        """Akció nélkül a kimenet bájtra ugyanaz, mint egy sale-mező nélküli gépé."""
        a = doc_xml([machine(), machine('desktop')])
        plain = [dict(machine(), sale=None), dict(machine('desktop'), sale=None)]
        for m in plain:
            m.pop('sale'); m.pop('sale_price')
        self.assertEqual(a, doc_xml(plain))
        self.assertNotIn('AKCIÓ', a)

    def test_sale_block_replaces_price_and_spacer(self):
        base = doc_xml([machine(), machine('desktop')])
        x = doc_xml([machine(sale=True), machine('desktop')])
        self.assertIn('AKCIÓ −15%', x)
        self.assertIn('119 000 Ft', x)
        self.assertEqual(x.count('<w:strike/>'), 1)
        # a régi ár CSAK áthúzva szerepel az 1. lapon, a 2. lap ára érintetlen
        self.assertEqual(x.count('140 000 Ft'), 2)
        # a kupon két bekezdése a régi két bekezdés (térköz + ár) helyére került:
        # a bekezdések száma nem változik
        n = lambda s: len(sc._P_RE.findall(s))
        self.assertEqual(n(x), n(base))
        self.assertEqual(x.count('w:lineRule="exact"'), base.count('w:lineRule="exact"') + 2)

    def test_both_sale(self):
        x = doc_xml([machine(sale=True, price='1249990', sale_price='999990'),
                     machine('desktop', sale=True)])
        self.assertIn('AKCIÓ −20%', x)
        self.assertIn('AKCIÓ −15%', x)
        self.assertEqual(x.count('<w:strike/>'), 2)
        self.assertIsNone(re.search('[\x00-\x08\x0b\x0c\x0e-\x1f]', x))

    def test_windows_plain(self):
        base = doc_xml([machine(), machine('desktop')])
        x = doc_xml([machine(windows=True), machine('desktop')])
        self.assertIn('Windows telepítéssel:', x)
        self.assertIn('155 000 Ft', x)            # 140 000 + 15 000
        self.assertEqual(x.count('140 000 Ft'), 2)  # a nagy ár marad
        n = lambda s: len(sc._P_RE.findall(s))
        self.assertEqual(n(x), n(base))           # a 2 eltűnő bekezdés helyén 2 új
        self.assertNotIn('<w:strike/>', x)

    def test_windows_on_sale_adds_to_sale_price(self):
        base = doc_xml([machine(), machine('desktop')])
        x = doc_xml([machine(sale=True, windows=True), machine('desktop')])
        self.assertIn('134 000 Ft', x)            # 119 000 (akciós) + 15 000
        self.assertIn('AKCIÓ −15%', x)
        n = lambda s: len(sc._P_RE.findall(s))
        self.assertEqual(n(x), n(base) + 1)       # a kupon 3 bekezdés a 2 helyett

    def test_sale_without_windows_unchanged(self):
        """A jóváhagyott akciós kupon (Windows nélkül) nem változhat a Windows-sor miatt."""
        a = doc_xml([machine(sale=True), machine('desktop')])
        plain = dict(machine(sale=True))
        plain.pop('windows'); plain.pop('windows_fee')
        self.assertEqual(a, doc_xml([plain, machine('desktop')]))
        self.assertNotIn('Windows telepítéssel', a)

    def test_windows_validation_and_text(self):
        self.assertEqual(sc.validate_machines([machine(windows=True), machine()]), [])
        miss = sc.validate_machines([machine(windows=True, fee=''), machine()])
        self.assertTrue(any('Windows' in m for m in miss), miss)
        self.assertEqual(sc.validate_machines([machine(windows=False, fee='abc'), machine()]), [])
        self.assertEqual(sc.windows_price_text(machine(price='Érdeklődjön!', windows=True)), '+ 15 000 Ft')
        self.assertEqual(sc.price_label(machine(windows=True)),
                         '140 000 Ft | Windows telepítéssel: 155 000 Ft (+15 000 Ft)')

    def test_non_numeric_sale(self):
        x = doc_xml([machine(sale=True, price='Érdeklődjön', sale_price='Most olcsóbb!'), machine()])
        self.assertIn('AKCIÓ!', x)          # százalék nélkül
        self.assertIn('Most olcsóbb!', x)


if __name__ == '__main__':
    unittest.main()
