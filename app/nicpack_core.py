"""NIC (LAN/Wi-Fi) driver vészcsomag - közös mag (GUI és CLI is ezt hívja).

A szerviz-klasszikus tyúk-tojás probléma megoldása: friss telepítés / friss
gép, nincs hálózati driver -> nincs internet -> nem lehet drivert letölteni.
A nicpack.zip egy kézzel összerakott csomag (Realtek/Intel LAN + gyakori Wi-Fi
INF-ek, pnputil /export-driver-rel kinyerve működő gépekről), amit a program
INTERNET NÉLKÜL is fel tud telepíteni. Keresési sorrend:
  1) az exe mappája (szerviz-USB-n az exe mellé másolva mindig kéznél van),
  2) az app-adatmappa (C:\\DriverVarazslo),
  3) ha van internet: letöltés GitHub release-ből az app-adatmappába
     (így egy netes gépen előkészíthető a következő nettelen bevetésre).
A ZIP tartalmát a program pnputil /add-driver /subdirs /install-lal telepíti -
a pnputil csak az oda illő, aláírt INF-eket fogadja el, a többit kihagyja."""

# === AUTO-IMPORTS ===
import os
import shutil
import zipfile
import logging
from app.common import _app_data_dir
from app.common import _app_exe_path
from app.common import download_with_cert_fallback
# === /AUTO-IMPORTS ===


NICPACK_URL = "https://github.com/egonixaimgod/DriverVarazslo/releases/download/nicpack/nicpack.zip"
NICPACK_FILENAME = "nicpack.zip"


def _find_nicpack_zip():
    """A nicpack.zip helyének felkutatása (exe mellett -> app-adatmappa). None, ha sehol."""
    candidates = [
        os.path.join(os.path.dirname(_app_exe_path()), NICPACK_FILENAME),
        os.path.join(_app_data_dir(), NICPACK_FILENAME),
    ]
    for c in candidates:
        try:
            if os.path.isfile(c) and os.path.getsize(c) > 0:
                logging.info(f"[NICPACK] Megtalálva: {c}")
                return c
        except OSError:
            continue
    return None


def _download_nicpack(run_fn):
    """A nicpack.zip letöltése az app-adatmappába (csak ha van internet). Ugyanaz a
    friss-Windows tanúsítvány-fallback, mint a block.bat letöltésnél
    (common.download_with_cert_fallback - TELJES tanúsítvány-ellenőrzéssel)."""
    dest = os.path.join(_app_data_dir(), NICPACK_FILENAME)
    return download_with_cert_fallback(
        run_fn, NICPACK_URL, dest, timeout=120, ps_timeout=180, log_tag='NICPACK',
        error_msg="A nicpack.zip letöltése sikertelen (nincs internet vagy nincs feltöltve a release).")


def _install_nicpack(run_fn, progress_fn):
    """A vészcsomag megkeresése/letöltése, kicsomagolása és telepítése.
    progress_fn(szöveg): a hívó kijelzési stílusa (GUI emit / CLI print).
    Visszatérés: (telepített_csomagok_száma, összes_inf_a_csomagban). Kivételt dob,
    ha a csomag sehol nem található."""
    zip_path = _find_nicpack_zip()
    if not zip_path:
        progress_fn("📦 A nicpack.zip nincs meg helyben - letöltési kísérlet (ehhez internet kell)...")
        zip_path = _download_nicpack(run_fn)

    extract_dir = os.path.join(_app_data_dir(), 'nicpack_extracted')
    shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)
    progress_fn(f"📂 Kicsomagolás: {zip_path}")
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_dir)

    inf_count = 0
    for _root, _dirs, files in os.walk(extract_dir):
        inf_count += sum(1 for f in files if f.lower().endswith('.inf'))
    if inf_count == 0:
        raise Exception("A nicpack.zip-ben egyetlen .inf sincs - ellenőrizd a csomag tartalmát!")

    progress_fn(f"🛠️ {inf_count} driver-INF telepítése (a pnputil csak az ide illőket fogadja el)...")
    res = run_fn(['pnputil', '/add-driver', os.path.join(extract_dir, '*.inf'), '/subdirs', '/install'], timeout=900)
    out = (res.stdout or '') if res else ''
    installed = out.lower().count('added successfully')
    run_fn(['pnputil', '/scan-devices'])
    logging.info(f"[NICPACK] Telepítve/regisztrálva: {installed} csomag ({inf_count} INF-ből)")
    return installed, inf_count
