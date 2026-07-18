"""Net Blokkoló Script (block.bat) letöltése - közös mag (GUI és CLI is ezt hívja).
CSAK letölti a scriptet, SOHA nem futtatja."""

# === AUTO-IMPORTS ===
import os
from app.common import _app_data_dir
from app.common import download_with_cert_fallback
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
    run_fn: a hívó API-osztály _run metódusa (a friss-Windows tanúsítvány-fallbackhez -
    lásd common.download_with_cert_fallback)."""
    dest = os.path.join(_app_data_dir(), 'block.bat')
    return download_with_cert_fallback(run_fn, BLOCK_SCRIPT_URL, dest,
                                       timeout=60, ps_timeout=120, log_tag='BLOCK-SCRIPT')
