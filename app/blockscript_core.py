"""Net Blokkoló Script (block.bat) letöltése - közös mag (GUI és CLI is ezt hívja).
CSAK letölti a scriptet, SOHA nem futtatja."""

# === AUTO-IMPORTS ===
import os
import logging
import shutil
from app.common import _app_data_dir
from app.common import _ps_quote
# === /AUTO-IMPORTS ===



# Net Blokkoló script (block.bat) - GitHub release-ből letölthető batch script, ami a
# saját mappájában (és almappáiban) lévő összes .exe kimenő internet-elérését letiltja a
# Windows tűzfalban (netsh advfirewall). A program CSAK letölti a _app_data_dir()
# mappába, NEM futtatja. Szándékosan .bat és nem .ps1: a batch fájlokra nem vonatkozik a
# PowerShell execution policy (ami alapértelmezetten Restricted, azaz a sima dupla katt
# / "Futtatás PowerShell-lel" egy azonnal bezáruló ablakkal elhal - terepen bizonyított),
# és a .bat magától kéri az admin jogot (UAC) is, így az ügyfélgépen tényleg csak dupla
# kattintás kell hozzá.
BLOCK_SCRIPT_URL = "https://github.com/egonixaimgod/DriverVarazslo/releases/download/block.bat/block.bat"


def _download_block_script(run_fn):
    """Letölti a block.bat scriptet a _app_data_dir() mappába - EGY helyen (a GUI
    download_block_script és a CLI download_block_script is ezt hívja), hogy a kettő ne
    driftelhessen szét. Visszaadja a mentett fájl teljes útvonalát, hibánál kivételt dob.
    run_fn: a hívó API-osztály _run metódusa (a friss-Windows tanúsítvány-fallbackhez)."""
    import urllib.request, urllib.error, ssl
    dest = os.path.join(_app_data_dir(), 'block.bat')
    logging.info("[BLOCK-SCRIPT] Letöltés INNEN: " + BLOCK_SCRIPT_URL)
    ssl_ctx = ssl.create_default_context()
    req = urllib.request.Request(BLOCK_SCRIPT_URL, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=60) as resp, open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.URLError as dl_err:
        # Ugyanaz a friss-Windows gyökértanúsítvány-probléma, mint a _download_stresstools-nál
        # (a github.com Sectigo/USERTrust gyökere hiányzik a vadonatúj gép tárából, amit csak
        # schannel-kliens tölt le igény szerint) - ezért CSAK erre a hibára esünk vissza
        # PowerShell Invoke-WebRequest-re, teljes tanúsítvány-ellenőrzéssel (SEMMIT nem
        # kapcsolunk ki!).
        if 'CERTIFICATE_VERIFY_FAILED' not in str(dl_err):
            raise
        logging.warning(f"[BLOCK-SCRIPT] Python SSL tanúsítvány-hiba ({dl_err}) - áttérés PowerShell (schannel) letöltésre, teljes tanúsítvány-ellenőrzéssel...")
        ps_cmd = ("$ProgressPreference='SilentlyContinue'; "
                  "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
                  f"Invoke-WebRequest -Uri '{_ps_quote(BLOCK_SCRIPT_URL)}' -OutFile '{_ps_quote(dest)}' -UseBasicParsing")
        result = run_fn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], timeout=120)
        if not result or result.returncode != 0 or not os.path.exists(dest):
            raise Exception("A letöltés sikertelen (nincs internet, vagy a GitHub nem elérhető).")
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise Exception("A letöltött fájl üres vagy hiányzik.")
    logging.info(f"[BLOCK-SCRIPT] Letöltve: {dest}")
    return dest
