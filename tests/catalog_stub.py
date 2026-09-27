"""Közös csonk a katalógus-döntés hálózat nélküli teszteléséhez.

`CatalogStub(rows=..., supported=..., models=..., no_url=...)` egy `GuiHwScanMixin`,
amiben minden hálózati hívás a megadott adatokból válaszol:
  rows:      {hwid_kisbetűvel: [(guid, cím, sor_szöveg, 'YYYY-MM-DD'), ...] | 'ERR'}
             ('__base__' kulcs: minden más kulcsra ez jön)
  supported: {guid: [támogatott azonosítók] | None}   (részletlap driverhwIDs)
  models:    {guid: 'driver model szöveg'}
  no_url:    {guid, ...} - ezekre a DownloadDialog nem ad linket
"""
import logging
import random

import tests  # noqa: F401  (sys.path)
from app.gui.hwscan import GuiHwScanMixin, _device_stem


def row(guid, title, date, os_text='windows 10 and later drivers', size='12.9 MB'):
    """Egy katalógus-sor a `_catalog_fetch_rows` alakjában."""
    y, m, d = date.split('-')
    return (guid, title, f"{title.lower()} {os_text} drivers (other hardware) "
                         f"{int(m)}/{int(d)}/{y} {size}", date)


class CatalogStub(GuiHwScanMixin):
    target_os_path = None

    def __init__(self, rows=None, supported=None, models=None, no_url=()):
        self.rows = rows or {}
        self.supported = supported or {}
        self.models = models or {}
        self.no_url = set(no_url)
        self._catalog_query_failed = []

    def emit(self, *a, **k):
        pass

    def _catalog_fetch_rows(self, hwid, ssl_ctx, deep=False, **kw):
        v = self.rows.get((hwid or '').lower(), self.rows.get('__base__', []))
        if v == 'ERR':
            raise IOError('csonk: lekérdezési hiba')
        return list(v)

    def _catalog_supported_hwids(self, guid, ssl_ctx):
        return self.supported.get(guid)

    def _catalog_driver_models(self, guid, ssl_ctx):
        return self.models.get(guid, '')

    def _catalog_download_url(self, guid, ssl_ctx, name=''):
        return None if guid in self.no_url else 'https://stub/' + guid

    def find(self, item, installed=None, no_bind=None):
        return self._catalog_find_driver(item, installed or {}, None, known_no_bind=no_bind)


def device(ids, compat=(), name='Eszköz', err=0, generic_ok=False):
    return {'id': ids[0], 'all_hwids': list(ids) + list(compat), 'pnp_id': ids[0] + '\\4&1&0',
            'name': name, 'cat': 'x', 'err_code': err, 'generic_ok': generic_ok}


def gen_scenario(seed):
    """Véletlen, de determinisztikus forgatókönyv (a fuzz-tesztekhez). Visszatérés:
    (item, installed, stub, no_bind, sup_by_guid)."""
    r = random.Random(seed)
    bus = r.choice(['PCI', 'HDAUDIO', 'USB'])
    ven = r.choice(['10EC', '8086', '1022'])
    dev = f"{r.randint(0, 0xFFFF):04X}"
    sub = f"{r.randint(0, 0xFFFFFFFF):08X}"
    if bus == 'PCI':
        ids = [f"PCI\\VEN_{ven}&DEV_{dev}&SUBSYS_{sub}&REV_0{r.randint(0, 9)}",
               f"PCI\\VEN_{ven}&DEV_{dev}&SUBSYS_{sub}",
               f"PCI\\VEN_{ven}&DEV_{dev}&CC_040300", f"PCI\\VEN_{ven}&DEV_{dev}&CC_0403"]
        compat = [f"PCI\\VEN_{ven}&DEV_{dev}", f"PCI\\VEN_{ven}&CC_0403"]
    elif bus == 'HDAUDIO':
        ids = [f"HDAUDIO\\FUNC_01&VEN_{ven}&DEV_{dev}&SUBSYS_{sub}&REV_1003",
               f"HDAUDIO\\FUNC_01&VEN_{ven}&DEV_{dev}&SUBSYS_{sub}"]
        compat = [f"HDAUDIO\\FUNC_01&VEN_{ven}&DEV_{dev}"]
    else:
        ids = [f"USB\\VID_{ven}&PID_{dev}&REV_0100", f"USB\\VID_{ven}&PID_{dev}"]
        compat = ['USB\\Class_FF']
    item = device(ids, compat, name=f'Dev{seed}', err=r.choice([0, 0, 0, 0, 10, 28, 24, 18]),
                  generic_ok=r.random() < 0.3)
    kind = r.choice(['none', 'inbox', 'vendor', 'vendor'])
    if kind == 'inbox':
        inst = {'version': '10.0.26100.1', 'date': '2006-06-21', 'provider': 'Microsoft', 'inf': 'hdaudio.inf'}
    elif kind == 'vendor':
        inst = {'version': f"6.0.{r.randint(8000, 10000)}.1",
                'date': f"20{r.randint(17, 26)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}",
                'provider': 'Realtek', 'inf': 'oem5.inf'}
    else:
        inst = {}
    installed = {item['pnp_id'].upper(): inst} if inst else {}
    tie = r.random() < 0.35
    packages = []
    for _ in range(r.randint(1, 8)):
        v = f"6.0.{r.randint(8000, 10100)}.1"
        d = (r.choice(['2025-10-02', '2026-03-01']) if tie else
             f"20{r.randint(17, 26)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}")
        t = r.choice([f"Realtek - MEDIA - {v}", f"Realtek Driver Update ({v})", f"Microsoft - MEDIA - {v}"])
        packages.append((t, d, r.choice(['yes', 'no', 'none', 'cc', 'mixed'])))
    rows, sup, models = {}, {}, {}
    for key in [k.lower() for k in ids + compat] + ['__base__']:
        if r.random() < 0.15:
            rows[key] = 'ERR'
            continue
        lst = []
        for _ in range(r.randint(0, 12)):
            t, d, sk = r.choice(packages)
            g = f"{r.randint(0, 16 ** 12):012x}"
            lst.append(row(g, t, d, r.choice(['windows 10 and later', 'windows 11 client, version 24h2',
                                               'windows server 2019', 'arm64 windows 11', '']),
                           r.choice(['12.9 MB', '1.2 GB', '300 KB', '150 MB'])))
            sup[g] = {'yes': [ids[1].lower()], 'no': ['pci\\ven_ffff&dev_0000'],
                      'cc': [(ids[2] if bus == 'PCI' else ids[0]).lower()], 'none': None,
                      'mixed': r.choice([[ids[1].lower()], ['pci\\ven_ffff&dev_0001'], None])}[sk]
            models[g] = r.choice(['', f'dev{seed}', 'other device'])
        rows[key] = lst
    allg = [x[0] for v in rows.values() if v != 'ERR' for x in v]
    no_bind = {}
    if allg and r.random() < 0.3:
        no_bind = {_device_stem(item['pnp_id']):
                   [{'guid': g} for g in r.sample(allg, min(len(allg), r.randint(1, 4)))]}
    no_url = set(r.sample(allg, min(len(allg), r.randint(0, 2)))) if allg else set()
    return item, installed, CatalogStub(rows, sup, models, no_url), no_bind, sup


def quiet():
    logging.disable(logging.CRITICAL)
