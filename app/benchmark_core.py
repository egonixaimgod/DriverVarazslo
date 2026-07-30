"""Benchmark - KÖZÖS mag: a stresstools.zip-be csomagolt benchmark exe-k megkeresése,
a gép hardver-adatainak begyűjtése (CPU/alaplap/RAM/GPU) a felhő-ranglistához, a végpont
beállításának tárolása, és a ranglista felhő-oldali le-/feltöltése.

A felhő-oldal egy egyszerű HTTP protokoll (Google Apps Script webalkalmazás vagy más
backend, lásd benchmark_leaderboard_setup.md): GET -> teljes ranglista JSON tömbként,
POST (JSON body) -> egy gép eredményének beszúrása/frissítése a machine_id alapján.

Fontos: a hálózati hívások a friss-Windows tanúsítvány-fallbackkel mennek (ugyanaz az elv,
mint a stresstools/block.bat letöltéseknél): CSAK CERTIFICATE_VERIFY_FAILED esetén esünk
vissza PowerShell (schannel) Invoke-WebRequest-re, a tanúsítvány-ellenőrzés ott is TELJES
értékű - ez NEM ellenőrzés-megkerülés."""

# === AUTO-IMPORTS ===
import os
import json
import logging
import fnmatch
import hashlib
import unicodedata
import winreg
from app.common import _ps_quote
from app.benchmark_defs import BENCH_TOOLS
from app.benchmark_defs import BENCHMARK_API_URL_DEFAULT
# === /AUTO-IMPORTS ===


def find_bench_tool_exes(stress_dir, keys):
    """Megkeresi a kicsomagolt stresstools mappában a megadott BENCH_TOOLS kulcsokhoz
    tartozó exe-ket (os.walk-kal, tetszőleges almappa-mélységben). A STRESS-oldali
    _find_stress_tool_exes párja, de a BENCH_TOOLS listával. Visszaad: {key: útvonal|None}.
    Egy kulcson belül a filenames-lista SORRENDJE prioritás (a legkorábbi találat nyer)."""
    candidates = {key: {} for key in keys}
    if not stress_dir or not os.path.isdir(stress_dir):
        return {key: None for key in keys}
    for root, dirs, files in os.walk(stress_dir):
        for file in files:
            fl = file.lower()
            for key in keys:
                for idx, pattern in enumerate(BENCH_TOOLS[key][1]):
                    if '*' in pattern or '?' in pattern:
                        matched = fnmatch.fnmatch(fl, pattern)
                    else:
                        matched = (fl == pattern)
                    if matched and idx not in candidates[key]:
                        candidates[key][idx] = os.path.join(root, file)
    return {key: (candidates[key][min(candidates[key])] if candidates[key] else None) for key in keys}


# ============================================================================
# Automata benchmark-futtatás: parancs-építők + pontszám-parserek (tiszta függvények,
# a gui/benchmark.py run_benchmark_suite-je hívja őket; offline is tesztelhetők)
# ============================================================================
def build_cinebench_cmd(exe_path):
    """A Cinebench parancssori multi-core futtatásának teljes parancsa. A CLI-módban a
    Cinebench NEM nyit ablakot: a stdout-ra írja a folyamatot és a végén a "CB <pont>"
    sort, majd kilép - a hívó a stdout-ot fájlba irányítja és a parse_cinebench_output-tal
    olvassa ki a pontszámot."""
    from app.benchmark_defs import CINEBENCH_CLI_ARGS
    return [exe_path] + list(CINEBENCH_CLI_ARGS)


def parse_cinebench_output(text):
    """A Cinebench CLI stdout-jából a multi-core pontszám kiolvasása. A kimenet végén
    (R20/R23 azonos) egy "CB <float>" sor áll - az UTOLSÓ ilyet vesszük (a futás közben
    részpontszám is előfordulhat). None, ha nincs értelmezhető pontszám - a hívó ebből
    tudja, hogy a futás nem adott eredményt."""
    if not text:
        logging.warning("[BENCHMARK] Cinebench: üres kimenet - nincs pontszám.")
        return None
    matches = _re.findall(r'\bCB\s+([0-9]+(?:[.,][0-9]+)?)', text)
    if not matches:
        tail = text.strip()[-400:]
        logging.warning(f"[BENCHMARK] Cinebench: nem található 'CB <pont>' sor a kimenetben. "
                        f"A kimenet vége: {tail!r}")
        return None
    score = float(matches[-1].replace(',', '.'))
    logging.info(f"[BENCHMARK] Cinebench pontszám kiolvasva: {score} "
                 f"({len(matches)} 'CB' sorból az utolsó)")
    return score


def build_furmark_cmd(exe_path):
    """A FurMark 1.x parancssori benchmark-futtatás teljes parancsa (/nogui /benchmark
    /max_time /log_score ... - a beállítások a benchmark_defs.py-ban)."""
    from app.benchmark_defs import FURMARK_CLI_ARGS
    return [exe_path] + list(FURMARK_CLI_ARGS)


def find_furmark_score_file(exe_dir, min_mtime=None):
    """A FurMark pontszám-fájljának megkeresése az exe mappájában. A /log_score a
    FurMark-Scores.txt-be ír (append), de a fájlnév verziónként változhatott, ezért
    minden '*score*.txt'-t megnézünk és a legfrissebbet vesszük. Ha min_mtime meg van
    adva, csak az ANNÁL újabban módosított fájl számít - így egy korábbi futás ott
    maradt fájlja nem olvasható be friss eredményként."""
    best, best_mtime = None, -1
    try:
        for fn in os.listdir(exe_dir):
            if fn.lower().endswith('.txt') and 'score' in fn.lower():
                path = os.path.join(exe_dir, fn)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > best_mtime:
                    best, best_mtime = path, mtime
    except OSError as e:
        logging.warning(f"[BENCHMARK] FurMark score-fájl keresési hiba ({exe_dir}): {e}")
        return None
    if best is None:
        logging.warning(f"[BENCHMARK] Nincs '*score*.txt' a FurMark mappájában: {exe_dir}")
        return None
    if min_mtime is not None and best_mtime < min_mtime:
        logging.warning(f"[BENCHMARK] A FurMark score-fájl ({best}) RÉGEBBI a mostani futásnál "
                        f"(mtime={best_mtime:.0f} < indítás={min_mtime:.0f}) - a mostani futás nem írt eredményt.")
        return None
    logging.info(f"[BENCHMARK] FurMark score-fájl: {best}")
    return best


def parse_furmark_scores(text):
    """A FurMark score-fájl (append-napló) UTOLSÓ bejegyzéséből az FPS (és a pontszám)
    kiolvasása. A fájlformátum ERŐSEN verziófüggő, ezért több mintát próbálunk, mindig az
    utolsó találatot véve. Visszaad: {'fps', 'score', 'resolution', 'mode'} vagy None, ha
    semmi értelmezhető nincs a szövegben.

    Formátumok (a sorrend prioritás):
      1. FurMark 1.39.x (TEREPEN MÉRT, 2026-07-29 - ez NEM ír sem 'FPS', sem 'points' szót,
         emiatt bukott el az első kiadás és jelent meg 'a FurMark nem indult el'-ként):
           AMD Radeon RX 6500 XT - [FRAMES=618] - [TIME_MS=10000] - [Resolution=1898x1024]
           - [MSAA=0X] - [Mode=Windowed] - GPU Temp Max=... - FurMark 1.39.3.0 - [Date=...]
         Innen az FPS SZÁMÍTOTT érték: FRAMES / (TIME_MS / 1000).
      2. Klasszikus 1.x: "... 3162 points (FPS: 52) ..." vagy "... 3162 points (52 FPS, ...)".
      3. Külön mezők: "FPS: 52" / "52 FPS" és "Score: 3162".
    """
    if not text:
        logging.warning("[BENCHMARK] FurMark: üres score-fájl tartalom.")
        return None
    fps = None
    score = None
    resolution = None
    mode = None

    res_m = _re.findall(r'\[\s*Resolution\s*=\s*([0-9]+x[0-9]+)', text, _re.IGNORECASE)
    if res_m:
        resolution = res_m[-1]
    mode_m = _re.findall(r'\[\s*Mode\s*=\s*([A-Za-z]+)', text, _re.IGNORECASE)
    if mode_m:
        mode = mode_m[-1]

    # 1) FurMark 1.39.x: [FRAMES=..] + [TIME_MS=..] -> az FPS-t nekünk kell kiszámolni.
    fr = _re.findall(r'\[\s*FRAMES\s*=\s*(\d+)\s*\]', text, _re.IGNORECASE)
    tm = _re.findall(r'\[\s*TIME_MS\s*=\s*(\d+)\s*\]', text, _re.IGNORECASE)
    if fr and tm and int(tm[-1]) > 0:
        frames, time_ms = int(fr[-1]), int(tm[-1])
        score = frames                      # a FurMark "pontszáma" maga a képkockaszám
        fps = round(frames * 1000.0 / time_ms, 1)
        logging.info(f"[BENCHMARK] FurMark (1.39-es formátum): FRAMES={frames}, TIME_MS={time_ms} "
                     f"-> FPS={fps}, felbontás={resolution}, mód={mode}")
        return {'fps': fps, 'score': score, 'resolution': resolution, 'mode': mode}

    # 2) A klasszikus 1.x sorok - a FurMark verziónként KÉTFÉLE sorrendet is írt:
    #    "... 3162 points (FPS: 52) ..."  ÉS  "... 3162 points (52 FPS, 60000 ms) ..."
    combo = _re.findall(r'(\d+)\s*points?\s*\(\s*FPS\s*[:=]?\s*(\d+)', text, _re.IGNORECASE)
    if combo:
        score, fps = int(combo[-1][0]), int(combo[-1][1])
    else:
        combo2 = _re.findall(r'(\d+)\s*points?\s*\(\s*(\d+)\s*FPS', text, _re.IGNORECASE)
        if combo2:
            score, fps = int(combo2[-1][0]), int(combo2[-1][1])
    if fps is None:
        # 3) Külön mezők (más verziók): "FPS: 52" vagy "52 FPS", "Score: 3162"
        fps_m = _re.findall(r'\bFPS\s*[:=]\s*(\d+)', text, _re.IGNORECASE)
        if not fps_m:
            fps_m = _re.findall(r'\b(\d+)\s*FPS\b', text, _re.IGNORECASE)
        if fps_m:
            fps = int(fps_m[-1])
    if score is None:
        score_m = _re.findall(r'\bScore\s*[:=]\s*(\d+)', text, _re.IGNORECASE)
        if score_m:
            score = int(score_m[-1])
    if fps is None and score is None:
        tail = text.strip()[-400:]
        logging.warning(f"[BENCHMARK] FurMark: sem FPS, sem Score, sem FRAMES/TIME_MS nem olvasható ki. "
                        f"A fájl vége: {tail!r}")
        return None
    logging.info(f"[BENCHMARK] FurMark eredmény kiolvasva: FPS={fps}, Score={score}, "
                 f"felbontás={resolution}, mód={mode}")
    return {'fps': fps, 'score': score, 'resolution': resolution, 'mode': mode}


# ============================================================================
# "Minden program bezárása a mérés előtt" (a benchmark indítása előtt megkérdezve)
# ============================================================================
def build_close_apps_ps(protected, skip_ids, wait_s):
    """A látható főablakkal rendelkező felhasználói programokat UDVARIASAN bezáró
    PowerShell-szkript. `protected`: kisbetűs folyamatnevek, amelyekhez nem nyúlunk;
    `skip_ids`: további kihagyandó PID-ek (a saját folyamatunk és gyerekei); `wait_s`:
    ennyit várunk a WM_CLOSE után, mielőtt megnézzük, mi maradt nyitva.

    A szkript CloseMainWindow()-t hív (WM_CLOSE), NEM Stop-Process-t: ez ugyanaz, mintha
    a felhasználó az ablak X gombjára kattintana, tehát egy mentetlen dokumentum esetén a
    program rákérdez és NYITVA marad - a munka nem vész el.

    A szűrő feltétele NEM csak `MainWindowHandle -ne 0`, hanem NEM ÜRES `MainWindowTitle`
    is (élő száraz próbán mérve, 2026-07-29): a puszta ablak-leíróra szűrve a lista a
    dev gépen tartalmazta az **svchost.exe**-t (van ablak-leírója, de nincs címe) - egy
    Windows szolgáltatás-gazdának küldött WM_CLOSE szolgáltatást állíthat le. Valódi,
    felhasználói programnak MINDIG van ablakcíme, tehát a cím megléte a helyes szűrő.

    Kimenet (soronként):
      TRY:<név>|<ablakcím>   - próbáltuk bezárni
      CLOSED:<név>           - a várakozás után már nem fut
      STAYED:<név>           - még mindig fut (mentetlen munka / rákérdezett)
      DONE                   - a szkript végigfutott
    """
    prot = ','.join("'" + str(p).replace("'", "''").lower() + "'" for p in protected) or "''"
    skips = ','.join(str(int(i)) for i in skip_ids) or '0'
    return f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$protect = @({prot})
$skipIds = @({skips})
$targets = @(Get-Process | Where-Object {{
    $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -and $_.Id -ne $PID -and
    ($skipIds -notcontains $_.Id) -and
    ($protect -notcontains $_.ProcessName.ToLower())
}})
foreach ($p in $targets) {{
    $null = $p.CloseMainWindow()
    Write-Output ("TRY:" + $p.ProcessName + "|" + $p.MainWindowTitle)
}}
if ($targets.Count -gt 0) {{ Start-Sleep -Seconds {int(wait_s)} }}
foreach ($p in $targets) {{
    $q = Get-Process -Id $p.Id
    if ($q) {{ Write-Output ("STAYED:" + $p.ProcessName) }}
    else {{ Write-Output ("CLOSED:" + $p.ProcessName) }}
}}
Write-Output "DONE"
"""


def parse_close_apps_output(text):
    """A build_close_apps_ps kimenetének feldolgozása. Visszaad:
    {'attempted': [(név, ablakcím)], 'closed': [név], 'stayed': [név], 'ok': bool}
    Az 'ok' azt jelzi, hogy a szkript végigfutott (DONE sor) - enélkül a lista hiányos
    lehet, és a hívó ezt jelzi is a felhasználónak."""
    attempted, closed, stayed = [], [], []
    done = False
    for line in (text or '').splitlines():
        line = line.strip()
        if line.startswith('TRY:'):
            body = line[4:]
            name, _, title = body.partition('|')
            attempted.append((name, title))
        elif line.startswith('CLOSED:'):
            closed.append(line[7:])
        elif line.startswith('STAYED:'):
            stayed.append(line[7:])
        elif line == 'DONE':
            done = True
    return {'attempted': attempted, 'closed': closed, 'stayed': stayed, 'ok': done}


def get_machine_id():
    """Stabil HARDVER-azonosító: a Windows MachineGuid-ja (a registry 64 bites nézetéből,
    hogy 32 bites Pythonból is a valódi értéket kapjuk); ha nem olvasható, a gépnévre esünk
    vissza. FIGYELEM: ez már NEM önmagában a ranglista-sor kulcsa - a felhőbe küldött
    machine_id a machine_row_id() által képzett (gép + ranglista-név) összetett kulcs, lásd
    ott, hogy miért."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            if guid:
                return str(guid)
    except Exception as e:
        logging.debug(f"[BENCHMARK] MachineGuid olvasási hiba (gépnévre esünk vissza): {e}")
    return os.environ.get('COMPUTERNAME', 'PC')


# A sor-azonosítóban a névből képzett, olvasható rész hossz-korlátja (a felhő-táblázatban
# ez csak azonosító, nem megjelenített szöveg - a megjelenő név a machine_name mező).
_ROW_ID_NAME_MAX = 40


def machine_row_id(machine_guid, display_name):
    """A ranglista-SOR azonosítója: a gép hardver-azonosítója ÉS a beírt ranglista-név
    együtt. A felhő-oldal erre a mezőre (machine_id) upsertel, tehát EZ dönti el, mi
    számít "ugyanannak a bejegyzésnek".

    Miért nem a puszta MachineGuid (TEREPEN BEJÖTT HIBA, 2026-07-30): ugyanazon a gépen két
    futás KÉT KÜLÖNBÖZŐ névvel a régi sort NÉMÁN felülírta, holott a szerviz szándéka az
    volt, hogy mindkettő fent legyen (előtte/utána mérés, két konfiguráció) - és a
    felülírt pontszám a felhőben visszaszerezhetetlen. Az összetett kulccsal: UGYANAZ a
    név -> a saját sorát frissíti (egy elrontott mérés javítható marad), MÁS név -> új sor.
    Ráadásul KLÓNOZOTT Windowsokon (a szerviz szokása) több fizikai gép ugyanazt a
    MachineGuid-ot hordozza, tehát a puszta GUID ott is némán egymásra írta volna
    különböző gépek eredményeit.

    A kulcs formája: "<guid>|<olvasható-név-szelet>-<6 hexa a névből>". Az olvasható rész
    azért van, mert a táblázatot ember is olvassa; a hexa-lezárás azért, mert a szeletelés
    és az ékezet-lebontás után két KÜLÖNBÖZŐ név is azonos szeletet adhatna (pl. "Gép 1" és
    "gep-1"), az pedig újra néma felülírás lenne. A név előbb normalizálódik (kisbetűs,
    összevont szóközök), így egy elütés nélküli újrafutás - más kis/nagybetűvel vagy
    záró szóközzel - ugyanazt a kulcsot adja, tehát tényleg frissít."""
    guid = str(machine_guid or '').strip() or os.environ.get('COMPUTERNAME', 'PC')
    norm = _re.sub(r'\s+', ' ', str(display_name or '').strip()).lower()
    if not norm:
        logging.info(f"[BENCHMARK] Ranglista-sor azonosító: {guid!r} (nincs megadott név, "
                     "csak a gép-azonosító a kulcs)")
        return guid
    # Ékezet-lebontás (ő/ű is: NFKD -> alap betű + kombináló jel), majd a jelek eldobása.
    ascii_name = ''.join(c for c in unicodedata.normalize('NFKD', norm)
                         if not unicodedata.combining(c))
    slug = _re.sub(r'[^0-9a-z]+', '-', ascii_name).strip('-')[:_ROW_ID_NAME_MAX].strip('-')
    digest = hashlib.sha1(norm.encode('utf-8')).hexdigest()[:6]
    row_id = f"{guid}|{slug}-{digest}" if slug else f"{guid}|{digest}"
    logging.info(f"[BENCHMARK] Ranglista-sor azonosító: {row_id!r} "
                 f"(gép={guid!r}, név={display_name!r} -> normalizált={norm!r})")
    return row_id


# OEM-alapértékek, amiket nem érdemes megjeleníteni (a report_core-ban is szűrt lista).
_OEM_JUNK = {"to be filled by o.e.m.", "default string", "system manufacturer",
             "system product name", "not applicable", "", "none", "o.e.m."}

# Win32_PhysicalMemory.SMBIOSMemoryType -> DDR-generáció (a gépnév "16GB DDR4" formájához).
_DDR_TYPES = {20: 'DDR', 21: 'DDR2', 24: 'DDR3', 26: 'DDR4', 34: 'DDR5', 35: 'DDR5'}

import re as _re


def _clean_cpu(name):
    """A processzornevet olvashatóbbá tisztítja a ranglista-névhez: leszedi a (R)/(TM)
    jelöléseket, a "CPU"/"Processor" szavakat és a záró "@ 3.20GHz" órajelet, a többszörös
    szóközöket összevonja. Pl. "Intel(R) Core(TM) i5-6500 CPU @ 3.20GHz" -> "Intel Core i5-6500",
    "AMD Ryzen 5 5600 6-Core Processor" -> "AMD Ryzen 5 5600 6-Core"."""
    if not name:
        return ''
    n = _re.sub(r'\((?:R|TM|tm|r)\)', '', name)
    n = _re.sub(r'\s*@.*$', '', n)                    # "@ 3.20GHz" és utána minden
    n = _re.sub(r'\bCPU\b', '', n, flags=_re.I)
    n = _re.sub(r'\bProcessor\b', '', n, flags=_re.I)
    n = _re.sub(r'\s+', ' ', n).strip(' -')
    return n


def _clean_gpu(name):
    """A videokártya-nevet rövidíti a ranglista-névhez: leszedi az "NVIDIA GeForce"/"NVIDIA"
    előtagot és a (R)/(TM) jelöléseket. Pl. "NVIDIA GeForce RTX 5080" -> "RTX 5080",
    "Intel(R) HD Graphics 530" -> "Intel HD Graphics 530". A több GPU-t vessző választja."""
    if not name:
        return ''
    parts = []
    for one in name.split(','):
        g = _re.sub(r'\((?:R|TM|tm|r)\)', '', one)
        g = _re.sub(r'NVIDIA GeForce ', '', g)
        g = _re.sub(r'NVIDIA ', '', g)
        g = _re.sub(r'\s+', ' ', g).strip()
        if g:
            parts.append(g)
    return ', '.join(parts)


def gather_machine_specs(run):
    """A ranglistához szükséges hardver-adatok: CPU, alaplap, memória (összes GB + sebesség
    + modulszám), videokártya(k). WMI/CIM lekérdezéssel, PowerShellen keresztül. A `run` a
    hívó subprocess-wrappere (self._run). Visszaad egy dict-et a felhő-sor mezőivel."""
    ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$d = @{}
try { $d.CPU = (Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name) } catch {}
try {
    $bb = Get-CimInstance Win32_BaseBoard | Select-Object -First 1
    $d.BOARDMAN = $bb.Manufacturer
    $d.BOARDPROD = $bb.Product
} catch {}
try {
    $ram = @(Get-CimInstance Win32_PhysicalMemory)
    if ($ram.Count -gt 0) {
        $tot = ($ram | Measure-Object -Property Capacity -Sum).Sum
        $d.RAMGB = [math]::Round($tot / 1GB)
        $d.RAMSPEED = ($ram | Select-Object -First 1 -ExpandProperty Speed)
        $d.RAMCOUNT = $ram.Count
        $d.RAMTYPE = ($ram | Select-Object -First 1 -ExpandProperty SMBIOSMemoryType)
    }
} catch {}
try {
    $gpus = Get-CimInstance Win32_VideoController | Where-Object {
        $_.Name -and $_.Name -notmatch 'Microsoft Basic|Remote Display|Virtual|Parsec|Meta |DisplayLink|IddCx'
    } | Select-Object -ExpandProperty Name
    $d.GPU = (@($gpus) -join ', ')
} catch {}
$d | ConvertTo-Json -Compress
"""
    data = {}
    try:
        res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
        if res and res.stdout and res.stdout.strip():
            data = json.loads(res.stdout.strip())
    except Exception as e:
        logging.error(f"[BENCHMARK] Hardver-lekérdezés hiba: {e}")

    cpu = _clean_cpu((data.get('CPU') or '').strip()) or 'Ismeretlen processzor'

    man = (data.get('BOARDMAN') or '').strip()
    prod = (data.get('BOARDPROD') or '').strip()
    if man.lower() in _OEM_JUNK:
        man = ''
    if prod.lower() in _OEM_JUNK:
        prod = ''
    board = (man + ' ' + prod).strip() or 'Ismeretlen alaplap'

    ram_gb = data.get('RAMGB')
    ram_speed = data.get('RAMSPEED')
    ram_count = data.get('RAMCOUNT')
    ddr = _DDR_TYPES.get(data.get('RAMTYPE'))
    if ram_gb:
        ram = f"{ram_gb} GB"
        if ram_speed:
            ram += f" {ram_speed} MHz"
        if ram_count:
            ram += f" ({ram_count} modul)"
        # Rövid forma a gépnévhez: "16GB DDR4"
        ram_short = f"{ram_gb}GB" + (f" {ddr}" if ddr else "")
    else:
        ram = 'Ismeretlen memória'
        ram_short = ''

    gpu = _clean_gpu((data.get('GPU') or '').strip()) or 'Ismeretlen videokártya'

    # A gép "neve" a ranglistában: proci / RAM / videokártya (a Windows gépnév - pl. "16065"
    # - semmitmondó lenne). A machine_id (a dedup kulcsa) továbbra is a MachineGuid.
    name_parts = [p for p in [cpu, ram_short, gpu] if p and not p.startswith('Ismeretlen')]
    machine_name = ' / '.join(name_parts) if name_parts else os.environ.get('COMPUTERNAME', 'PC')

    return {
        'cpu': cpu,
        'motherboard': board,
        'ram': ram,
        'gpu': gpu,
        'machine_id': get_machine_id(),
        'machine_name': machine_name,
    }


# ============================================================================
# Végpont
# ============================================================================
def resolve_endpoint():
    """A felhő-ranglista végpont URL-je - fixen a programba drótozva (benchmark_defs.py:
    BENCHMARK_API_URL_DEFAULT). Szándékosan nincs futásidejű felülírás/beállítás: minden
    exébe alapból ugyanaz a végpont kerül, módosítani a forrásban (benchmark_defs.py) lehet.
    Üres string, ha nincs beállítva (a nézet ilyenkor "nincs beállítva" állapotot mutat)."""
    return BENCHMARK_API_URL_DEFAULT


# ============================================================================
# HTTP (friss-Windows tanúsítvány-fallbackkel)
# ============================================================================
def _http_via_powershell(run, url, method, body):
    """CSAK a CERTIFICATE_VERIFY_FAILED-ágon hívjuk (friss Windows, hiányos gyökértár):
    PowerShell (schannel) Invoke-WebRequest, TELJES tanúsítvány-ellenőrzéssel. POST esetén
    a JSON body-t ideiglenes fájlba írjuk és -InFile-lal küldjük (így semmilyen idézőjel/
    speciális karakter nem törheti meg a generált parancsot)."""
    tls = "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
    tmp = None
    try:
        if method == 'POST':
            import tempfile
            tf = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
            tf.write(body or '')
            tf.close()
            tmp = tf.name
            ps = ("$ProgressPreference='SilentlyContinue'; " + tls +
                  f"(Invoke-WebRequest -Uri '{_ps_quote(url)}' -Method Post -InFile '{_ps_quote(tmp)}' "
                  "-ContentType 'application/json' -UseBasicParsing).Content")
        else:
            ps = ("$ProgressPreference='SilentlyContinue'; " + tls +
                  f"(Invoke-WebRequest -Uri '{_ps_quote(url)}' -UseBasicParsing).Content")
        res = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps], timeout=40)
        if not res or res.returncode != 0:
            raise Exception("A PowerShell (schannel) HTTP hívás sikertelen.")
        return res.stdout or ''
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception as e:
                logging.debug(f"[BENCHMARK] ideiglenes POST-fájl törlése sikertelen: {e}")


def _http_request(run, url, method='GET', body=None, timeout=25):
    """HTTP kérés a ranglista-végponthoz. Elsőként Python urllib (teljes SSL-ellenőrzés);
    CSAK CERTIFICATE_VERIFY_FAILED esetén esünk vissza PowerShell (schannel) hívásra."""
    import urllib.request
    import urllib.error
    import ssl
    ctx = ssl.create_default_context()
    headers = {'User-Agent': 'DriverVarazslo', 'Content-Type': 'application/json'}
    data = body.encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except urllib.error.URLError as e:
        if 'CERTIFICATE_VERIFY_FAILED' not in str(e):
            raise
        logging.warning(f"[BENCHMARK] Python SSL tanúsítvány-hiba ({e}) - friss Windows gyanú, "
                        "áttérés PowerShell (schannel) hívásra, teljes tanúsítvány-ellenőrzéssel...")
        return _http_via_powershell(run, url, method, body)


def fetch_leaderboard(run):
    """A teljes ranglista lekérése a felhőből. Visszaad egy dict-et:
      {'configured': bool, 'entries': [...], 'error': str|None}
    'configured' False, ha nincs beállítva a végpont (a nézet ilyenkor beállítást kér)."""
    url = resolve_endpoint()
    if not url:
        return {'configured': False, 'entries': []}
    try:
        txt = _http_request(run, url, 'GET')
        parsed = json.loads(txt) if txt and txt.strip() else []
        if isinstance(parsed, dict):
            entries = parsed.get('entries', []) or []
        elif isinstance(parsed, list):
            entries = parsed
        else:
            entries = []
        return {'configured': True, 'entries': entries}
    except Exception as e:
        logging.error(f"[BENCHMARK] Ranglista lekérés hiba: {e}")
        return {'configured': True, 'entries': [], 'error': str(e)}


def upload_result(run, entry):
    """Egy gép eredményének feltöltése a felhő-ranglistára (POST). A felhő-oldal a
    machine_id alapján upsertel. Hibánál kivételt dob."""
    url = resolve_endpoint()
    if not url:
        raise Exception("A felhő-ranglista végpont nincs beállítva (add meg a Benchmark nézet ⚙️ beállításánál).")
    body = json.dumps(entry, ensure_ascii=False)
    txt = _http_request(run, url, 'POST', body=body)
    try:
        resp = json.loads(txt) if txt and txt.strip() else {}
    except Exception:
        resp = {}
    if isinstance(resp, dict) and resp.get('ok') is False:
        raise Exception(resp.get('error') or "A szerver hibát jelzett a feltöltéskor.")
    return True
