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
from app.common import _ps_quote
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
    friss-Windows tanúsítvány-fallback, mint a block.bat letöltésnél: CERTIFICATE_
    VERIFY_FAILED esetén PowerShell (schannel) letöltés, TELJES ellenőrzéssel."""
    import urllib.request
    import urllib.error
    import ssl
    dest = os.path.join(_app_data_dir(), NICPACK_FILENAME)
    logging.info(f"[NICPACK] Letöltés innen: {NICPACK_URL}")
    ssl_ctx = ssl.create_default_context()
    req = urllib.request.Request(NICPACK_URL, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=120) as resp, open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.URLError as dl_err:
        if 'CERTIFICATE_VERIFY_FAILED' not in str(dl_err):
            raise
        logging.warning(f"[NICPACK] Python SSL tanúsítvány-hiba ({dl_err}) - PowerShell (schannel) letöltés, teljes ellenőrzéssel...")
        ps_cmd = ("$ProgressPreference='SilentlyContinue'; "
                  "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
                  f"Invoke-WebRequest -Uri '{_ps_quote(NICPACK_URL)}' -OutFile '{_ps_quote(dest)}' -UseBasicParsing")
        result = run_fn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], timeout=180)
        if not result or result.returncode != 0 or not os.path.exists(dest):
            raise Exception("A nicpack.zip letöltése sikertelen (nincs internet vagy nincs feltöltve a release).")
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise Exception("A letöltött nicpack.zip üres vagy hiányzik.")
    logging.info(f"[NICPACK] Letöltve: {dest}")
    return dest


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
