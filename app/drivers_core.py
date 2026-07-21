"""Driver listázás/törlés - KÖZÖS mag (GUI + CLI, online + offline).

A `dism /Get-Drivers` szöveg-parzolás korábban 4 közel azonos példányban élt
(GUI online/offline az app/gui/drivers.py-ban, CLI online/offline az
app/cli/drivers.py-ban) - most EGY példányban itt. Ugyanígy a "phantom csomag"
szűrés (force-delete-elt bejegyzések), az egy-driveres törlés és az agresszív
force-törlés fallback is.

FONTOS (CLAUDE.md): a dism hívásoknak /English-sel KELL futniuk, mert a lenti
angol kulcsszavas parzolás ("Published Name" stb.) más nyelvű Windows/WinPE
alatt különben némán üres listát ad. A driver-verzió a valódi kimenetben önálló
"Version :" sor (NEM "Date and Version").
"""

# === AUTO-IMPORTS ===
import os
import shutil
import json
import glob
# === /AUTO-IMPORTS ===


# EGY driver-csomag törlésének felső korlátja (mp). A pnputil a PnP query-remove-ra vár;
# egy nem válaszoló eszközverem (terepen: Intel RST tárolóvezérlő, 143 mp) timeout nélkül
# az egész törlő ciklust - GUI-ban az egész AutoFix lábat - megakasztja.
DELETE_DRIVER_TIMEOUT = 180


def parse_dism_driver_list(stdout):
    """`dism /English ... /Get-Drivers` kimenet -> driver-dict lista
    ({'published','original','provider','class','version'} kulcsokkal)."""
    drivers = []
    current = {}
    for line in (stdout or '').splitlines():
        line = line.strip()
        if not line:
            if current and "published" in current:
                drivers.append(current)
                current = {}
            continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            key, val = parts[0].strip(), parts[1].strip()
            if "Published Name" in key:
                current["published"] = val
            elif "Original File Name" in key:
                current["original"] = val
            elif "Provider Name" in key:
                current["provider"] = val
            elif "Class Name" in key:
                current["class"] = val
            elif "Version" in key:
                current["version"] = val
    if current and "published" in current:
        drivers.append(current)
    return drivers


def _file_repository_path(target_os_path=None):
    """A DriverStore FileRepository útvonala (online: futó rendszer, offline: cél-OS)."""
    if target_os_path:
        return os.path.join(target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
    return os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")


def _inf_dir_path(target_os_path=None):
    """A Windows\\INF mappa útvonala (online/offline)."""
    if target_os_path:
        return os.path.join(target_os_path, "Windows", "INF")
    return os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "INF")


def filter_phantom_packages(drivers, target_os_path=None):
    """Szellem (korábban force-delete-elt) csomagok kiszűrése: egy nem-oem publikált
    nevű bejegyzés csak akkor valódi, ha még van hozzá tartozó mappa a DriverStore
    FileRepository-jában."""
    rep = _file_repository_path(target_os_path)
    valid_drivers = []
    for d in drivers:
        pub = d.get("published", "")
        if not pub:
            continue
        if pub.lower().startswith("oem"):
            valid_drivers.append(d)
            continue
        if glob.glob(os.path.join(rep, f"{pub}_*")):
            valid_drivers.append(d)
    return valid_drivers


def get_third_party_drivers(run):
    """Third-party driverek listája (online). /English: kényszerített angol DISM
    kimenet, függetlenül a Windows nyelvi beállításától."""
    res = run(['dism', '/English', '/Online', '/Get-Drivers'])
    return parse_dism_driver_list(res.stdout)


def get_all_drivers(run):
    """Összes driver listája (online, veszélyes mód). JSON-hiba esetén kivételt dob -
    a hívó dönt (a GUI hibát jelenít meg, a CLI üres listával folytat)."""
    cmd = ['powershell', '-NoProfile', '-Command',
           '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-WindowsDriver -Online -All | Select-Object ProviderName, ClassName, Version, Driver, OriginalFileName | ConvertTo-Json -Depth 2 -WarningAction SilentlyContinue']
    res = run(cmd, encoding='utf-8')
    out = (res.stdout or '').strip()
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    parsed_drivers = [{"published": d.get("Driver", ""), "original": d.get("OriginalFileName", ""),
                       "provider": d.get("ProviderName", ""), "class": d.get("ClassName", ""),
                       "version": d.get("Version", "")} for d in data]
    return filter_phantom_packages(parsed_drivers)


def get_offline_drivers(run, target_os_path, all_drivers=False):
    """Offline cél-OS drivereinek listája (dism /Image:...)."""
    cmd = ['dism', '/English', f'/Image:{target_os_path}', '/Get-Drivers']
    if all_drivers:
        cmd.append('/all')
    res = run(cmd)
    drivers = parse_dism_driver_list(res.stdout)
    return filter_phantom_packages(drivers, target_os_path)


def delete_succeeded(res):
    """Egy driver-törlő parancs eredményéből eldönti, sikeres volt-e (a returncode
    mellett a lokalizált sikerszövegeket is elfogadja, kis/nagybetű-függetlenül)."""
    if res.returncode == 0:
        return True
    out = (res.stdout or '').lower()
    return any(k in out for k in ('deleted', 'törölve', 'successfully'))


def delete_driver_package(run, pub, target_os_path=None, timeout=None):
    """Egy driver-csomag törlése (online: pnputil, offline: dism). A nyers eredményt
    adja vissza - a sikert a hívó delete_succeeded()-del dönti el.

    timeout (mp): felső korlát EGY csomag törlésére. Kell, mert a pnputil a PnP
    query-remove-ra vár, és egy nem válaszoló eszközverem (terepen: Intel RST
    tárolóvezérlő) percekig lógatja - timeout nélkül ez az egész AutoFix lábat
    megakasztja. Időtúllépéskor a _run CMD_TIMEOUT_RETURNCODE-dal tér vissza."""
    if target_os_path:
        return run(['dism', f'/Image:{target_os_path}', '/Remove-Driver', f'/Driver:{pub}'], timeout=timeout)
    # ok_codes 3010: siker, de reboot kell a lezáráshoz - a delete_succeeded sikeresnek veszi.
    return run(['pnputil', '/delete-driver', pub, '/uninstall', '/force'], ok_codes=(0, 3010), timeout=timeout)


def force_delete_driver_files(run, pub, target_os_path=None):
    """Agresszív force-törlés fallback (takeown/icacls/rmtree a FileRepository +
    Windows\\INF alól). CSAK "összes driver" módban, nem-oem csomagra hívható -
    third-party nézetben egy sikertelen törlés egyszerűen sikertelen marad.
    Visszatérési érték: talált-e (és törölt-e) bármit."""
    rep = _file_repository_path(target_os_path)
    inf_dir = _inf_dir_path(target_os_path)
    found_any = False

    dirs = glob.glob(os.path.join(rep, f"{pub}_*"))
    if dirs:
        for d in dirs:
            run(f'takeown /f "{d}" /r /A', shell=True)
            run(f'icacls "{d}" /grant *S-1-5-32-544:F /t', shell=True)
            shutil.rmtree(d, ignore_errors=True)
            run(f'rmdir /s /q "{d}"', shell=True)
        found_any = True

    bname = os.path.splitext(pub)[0]
    for ext in ['.inf', '.pnf', '.INF', '.PNF']:
        fpath = os.path.join(inf_dir, bname + ext)
        if os.path.exists(fpath):
            run(f'takeown /f "{fpath}" /A', shell=True)
            run(f'icacls "{fpath}" /grant *S-1-5-32-544:F', shell=True)
            try:
                os.remove(fpath)
                found_any = True
            except OSError:
                run(f'del /f /q "{fpath}"', shell=True)
                found_any = True
    return found_any
