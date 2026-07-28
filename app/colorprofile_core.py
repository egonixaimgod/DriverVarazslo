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
    amit a monitor INF-je (vagy az EDID) ad - ez a "gyári" állapot. Ez a művelet LÉNYEGE
    és teljes tartalma: a felrakott profilok leszedése, semmi több;
  - MINDEN törölt hozzárendelést NÉVVEL naplóz a törlés ELŐTT (lásd lentebb, miért);
  - a betöltött gamma-rámpa a következő indulásig élhet - az AutoFix úgyis ÚJRAINDÍT
    közvetlenül ezután, tehát a hatás a felhasználó szemével is azonnal látszik.

  - NEM törli magukat az ICC-fájlokat a `spool\\drivers\\color` mappából. Az a Windows
    saját sRGB-profiljait is elvinné, és a nyomtatók/szkennerek színkezelését is (a
    nyomtatóvédelem pont ez ellen van). Aki a fájlokat is takarítaná, annak ott a
    "Temp Fájlok Törlése" nézet opt-in 'color_profiles' kategóriája;
  - NEM nyúl a NYOMTATÓ-profil hozzárendelésekhez (csak a Display ág): egy ügyfél
    kalibrált nyomtatója visszaállíthatatlan kár lenne, és a fix célja a kijelző;
  - NEM KAPCSOLJA KI a Windows kijelző-kalibráció kezelését. Ez korábban benne volt
    (`CalibrationManagementEnabled=0`), és HIBA VOLT - lásd a következő bekezdést.

MIÉRT NEM NYÚLUNK A CalibrationManagementEnabled ÉRTÉKHEZ (2026-07-28, terepen bizonyított,
explicit user decision):
Ez a Color Management > Speciális fül "Windows-kijelzőkalibráció használata" kapcsolója,
és ez vezérli, hogy a Windows betölti-e a profilhoz tartozó gamma-görbét (VCGT). Nullázva
a gépen ezután BÁRMILYEN ICC-profilt lehet társítani, a gamma akkor sem töltődik be - azaz
elvettük a felhasználótól a visszaállítás lehetőségét is, nem csak a rossz kalibrációt.
Terepi eset: TN panel, no-name monitor (MONITOR\\RGT1352, generikus monitor.inf, a
katalógusban NINCS hozzá gyári csomag). A fix törölte a 4 hozzárendelést ÉS kikapcsolta a
betöltőt; a panel korrekció nélkül maradt, a fehér kiégett, a világosszürke árnyalatok
eltűntek (a Google keresősávja nem látszott a fehér háttéren). A kapcsoló nullázása miatt
egy profil visszatársítása sem segített volna - csak a `dccw` varázsló, ami maga is
visszaállítja a kapcsolót 1-re.
A profil-hozzárendelés törlése ÖNMAGÁBAN eléri a célt: társított profil híján nincs mit
betölteni, a rámpa lineáris lesz, a kép a panel gyári állapotát mutatja. A kapcsoló
átállítása ehhez nem kell, viszont maradandó és nehezen kideríthető kárt okoz.
Az értéket ezért csak OLVASSUK, és ha ki van kapcsolva, SZÓLUNK róla (egy fielded gépen
lehet, hogy pont egy korábbi buildünk kapcsolta ki).

A PowerShell soronkénti protokollt ír (PROF:/ASSOC:/CALIB:/ERROR:/DONE), amit a
`parse_color_line` értelmez - ugyanaz a minta, mint a ghost_core-nál.
"""

# === AUTO-IMPORTS ===
import logging
# === /AUTO-IMPORTS ===


# A színkezelés két registry-lába. A Display ág ALATT gyártó/monitor GUID-onként ülnek
# a hozzárendelések; az egész ágat töröljük, mert a Windows üres ágból is helyreáll.
_ICM_BASE = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM'

# A spool\drivers\color mappából CSAK a monitorhoz rendelt profilfájlokat töröljük, az
# egész mappát SOHA. A mappa tartalma élő gépen mérve: 'sRGB Color Space Profile.icm'
# (a Windows saját alapprofilja - enélkül nincs mihez viszonyítania a színkezelésnek),
# 'RSWOP.icm', a .camp/.cdmp/.gmmp WCS-modellfájlok, és 3 NYOMTATÓ-profil (Canon, Epson).
# Az "üríteni az egészet" tehát nem gyári alapállapotot ad, hanem egy sRGB-referencia
# nélküli gépet + tönkretett nyomtató-színkezelést - utóbbi ellen szól a projekt
# nyomtatóvédelmi szabálya is. A monitor-hozzárendelésekből viszont pontosan tudjuk, mely
# fájlokat rakta fel valaki a KIJELZŐHÖZ: azokat visszük el, és csak azokat.

# A Windows saját, SOHA nem törölhető színfájljai. A .camp/.cdmp/.gmmp kiterjesztés
# mind WCS-modellfájl (a Windows szállítja), ezért azokat kiterjesztés alapján védjük.
PROTECTED_COLOR_FILES = {
    'srgb color space profile.icm',
    'rswop.icm',
}
PROTECTED_COLOR_EXTS = {'.camp', '.cdmp', '.gmmp'}


RESET_COLOR_PROFILES_PS = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
$assoc = 0
$profileFiles = New-Object System.Collections.Generic.HashSet[string]
$colorDir = Join-Path $env:SystemRoot 'System32\spool\drivers\color'
$protectedNames = @('srgb color space profile.icm', 'rswop.icm')
$protectedExts  = @('.camp', '.cdmp', '.gmmp')

# 1) MONITOR-profil hozzárendelések (per-felhasználó ÉS gépszintű). A Display ágat
#    töröljük, a Printer ágat SZÁNDÉKOSAN nem (lásd a modul docstringjét).
foreach ($root in @('HKCU:', 'HKLM:')) {
    $p = "$root\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM\ProfileAssociations\Display"
    if (Test-Path $p) {
        try {
            $keys = @(Get-ChildItem -Path $p -Recurse -ErrorAction SilentlyContinue)
            # A TORLES ELOTT nevesitjuk, mit viszunk el. Enelkul a "hova tunt a
            # kalibraciom?" kerdes a naplobol MEGVALASZOLHATATLAN - egy terepi esetben
            # pontosan ez tortent: 4 "hozzarendeles" tunt el, es utolag semmi nem arulta
            # el, mik voltak.
            #
            # FONTOS: az ag alatt a kulcsok tobbsege URES konyvtar-kulcs
            # (...\Display\{osztaly-GUID}\0000, \0002, ...), a tenyleges profil egy
            # ERTEK ezekben. Elo gepen merve: 8 kulcs, 0 ertek - vagyis SEMMILYEN egyedi
            # profil nem volt tarsitva. A regi kod a KULCSOKAT szamolta, es "8 egyedi
            # monitor-hozzarendeles torolve"-t irt volna ki oda is, ahol nem tortent
            # semmi. A szam ezert az ERTEKEKET szamolja, a kulcsszam kulon megy.
            $vals = 0
            foreach ($k in $keys) {
                $mon = $k.PSChildName
                try {
                    $item = Get-Item -Path $k.PSPath -ErrorAction Stop
                    foreach ($vn in $item.GetValueNames()) {
                        $raw = $item.GetValue($vn)
                        # Az ertek lehet REG_SZ vagy REG_MULTI_SZ (tomb) is, verziofuggo.
                        $parts = @()
                        if ($raw -is [array]) { $parts = $raw } else { $parts = @("$raw") }
                        $val = ($parts -join ' ; ')
                        if ($val.Length -gt 160) { $val = $val.Substring(0, 160) }
                        $shown = $vn; if ([string]::IsNullOrEmpty($shown)) { $shown = '(alapertelmezett)' }
                        Write-Output "PROF:$root|$mon|$shown|$val"
                        $vals++
                        # A hivatkozott .icm/.icc fajlneveket osszegyujtjuk - EZEK a
                        # kijelzohoz "hozzaadott" profilok, csak ezeket toroljuk a
                        # color mappabol.
                        foreach ($piece in $parts) {
                            foreach ($tok in ("$piece" -split "[`r`n`0]")) {
                                $t = $tok.Trim()
                                if ($t -match '\.(icm|icc)$') {
                                    [void]$profileFiles.Add([System.IO.Path]::GetFileName($t))
                                }
                            }
                        }
                    }
                } catch { }
            }
            Remove-Item -Path $p -Recurse -Force -ErrorAction Stop
            $assoc += $vals
            Write-Output "ASSOC:$root|$vals|$($keys.Count)"
        } catch {
            Write-Output "ERROR:$root profil-hozzarendeles torles: $($_.Exception.Message)"
        }
    } else {
        Write-Output "ASSOC:$root|0|0"
    }
}

# 2) A KIJELZOHOZ rendelt profilFAJLOK torlese a color mappabol. CSAK az 1) pontban
#    osszegyujtott, monitorhoz tarsitott nevek - az egesz mappa uritese TILOS (elvinne a
#    Windows sajat sRGB-jet es a nyomtatoprofilokat is, lasd a modul docstringjet).
foreach ($fn in $profileFiles) {
    $lower = $fn.ToLowerInvariant()
    $ext = [System.IO.Path]::GetExtension($lower)
    if ($protectedNames -contains $lower) { Write-Output "KEEP:$fn|windows-alapprofil"; continue }
    if ($protectedExts -contains $ext)    { Write-Output "KEEP:$fn|wcs-modellfajl";    continue }
    $full = Join-Path $colorDir $fn
    if (Test-Path -LiteralPath $full) {
        try {
            Remove-Item -LiteralPath $full -Force -ErrorAction Stop
            Write-Output "FILE:$fn"
        } catch {
            Write-Output "ERROR:profilfajl torles ($fn): $($_.Exception.Message)"
        }
    } else {
        Write-Output "KEEP:$fn|nincs a color mappaban"
    }
}

# 3) A Windows kijelzo-kalibracio kezelese BE (1). Ezt korabban 0-ra allitottuk, ami
#    megakadalyozta BARMELY ICC-profil gamma-gorbejenek betolteset - lasd a docstringet.
#    1-re allitva a gep a takaritas utan kepes uj profilt/kalibraciot fogadni.
try {
    $cal = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM\Calibration'
    $prev = $null
    if (Test-Path $cal) {
        $prev = (Get-ItemProperty -Path $cal -Name 'CalibrationManagementEnabled' -ErrorAction SilentlyContinue).CalibrationManagementEnabled
    } else {
        New-Item -Path $cal -Force | Out-Null
    }
    Set-ItemProperty -Path $cal -Name 'CalibrationManagementEnabled' -Value 1 -Type DWord -ErrorAction Stop
    if ($null -eq $prev) { $prev = 'nincs' }
    Write-Output "CALIB:1|$prev"
} catch {
    Write-Output "ERROR:kalibracio-kezeles bekapcsolas: $($_.Exception.Message)"
    Write-Output "CALIB:unknown|unknown"
}

Write-Output "DONE:$assoc"
'''


def parse_color_line(line):
    """A reset-script egy kimeneti sora -> (esemény, adat) vagy None.

    Események: 'prof' (str: "HKCU:|{GUID}|érték|profil.icm"),
    'assoc' (str: "HKCU:|<profil-érték darab>|<registry-kulcs darab>"),
    'file' (str: törölt profilfájl neve), 'keep' (str: "fájl|ok" - megtartott fájl),
    'calib' (str: "<új érték>|<előző érték>"), 'error' (str), 'done' (int)."""
    s = (line or '').strip()
    if not s:
        return None
    if s.startswith('PROF:'):
        return ('prof', s[5:].strip())
    if s.startswith('ASSOC:'):
        return ('assoc', s[6:].strip())
    if s.startswith('FILE:'):
        return ('file', s[5:].strip())
    if s.startswith('KEEP:'):
        return ('keep', s[5:].strip())
    if s.startswith('CALIB:'):
        return ('calib', s[6:].strip())
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
    calib = None
    calib_prev = None
    files = []
    for line in (getattr(res, 'stdout', '') or '').splitlines():
        parsed = parse_color_line(line)
        if not parsed:
            continue
        event, data = parsed
        if event == 'file':
            files.append(data)
            logging.warning(f"[COLOR] Monitorhoz rendelt profilfájl TÖRÖLVE: {data}")
        elif event == 'keep':
            logging.info(f"[COLOR] Profilfájl megtartva: {data}")
        elif event == 'prof':
            # A "hova tűnt a kalibrációm?" kérdésre EZ a válasz - a törölt hozzárendelés
            # névvel. WARNING szint, mert romboló művelet: a naplóból utólag ki kell
            # derülnie, pontosan mit vittünk el.
            logging.warning(f"[COLOR] Törlendő profil-hozzárendelés: {data}")
        elif event == 'assoc':
            logging.info(f"[COLOR] Monitor profil-hozzárendelések törölve (ág|profil-érték|kulcs): {data}")
        elif event == 'calib':
            calib, _, calib_prev = data.partition('|')
            logging.info(f"[COLOR] CalibrationManagementEnabled: {calib_prev or '?'} -> {calib} "
                         f"(bekapcsolva, hogy a gép a takarítás után tudjon profilt betölteni).")
        elif event == 'error':
            errors.append(data)
            logging.warning(f"[COLOR] Részhiba a színprofil-visszaállításban: {data}")
        elif event == 'done':
            removed = data

    if errors:
        _say(f'⚠️ A színprofilok visszaállítása részben sikerült ({len(errors)} részhiba) - a folyamat megy tovább.')
    elif removed or files:
        _say(f'✅ Színprofilok gyári alapállapotra állítva ({removed} monitor-hozzárendelés, '
             f'{len(files)} profilfájl törölve).')
        for fn in files:
            _say(f'   • törölt profil: {fn}')
        # A kép látványosan megváltozik, ha a leszedett profil valódi kalibráció volt.
        # Ezt KI KELL mondani, mert különben a szerelő a monitorra vagy a videokártyára fogja.
        _say('   Ha a kép ettől világosabb lett, a gépen egyedi kalibráció volt. '
             'Új kalibráció: Win+R -> dccw')
    else:
        _say('✅ Színprofilok ellenőrizve - nem volt egyedi (kalibrált) monitor-profil.')
    _say('   A Windows saját sRGB profilja és a nyomtatóprofilok érintetlenek.')

    logging.info(f"[COLOR] Kész - {removed} hozzárendelés, {len(files)} profilfájl törölve, "
                 f"kalibráció-kapcsoló: {calib_prev or '?'} -> {calib}, részhibák: {len(errors)}.")
    return True, removed
