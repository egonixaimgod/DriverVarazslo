"""2026-11-24-i terepi napló (Dell G15 5530, Build 421, 1 kattintásos fix, a technikus
11:45-kor leállította). A bemenetek a naplóból szó szerint.

1. A WU minden körben ugyanazt az „Intel Corporation - Display - 31.0.101.5382”-t ajánlotta,
   és minden körben „SIKERES”-en újra felment (a katalógus már feltette, telepített:
   31.0.101.5382 [2024-03-27]) -> „települt valami” -> új láb, a lábplafonig.
2. Az SMBus (PCI\\VEN_8086&DEV_7A23) driverét egy másik tétel ugyanazzal a chipset-INF-fel
   már rátette („Driver package installed on device: …7A23…”); a saját tétele „Already
   exists” lett -> hamis „a Windows NEM rakta fel” + hamis no-bind bejegyzés.
3. A beépített kijelző (MONITOR\\AUO5C9C\\4&2b9e6141&0&UID0) a grafikus driver cseréje után
   új példánnyal jelent meg -> hamis „eltűnt a listából (kihúzva?)”."""

import concurrent.futures
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.request
from unittest import mock

from tests.catalog_stub import quiet
from tests.test_clock import _ChainCase
from app import wu_core
from app.gui import autofix as af
from app.gui.hwscan import GuiHwScanMixin
from app.common import CommandResult

IGPU = r'PCI\VEN_8086&DEV_468B&SUBSYS_0BF71028&REV_0C\3&11583659&0&10'
DISPLAY = 'Intel Corporation - Display - 31.0.101.5382'


class SameReleaseWuTest(unittest.TestCase):
    def _m(self, title=DISPLAY, err=0):
        return {'uid': '0d9456b3-713e-4bf2-b031-c4b942f9626c', 'title': title,
                'device': {'name': 'Intel(R) UHD Graphics', 'pnp_id': IGPU, 'err_code': err}}

    def _filt(self, m, inst):
        wu = {m['uid']: {'UpdateID': m['uid'], 'Title': m['title'], 'DriverVerDate': '2024-03-27'}}
        return wu_core._filter_wu_downgrades([m], wu, {IGPU.upper(): inst})

    INST = {'version': '31.0.101.5382', 'date': '2024-03-27', 'provider': 'Intel Corporation', 'inf': 'oem20.inf'}

    def test_azonos_kiadas_mar_telepitve(self):
        kept, skipped = self._filt(self._m(), self.INST)
        self.assertEqual(kept, [])
        self.assertEqual(skipped[0]['label'], 'Már telepítve')
        self.assertIn('31.0.101.5382', skipped[0]['reason'])

    def test_mas_verzio_es_hibas_eszkoz_marad(self):
        self.assertEqual(len(self._filt(self._m('Intel Corporation - Display - 32.0.101.6127'), self.INST)[0]), 1)
        self.assertEqual(len(self._filt(self._m(err=43), self.INST)[0]), 1)


class WuLoopGuardTest(_ChainCase):
    """A valódi _scan_and_install_wu_sync (gyors mód): a láncban már sikeresen felment
    UpdateID nem kerül újra a telepítési listára."""

    def test_sikeresen_felment_csomag_nem_megy_fel_ujra(self):
        c = self.chain(('ok', 0.0))
        c._autofix_use_catalog = False
        uid = '0d9456b3-713e-4bf2-b031-c4b942f9626c'
        c._autofix_remember_wu_ok(uid, set())
        self.assertEqual(c._autofix_stats_get('wu_ok_uids'), [uid])
        c._query_pnp_devices = lambda: []
        c._search_wu_api = lambda: [{'UpdateID': uid, 'Title': DISPLAY}]
        c._get_installed_driver_info = lambda: {}
        c._defer_wu_to_newer_catalog = lambda m, *a: (m, [])
        seen = []
        with mock.patch.object(af, '_match_wu_updates_to_devices',
                               side_effect=lambda res, devs, exclude_uids=None: (seen.append(set(exclude_uids or ())), [])[1]), \
                mock.patch.object(af.time, 'sleep'):
            self.assertEqual(c._scan_and_install_wu_sync(), 0)
        self.assertIn(uid, seen[0])
        self.assertIn('már sikeresen felment', c.text())


class _Resp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SMBUS = r'PCI\VEN_8086&DEV_7A23&SUBSYS_0BF71028&REV_11\3&11583659&0&FC'
LPC = r'PCI\VEN_8086&DEV_7A27&SUBSYS_0BF71028&REV_11\3&11583659&0&A2'
CHIPSET_INF = ('[Version]\nClass=System\n[Manufacturer]\n%M%=M,NTamd64\n[M.NTamd64]\n'
               '%D1%=I,PCI\\VEN_8086&DEV_7A27&SUBSYS_0BF71028\n%D2%=I,PCI\\VEN_8086&DEV_7A23&SUBSYS_0BF71028\n'
               '[Strings]\nM="Intel"\nD1="LPC"\nD2="SMBus"\n')
# A naplóból (11:07:45 és 11:08:11), rövidítve.
OUT_FIRST = ('Adding driver package:  RaptorLakePCH-SSystem.inf\nDriver package added successfully.\n'
             'Published Name:         oem3.inf\nSystem reboot is needed to complete install operations!\n'
             f'Driver package installed on device: {LPC}\nDriver package installed on device: {SMBUS}\n'
             'Total driver packages:  1\nAdded driver packages:  1\n')
OUT_SECOND = ('Adding driver package:  RaptorLakePCH-SSystem.inf\n'
              'Driver package added successfully. (Already exists in the system)\nPublished Name:         oem3.inf\n'
              f'Driver package is up-to-date on device: {SMBUS}\nTotal driver packages:  1\nAdded driver packages:  1\n')


class _ChipsetStub(GuiHwScanMixin):
    target_os_path = None

    def __init__(self):
        self.log, self.cmds, self.add_calls = [], [], 0
        self._cancel_flag = False

    def emit(self, ev, data=None):
        if isinstance(data, dict) and data.get('log'):
            self.log.append(data['log'])

    def _check_cancel(self):
        return False

    def _no_bind_load(self):
        return []

    def _no_bind_record(self, *a, **k):
        pass

    def _catalog_download_url(self, guid, ssl_ctx, name=''):
        return 'https://x/' + guid + '.cab'

    def _present_hwid_sets(self, drv):
        return [set(drv.get('all_hwids') or [])]

    def _cleanup_unused_staged_infs(self, *a, **k):
        pass

    def _get_installed_driver_info(self):
        return {}

    def _verify_generic_replacements(self, *a, **k):
        pass

    def _run_add_driver(self, cmd):
        self.add_calls += 1
        return CommandResult(3010 if self.add_calls == 1 else 0, OUT_FIRST if self.add_calls == 1 else OUT_SECOND, '')

    def _run(self, cmd, **kw):
        self.cmds.append(cmd)
        if cmd[0] == 'expand' and cmd[1].endswith(('.cab', '.CAB')):
            with open(os.path.join(cmd[3], 'RaptorLakePCH-SSystem.inf'), 'w', encoding='utf-8') as f:
                f.write(CHIPSET_INF)
        return CommandResult(0, '', '')


class SameRoundBindTest(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.dict(os.environ, {'SystemDrive': self.tmp})
        p.start()
        self.addCleanup(p.stop)

    def _item(self, name, pnp, guid, no_driver):
        hw = pnp.split('\\3&')[0]
        return {'name': name, 'cat': 'x', 'hwid': hw, 'pnp_id': pnp, 'url': f'https://x/{guid}.cab',
                'cat_guid': guid, 'wu_title': 'MS Katalógus: INTEL System Driver Update (10.1.46.5)',
                'wu_date': '1968-07-17', 'alt_candidates': [], 'no_driver': no_driver,
                'all_hwids': [hw, hw.rsplit('&REV_', 1)[0]], 'installed_inf': ''}

    def test_masik_tetel_mar_feltette_nem_hamis_no_bind(self):
        stub = _ChipsetStub()
        pool = [self._item('Alaplap erőforrásai', LPC, 'a', False),
                self._item('SM buszvezérlő', SMBUS, 'b', True)]
        real = concurrent.futures.ThreadPoolExecutor
        with mock.patch.object(urllib.request, 'urlopen', lambda req, context=None, timeout=None: _Resp(b'CAB')), \
                mock.patch.object(concurrent.futures, 'ThreadPoolExecutor', lambda max_workers=None: real(max_workers=1)):
            stub._install_catalog_sync(pool, task_id='t')
        text = '\n'.join(stub.log)
        self.assertIn('ugyanebben a körben egy másik tétel már feltette', text)
        self.assertNotIn('a Windows NEM rakta fel', text)
        self.assertFalse([c for c in stub.cmds if c[:2] == ['pnputil', '/delete-driver']])


class MonitorNewInstanceTest(unittest.TestCase):
    def test_uj_peldany_azonositoval_jelen_van(self):
        quiet()
        old = r'MONITOR\AUO5C9C\4&2B9E6141&0&UID0'
        new = r'MONITOR\AUO5C9C\5&1A2B3C4D&0&UID8388688'

        class S(GuiHwScanMixin):
            target_os_path = None
            log = []

            def emit(self, ev, data=None):
                if isinstance(data, dict) and data.get('log'):
                    self.log.append(data['log'])

            def _run(self, cmd, **k):
                return CommandResult(0, json.dumps([{'PNPDeviceID': new, 'ConfigManagerErrorCode': 0}]), '')

            def _get_installed_driver_info(self):
                return {new: {'version': '1.0.0.4', 'date': '2022-05-05', 'provider': 'Dell', 'inf': 'oem60.inf'}}

        s = S()
        with mock.patch('app.gui.hwscan.time.sleep'):
            rolled = s._verify_generic_replacements([({'name': 'Integrated Monitor', 'pnp_id': old}, ['oem60.inf'])])
        text = '\n'.join(s.log)
        self.assertEqual(rolled, 0)
        self.assertNotIn('nincs jelen', text)
        self.assertNotIn('kihúzva', text)
        self.assertIn('gyári driver működik', text)


if __name__ == '__main__':
    unittest.main()
