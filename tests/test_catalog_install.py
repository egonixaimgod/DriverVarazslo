"""A katalógus-TELEPÍTŐ (`_install_catalog_sync`) - hálózat, expand és pnputil nélkül.

A letöltés, a kicsomagolás és a pnputil csonkolva; az INF-vizsgálat VALÓDI (ideiglenes
mappában létrehozott INF-fájlokon), mert pont az dönti el, melyik jelölt való ide."""
import io
import os
import shutil
import tempfile
import unittest
import urllib.request

from tests.catalog_stub import quiet
from app.gui.hwscan import GuiHwScanMixin
from app.common import CommandResult

DEV_ID = r'PCI\VEN_10EC&DEV_8168&SUBSYS_81681849&REV_15'
GOOD_INF = '[Version]\nClass=Net\n[Manufacturer]\n%M%=M,NTamd64\n[M.NTamd64]\n%D%=I,PCI\\VEN_10EC&DEV_8168&SUBSYS_81681849\n[Strings]\nM="R"\nD="NIC"\n'
BAD_INF = GOOD_INF.replace('SUBSYS_81681849', 'SUBSYS_11111111')


class _Resp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class InstallStub(GuiHwScanMixin):
    target_os_path = None

    def __init__(self, tmp, inf_by_url, urls):
        self.tmp, self.inf_by_url, self.urls = tmp, inf_by_url, urls
        self.log = []
        self.cur_url = None
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
        return self.urls.get(guid)

    def _present_hwid_sets(self, drv):
        return [set(drv.get('all_hwids') or [])]

    def _cleanup_unused_staged_infs(self, *a, **k):
        pass

    def _get_installed_driver_info(self):
        return {}

    def _verify_generic_replacements(self, *a, **k):
        pass

    def _run_add_driver(self, cmd):
        return CommandResult(0, 'Driver package added successfully.\nPublished Name: oem50.inf\n'
                                'Driver package installed on matching devices.\nAdded driver packages: 1', '')

    def _run(self, cmd, **kw):
        if cmd[0] == 'expand' and cmd[1].endswith(('.cab', '.CAB')):
            dest = cmd[3]
            with open(os.path.join(dest, 'x.inf'), 'w', encoding='utf-8') as f:
                f.write(self.inf_by_url[self.cur_url])
            return CommandResult(0, '', '')
        return CommandResult(0, '', '')


class CatalogInstall(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self._orig_sysdrive = os.environ.get('SystemDrive')
        os.environ['SystemDrive'] = self.tmp        # a temp mappa ide kerül
        self._orig_urlopen = urllib.request.urlopen

    def tearDown(self):
        urllib.request.urlopen = self._orig_urlopen
        if self._orig_sysdrive is None:
            os.environ.pop('SystemDrive', None)
        else:
            os.environ['SystemDrive'] = self._orig_sysdrive
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pool(self, winner_url, alts, inf_by_url, urls):
        stub = InstallStub(self.tmp, inf_by_url, urls)

        def fake_urlopen(req, context=None, timeout=None):
            stub.cur_url = req.full_url
            return _Resp(b'CAB')
        urllib.request.urlopen = fake_urlopen
        drv = {'name': 'Realtek NIC', 'cat': 'x', 'hwid': DEV_ID, 'pnp_id': DEV_ID + '\\3&1',
               'url': winner_url, 'cat_guid': 'w', 'wu_title': 'MS Katalógus: Realtek - Net - 1',
               'wu_date': '2026-01-01', 'alt_candidates': alts, 'inbox_now': True,
               'all_hwids': [DEV_ID, r'PCI\VEN_10EC&DEV_8168&SUBSYS_81681849'], 'installed_inf': 'rt.inf'}
        res = stub._install_catalog_sync([drv], task_id='t')
        return res, stub

    def test_korlat_miatt_kimaradt_jelolt_nem_nincs_csomag(self):
        res, stub = self.run_pool(
            'https://x/win.cab',
            [('a1', 'Realtek - Net - 2', '2025-01-01'), ('a2', 'Realtek - Net - 3', '2024-01-01')],
            {'https://x/win.cab': BAD_INF}, {'a1': 'https://x/setup.exe', 'a2': None})
        text = '\n'.join(stub.log)
        self.assertIn('nem minden katalógus-jelöltet tudtunk kipróbálni', text)
        self.assertNotIn('Nincs hozzá való csomag', text)
        self.assertEqual(res[0], 0)

    def test_minden_jelolt_bizonyitottan_rossz_nincs_csomag(self):
        res, stub = self.run_pool(
            'https://x/win.cab', [('a1', 'Realtek - Net - 2', '2025-01-01')],
            {'https://x/win.cab': BAD_INF, 'https://x/a1.cab': BAD_INF}, {'a1': 'https://x/a1.cab'})
        self.assertIn('Nincs hozzá való csomag', '\n'.join(stub.log))

    def test_tartalek_illik_telepul(self):
        res, stub = self.run_pool(
            'https://x/win.cab', [('a1', 'Realtek - Net - 2', '2025-01-01')],
            {'https://x/win.cab': BAD_INF, 'https://x/a1.cab': GOOD_INF}, {'a1': 'https://x/a1.cab'})
        self.assertEqual(res[0], 1, '\n'.join(stub.log))


if __name__ == '__main__':
    unittest.main()
