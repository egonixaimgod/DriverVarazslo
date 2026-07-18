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
    """A WU driver-frissítés állapotának nyers tényei - a megjelenítendő szöveget a
    hívó formázza. Visszatérés: dict(policy_disabled, search_disabled,
    service_disabled, paused_until [ISO string vagy None])."""
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

    service_disabled = False
    try:
        res = run(['powershell', '-NoProfile', '-Command', '(Get-Service wuauserv).StartType'], encoding='utf-8')
        if res.stdout and 'Disabled' in res.stdout:
            service_disabled = True
    except Exception as e:
        logging.debug(f"[WU] wuauserv StartType lekérdezése sikertelen: {e}")

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

    return {'policy_disabled': policy_disabled, 'search_disabled': search_disabled,
            'service_disabled': service_disabled, 'paused_until': paused_until}


def set_wu_driver_policy(run, disabled):
    """A WU driver-letöltést vezérlő két registry-érték beállítása.
    disabled=True: SearchOrderConfig=0 + ExcludeWUDriversInQualityUpdate=1;
    disabled=False: SearchOrderConfig=1 + a policy-érték törlése."""
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


def set_wu_pause(run, pause=True):
    """Egyszerű pause be/ki a driver-policy értékekkel együtt (az AutoFix Leg A
    használja): pause=True fix 7 napos szünet mostantól + driver-tiltás; pause=False
    a szünet feloldása + driver-engedélyezés + wuauserv restart."""
    if pause:
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", build_wu_pause_ps(7, additive=False)])
        set_wu_driver_policy(run, disabled=True)
    else:
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", WU_PAUSE_REMOVE_PS + """
Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
Start-Service wuauserv -ErrorAction SilentlyContinue
"""])
        set_wu_driver_policy(run, disabled=False)


def _clear_software_distribution(run, retries=4):
    """A SoftwareDistribution mappa törlése újrapróbálásokkal, végső PS fallbackkal.
    Visszatérés: sikerült-e Python-oldalról törölni (a PS fallback után is lehet kész)."""
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
    return deleted


def _stop_wu_services_and_clear_reboot_flag(run, log):
    """WU szolgáltatások leállítása + a beragadt RebootRequired jelzés törlése."""
    log('Szolgáltatások leállítása és újraindítási jelzések (Pending Reboot) törlése...')
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", WU_STOP_SERVICES_PS])
    time.sleep(2)
    # ok_codes=(0, 1): az 1-es kód a "kulcs nem létezik" - nincs beragadt reboot-jelzés, várt eset.
    run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'], ok_codes=(0, 1))


def disable_wu_full(run, log):
    """WU driver-frissítések teljes letiltása: policy + szolgáltatás-leállítás +
    RebootRequired törlés + SoftwareDistribution cache ürítés + wuauserv újraindítás."""
    log('Windows automata driver frissítések letiltása a Registryben...')
    set_wu_driver_policy(run, disabled=True)
    _stop_wu_services_and_clear_reboot_flag(run, log)
    log('Beragadt frissítések és WU gyorsítótár (SoftwareDistribution) ürítése...')
    _clear_software_distribution(run)
    log('✅ Gyorsítótár törölve.')
    run('net start wuauserv', shell=True)
    log('✅ WU szolgáltatás újraindítva')


def _start_services_with_retry(run, services, attempts=3):
    """Szolgáltatások indítása újrapróbálásokkal ('already running' is siker)."""
    for svc in services:
        for _ in range(attempts):
            res = run(f'net start {svc}', shell=True)
            if res.returncode == 0 or 'already' in ((res.stdout or '') + (res.stderr or '')).lower():
                break
            time.sleep(3)


def enable_wu_reset(run, log):
    """WU driver-frissítések engedélyezése + teljes WU-komponens reset (pause-feloldás,
    policy törlés, SoftwareDistribution + catroot2, DLL-újraregisztrálás, winsock)."""
    log('Szüneteltetés (Pause) feloldása...')
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", WU_PAUSE_REMOVE_PS])
    log('✅ Szüneteltetés törölve')

    # Policy törlés
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, "ExcludeWUDriversInQualityUpdate")
        logging.info("[WU_ENABLE] ExcludeWUDrivers policy törölve")
        log('✅ ExcludeWUDrivers policy törölve')
    except FileNotFoundError:
        logging.debug("[WU_ENABLE] Policy nem létezett")
        log('  Policy nem létezett')
    except Exception as e:
        logging.warning(f"[WU_ENABLE] Policy törlés hiba: {e}")
        log(f'⚠ {e}')

    # SearchOrderConfig = 1
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 1)
        logging.info("[WU_ENABLE] SearchOrderConfig = 1")
        log('✅ SearchOrderConfig = 1')
    except Exception as e:
        logging.warning(f"[WU_ENABLE] SearchOrderConfig hiba: {e}")
        log(f'⚠ {e}')

    run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
         '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])

    # Szolgáltatások leállítása
    logging.info("[WU_ENABLE] Szolgáltatások leállítása...")
    for svc in ['wuauserv', 'bits', 'cryptsvc']:
        run(f'net stop {svc} /y', shell=True)
    time.sleep(2)

    # SoftwareDistribution törlés
    sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
    log('SoftwareDistribution törlése...')
    if _clear_software_distribution(run, retries=3):
        log('  ✅ Törölve')

    # catroot2 átnevezés - enélkül a WU komponensraktár korrupciója gyakran nem javul,
    # csak a friss frissítés-cache törlésétől.
    catroot2 = os.path.join(sysroot, 'System32', 'catroot2')
    bak = catroot2 + '.bak'
    try:
        if os.path.exists(bak):
            shutil.rmtree(bak, ignore_errors=True)
        if os.path.exists(catroot2):
            os.rename(catroot2, bak)
            logging.info("[WU_ENABLE] catroot2 átnevezve")
            log('✅ catroot2 átnevezve')
    except Exception as e:
        logging.warning(f"[WU_ENABLE] catroot2 hiba: {e}")
        log(f'⚠ catroot2: {e}')

    # WU DLL-ek újraregisztrálása
    logging.info("[WU_ENABLE] WU DLL-ek újraregisztrálása...")
    sys32 = os.path.join(sysroot, 'System32')
    for dll in ['wuaueng.dll', 'wuapi.dll', 'wups.dll', 'wups2.dll', 'wuwebv.dll', 'wucltux.dll']:
        fp = os.path.join(sys32, dll)
        if os.path.exists(fp):
            run(f'regsvr32.exe /s "{fp}"', shell=True)
    log('✅ WU DLL-ek újraregisztrálva')

    # Winsock reset
    logging.info("[WU_ENABLE] Winsock reset...")
    run('netsh winsock reset', shell=True)

    # Szolgáltatások indítása
    logging.info("[WU_ENABLE] Szolgáltatások indítása...")
    _start_services_with_retry(run, ['cryptsvc', 'bits', 'wuauserv'])

    run('wuauclt.exe /resetauthorization /detectnow', shell=True)
    run('UsoClient.exe StartScan', shell=True)
    log('✅ Frissítés-keresés elindítva')


def restart_wu_services(run, log):
    """WU szolgáltatások teljes újraindítása + frissítés-keresés indítása."""
    logging.info("[WU_RESTART] Szolgáltatások leállítása...")
    for svc in ['wuauserv', 'bits', 'cryptsvc', 'msiserver']:
        run(f'net stop {svc} /y', shell=True)
        log(f'  stop {svc}')
    time.sleep(2)
    logging.info("[WU_RESTART] Szolgáltatások indítása...")
    for svc in ['rpcss', 'cryptsvc', 'bits', 'msiserver', 'wuauserv']:
        _start_services_with_retry(run, [svc])
        log(f'  start {svc}')
    run('wuauclt.exe /resetauthorization /detectnow', shell=True)
    run('UsoClient.exe StartScan', shell=True)
    log('✅ Frissítés-keresés elindítva')


def pause_wu(run, log, days):
    """WU szüneteltetése N napra (hosszabbító logikával) + a beragadt frissítések és a
    WU cache ürítése. Visszatérés: az új lejárati dátum (a script kimenete)."""
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", build_wu_pause_ps(days, additive=True)], encoding='utf-8')
    new_date = (res.stdout or '').strip()

    _stop_wu_services_and_clear_reboot_flag(run, log)

    log('Beragadt frissítések és WU gyorsítótár ürítése...')
    _clear_software_distribution(run)
    run('net start wuauserv', shell=True)
    return new_date


def resume_wu(run, log):
    """A WU szüneteltetés feloldása (registry + wuauserv restart + COM Resume)."""
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", WU_PAUSE_REMOVE_PS + """
Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
Start-Service wuauserv -ErrorAction SilentlyContinue

try { (New-Object -ComObject Microsoft.Update.AutoUpdate).Resume() | Out-Null } catch {}
"""])
    log('✅ Szüneteltetés feloldva!')
