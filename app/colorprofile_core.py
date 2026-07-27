"""Színprofil-visszaállítás GYÁRI (Windows-alapértelmezett) állapotra - KÖZÖS mag
(GUI AutoFix + CLI AutoFix).

MIÉRT AZ AUTOFIX RÉSZE (explicit user decision, 2026-07-27): a szervizbe kerülő gépeken
a színprofil az egyik leggyakoribb "elrontott, de senki nem tudja mitől" beállítás -
kalibráló programok, gyártói segédeszközök és játékok is beleírnak, az eredmény meg egy
sárga/lila/túl sötét kép, amit a felhasználó a monitorra vagy a videokártyára fog. Az
1 kattintásos fix célja a gyári alapállapot, ezért a színkezelés is oda áll vissza.

MIT CSINÁL - és mit NEM:
  - TÖRLI a MONITOROKHOZ rendelt egyedi ICC-profil hozzárendeléseket (HKCU + HKLM
    ICM\\ProfileAssociations\\Display). A monitor ezután a saját alap-profilját kapja,
    amit a monitor INF-je (vagy az EDID) ad - ez a "gyári" állapot;
  - KIKAPCSOLJA a Windows kijelző-kalibráció kezelését (CalibrationManagementEnabled=0),
    ami a bejelentkezéskor betöltött egyedi gamma-görbét adja vissza;
  - a betöltött gamma-rámpa a következő indulásig élhet - az AutoFix úgyis ÚJRAINDÍT
    közvetlenül ezután, tehát a hatás a felhasználó szemével is azonnal látszik.

  - NEM törli magukat az ICC-fájlokat a `spool\\drivers\\color` mappából. Az a Windows
    saját sRGB-profiljait is elvinné, és a nyomtatók/szkennerek színkezelését is (a
    nyomtatóvédelem pont ez ellen van). Aki a fájlokat is takarítaná, annak ott a
    "Temp Fájlok Törlése" nézet opt-in 'color_profiles' kategóriája;
  - NEM nyúl a NYOMTATÓ-profil hozzárendelésekhez (csak a Display ág): egy ügyfél
    kalibrált nyomtatója visszaállíthatatlan kár lenne, és a fix célja a kijelző.

A PowerShell soronkénti protokollt ír (ASSOC:/CALIB:/ERROR:/DONE), amit a
`parse_color_line` értelmez - ugyanaz a minta, mint a ghost_core-nál.
"""

# === AUTO-IMPORTS ===
import logging
# === /AUTO-IMPORTS ===


# A színkezelés két registry-lába. A Display ág ALATT gyártó/monitor GUID-onként ülnek
# a hozzárendelések; az egész ágat töröljük, mert a Windows üres ágból is helyreáll.
_ICM_BASE = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM'

RESET_COLOR_PROFILES_PS = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
$assoc = 0

# 1) MONITOR-profil hozzárendelések (per-felhasználó ÉS gépszintű). A Display ágat
#    töröljük, a Printer ágat SZÁNDÉKOSAN nem (lásd a modul docstringjét).
foreach ($root in @('HKCU:', 'HKLM:')) {
    $p = "$root\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM\ProfileAssociations\Display"
    if (Test-Path $p) {
        try {
            $n = @(Get-ChildItem -Path $p -Recurse -ErrorAction SilentlyContinue).Count
            Remove-Item -Path $p -Recurse -Force -ErrorAction Stop
            $assoc += $n
            Write-Output "ASSOC:$root|$n"
        } catch {
            Write-Output "ERROR:$root profil-hozzarendeles torles: $($_.Exception.Message)"
        }
    } else {
        Write-Output "ASSOC:$root|0"
    }
}

# 2) Windows kijelzo-kalibracio kezelesenek kikapcsolasa (ez tolti be a bejelentkezeskor
#    az egyedi gamma-gorbet). 0 = alapertelmezett/gyari allapot.
try {
    $cal = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM\Calibration'
    if (!(Test-Path $cal)) { New-Item -Path $cal -Force | Out-Null }
    Set-ItemProperty -Path $cal -Name 'CalibrationManagementEnabled' -Value 0 -Type DWord -ErrorAction Stop
    Write-Output "CALIB:ok"
} catch {
    Write-Output "ERROR:kalibracio-kezeles kikapcsolas: $($_.Exception.Message)"
}

Write-Output "DONE:$assoc"
'''


def parse_color_line(line):
    """A reset-script egy kimeneti sora -> (esemény, adat) vagy None.

    Események: 'assoc' (str: "HKCU:|3"), 'calib' (None), 'error' (str), 'done' (int)."""
    s = (line or '').strip()
    if not s:
        return None
    if s.startswith('ASSOC:'):
        return ('assoc', s[6:].strip())
    if s.startswith('CALIB:'):
        return ('calib', None)
    if s.startswith('ERROR:'):
        return ('error', s[6:].strip())
    if s.startswith('DONE:'):
        try:
            return ('done', int(s[5:].strip()))
        except ValueError:
            return ('done', 0)
    return None


def reset_color_profiles(run, log=None):
    """Színprofilok visszaállítása gyári alapállapotra. `log` egy egysoros callback
    (GUI: task_progress-t kibocsátó lambda, CLI: print) - lehet None.

    Visszatérés: (sikerült-e egyáltalán lefutni: bool, törölt hozzárendelések száma: int).
    Hibát NEM dob: a színprofil sosem lehet ok arra, hogy az AutoFix lánc megálljon.

    Módszer-megjegyzés: az egész scriptet EGY `run` hívásban futtatjuk (nem Popen-nel),
    mert a művelet másodperc alatt lefut és nincs értelmes köztes állapota - a
    `run` viszont magától naplózza a parancsot és a kimenetet (CLAUDE.md: minden
    subprocess hagyjon nyomot)."""
    def _say(msg):
        if log:
            try:
                log(msg)
            except Exception as e:
                logging.debug(f"[COLOR] log callback hiba: {e}")

    logging.info("[COLOR] Színprofilok visszaállítása gyári alapállapotra - indul.")
    _say('🎨 Színprofilok visszaállítása gyári alapállapotra...')
    try:
        res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                   RESET_COLOR_PROFILES_PS], timeout=120)
    except Exception as e:
        logging.warning(f"[COLOR] A színprofil-visszaállítás nem futott le: {e}")
        _say(f'⚠️ A színprofilok visszaállítása nem sikerült (nem kritikus): {e}')
        return False, 0

    removed = 0
    errors = []
    calib_ok = False
    for line in (getattr(res, 'stdout', '') or '').splitlines():
        parsed = parse_color_line(line)
        if not parsed:
            continue
        event, data = parsed
        if event == 'assoc':
            # Minden ág NEVESÍTVE a logba: a "hova tűnt a kalibrációm?" kérdésre ez a válasz.
            logging.info(f"[COLOR] Monitor profil-hozzárendelések törölve: {data}")
        elif event == 'calib':
            calib_ok = True
            logging.info("[COLOR] Windows kijelző-kalibráció kezelése kikapcsolva (CalibrationManagementEnabled=0).")
        elif event == 'error':
            errors.append(data)
            logging.warning(f"[COLOR] Részhiba a színprofil-visszaállításban: {data}")
        elif event == 'done':
            removed = data

    if errors:
        _say(f'⚠️ A színprofilok visszaállítása részben sikerült ({len(errors)} részhiba) - a folyamat megy tovább.')
    elif removed:
        _say(f'✅ Színprofilok gyári alapállapotra állítva ({removed} egyedi monitor-hozzárendelés törölve).')
    else:
        _say('✅ Színprofilok ellenőrizve - nem volt egyedi (kalibrált) monitor-profil.')
    if calib_ok:
        _say('   A Windows kijelző-kalibráció kikapcsolva; a kép az újraindulás után a monitor gyári profilját használja.')

    logging.info(f"[COLOR] Kész - {removed} hozzárendelés törölve, kalibráció-kikapcsolás: {calib_ok}, "
                 f"részhibák: {len(errors)}.")
    return True, removed
