"""Windows Update beállítások (driver-push tiltás/engedélyezés, szüneteltetés,
szolgáltatás-újraindítás, teljes WU-reset) - KÖZÖS mag (GUI + CLI + AutoFix).

Korábban a registry-kulcsok, PowerShell scriptek és a reset-szekvencia két(-három)
példányban éltek (app/gui/wu.py, app/cli/wu.py, app/gui/autofix.py) - most EGY
példányban itt. A kiírás a hívóé: minden hosszabb szekvencia egy log(msg) callbackot
kap (GUI: task_progress emit, CLI: print).

Nem tévesztendő össze az app/wu_core.py-jal: az a WU driver-SZKENNELÉS/TELEPÍTÉS
közös magja, ez pedig a WU-BEÁLLÍTÁSOKÉ.
"""

# === AUTO-IMPORTS ===
import os
import time
import logging
import shutil
import winreg
from datetime import datetime, timezone
# === /AUTO-IMPORTS ===


# A WU szüneteltetés registry-értékeinek eltávolítása (feloldás) - a resume_wu, az
# enable_wu_reset és a set_wu_pause(False) is ugyanezt használja.
WU_PAUSE_REMOVE_PS = """
$regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
"""

# A WU szolgáltatások leállítása a cache-műveletek előtt.
WU_STOP_SERVICES_PS = """
Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
Stop-Service bits -Force -ErrorAction SilentlyContinue
Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
"""


# Az AUTOFIX ZÁRÓ szüneteltetésének hossza NAPBAN (explicit user decision, 2026-07-31).
# A szerviz a gépeket úgy adja át az ügyfélnek, hogy a Windows Update alapból NE induljon
# el magától: a fix épp most rakott fel egy konzisztens driver-készletet, és a legrosszabb,
# ami történhet, hogy a WU pár nap múlva magától felülírja. A korábbi 7 nap ehhez kevés volt.
# 3650 nap = kb. 10 év, azaz gyakorlatilag "amíg valaki kézzel vissza nem kapcsolja".
# FONTOS, és ezért nem "letiltás": ez SZÜNETELTETÉS, amit a felhasználó bármikor felold a
# Beállítások > Windows Update oldalon a "Frissítések folytatása" gombbal - egy kattintás,
# nem kell hozzá szerviz. A biztonsági frissítések tehát nincsenek véglegesen elvágva, csak
# nem indulnak el maguktól.
AUTOFIX_WU_PAUSE_DAYS = 3650


def build_wu_pause_ps(days, additive=True):
    """A WU szüneteltetést beállító PowerShell script.

    additive=True (felhasználói Pause gomb): ha már van érvényes szünet ÉS days==7,
    a meglévő lejárathoz adja hozzá a napokat (így a gomb ismételt nyomogatása
    hosszabbít); különben mostantól számol. A script kimenete az új lejárati dátum.

    additive=False (AutoFix): mindig mostantól számolt fix lejárat - az AutoFix
    szándéka "kb. N nap mostantól", nem a meglévő szünet hosszabbítása."""
    days = int(days)
    if additive:
        date_calc = """
    $currentPauseStr = (Get-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue).PauseUpdatesExpiryTime

    if ($currentPauseStr -and $daysToAdd -eq 7) {
        try {
            $currentPause = [datetime]$currentPauseStr
            if ($currentPause -lt $now) { $currentPause = $now }
        } catch {
            $currentPause = $now
        }
        $newDate = $currentPause.AddDays($daysToAdd)
    } else {
        $newDate = $now.AddDays($daysToAdd)
    }
"""
    else:
        date_calc = """
    $newDate = $now.AddDays($daysToAdd)
"""
    return f"""
    $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
    if (!(Test-Path $regPath)) {{ New-Item -Path $regPath -Force | Out-Null }}

    $daysToAdd = {days}
    $now = (Get-Date).ToUniversalTime()
{date_calc}
    $dateStr = $newDate.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $startStr = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")

    Set-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -Value $dateStr -Type String -Force | Out-Null
    Set-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -Value $dateStr -Type String -Force | Out-Null
    Set-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -Value $dateStr -Type String -Force | Out-Null
    Set-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
    Set-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
    Set-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null

    Write-Output $dateStr
    """


def read_wu_status(run):
    """A WU állapotának nyers tényei - a megjelenítendő szöveget a hívó formázza.
    Visszatérés: dict(policy_disabled, search_disabled, service_disabled, paused_until
    [a BEÁLLÍTOTT szünet, ISO vagy None], effective [a Windows TÉNYLEGES állapota vagy
    None], reboot_flags). A `run` a régi hívási alak miatt maradt (2026-11-03 óta minden
    registryből jön, PowerShell nélkül)."""
    policy_disabled, search_disabled = _driver_policy_state()
    # wuauserv Start: 4 = Disabled (a services.msc „Letiltva”).
    start = _reg_read(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Services\wuauserv', 'Start')
    service_disabled = bool(start and start[0] == 4)
    return {'policy_disabled': policy_disabled, 'search_disabled': search_disabled,
            'service_disabled': service_disabled, 'paused_until': wu_paused_until(),
            'effective': read_effective_state(), 'reboot_flags': wu_reboot_flags()}


def _driver_policy_state():
    """(ExcludeWUDriversInQualityUpdate == 1, SearchOrderConfig == 0) - a két driver-tiltó
    registry-érték, a WU nézet ÉS a beírások visszaolvasása is ezt használja."""
    policy_disabled = False
    search_disabled = False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "ExcludeWUDriversInQualityUpdate")
            if val == 1:
                policy_disabled = True
            logging.debug(f"[WU_STATUS] ExcludeWUDriversInQualityUpdate = {val}")
    except (FileNotFoundError, OSError):
        logging.debug("[WU_STATUS] ExcludeWUDriversInQualityUpdate kulcs nem létezik")

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "SearchOrderConfig")
            if val == 0:
                search_disabled = True
            logging.debug(f"[WU_STATUS] SearchOrderConfig = {val}")
    except (FileNotFoundError, OSError):
        logging.debug("[WU_STATUS] SearchOrderConfig kulcs nem létezik")
    return policy_disabled, search_disabled


def wu_paused_until():
    """A WU-szünet lejárata (ISO string), ha a jövőben van; különben None."""
    paused_until = None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")
            if val:
                dt = datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
                if dt > datetime.now(timezone.utc).replace(tzinfo=None):
                    paused_until = val
    except Exception as e:
        logging.debug(f"[WU] PauseUpdatesExpiryTime olvasás/értelmezés sikertelen (nincs szünet beállítva?): {e}")
    return paused_until


def set_wu_driver_policy(run, disabled):
    """A WU driver-letöltést vezérlő két registry-érték beállítása.
    disabled=True: SearchOrderConfig=0 + ExcludeWUDriversInQualityUpdate=1;
    disabled=False: SearchOrderConfig=1 + a policy-érték törlése.
    Visszaad: a VISSZAOLVASOTT állapot egyezik-e a kérttel (2026-10-12; eddig a hívó
    feltétel nélkül "✅ letiltva"-t írt, egy ACL-lel lezárt kulcsnál is)."""
    if disabled:
        run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
             '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])
        run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate',
             '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
    else:
        run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
             '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])
        # ok_codes 1: a kulcs nem létezik - várt eset, ha sosem volt letiltva.
        run(['reg', 'delete', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate',
             '/v', 'ExcludeWUDriversInQualityUpdate', '/f'], ok_codes=(0, 1))
    pol, srch = _driver_policy_state()
    ok = (pol and srch) if disabled else (not pol and not srch)
    if not ok:
        logging.error(f"[WU_SETTINGS] A driver-tiltás visszaolvasva nem egyezik (kért: "
                      f"{'tiltás' if disabled else 'engedélyezés'}; ExcludeWUDrivers=1: {pol}, "
                      f"SearchOrderConfig=0: {srch}).")
    return ok


def set_wu_pause(run, pause=True, days=AUTOFIX_WU_PAUSE_DAYS):
    """Egyszerű pause be/ki a driver-policy értékekkel együtt (az AutoFix használja):
    pause=True fix (nem hosszabbító) szünet mostantól + driver-tiltás; pause=False a
    szünet feloldása + driver-engedélyezés + wuauserv restart.

    A `days` alapértéke AUTOFIX_WU_PAUSE_DAYS (~10 év) - lásd a konstans indoklását."""
    if pause:
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             build_wu_pause_ps(days, additive=False)])
        set_wu_driver_policy(run, disabled=True)
    else:
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", WU_PAUSE_REMOVE_PS + """
Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
Start-Service wuauserv -ErrorAction SilentlyContinue
"""])
        set_wu_driver_policy(run, disabled=False)


# =====================================================================
# IDEIGLENES KERESÉSI ABLAK SZÜNETELTETETT WU MELLETT (2026-09-27, mérve)
# =====================================================================
# A MÉRÉS (a fejlesztői gépen, rendszergazdaként, ugyanazzal a Microsoft Update
# ServiceID-vel, amit a program használ):
#     szüneteltetve (a fix 10 éves szünete):        driver 0 db, BÁRMI 0 db
#     a szüneteltetési értékek törlése után:        driver 0 db, BÁRMI 5 db (biztonsági)
#     + a driver-tiltás (Exclude...) törlése után:  driver 0 db, BÁRMI 5 db
# Vagyis a SZÜNETELTETÉS a WUA API keresését is elvakítja (létező frissítéseket tagad le),
# a driver-tiltás a keresésre nem hatott. A fix a keresés előtt mindig feloldott, a KÉZI
# szken viszont soha - tehát minden gépen, amin korábban lefutott a fix, a kézi szken WU-
# része vak volt. Szolgáltatás-újraindítás NEM kellett: az értékek puszta törlése elég volt.
_UX_SETTINGS_KEY = r'SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
_AU_POLICY_KEY = r'SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
WU_PAUSE_VALUE_NAMES = ('PauseUpdatesExpiryTime', 'PauseUpdatesStartTime',
                        'PauseFeatureUpdatesStartTime', 'PauseFeatureUpdatesEndTime',
                        'PauseQualityUpdatesStartTime', 'PauseQualityUpdatesEndTime')


def _reg_read(root_key, sub, name):
    try:
        with winreg.OpenKey(root_key, sub, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, name)
    except OSError:
        return None


def _reg_write(sub, name, value, vtype):
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, sub, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
        winreg.SetValueEx(k, name, 0, vtype, value)


def _reg_delete(sub, name):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass


def open_search_window(marker_path):
    """Ha a WU szüneteltetve van: menti a pontos értékeket (a lemezre is - összeomlás
    esetén a következő indulás innen állít vissza), NoAutoUpdate=1 biztosítékot tesz (a
    fix is így csinálja: a feloldás pillanatában a Windows ne kezdjen magától
    frissíteni), majd törli a szüneteltetést. Nem szüneteltetett gépen None (nincs dolga).
    A visszaállítás a close_search_window dolga - a hívó `finally`-ben hívja."""
    import json
    pause = {}
    for n in WU_PAUSE_VALUE_NAMES:
        v = _reg_read(winreg.HKEY_LOCAL_MACHINE, _UX_SETTINGS_KEY, n)
        if v is not None:
            pause[n] = v[0]
    if not pause:
        logging.info("[WU_WINDOW] A WU nincs szüneteltetve - a keresés változtatás nélkül fut.")
        return None
    noau = _reg_read(winreg.HKEY_LOCAL_MACHINE, _AU_POLICY_KEY, 'NoAutoUpdate')
    state = {'pause': pause, 'noau_prev': noau[0] if noau else None}
    try:
        with open(marker_path, 'w', encoding='utf-8') as f:
            json.dump(state, f)
    except OSError as e:
        # Mentés nélkül NEM nyúlunk hozzá: egy összeomlás után nem tudnánk visszaállítani.
        logging.warning(f"[WU_WINDOW] A visszaállítási mentés nem írható ({e}) - a szüneteltetést "
                        f"nem oldjuk fel, a WU-keresés vak maradhat.")
        return None
    logging.warning(f"[WU_WINDOW] A WU szüneteltetve ({pause.get('PauseUpdatesExpiryTime', '?')}-ig) - "
                    f"a keresés idejére IDEIGLENESEN feloldjuk (mentés: {marker_path}).")
    try:
        _reg_write(_AU_POLICY_KEY, 'NoAutoUpdate', 1, winreg.REG_DWORD)
        for n in pause:
            _reg_delete(_UX_SETTINGS_KEY, n)
    except OSError as e:
        logging.warning(f"[WU_WINDOW] A feloldás részben elbukott ({e}) - azonnali visszaállítás.")
        close_search_window(state, marker_path)
        return None
    return state


def close_search_window(state, marker_path):
    """A pontos előző állapot visszaírása + visszaolvasás. Igaz, ha minden visszaállt."""
    if not state:
        return True
    ok = True
    # CSAK AZT ÍRJUK, AMI ELTÉR: ha a feloldás el sem indult (pl. jog nélkül az első írás
    # elbukott), nincs mit visszaírni - és egy fölösleges írási kísérlet hamis "NEM teljes"
    # hibát adna (mérve 2026-09-27, nem emelt jogú futásnál).
    for n, v in (state.get('pause') or {}).items():
        cur = _reg_read(winreg.HKEY_LOCAL_MACHINE, _UX_SETTINGS_KEY, n)
        if cur and cur[0] == v:
            continue
        try:
            _reg_write(_UX_SETTINGS_KEY, n, v, winreg.REG_SZ)
        except OSError as e:
            ok = False
            logging.error(f"[WU_WINDOW] {n} visszaírása SIKERTELEN: {e}")
    cur = _reg_read(winreg.HKEY_LOCAL_MACHINE, _AU_POLICY_KEY, 'NoAutoUpdate')
    if (cur[0] if cur else None) != state.get('noau_prev'):
        try:
            if state.get('noau_prev') is None:
                _reg_delete(_AU_POLICY_KEY, 'NoAutoUpdate')
            else:
                _reg_write(_AU_POLICY_KEY, 'NoAutoUpdate', int(state['noau_prev']), winreg.REG_DWORD)
        except OSError as e:
            ok = False
            logging.error(f"[WU_WINDOW] A NoAutoUpdate visszaállítása SIKERTELEN: {e}")
    # VISSZAOLVASÁS - a verdikt nem az írás, hanem hogy tényleg ott van-e.
    for n, v in (state.get('pause') or {}).items():
        cur = _reg_read(winreg.HKEY_LOCAL_MACHINE, _UX_SETTINGS_KEY, n)
        if not cur or cur[0] != v:
            ok = False
            logging.error(f"[WU_WINDOW] Visszaolvasva {n} = {cur[0] if cur else None}, várt: {v}")
    cur = _reg_read(winreg.HKEY_LOCAL_MACHINE, _AU_POLICY_KEY, 'NoAutoUpdate')
    if (cur[0] if cur else None) != state.get('noau_prev'):
        ok = False
        logging.error(f"[WU_WINDOW] Visszaolvasva NoAutoUpdate = {cur[0] if cur else None}, "
                      f"várt: {state.get('noau_prev')}")
    if ok:
        logging.info("[WU_WINDOW] A WU szüneteltetése pontosan visszaállítva.")
        try:
            os.remove(marker_path)
        except OSError as e:
            logging.debug(f"[WU_WINDOW] A mentés törlése nem sikerült: {e}")
    else:
        logging.error(f"[WU_WINDOW] A visszaállítás NEM teljes - a mentés megmarad ({marker_path}), "
                      f"a következő induláskor újrapróbáljuk.")
    return ok


def restore_stranded_search_window(marker_path):
    """Induláskor: ha egy keresési ablak nyitva maradt (összeomlás), visszaállítjuk."""
    import json
    if not os.path.exists(marker_path):
        return None
    try:
        with open(marker_path, encoding='utf-8') as f:
            state = json.load(f)
    except Exception as e:
        logging.warning(f"[WU_WINDOW] Nyitva maradt keresési ablak mentése olvashatatlan ({e}) - törölve.")
        try:
            os.remove(marker_path)
        except OSError:
            pass
        return False
    logging.warning("[WU_WINDOW] Egy korábbi WU-keresés ablaka nyitva maradt (összeomlás?) - visszaállítás.")
    return close_search_window(state, marker_path)


def _software_distribution_leftovers(sw_dist, limit=3):
    """A törlés UTÁN még ott lévő fájlok száma + néhány név (relatív útvonal)."""
    if not os.path.exists(sw_dist):
        return 0, []
    n, sample = 0, []
    for root, _dirs, files in os.walk(sw_dist):
        for fn in files:
            n += 1
            if len(sample) < limit:
                sample.append(os.path.relpath(os.path.join(root, fn), sw_dist))
    return n, sample


def clear_software_distribution(run, retries=4):
    """A SoftwareDistribution mappa törlése újrapróbálásokkal, végső PS fallbackkal - és a
    VERDIKT A VISSZAOLVASÁS, nem a próbálkozások kimenete (4. elv, 2026-09-28).

    Terepen (Build 344, CLI "WU driver letiltás"): a törlés négyszer `Access is denied`-dal
    bukott (`Download\\...\\Metadata\\UpdateAgent.dll` zárolva), a PowerShell-tartalék 1-es
    kóddal tért vissza - a képernyőn mégis "✅ Gyorsítótár törölve." állt, mert a hívó a
    visszatérési értéket meg sem nézte. Visszatérés: {'ok', 'remaining', 'sample'}."""
    sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
    sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
    deleted = False
    for _ in range(retries):
        try:
            if os.path.exists(sw_dist):
                shutil.rmtree(sw_dist, ignore_errors=False)
            deleted = True
            break
        except Exception as e:
            logging.warning(f"[WU_SETTINGS] SoftwareDistribution törlés újrapróbálás: {e}")
            time.sleep(3)
    if not deleted:
        run(["powershell", "-NoProfile", "-Command", f'Remove-Item -Path "{sw_dist}" -Recurse -Force -ErrorAction SilentlyContinue'])
    remaining, sample = _software_distribution_leftovers(sw_dist)
    if remaining:
        logging.warning(f"[WU_SETTINGS] A SoftwareDistribution törlése után {remaining} fájl MARADT "
                        f"(zárolva), pl.: {sample}")
    else:
        logging.info("[WU_SETTINGS] SoftwareDistribution: visszaolvasva üres/nem létezik - törölve.")
    return {'ok': remaining == 0, 'remaining': remaining, 'sample': sample}


def software_distribution_message(res):
    """A képernyőre kerülő sor a törlés VALÓDI eredményéről."""
    if res.get('ok'):
        return '✅ Gyorsítótár törölve.'
    pelda = f", pl. {res['sample'][0]}" if res.get('sample') else ''
    return (f"⚠️ A WU-gyorsítótár {res.get('remaining', '?')} fájlja most nem törölhető (zárolva{pelda}) - "
            f"a Windows Update működését nem akadályozza, egy újraindítás után törölhető.")


# =====================================================================
# WU-JAVÍTÁS + A WINDOWS SAJÁT (TÉNYLEGES) ÁLLAPOTA (2026-11-03, explicit user decision)
# =====================================================================
# A FELHASZNÁLÓ KÉRÉSE: minden WU-gomb (szünet, feloldás, driver-tiltás/engedélyezés,
# szolgáltatás-újraindítás) takarítsa ki a beragadt WU-szemetet, rakja rendbe a
# szolgáltatásokat, SOHA ne indítson magától frissítés-keresést, és csak akkor mondja,
# hogy kész, ha a Windows tényleg átvette. Ha ehhez újraindítás kell: 10 mp múlva
# újraindít, és bejelentkezés után a program magától befejezi és ellenőrzi.
#
# MÉRVE (2026-11-03, a fejlesztői gépen, Win10 22H2):
#   - A Windows Update-vezénylő a TÉNYLEGES szünet- és driver-állapotot a
#     `UpdatePolicy\PolicyState` kulcsban tartja (QualityUpdatesPaused,
#     PauseQualityUpdatesEndTime, ExcludeWUDrivers). Ugyanakkor a `UX\Settings` alatt
#     NEM volt szünet-érték, a PolicyState mégis 2036-ig szüneteltetett -> a régi kiírás
#     („nincs szünet”) hazudott, mert csak a UX\Settings-et olvasta.
#   - A UX\Settings átírása után sem az UsoSvc+wuauserv újraindítása (60 mp), sem a
#     `UsoClient RefreshSettings` (40 mp) NEM vitte át az új értéket a PolicyState-be.
#     Ezért a PolicyState-tel való egyezés hiánya = újraindítás kell. Hogy az újraindítás
#     átviszi-e, NINCS mérve: a befejező lépés ezt az újraindítás után visszaolvassa, és ha
#     nem, kimondja (nem indít újra még egyszer).
#   - A régi „Szolgáltatások újraindítása” gomb `wuauclt /detectnow` + `UsoClient
#     StartScan` hívással KERESÉST indított (szüneteltetett gépen is) - ez letöltötte a
#     „szemetet”. A régi szünet-gomb leállította a cryptsvc/bits/UsoSvc szolgáltatást, és
#     csak a wuauservet indította vissza -> a WU „halott” maradt.
_POLICY_STATE_KEY = r'SOFTWARE\Microsoft\WindowsUpdate\UpdatePolicy\PolicyState'
WU_REPAIR_SERVICES = ('UsoSvc', 'wuauserv', 'bits', 'cryptsvc')   # leállítási sorrend
WU_REPAIR_START_ORDER = ('cryptsvc', 'bits', 'wuauserv', 'UsoSvc')
WU_SERVICE_STOP_WAIT = 30          # mp szolgáltatásonként, utána a SAJÁT folyamatát lőjük le
WU_PAUSE_MAX_DAYS = 3650
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

WU_REPAIR_STOP_PS = r"""
$deps = @()
foreach ($n in @(__SERVICES__)) {
  $s = Get-Service -Name $n -ErrorAction SilentlyContinue
  if (-not $s) { Write-Output "STOP|$n|MISSING"; continue }
  foreach ($d in $s.DependentServices) { if ($d.Status -eq 'Running') { $deps += $d.Name } }
  if ($s.Status -eq 'Stopped') { Write-Output "STOP|$n|ALREADY"; continue }
  try { Stop-Service -Name $n -Force -NoWait -ErrorAction Stop } catch { Write-Output ("STOPERR|$n|" + $_.Exception.Message) }
  $sw = [Diagnostics.Stopwatch]::StartNew()
  do { Start-Sleep -Milliseconds 500; $s.Refresh() } while ($s.Status -ne 'Stopped' -and $sw.Elapsed.TotalSeconds -lt __WAIT__)
  if ($s.Status -eq 'Stopped') { Write-Output "STOP|$n|OK"; continue }
  # Beragadt (StopPending): CSAK akkor lőjük le a folyamatát, ha abban egyedül ez a
  # szolgáltatás fut - egy közös svchost lelövése más szolgáltatásokat is megölne.
  $svcPid = [int](Get-CimInstance Win32_Service -Filter "Name='$n'").ProcessId
  $shared = @(Get-CimInstance Win32_Service -Filter "ProcessId=$svcPid").Count
  if ($svcPid -gt 0 -and $shared -eq 1) {
    Stop-Process -Id $svcPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2; $s.Refresh()
    Write-Output "STOP|$n|KILLED|$svcPid|$($s.Status)"
  } else { Write-Output "STOP|$n|STUCK|$svcPid|$shared" }
}
Write-Output ('DEPS|' + (($deps | Select-Object -Unique) -join ','))
"""

WU_REPAIR_START_PS = r"""
foreach ($n in @(__SERVICES__)) {
  $s = Get-Service -Name $n -ErrorAction SilentlyContinue
  if (-not $s) { Write-Output "START|$n|MISSING"; continue }
  if ($s.StartType -eq 'Disabled') { Write-Output "START|$n|DISABLED"; continue }
  if ($s.Status -ne 'Running') {
    try { Start-Service -Name $n -ErrorAction Stop } catch { Write-Output ("STARTERR|$n|" + $_.Exception.Message) }
  }
  try { $s.WaitForStatus('Running', [TimeSpan]::FromSeconds(30)) } catch {}
  $s.Refresh(); Write-Output "START|$n|$($s.Status)"
}
"""


def _ps_list(names):
    return ','.join("'" + str(n).replace("'", "''") + "'" for n in names)


def parse_service_lines(out):
    """A STOP/START szkriptek soronkénti protokolljának értelmezése."""
    res = {'stop': {}, 'start': {}, 'errors': [], 'deps': []}
    for ln in (out or '').splitlines():
        parts = ln.strip().split('|')
        if len(parts) < 2:
            continue
        tag = parts[0]
        if tag == 'STOP' and len(parts) >= 3:
            res['stop'][parts[1]] = parts[2]
        elif tag == 'START' and len(parts) >= 3:
            res['start'][parts[1]] = parts[2]
        elif tag in ('STOPERR', 'STARTERR'):
            res['errors'].append(f"{parts[1]}: {'|'.join(parts[2:])}")
        elif tag == 'DEPS':
            res['deps'] = [d for d in parts[1].split(',') if d]
    return res


def _schedule_tree_delete_on_reboot(root):
    """A még zárolt fájlok törlése a KÖVETKEZŐ INDULÁSKOR (MoveFileEx +
    MOVEFILE_DELAY_UNTIL_REBOOT, a Windows saját mechanizmusa). Alulról felfelé: előbb a
    fájlok, utána a (addigra üres) mappák. Visszatérés: (ütemezett, sikertelen)."""
    import ctypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.MoveFileExW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    k32.MoveFileExW.restype = ctypes.c_int
    ok = fail = 0
    for dirpath, _dirs, files in os.walk(root, topdown=False):
        for p in [os.path.join(dirpath, f) for f in files] + [dirpath]:
            if k32.MoveFileExW(p, None, 4):
                ok += 1
            else:
                fail += 1
                logging.warning(f"[WU_REPAIR] Újraindításkori törlés nem ütemezhető: {p} "
                                f"(WinError {ctypes.get_last_error()})")
    logging.info(f"[WU_REPAIR] Újraindításkori törlésre ütemezve: {ok} elem, sikertelen: {fail} ({root})")
    return ok, fail


def wu_reboot_flags():
    """A Windows saját „újraindítás kell” jelzői, amelyek a WU működését akasztják.
    (A PendingFileRenameOperations szándékosan nincs itt: sok gépen tartósan nem üres,
    és minden gombnyomásra újraindítást kérne.)"""
    reasons = []
    for sub, text in ((r'SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
                       'a Windows egy korábbi frissítés befejezéséhez újraindítást vár (CBS RebootPending)'),
                      (r'SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired',
                       'a Windows Update újraindítást vár (RebootRequired)')):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub, 0,
                                           winreg.KEY_READ | winreg.KEY_WOW64_64KEY))
            reasons.append(text)
        except OSError:
            pass
    return reasons


def repair_wu(run, log, full_reset=False):
    """A KÖZÖS WU-javítás, minden WU-gomb ezt futtatja (keresést SOHA nem indít, a szünet-
    és driver-beállításhoz NEM nyúl):
      1. UsoSvc, wuauserv, bits, cryptsvc leállítása (beragadásnál a saját folyamatuk lelövése),
      2. a SoftwareDistribution (beragadt letöltések, WU-adatbázis) törlése; ami zárolt,
         az újraindításkor törlődik,
      3. full_reset: catroot2 átnevezése, WU DLL-ek újraregisztrálása, winsock reset,
      4. a szolgáltatások (és a leállított függőik) visszaindítása, visszaolvasva.
    Visszatérés: {'services_ok', 'problems', 'cache', 'scheduled', 'reboot_reasons'}."""
    problems, reboot_reasons = [], []
    log('🧹 Windows Update szolgáltatások leállítása (UsoSvc, wuauserv, BITS, cryptsvc)...')
    ps = WU_REPAIR_STOP_PS.replace('__SERVICES__', _ps_list(WU_REPAIR_SERVICES)).replace(
        '__WAIT__', str(WU_SERVICE_STOP_WAIT))
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
              timeout=WU_SERVICE_STOP_WAIT * len(WU_REPAIR_SERVICES) + 60, encoding='utf-8')
    stop = parse_service_lines(getattr(res, 'stdout', '') or '')
    logging.info(f"[WU_REPAIR] Leállítás: {stop['stop']} | futó függők: {stop['deps']} | hibák: {stop['errors']}")
    for n, st in stop['stop'].items():
        if st == 'KILLED':
            log(f'  ⚠️ {n} beragadt leállítás közben - a folyamata le lett lőve.')
        elif st == 'STUCK':
            log(f'  ⚠️ {n} nem állt le (közös folyamatban fut, nem lőhető le) - újraindítás kell.')
            reboot_reasons.append(f'a(z) {n} szolgáltatás beragadt, nem állt le')
    if not stop['stop']:
        logging.error(f"[WU_REPAIR] A leállító szkript nem adott választ (kód={getattr(res, 'returncode', None)}).")
        problems.append('a szolgáltatások leállítása nem ellenőrizhető')

    log('🧹 Beragadt letöltések és WU-gyorsítótár (SoftwareDistribution) törlése...')
    cache = clear_software_distribution(run, retries=2)
    scheduled = 0
    if cache['ok']:
        log('  ✅ Gyorsítótár törölve.')
    else:
        sw_dist = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'SoftwareDistribution')
        scheduled, failed = _schedule_tree_delete_on_reboot(sw_dist)
        log(f"  ⏳ {cache['remaining']} fájl zárolva - újraindításkor törlődik ({scheduled} elem ütemezve"
            + (f', {failed} NEM ütemezhető' if failed else '') + ').')
        reboot_reasons.append(f"{cache['remaining']} zárolt WU-gyorsítótár fájl csak újraindításkor törölhető")
        if failed:
            problems.append(f'{failed} WU-gyorsítótár elem újraindításkor sem törlődik (nem ütemezhető)')

    if full_reset:
        sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
        catroot2 = os.path.join(sysroot, 'System32', 'catroot2')
        bak = catroot2 + '.bak'
        try:
            if os.path.exists(bak):
                shutil.rmtree(bak, ignore_errors=True)
            if os.path.exists(catroot2):
                os.rename(catroot2, bak)
            log('  ✅ catroot2 átnevezve (a Windows újraépíti).')
            logging.info("[WU_REPAIR] catroot2 átnevezve")
        except Exception as e:
            logging.warning(f"[WU_REPAIR] catroot2 átnevezése sikertelen: {e}")
            log(f'  ⚠️ catroot2 nem nevezhető át ({e}).')
            problems.append(f'catroot2: {e}')
        sys32 = os.path.join(sysroot, 'System32')
        for dll in ('wuaueng.dll', 'wuapi.dll', 'wups.dll', 'wups2.dll', 'wuwebv.dll', 'wucltux.dll'):
            fp = os.path.join(sys32, dll)
            if os.path.exists(fp):
                run(f'regsvr32.exe /s "{fp}"', shell=True, ok_codes=(0, 3, 4, 5))
        log('  ✅ WU DLL-ek újraregisztrálva.')
        r = run('netsh winsock reset', shell=True)
        if getattr(r, 'returncode', 1) == 0:
            reboot_reasons.append('a winsock reset csak újraindítás után lép életbe')
        else:
            problems.append('a winsock reset nem sikerült')

    log('▶️ Szolgáltatások visszaindítása (frissítés-keresést NEM indítunk)...')
    names = list(WU_REPAIR_START_ORDER) + [d for d in stop['deps'] if d not in WU_REPAIR_START_ORDER]
    ps = WU_REPAIR_START_PS.replace('__SERVICES__', _ps_list(names))
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
              timeout=30 * len(names) + 60, encoding='utf-8')
    start = parse_service_lines(getattr(res, 'stdout', '') or '')
    logging.info(f"[WU_REPAIR] Indítás: {start['start']} | hibák: {start['errors']}")
    for n in names:
        st = start['start'].get(n)
        if st == 'Running':
            continue
        if st == 'DISABLED':
            msg = f'a(z) {n} szolgáltatás LE VAN TILTVA (services.msc) - a program nem kapcsolja vissza'
        elif st is None:
            msg = f'a(z) {n} szolgáltatás állapota nem ellenőrizhető'
        else:
            msg = f'a(z) {n} szolgáltatás nem indult el (állapot: {st})'
        problems.append(msg)
        log(f'  ⚠️ {msg}')
    services_ok = not problems
    if services_ok:
        log('  ✅ A szolgáltatások futnak.')
    return {'services_ok': services_ok, 'problems': problems, 'cache': cache,
            'scheduled': scheduled, 'reboot_reasons': reboot_reasons}


def _parse_iso(val):
    try:
        return datetime.strptime(val, _ISO_FMT)
    except (TypeError, ValueError):
        return None


def read_effective_state():
    """A Windows Update-vezénylő TÉNYLEGES állapota (UpdatePolicy\\PolicyState), vagy None,
    ha nem olvasható (régebbi Windows / hiányzó kulcs) - ilyenkor nem ellenőrizhető, és ezt
    a hívó ki is mondja. {'paused_until': ISO|None, 'exclude_drivers': bool|None}"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _POLICY_STATE_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            def q(n):
                try:
                    return winreg.QueryValueEx(k, n)[0]
                except OSError:
                    return None
            paused, end, excl = q('QualityUpdatesPaused'), q('PauseQualityUpdatesEndTime'), q('ExcludeWUDrivers')
    except OSError as e:
        logging.info(f"[WU_STATUS] PolicyState nem olvasható ({e}) - a tényleges állapot nem ellenőrizhető.")
        return None
    paused_until = None
    if paused == 1:
        dt = _parse_iso(end)
        if dt is None or dt > datetime.now(timezone.utc).replace(tzinfo=None):
            paused_until = end or '?'
    st = {'paused_until': paused_until, 'exclude_drivers': (excl == 1) if excl is not None else None}
    logging.debug(f"[WU_STATUS] PolicyState: QualityUpdatesPaused={paused}, End={end}, ExcludeWUDrivers={excl}")
    return st


def desired_state():
    """Amit a program (vagy a felhasználó) BEÁLLÍTOTT: a UX\\Settings szünete és a házirend."""
    return {'paused_until': wu_paused_until(), 'exclude_drivers': _driver_policy_state()[0]}


def _day(v):
    return (v or '')[:10] or None


def state_mismatches(desired, effective):
    """Ahol a Windows tényleges állapota eltér a beállítottól (emberi szöveggel). Nem
    olvasható tényleges állapotnál üres lista (a hívó külön jelzi, hogy nem ellenőrizhető)."""
    if effective is None:
        return []
    out = []
    d, e = _day(desired.get('paused_until')), _day(effective.get('paused_until'))
    if d != e:
        if d and not e:
            out.append(f'a szünet ({d}-ig) be van állítva, de a Windows még nem vette át')
        elif e and not d:
            out.append(f'a Windows még szüneteltetve tartja ({e}-ig), pedig a szünet nincs beállítva')
        else:
            out.append(f'a Windows még a régi szünetet tartja ({e}-ig), a beállított: {d}-ig')
    ex = effective.get('exclude_drivers')
    if ex is not None and ex != bool(desired.get('exclude_drivers')):
        out.append('a driver-letiltást a Windows még nem vette át' if desired.get('exclude_drivers')
                   else 'a Windows még tiltja a drivereket, pedig engedélyezve vannak')
    return out


def pause_end_for(days, extend):
    """A szünet új lejárata (UTC, naive). extend=True: egy még érvényes szünethez adódik
    (a „+1 hét” gomb halmozható); különben mostantól számol."""
    days = int(days)
    now = datetime.now(timezone.utc).replace(tzinfo=None).replace(microsecond=0)
    base = now
    if extend:
        cur = _parse_iso(wu_paused_until())
        if cur and cur > now:
            base = cur
    from datetime import timedelta
    return base + timedelta(days=days)


def write_pause_until(end_dt):
    """A szünet beírása a UX\\Settings alá (ugyanazok az értékek, amiket a Beállítások
    alkalmazás ír), visszaolvasva. Visszatérés: a lejárat ISO-ban, vagy None (bukás)."""
    end = end_dt.strftime(_ISO_FMT)
    start = datetime.now(timezone.utc).strftime(_ISO_FMT)
    logging.info(f"[WU_SETTINGS] Szünet beírása: {start} -> {end}")
    try:
        for n in ('PauseUpdatesExpiryTime', 'PauseFeatureUpdatesEndTime', 'PauseQualityUpdatesEndTime'):
            _reg_write(_UX_SETTINGS_KEY, n, end, winreg.REG_SZ)
        for n in ('PauseUpdatesStartTime', 'PauseFeatureUpdatesStartTime', 'PauseQualityUpdatesStartTime'):
            _reg_write(_UX_SETTINGS_KEY, n, start, winreg.REG_SZ)
    except OSError as e:
        logging.error(f"[WU_SETTINGS] A szünet beírása SIKERTELEN: {e}")
        return None
    if wu_paused_until() != end:
        logging.error(f"[WU_SETTINGS] A szünet visszaolvasva nem egyezik (várt: {end}, olvasott: {wu_paused_until()}).")
        return None
    return end


def remove_pause():
    """A szünet törlése a UX\\Settings alól, visszaolvasva. Igaz, ha nincs érvényes szünet."""
    for n in WU_PAUSE_VALUE_NAMES:
        try:
            _reg_delete(_UX_SETTINGS_KEY, n)
        except OSError as e:
            logging.error(f"[WU_SETTINGS] {n} törlése SIKERTELEN: {e}")
    left = wu_paused_until()
    if left:
        logging.error(f"[WU_SETTINGS] A szünet törlése után még érvényes: {left}")
    return left is None


def describe_state(desired, effective):
    """Rövid emberi összefoglaló a TÉNYLEGES állapotról (ha olvasható), különben a beállítottról."""
    src = effective if effective is not None else desired
    pu = _day(src.get('paused_until'))
    ex = src.get('exclude_drivers')
    if ex is None:
        ex = desired.get('exclude_drivers')
    pause = f'szüneteltetve {pu}-ig' if pu else 'NINCS szüneteltetve'
    drv = 'driver-frissítés LETILTVA' if ex else 'driver-frissítés engedélyezve'
    return f"{pause} · {drv}" + ('' if effective is not None else ' (a Windows saját állapota nem olvasható)')


# --- Újraindítás utáni befejezés (a feladat a következő bejelentkezéskor indítja a programot) ---
WU_PENDING_FILE = 'wu_pending.json'


def _pending_path():
    from app.common import _app_data_dir
    return os.path.join(_app_data_dir(), WU_PENDING_FILE)


def save_pending(action, desired):
    """A befejezésre váró művelet: mit kért a technikus, és mikor (a falióra-idő az
    újraindulás felismeréséhez kell: bootidő = most - uptime)."""
    import json
    data = {'action': action, 'paused_until': desired.get('paused_until'),
            'exclude_drivers': bool(desired.get('exclude_drivers')), 'saved_at': time.time()}
    p = _pending_path()
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    logging.info(f"[WU_PENDING] Befejezésre vár újraindítás után: {data}")


def load_pending():
    import json
    try:
        with open(_pending_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logging.error(f"[WU_PENDING] A befejezésre váró WU-művelet olvashatatlan ({e}) - eldobva.")
        clear_pending()
        return None


def clear_pending():
    try:
        os.remove(_pending_path())
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.error(f"[WU_PENDING] A befejezés-jelző nem törölhető: {e}")


def rebooted_since(saved_at, now=None, uptime=None):
    """Igaz, ha a mentés óta újraindult a gép (a mostani boot ideje későbbi a mentésnél).
    Ismeretlen uptime-nál None (nem eldönthető)."""
    if uptime is None:
        from app.win32 import system_uptime_seconds
        uptime = system_uptime_seconds()
    if uptime is None or not saved_at:
        return None
    now = time.time() if now is None else now
    return (now - uptime) > float(saved_at)


def reassert_pending(run, pending, log):
    """Újraindítás után: ha a beállítás közben elveszett (pl. a Windows kitörölte), újra
    beírjuk pontosan azt, amit a technikus kért. Igaz, ha a beállítás a helyén van."""
    ok = True
    want = pending.get('paused_until')
    have = wu_paused_until()
    if want and have != want:
        dt = _parse_iso(want)
        if dt and dt > datetime.now(timezone.utc).replace(tzinfo=None):
            log(f'  ↻ A szünet beállítása elveszett - újra beírva ({want[:10]}-ig).')
            ok = write_pause_until(dt) is not None and ok
    elif not want and have:
        log('  ↻ A szünet újra megjelent - újra feloldva.')
        ok = remove_pause() and ok
    if bool(pending.get('exclude_drivers')) != _driver_policy_state()[0]:
        log('  ↻ A driver-beállítás eltért - újra beírva.')
        ok = set_wu_driver_policy(run, disabled=bool(pending.get('exclude_drivers'))) and ok
    return ok


def resume_wu(run, log):
    """A WU szüneteltetés feloldása (registry + wuauserv restart + COM Resume)."""
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", WU_PAUSE_REMOVE_PS + """
Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
Start-Service wuauserv -ErrorAction SilentlyContinue

try { (New-Object -ComObject Microsoft.Update.AutoUpdate).Resume() | Out-Null } catch {}
"""])
    left = wu_paused_until()        # a verdikt a visszaolvasás
    if left:
        logging.error(f"[WU_SETTINGS] A szüneteltetés feloldása után a szünet még érvényes ({left}).")
        log(f'⚠️ A szüneteltetés NEM oldódott fel (a registry szerint {left}-ig érvényes) - a Beállítások > Windows Update alatt kézzel folytatható.')
        return False
    log('✅ Szüneteltetés feloldva!')
    return True
