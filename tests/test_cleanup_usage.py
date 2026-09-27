"""Záró takarítás (kiváltott csomag), használat-besorolás (Extension INF) és a záró
jelentés szövegei - a 2026-09-27-i Build 339-es terepi napló esetei."""
import unittest

import tests  # noqa: F401
from app import dupdrivers_core, driverusage_core as du
from app.wu_core import _hwid_matches, release_rank
from app.gui.autofix import GuiAutofixMixin


def _drv(pub, orig, prov, cls, ver, date):
    return {'published': pub, 'original': orig, 'provider': prov, 'class': cls, 'version': ver, 'date': date}


class Superseded(unittest.TestCase):
    DRV = [
        _drv('oem3.inf', 'old.inf', 'ACME Inc', 'System', '1.0.0.1', '2017-01-01'),
        _drv('oem9.inf', 'new.inf', 'ACME, Inc.', 'System', '2.0.0.1', '2026-01-01'),
        _drv('oem4.inf', 'ext.inf', 'ACME', 'Extension', '1.0.0.1', '2017-01-01'),
        _drv('oem5.inf', 'other.inf', 'Foo', 'System', '1.0.0.1', '2017-01-01'),
        _drv('oem6.inf', 'lap.inf', 'ACME', 'System', '1.0.0.1', '2017-01-01'),
        _drv('oem7.inf', 'cc.inf', 'ACME', 'System', '3.0.0.1', '2018-01-01'),
    ]
    NODES = [{'present': True, 'id': 'PCI\\VEN_1111&DEV_2222\\X', 'desc': 'Dev', 'inf': 'oem9.inf',
              'hwids': ['PCI\\VEN_1111&DEV_2222&SUBSYS_1'], 'compat': ['PCI\\VEN_1111&DEV_2222', 'PCI\\VEN_1111&CC_0C05']}]
    HW = {'oem3.inf': ['PCI\\VEN_1111&DEV_2222'], 'oem4.inf': ['PCI\\VEN_1111&DEV_2222'],
          'oem5.inf': ['PCI\\VEN_1111&DEV_2222'], 'oem6.inf': ['ACPI\\NVDA0820'],
          'oem7.inf': ['PCI\\VEN_1111&CC_0C05']}

    def _usage(self, **over):
        u = {d['published']: {'state': 'unused'} for d in self.DRV}
        u['oem9.inf'] = {'state': 'active'}
        u.update(over)
        return u

    def find(self, drv=None, usage=None, nodes=None):
        return dupdrivers_core.find_superseded_packages(drv or self.DRV, usage or self._usage(),
                                                        nodes or self.NODES, self.HW, _hwid_matches, release_rank)

    def test_csak_a_bizonyitottan_kivaltott(self):
        # smbusamd.inf-eset: az Extension, a más gyártójú, a hardver nélküli (NVIDIA
        # laptopos kísérő) és a csak osztálykódos csomag MARAD.
        self.assertEqual([x['published'] for x in self.find()], ['oem3.inf'])

    def test_nemtudasra_nem_torol(self):
        self.assertEqual(self.find(usage=self._usage(**{'oem3.inf': {'state': 'unknown'}})), [])

    def test_regebbin_futo_eszkoznel_marad(self):
        drv = [dict(x) for x in self.DRV]
        drv[1]['date'] = '2010-01-01'
        self.assertEqual(self.find(drv=drv), [])

    def test_inbox_driveren_futo_eszkoznel_marad(self):
        self.assertEqual(self.find(nodes=[dict(self.NODES[0], inf='hdaudio.inf')]), [])


EXT_INF = r"""
[Version]
Class=Extension
Provider=%P%
[Manufacturer]
%P%=M,NTamd64
[M.NTamd64]
%D%=Inst,HDAUDIO\FUNC_01&VEN_10EC&DEV_0892&SUBSYS_18496893
[Strings]
P="Realtek"
D="Realtek HD Audio OEM ext"
"""


class ExtensionUsage(unittest.TestCase):
    NODE = {'id': 'HDAUDIO\\X\\1', 'present': True, 'friendly': 'Realtek High Definition Audio', 'inf': 'oem21.inf',
            'hwids': ['HDAUDIO\\FUNC_01&VEN_10EC&DEV_0892&SUBSYS_18496893&REV_1003'], 'compat': []}

    def _state(self, nodes):
        facts = {'oem50.inf': du.parse_inf_facts(EXT_INF)}
        raw = {'devices': [], 'services': [], 'filters': [], 'printers': [], 'nodes': nodes}
        return du.build_usage(raw, facts, ['oem50.inf'])['oem50.inf']['state']

    def test_jelenlevo_eszkozon_hasznalatban(self):
        self.assertEqual(self._state([self.NODE]), 'active')

    def test_kihuzott_eszkoznel_keszenletben(self):
        self.assertEqual(self._state([dict(self.NODE, present=False)]), 'standby')

    def test_nem_illo_eszkoz_nem_hasznalt(self):
        self.assertEqual(self._state([dict(self.NODE, hwids=['HDAUDIO\\FUNC_01&VEN_10EC&DEV_0897&SUBSYS_1'])]), 'unused')

    def test_powershell_tartalekon_nema(self):
        self.assertEqual(self._state(None), 'unused')


class ClosingReportText(unittest.TestCase):
    def test_monitor_teendoje_nem_alaplap(self):
        t = GuiAutofixMixin._health_remedy_text(['Monitor'])
        self.assertIn('monitor gyártójának', t)
        self.assertNotIn('alaplap', t.lower())

    def test_hang_teendoje_alaplap(self):
        self.assertIn('alaplap', GuiAutofixMixin._health_remedy_text(['MEDIA']).lower())


if __name__ == '__main__':
    unittest.main()
