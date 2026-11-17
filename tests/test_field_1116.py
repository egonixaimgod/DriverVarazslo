"""2026-11-16-i terepi naplók (Dell OptiPlex 5050 + ASUS VivoBook X421EA Build 417, MSI
MS-7B61 Build 415) javításai. A bemenetek a naplókból szó szerint."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.catalog_stub import quiet
from tests import test_catalog_install as tci     # modulként: a tesztosztályai ne fussanak itt is
from tests.test_clock import _ChainCase
from app.gui.rebind import GuiRebindMixin
from app.gui.logs import GuiLogsMixin
from app.gui.updater import GuiUpdaterMixin
from app.gui import updater as updater_mod
import urllib.request

HDA = r'PCI\VEN_8086&DEV_A2F0&SUBSYS_EB611462&REV_00'
HDA_PNP = HDA + r'\3&11583659&0&FB'
LOGI = r'USB\VID_046D&PID_C52B&REV_1203&MI_01'


class RebindExtensionTest(unittest.TestCase):
    """Kiegészítő (Extension) INF nem "gyári csomag": a kör nem rúghatja meg miatta az
    eszközt (MSI: HD Audio vezérlő <- hdbusext.inf, USB beviteli <- unifying_receiver.inf,
    eltávolítás + újraindítás, utána ugyanúgy hdaudbus.inf / input.inf)."""

    def _stub(self, ids):
        class S(GuiRebindMixin):
            def _rebind_pkg_ids(self, orig):
                return (r'C:\DS\%s' % orig, ids[orig])
        return S()

    def _pair(self, dev, orig, inf_id, cls):
        s = self._stub({orig: [inf_id]})
        return s._staged_vendor_inf_for(dev, [{'original': orig, 'class': cls}])

    def test_extension_inf_nem_jelolt(self):
        hda = {'name': 'High Definition Audio hangvezérlő',
               'all_hwids': [HDA, r'PCI\VEN_8086&DEV_A2F0&SUBSYS_EB611462', r'PCI\VEN_8086&DEV_A2F0&CC_040300']}
        logi = {'name': 'USB beviteli eszköz',
                'all_hwids': [LOGI, r'USB\VID_046D&PID_C52B&MI_01']}
        self.assertEqual(self._pair(hda, 'hdbusext.inf', r'PCI\VEN_8086&DEV_A2F0', 'Extension'), (None, ''))
        self.assertEqual(self._pair(logi, 'unifying_receiver.inf', r'USB\VID_046D&PID_C52B&MI_01', 'Extension'), (None, ''))
        # Kontroll: ugyanaz az azonosító egy funkció-driver csomagban továbbra is jelölt.
        self.assertEqual(self._pair(hda, 'x.inf', r'PCI\VEN_8086&DEV_A2F0', 'System')[1], 'x.inf')


HDA_DEV = r'PCI\VEN_8086&DEV_A2F0&SUBSYS_07A21028&REV_00'
SST_INF = ('[Version]\nClass=System\n[Manufacturer]\n%M%=M,NTamd64\n[M.NTamd64]\n'
           '%D%=I,PCI\\VEN_8086&DEV_9D71&SUBSYS_07A71028\n[Strings]\nM="Intel"\nD="SST"\n')


class NoSourceWordingTest(unittest.TestCase):
    """Ahol a Windows saját drivere a helyes (hdaudbus.inf), ott a "nincs való csomag"
    után nem jöhet "a gyártó oldaláról kell pótolni" teendő (Dell + MSI)."""

    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.dict(os.environ, {'SystemDrive': self.tmp})    # a temp mappa ide kerül
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(urllib.request, 'urlopen')
        self.urlopen = p.start()
        self.addCleanup(p.stop)

    def _run(self, installed_inf):
        stub = tci.InstallStub(self.tmp, {'https://x/w.cab': SST_INF, 'https://x/a1.cab': SST_INF},
                               {'a1': 'https://x/a1.cab'})

        def fake_urlopen(req, context=None, timeout=None):
            stub.cur_url = req.full_url
            return tci._Resp(b'CAB')
        self.urlopen.side_effect = fake_urlopen
        drv = {'name': 'High Definition Audio hangvezérlő', 'cat': 'x', 'hwid': HDA_DEV,
               'pnp_id': HDA_DEV + r'\3&11583659&0&FB', 'url': 'https://x/w.cab', 'cat_guid': 'w',
               'wu_title': 'MS Katalógus: Intel(R) Corporation - System - 6/19/2017 12:00:00 AM - 9.21.0.2500',
               'wu_date': '2017-06-19', 'inbox_now': True, 'installed_inf': installed_inf,
               'alt_candidates': [('a1', 'Intel(R) Corporation - System - 4/23/2017 12:00:00 AM - 9.21.0.2307', '2017-04-23')],
               'all_hwids': [HDA_DEV, r'PCI\VEN_8086&DEV_A2F0&SUBSYS_07A21028']}
        stub._install_catalog_sync([drv], task_id='t')
        return '\n'.join(stub.log)

    def test_hangbusz_vezerlo_nem_teendo(self):
        t = self._run('hdaudbus.inf')
        self.assertIn('Nincs hozzá való csomag', t)
        self.assertNotIn('saját driver-oldaláról kell pótolni', t)
        self.assertIn('a helyes drivere', t)

    def test_alapdriveres_hangchip_tovabbra_is_teendo(self):
        t = self._run('hdaudio.inf')
        self.assertIn('saját driver-oldaláról kell pótolni', t)


class NoBindReportTest(_ChainCase):
    def _report(self, inf):
        c = self.chain(('ok', 0.0))
        c._get_third_party_drivers = lambda *a, **k: []
        c._get_installed_driver_info = lambda: {HDA_PNP.upper(): {'inf': inf, 'provider': 'Microsoft',
                                                                  'version': '10.0.19041.6157'}}
        c._emit_catalog_no_bind([
            {'pnp': HDA_PNP.upper(), 'name': 'High Definition Audio hangvezérlő',
             'reason': 'a csomag INF-je nem alkalmazható erre az eszközre',
             'title': 'MS Katalógus: Intel(R) Corporation - System - 6/19/2017 12:00:00 AM - 9.21.0.2500'}])
        return c.text()

    def test_helyes_windows_drivernel_nincs_teendo(self):
        t = self._report('hdaudbus.inf')
        self.assertNotIn('TEENDŐ', t)
        self.assertNotIn('MÁS GÉPRE', t)

    def test_alapdriveres_hangchipnel_marad(self):
        self.assertIn('TEENDŐ', self._report('hdaudio.inf'))


class _LogApi(GuiLogsMixin):
    def __init__(self, net):
        self.net, self.events, self.checked, self.uploads = net, [], [], []
    def emit(self, ev, data=None): self.events.append((ev, data))
    def _safe_thread(self, name, fn): fn()
    def _wait_for_internet(self, timeout, task_id=None, reason=''): return self.net
    def _toollib_check_password(self, pw, key):
        self.checked.append(pw)
        return True, ''
    def upload_run_log(self, task, outcome):
        self.uploads.append(task)
        return True


class LogUploadWaitsForNetTest(unittest.TestCase):
    def test_halozat_nelkul_nem_nyers_hiba(self):
        quiet()
        api = _LogApi(net=False)
        api.upload_debug_log('1234')
        self.assertEqual((api.checked, api.uploads), ([], []))
        text = ' '.join(str((d or {}).get('log') or (d or {}).get('status')) for _, d in api.events)
        self.assertIn('Nincs internet', text)
        self.assertNotIn('getaddrinfo', text)

    def test_halozattal_feltolt(self):
        quiet()
        api = _LogApi(net=True)
        api.upload_debug_log('1234')
        self.assertEqual(api.uploads, ['logupload'])


class UpdateCheckWaitsForNetTest(unittest.TestCase):
    def test_elobb_a_halozatot_varja(self):
        calls = []

        class U(GuiUpdaterMixin):
            def _wait_for_internet(self, timeout, task_id=None, reason=''):
                calls.append(('wait', timeout))
                return True
        with mock.patch.object(updater_mod.update_core, 'check_for_updates',
                               side_effect=lambda: calls.append(('check',)) or {'has_update': False}):
            U().check_for_updates()
        self.assertEqual(calls[0][0], 'wait')
        self.assertGreaterEqual(calls[0][1], 90)          # a mért ~66 mp-es felállást kivárja
        self.assertEqual(calls[1], ('check',))


if __name__ == '__main__':
    unittest.main()
