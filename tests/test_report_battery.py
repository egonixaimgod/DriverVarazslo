"""A riport akku-szekciója: MINDEN akku megjelenik (2026-10-21, ThinkPad T440s: belső +
cserélhető akku, a riport csak az elsőt mutatta)."""
import unittest

from app.report_core import parse_batteries, battery_section_html


class BatteryReportTest(unittest.TestCase):
    def test_two_batteries_both_rendered(self):
        out = ('[{"Name":"45N1111","Manufacturer":"SANYO","Design":"23200","Full":"18000"},'
               '{"Name":"45N1127","Manufacturer":"LGC","Design":"23480","Full":"9000"}]')
        bats = parse_batteries(out)
        self.assertEqual(len(bats), 2)
        html = battery_section_html(bats)
        for s in ('(2 db)', '1. akku', '2. akku', 'SANYO 45N1111', 'LGC 45N1127',
                  '23200 mWh', '9000 mWh', '78%', '38%'):
            self.assertIn(s, html)

    def test_single_dict_and_empty(self):
        bats = parse_batteries('{"Name":"X","Manufacturer":"","Design":50000,"Full":45000}')
        self.assertEqual(len(bats), 1)
        html = battery_section_html(bats)
        self.assertIn('90%', html)
        self.assertNotIn('akku</b>', html)
        self.assertEqual(parse_batteries('[]'), [])
        self.assertEqual(parse_batteries(''), [])
        self.assertEqual(battery_section_html([]), '')

    def test_unreadable_capacity_is_not_zero(self):
        bats = parse_batteries('[{"Name":"A","Design":"","Full":"1000"}]')
        self.assertIsNone(bats[0]['design'])
        self.assertIn('nem olvasható', battery_section_html(bats))

    def test_garbage_output(self):
        self.assertEqual(parse_batteries('nem json'), [])


if __name__ == '__main__':
    unittest.main()
