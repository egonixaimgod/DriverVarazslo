"""In-app auto-updater - KÖZÖS mag (GUI automatikus/manuális ellenőrzés + CLI menüpont).

A BUILD_NUMBER-ellenőrzés és a frissítés letöltése/előkészítése EGY példányban itt él;
a GUI (app/gui/updater.py) emit-ekkel, a CLI (app/cli/updater.py) printtel csomagolja.

=============================================================================
KÉT FORRÁS, ÉS A SORRENDJÜK A LÉNYEG (2026-09-17, explicit user decision)
=============================================================================

Terepi panasz: *"fel pusholom, nem mindig talalja meg, vagy csak 10 percre ra"*.
A mérés megadta az okot (2026-09-17, curl-lel a két végpontra):

  | forrás                      | Cache-Control | kiszolgáló                         |
  |-----------------------------|---------------|------------------------------------|
  | raw.githubusercontent.com   | **max-age=300** | Fastly CDN (`Via: 1.1 varnish`)  |
  | api.github.com/.../releases | max-age=60    | GitHub API, ETag-alapú             |

A raw tartalmat tehát egy CDN edge **öt percig** kiszolgálhatja a push után is, és a
`?t=<timestamp>` ezen NEM segít (a GitHub raw a query stringet nem veszi a cache
kulcsába) - ezért született annak idején a 3 próbálkozás, ami viszont csak a tünetet
kezelte, az okot nem.

A megoldás ugyanaz, ami a felhasználó YouTube-letöltő projektjében már bevált: a
**GitHub Releases API** az elsődleges forrás. Ott a kiadás `build-N` címkét kap, az exe
pedig melléklet - és az API a kiadás létrehozása után azonnal látja.

**A RAW ÚT VISZONT NEM DOBHATÓ KI, HÁROM OKBÓL:**
 1. **A MÁR KIADOTT exe-k (Build <= 310) CSAK a raw utat ismerik.** Ha a rebuild abbahagyná
    a `driver_tool.py` + `dist/DriverVarazslo.exe` pusholását, azok a példányok soha többé
    nem értesülnének új verzióról. A rebuild ezért mindkettőt csinálja: push ÉS release.
 2. **Az API óránként 60 kérést enged IP-nként** (nem autentikált hívás). Egy szervizben
    több gép ugyanazon a nyilvános IP-n ül, tehát ez elfogyhat; 403 esetén a raw-ra esünk.
 3. Ha egy kiadásnál a `gh release create` kimarad (nincs gh, nincs jog), a raw akkor is
    friss - csak lassabban.

**A `/releases/latest` SZÁNDÉKOSAN NINCS HASZNÁLVA, és ez eltérés a YouTube-projekttől.**
Ebben a repóban a kiadások egy részé nem build, hanem ASSET-TÁROLÓ (`stresstools.zip`,
`mas.zip`, `block.bat` - mérve 2026-09-17), és a `/releases/latest` ezek közül a
legfrissebbet adná vissza. Ezért a lista kérdezzük le, és abból a legnagyobb `build-N`
címkéjűt választjuk ki.

Frissítés CSAK szigorúan nagyobb build esetén ajánlható (new_build > BUILD_NUMBER). A
letöltés ssl.create_default_context()-tel, teljes tanúsítvány-ellenőrzéssel megy - ezt
SOHA nem szabad kikapcsolni (admin-jogú exe-t töltünk le)."""

# === AUTO-IMPORTS ===
import os
import json
import locale
import subprocess
import re
import time
import logging
import winreg
from app import common
from app.common import _app_exe_path
# === /AUTO-IMPORTS ===


# AZ ÚJRAPRÓBÁLKOZÁS A FASTLY CDN ELAVULT PÉLDÁNYA MIATT VAN, ÉS CSAK AKKOR FUT LE, HA
# TÉNYLEG SEGÍTHET (2026-09-08, terepen mérve). A raw.githubusercontent.com mögötti CDN
# egy push után percekig kiszolgálhat régi másolatot, és a `?t=<timestamp>` csak a
# kliens-oldali gyorsítótárat kerüli meg - ezért született a 3 próbálkozás.
#
# A ciklus viszont a SIKERES, egyértelmű válasz után is továbbment. Mérve egy induláson:
#   22:58:23  Letöltött BUILD_NUMBER: 304, Helyi: 304 -> Nincs újabb verzió.
#   22:58:26  ...ugyanaz
#   22:58:29  ...ugyanaz          ->  check_for_updates -> 6.30s
# Vagyis MINDEN kézi indítás 6,3 másodpercet és 3 HTTP-kérést költött egy olyan kérdésre,
# amire az első válasz definitív volt.
#
# AZ ÚJ SZABÁLY (a védőháló megmarad, az ár eltűnik):
#   újabb build      -> azonnal vissza (eddig is így volt)
#   AZONOS build     -> DEFINITÍV válasz, azonnal vissza  <- ez a nyereség
#   KISEBB build     -> gyanús: elavult CDN-példány (vagy helyi bump push előtt) -> újra
#   hiba / nem parse -> újra
UPDATE_CHECK_ATTEMPTS = 3
UPDATE_CHECK_RETRY_SEC = 3
_GITHUB_REPO = "egonixaimgod/DriverVarazslo"
_RAW_BASE = f"https://raw.githubusercontent.com/{_GITHUB_REPO}/main"
_API_RELEASES = f"https://api.github.com/repos/{_GITHUB_REPO}/releases?per_page=30"
# A kiadás címkéje, amit a rebuild szkript ad (`gh release create build-<N>`). Ami nem
# ilyen alakú, az ASSET-TÁROLÓ kiadás (stresstools.zip, mas.zip, block.bat) - azokat itt
# figyelmen kívül kell hagyni, különben a `stresstools.zip` címke "buildnek" látszana.
_BUILD_TAG_RE = re.compile(r'^build-(\d+)$', re.IGNORECASE)
# Az API-nak rövid időkorlát elég (néhány KB JSON), és ha nem felel, ott a raw tartalék.
API_TIMEOUT = 8


def _latest_release():
    """A legfrissebb `build-N` címkéjű kiadás a GitHub Releases API-ról.

    Visszatérés: (build_szam, exe_url) vagy (None, None), ha nincs ilyen kiadás vagy
    az API nem elérhető. SOHA nem dob: a hívónak van tartalék útja (raw), és egy
    elbukott API-hívás nem akaszthatja meg az indulást.

    A legnagyobb `build-N`-t választjuk, nem a listaelsőt: a kiadások sorrendje a
    létrehozás ideje szerint megy, és egy utólag feltöltött asset-tároló kiadás
    (pl. új stresstools.zip) a build-kiadások ELÉ kerülne."""
    import urllib.request
    import ssl
    try:
        req = urllib.request.Request(_API_RELEASES, headers={
            'User-Agent': 'DriverVarazslo',
            'Accept': 'application/vnd.github+json',
        })
        with urllib.request.urlopen(req, context=ssl.create_default_context(),
                                    timeout=API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        # A 403 itt jellemzően az óránkénti 60 kérés kimerülése (több gép egy IP-n).
        # Nem hiba, csak annyit jelent, hogy most a raw úton megyünk.
        logging.warning(f"[UPDATE] A Releases API nem elérhető ({e}) - a raw útra váltunk.")
        return None, None
    best_build, best_url, seen = None, None, []
    for rel in (data if isinstance(data, list) else []):
        tag = (rel.get('tag_name') or '').strip()
        seen.append(tag)
        m = _BUILD_TAG_RE.match(tag)
        if not m or rel.get('draft'):
            continue
        build = int(m.group(1))
        if best_build is not None and build <= best_build:
            continue
        exe_url = None
        for asset in (rel.get('assets') or []):
            if (asset.get('name') or '').lower().endswith('.exe'):
                exe_url = asset.get('browser_download_url')
                break
        best_build, best_url = build, exe_url
    if best_build is None:
        logging.info(f"[UPDATE] A Releases API válaszol, de nincs `build-N` címkéjű kiadás "
                     f"(látott címkék: {seen[:8]}) - a raw út dönt.")
        return None, None
    if not best_url:
        # A kiadás megvan, de exe nincs mellékelve: a build-szám így is hiteles, az exe-t
        # a raw útról töltjük le. Ezt ki kell mondani, mert egy elrontott kiadásnál ez az
        # egyetlen nyom.
        logging.warning(f"[UPDATE] A build-{best_build} kiadáshoz NINCS .exe melléklet - "
                        f"a build-szám érvényes, de a letöltés a raw útra esik vissza.")
    logging.info(f"[UPDATE] Releases API: legfrissebb kiadás build-{best_build} "
                 f"(exe melléklet: {'igen' if best_url else 'nincs'})")
    return best_build, best_url


def check_for_updates():
    """Update-ellenőrzés. ELSŐDLEGESEN a GitHub Releases API-ról (azonnal friss),
    tartalékként a raw.githubusercontent.com-on lévő driver_tool.py BUILD_NUMBER-éből
    (CDN-cache miatt akár 5 perc késéssel) - a részletes indoklás a modul tetején.

    Visszatérés: {'has_update': bool, 'new_version': int, 'exe_url': str|None,
                  'source': 'api'|'raw'}."""
    logging.info("[UPDATE] check_for_updates()")
    import urllib.request
    import urllib.error
    import ssl

    # --- 1) ELSŐDLEGES: Releases API ---
    api_build, api_exe = _latest_release()
    if api_build is not None:
        logging.info(f"[UPDATE] Kiadás-szám (API): {api_build}, Helyi: {common.BUILD_NUMBER}")
        if api_build > common.BUILD_NUMBER:
            return {'has_update': True, 'new_version': api_build,
                    'exe_url': api_exe, 'source': 'api'}
        # AZONOS VAGY KISEBB BUILD AZ API-RÓL: ez definitív válasz, NEM kell a raw-ot is
        # megkérdezni. Az API nem CDN-cache-elt, tehát nincs mit "frissebbre" várni -
        # a raw legfeljebb ugyanazt vagy egy elavult példányt adna.
        logging.info("[UPDATE] Nincs újabb kiadás (az API definitív válasza).")
        return {'has_update': False, 'source': 'api'}

    # --- 2) TARTALÉK: a régi raw út (ezt ismerik a Build <= 310 példányok is) ---
    logging.info("[UPDATE] Nincs használható kiadás az API-n - a raw út következik.")
    ssl_ctx = ssl.create_default_context()
    for attempt in range(1, UPDATE_CHECK_ATTEMPTS + 1):
        try:
            # A ?t=<timestamp> a kliens-oldali cache megkerülésére (a CDN-ét nem védi ki)
            url = f"{_RAW_BASE}/driver_tool.py?t={int(time.time())}"
            logging.info(f"[UPDATE] Update ellenőrzése erről a címről ({attempt}/{UPDATE_CHECK_ATTEMPTS}. próbálkozás): {url}")
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            try:
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
                    content = resp.read().decode('utf-8')
            except (urllib.error.URLError, ssl.SSLError) as ssl_err:
                # RÉGI WINDOWS (7/8/8.1) MIATT KELL: a Python OpenSSL-verme itt olyan
                # TLS-hibába futhat, amibe a rendszer schannel-je nem - terepen mérve
                # (2026-08-13, Win 8.1) pontosan ez buktatta el a WebView2 telepítő
                # letöltését ugyanezen a gépen ([SSL: UNEXPECTED_EOF_WHILE_READING]).
                # Ha ezt itt nem kapjuk el, a régi gép SOHA nem értesül róla, hogy van
                # újabb build - vagyis épp az a javítás nem jut el hozzá, ami a hibát
                # orvosolná. A közös letöltő ilyenkor PowerShellre (schannel) vált,
                # teljes tanúsítvány-ellenőrzéssel.
                if not common._should_try_ps_download(ssl_err):
                    raise
                logging.warning(f"[UPDATE] Python SSL/TLS hiba ({ssl_err}) - áttérés a közös "
                                f"letöltő PowerShell (schannel) ágára...")
                content = common.fetch_text_with_cert_fallback(url, timeout=10, ps_timeout=120,
                                                               log_tag='UPDATE')
            m = re.search(r'^BUILD_NUMBER\s*=\s*(\d+)', content, re.MULTILINE)
            if m:
                new_build = int(m.group(1))
                logging.info(f"[UPDATE] Letöltött BUILD_NUMBER: {new_build}, Helyi: {common.BUILD_NUMBER}")
                if new_build > common.BUILD_NUMBER:
                    logging.info(f"[UPDATE] Új verzió elérhető: {new_build} (Jelenlegi: {common.BUILD_NUMBER})")
                    return {'has_update': True, 'new_version': new_build,
                            'exe_url': None, 'source': 'raw'}
                if new_build == common.BUILD_NUMBER:
                    # DEFINITÍV VÁLASZ - nincs mit újrapróbálni (lásd a konstansok indoklását).
                    logging.info("[UPDATE] Nincs újabb verzió (a szerver a helyivel azonos "
                                 "build-et ad - definitív válasz, nincs újrapróbálkozás).")
                    return {'has_update': False, 'source': 'raw'}
                logging.info(f"[UPDATE] A szerver a helyinél RÉGEBBI build-et ad "
                             f"({new_build} < {common.BUILD_NUMBER}) - elavult CDN-példány vagy "
                             f"push előtti helyi verziószám-emelés lehet, újrapróbáljuk.")
            else:
                logging.error("[UPDATE] Nem található BUILD_NUMBER a letöltött fájlban!")
        except Exception:
            logging.error(f"[UPDATE] Ellenőrzési hiba ({attempt}/{UPDATE_CHECK_ATTEMPTS}. próbálkozás):", exc_info=True)
        if attempt < UPDATE_CHECK_ATTEMPTS:
            time.sleep(UPDATE_CHECK_RETRY_SEC)
    return {'has_update': False, 'source': 'raw'}


def stage_update(log, exe_url=None):
    """Az új exe letöltése + a cserét végző .bat előkészítése. Visszatérés: a .bat
    útvonala (a futtatás a hívóé: launch_update_and_exit). Hibánál kivételt dob.

    `exe_url`: a kiadás mellékletének címe, ha a hívó ismeri (a check_for_updates adja
    vissza). Enélkül újra lekérdezzük az API-t, és ha ott sincs, a raw útra esünk -
    így a frissítés akkor is működik, ha a hívó nem adott át semmit."""
    import tempfile

    if not exe_url:
        _, exe_url = _latest_release()
    if exe_url:
        logging.info("[UPDATE] Az exe a GitHub kiadás mellékletéből jön (nem a raw CDN-ről).")
    else:
        exe_url = f"{_RAW_BASE}/dist/DriverVarazslo.exe?t={int(time.time())}"
        logging.info("[UPDATE] Nincs kiadás-melléklet - az exe a raw útról jön.")
    # WinPE-ben a %TEMP% az X: RAM-diskre mutat - a letöltött exe-t a valódi C: meghajtóra tesszük.
    is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
    if is_pe:
        temp_dir = r'C:\DV_Temp'
        os.makedirs(temp_dir, exist_ok=True)
    else:
        temp_dir = tempfile.gettempdir()
    new_exe = os.path.join(temp_dir, f"DriverVarazslo_Update_{int(time.time())}.exe")

    logging.info(f"[UPDATE] EXE letöltése innen: {exe_url}")
    logging.info(f"[UPDATE] Cél fájl: {new_exe}")
    log('Új verzió letöltése GitHubról...')

    # A KÖZÖS letöltő (Python -> PowerShell schannel fallback), nem csupasz urllib:
    # ugyanaz az indok, mint a check_for_updates-nél - egy régi Windowson a Python
    # TLS-verme elhasalhat ott, ahol a rendszeré nem, és akkor a frissítés magán a
    # frissítendő gépen válik lehetetlenné. Az exe több MB, ezért bőven nagyobb
    # időkorláttal (a bootstrapperhez képest is), lassú gépet/hálózatot feltételezve.
    common.download_with_cert_fallback(
        common.default_run, exe_url, new_exe,
        timeout=120, ps_timeout=600, log_tag='UPDATE',
        error_msg="A frissítés letöltése nem sikerült (nincs internet, vagy a GitHub nem elérhető).")

    # A LETÖLTÖTT FÁJL VALÓDI PROGRAM-E? EZ NEM FIGYELMEZTETÉS, HANEM KAPU.
    # A régi kód csak WARNING-ot írt egy gyanúsan kicsi fájlra, és utána ugyanúgy
    # lecserélte VELE a futó programot - egy 404-es HTML oldalból így "frissítés" lett,
    # a gép pedig egy indíthatatlan exe-vel maradt. Két olcsó, egyértelmű jel dönt:
    # az `MZ` aláírás (minden Windows-exe ezzel kezdődik) és a minimális méret.
    # Ugyanez a kapu a felhasználó YouTube-letöltő projektjében is ott van.
    downloaded_size = os.path.getsize(new_exe)
    with open(new_exe, 'rb') as f:
        magic = f.read(2)
    logging.info(f"[UPDATE] EXE letöltve. Fájlméret: {downloaded_size} byte, fejléc: {magic!r}")
    if magic != b'MZ' or downloaded_size < 1000000:
        try:
            os.remove(new_exe)
        except Exception:
            pass
        raise RuntimeError(
            f"A letöltött fájl nem futtatható program ({downloaded_size} byte, "
            f"fejléc: {magic!r}). A frissítés megszakadt - a jelenlegi verzió sértetlen marad.")

    log('✅ Letöltés kész! A program frissítése és újraindítása következik...')

    current_exe = _app_exe_path()
    logging.info(f"[UPDATE] Jelenlegi futtatható fájl: {current_exe}")

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
            desktop_dir, _ = winreg.QueryValueEx(key, "Desktop")
    except Exception:
        desktop_dir = os.path.join(os.environ.get('USERPROFILE', 'C:\\'), 'Desktop')

    desktop_exe = os.path.join(desktop_dir, "DriverVarazslo.exe")
    logging.info(f"[UPDATE] Asztali elérési út: {desktop_exe}")

    bat_path = os.path.join(temp_dir, f"dv_update_{int(time.time())}.bat")
    log_path = update_report_path()
    # A ".old" biztonsági másolatokat sosem olvassuk vissza (nincs rollback funkció),
    # kizárólag a következő indításkori törlésre szolgálnak - ha viszont a program
    # legközelebb egy MÁSIK elérési útról indul (pl. nem az asztalról), a __init__-beli
    # takarítás sosem találja meg és törli őket, és örökre a lemezen maradnak. Ezért itt,
    # helyben (és csak sikeres másolás esetén) rögtön eltávolítjuk mindkettőt.
    #
    # HÁROM JAVÍTÁS A RÉGI SZKRIPTHEZ KÉPEST (2026-09-17) - mindhárom NÉMA hibát okozott:
    #
    # 1) A `timeout /t 3` FIX VÁRAKOZÁS KEVÉS. Egy PyInstaller onefile exe KÉT folyamat
    #    (a kicsomagoló bootloader és a Python gyerek), és a bootloader még fogja a fájlt,
    #    amikor a gyerek már kilépett. Ilyenkor a `copy` elbukik - a `2>&1` pedig elnyelte,
    #    tehát a régi verzió indult el újra, és a technikus azt hitte, frissült. A valódi
    #    feltétel nem az idő, hanem az, hogy a fájl CSERÉLHETŐ-e: addig próbáljuk.
    # 2) A SZKRIPT NEM HAGYOTT NYOMOT. Ha a csere nem sikerült, arról soha semmi nem szólt
    #    (a Rule 0 megsértése a program egyik legkényesebb pontján). Most minden lépés egy
    #    apró jelentés-fájlba megy, amit a következő indulás beolvas a fő naplóba
    #    (read_update_report) - így a "miért nem frissült?" kérdés utólag is válaszolható.
    # 3) A .bat UTF-8-CAL ÍRÓDOTT, a cmd viszont a RENDSZER KÓDLAPJÁN olvassa. Egy ékezetes
    #    felhasználónévnél (C:\\Users\\Józsi\\AppData\\Local\\Temp\\...) az útvonalak
    #    elromlanak, és a szkript némán nem csinál semmit. Ezért a kódlap + newline=''
    #    (a szövegmód a saját \\r\\n-jeinket \\r\\r\\n-re fordítaná, amitől a cmd nem
    #    találja meg a `goto` címkéket).
    bat_content = build_swap_bat(new_exe, current_exe, desktop_exe, log_path)
    logging.info(f"[UPDATE] .bat fájl írása: {bat_path} (kódlap: "
                 f"{locale.getpreferredencoding(False)})")
    with open(bat_path, 'w', encoding=locale.getpreferredencoding(False),
              errors='replace', newline='') as f:
        f.write(bat_content)
    return bat_path


def build_swap_bat(new_exe, current_exe, desktop_exe, log_path):
    """A cserét végző .bat TARTALMA. Tiszta függvény - offline tesztelhető, és épp ezért
    van külön: egy elrontott .bat NÉMÁN nem csinál semmit, a hibája csak egy ügyfél
    gépén derülne ki.

    A `\\r\\n` sorvégek szándékosak (a cmd a `goto` címkéket máskülönben nem találja meg,
    ha a szövegmód `\\r\\r\\n`-re fordítaná őket - ezért ír a hívó `newline=''`-lel)."""
    same_path = (current_exe or '').lower() == (desktop_exe or '').lower()
    lines = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        # A PyInstaller saját jelölései nélkül kell indítanunk az új exe-t, különben a
        # bootloader azt hiszi, ő egy már kicsomagolt gyerekfolyamat, és a régi (addigra
        # törölt) _MEI mappából próbálná betölteni a python DLL-t.
        "set _MEIPASS2=",
        "set _MEIPASS=",
        "set _PYIBoot_Pkg_ID=",
        'set "PID=%~1"',
        f'set "UJ={new_exe}"',
        f'set "ASZTAL={desktop_exe}"',
        f'set "FUTO={current_exe}"',
        f'set "NAPLO={log_path}"',
        '> "%NAPLO%" echo [%date% %time%] frissites indul (pid=%PID%)',
        "",
        "rem --- 1) varjuk meg, amig a regi folyamat kilep (max ~60 mp) ---",
        "set /a n=0",
        ":varakozas",
        'if "%PID%"=="" goto csere',
        'tasklist /fi "pid eq %PID%" /nh 2>nul | find /i "%PID%" >nul',
        "if errorlevel 1 goto csere",
        "ping -n 2 127.0.0.1 >nul",
        "set /a n+=1",
        "if !n! lss 30 goto varakozas",
        '>> "%NAPLO%" echo [%date% %time%] a regi folyamat meg fut, a csere igy is indul',
        "",
        "rem --- 2) csere: addig probaljuk, amig a fajl cserelheto lesz (max ~60 mp) ---",
        ":csere",
        "set /a n=0",
        "set ASZTAL_OK=0",
        "set FUTO_OK=0",
        ":csere_ismet",
        'copy /y "%UJ%" "%ASZTAL%" >nul 2>&1',
        "if not errorlevel 1 set ASZTAL_OK=1",
    ]
    if same_path:
        # A futó példány MAGA az asztali - egy másolás mindkettőt elintézi.
        lines += ['if "!ASZTAL_OK!"=="1" goto kesz']
    else:
        lines += [
            'copy /y "%UJ%" "%FUTO%" >nul 2>&1',
            "if not errorlevel 1 set FUTO_OK=1",
            'if "!ASZTAL_OK!"=="1" if "!FUTO_OK!"=="1" goto kesz',
        ]
    lines += [
        "ping -n 2 127.0.0.1 >nul",
        "set /a n+=1",
        "if !n! lss 30 goto csere_ismet",
        '>> "%NAPLO%" echo [%date% %time%] SIKERTELEN csere (asztal=!ASZTAL_OK! futo=!FUTO_OK!) - a regi verzio marad',
        "goto inditas",
        "",
        ":kesz",
        '>> "%NAPLO%" echo [%date% %time%] csere OK (asztal=!ASZTAL_OK! futo=!FUTO_OK!)',
        'del /f /q "%ASZTAL%.old" >nul 2>&1',
        'del /f /q "%FUTO%.old" >nul 2>&1',
        'del /f /q "%UJ%" >nul 2>&1',
        "",
        ":inditas",
        "ping -n 2 127.0.0.1 >nul",
        'if exist "%ASZTAL%" (start "" "%ASZTAL%") else (start "" "%FUTO%")',
        '>> "%NAPLO%" echo [%date% %time%] program elinditva',
        'del "%~f0"',
    ]
    return "\r\n".join(lines) + "\r\n"


def update_report_path():
    """A csere-szkript jelentés-fájlja. Az app-data mappában van (nem a %TEMP%-ben),
    mert a program saját temp-takarítója a %TEMP%-et üríti."""
    return os.path.join(common._app_data_dir(), 'frissites_jelentes.txt')


def read_update_report():
    """A LEGUTÓBBI EXE-CSERE KIMENETELE A FŐ NAPLÓBA, majd a jelentés-fájl törlése.

    MIÉRT: a csere egy külön .bat-ban, a program kilépése UTÁN történik - tehát a
    legkritikusabb lépésről (lecserélődött-e egyáltalán az exe?) a fő napló eddig
    semmit nem tudott. Ha a felhasználó azt jelenti, hogy "frissítettem, mégis a régi
    van fent", ez a pár sor a válasz. Az indulásból hívjuk, és soha nem dob."""
    path = update_report_path()
    try:
        if not os.path.exists(path):
            return
        with open(path, 'r', encoding=locale.getpreferredencoding(False),
                  errors='replace') as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        for ln in lines:
            level = logging.warning if 'SIKERTELEN' in ln else logging.info
            level(f"[UPDATE] Az előző frissítés naplója: {ln}")
        os.remove(path)
    except Exception as e:
        logging.debug(f"[UPDATE] A frissítés-jelentés nem olvasható ({path}): {e}")


def launch_update_and_exit(bat_path):
    """A csere-.bat elindítása tiszta (PyInstaller-változóktól mentes) környezetben,
    majd azonnali kilépés - a bat várja meg a folyamat leállását és cseréli az exe-t."""
    env = os.environ.copy()
    keys_to_remove = [k for k in env.keys() if k.startswith('_MEI') or k.startswith('_PYI')]
    for k in keys_to_remove:
        env.pop(k, None)

    # A PID ÁTADÁSA KÖTELEZŐ: a szkript erre vár, mielőtt cserélni próbál. Enélkül a
    # `%~1` üres, és a bat azonnal a cserével kezdene - pont abba a versenyhelyzetbe
    # futva, ami miatt az egész várakozás készült. (A bat erre is felkészült: üres PID
    # esetén rögtön a cserélhetőség-ciklusra ugrik, ami önmagában is véd.)
    logging.info(f"[UPDATE] .bat fájl elindítása (pid={os.getpid()}) és program bezárása...")
    subprocess.Popen(["cmd.exe", "/c", bat_path, str(os.getpid())],
                     creationflags=subprocess.CREATE_NO_WINDOW,
                     close_fds=True, env=env)
    os._exit(0)
