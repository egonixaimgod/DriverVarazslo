"""RAM TEST - közös mag (GUI + CLI): a három memóriateszt (TestMem5, y-cruncher, HCI MemTest)
kimenetének értelmezése, a HCI-példányok felosztása, a WHEA-eseménynapló lekérdezése és a
záró verdikt. Tiszta függvények - a futtatás és az ablak-olvasás az app/gui/ramtest.py-ban van.

Ami ÉLŐBEN mérve van (2026-10-26, Ryzen 5 5600, 64 GB, a szervizes saját gépe):
  - TM5 0.13.1: `TM5.exe Config File="<név>.cfg"` (a bin\\ mappából) elindul a megadott
    konfiggal, a naplót a saját mappájába írja (Log.txt): fejléc, "Configuration: …",
    "Testing 4592 MB x 12 (84%) out of 65462 MB", "Testing stopped by user". A konfig
    "Reserved Memory for Windows" értékét NEM veszi figyelembe: mindig a szabad RAM ~84%-át
    foglalja le, és magától rendszergazdaként indul újra (nem emelt folyamat nem lövi le).
  - y-cruncher 0.8.7: `y-cruncher skip-warnings pause:-2 stress -M:1G -D:10 -TL:25 VT3`
    a kimenetet átirányítva is kiírja ("Running VT3: Passed …", "Test Finished."), rc=0.
  - HCI MemTest 7.0 (ingyenes): induláskor "Welcome" dialógus (OK), a főablakban Edit (1000)
    + "Start Testing" gomb (1001), indítás után egy "Message for first-time users" ablak; az
    állapot a 1999-es Static-ban: " [/]   5.1% Coverage, 0 Errors", a címsorban
    "[0 Errors] MemTest". 32 bites, LAA - egy példány legfeljebb ~4 GB-ot címez.
NINCS mérve (nincs hibás RAM a kézben): a TM5, az y-cruncher és a HCI HIBÁS sorának pontos
alakja. Az értelmezők ezért a binárisokból kiolvasott hibaszövegekre ("Error in test",
"error(s)", "crashed") és általános "error/fail" mintára építenek, a "0/no errors" kivételével.
"""

import math
import re

# A stresstools.zip-ben keresett fájlnevek (a STRESS_TOOLS mintájára, de külön tábla: ezek
# NEM egyenként indítható szervíz programok, csak a RAM TEST lánc használja őket).
RAMTEST_TOOLS = {
    'tm5': ('TestMem5', ['tm5.exe']),
    'ycruncher': ('y-cruncher', ['y-cruncher.exe']),
    'hcimemtest': ('HCI MemTest', ['memtest.exe']),
}

# A TM5 csomagjában gyárilag ott lévő anta777 Extreme konfig - ebből írunk egy saját
# példányt a mód ciklusszámával (a gyári fájlt nem írjuk felül).
TM5_CONFIG_SOURCE = 'Extreme @ anta777.cfg'
TM5_CONFIG_NAME = 'DriverVarazslo @ anta777.cfg'

# A két mód (explicit user decision, 2026-10-26: "Választható a gombnál"). A felület és a
# CLI is innen olvassa a feliratot és a leírást.
RAMTEST_MODES = {
    'gyors': {
        'label': 'Gyors',
        'desc': 'TM5 1 kör + y-cruncher 15 perc + HCI 100%. A durva hibákat megfogja (kb. 1-3 óra, a RAM méretétől függ).',
        'tm5_cycles': 1, 'yc_seconds': 15 * 60, 'hci_coverage': 100,
    },
    'teljes': {
        'label': 'Teljes',
        'desc': 'TM5 3 kör + y-cruncher 30 perc + HCI 400%. A ritkán jelentkező hibákra is (több óra, egy éjszakás futás).',
        'tm5_cycles': 3, 'yc_seconds': 30 * 60, 'hci_coverage': 400,
    },
}

# Felső korlátok (csak végszükség-fék egy beragadt programra; normál esetben a teszt maga ér véget).
TM5_MAX_HOURS_PER_CYCLE = 4
HCI_MAX_HOURS_PER_100 = 6
YC_GRACE_SECONDS = 600

# y-cruncher: a VT3 (= a régi VST utódja) és az FFTv4 (a legnagyobb memória-terhelés a
# listájában). Tesztenként 2 perc, a -TL a mód szerinti teljes idő.
YC_TESTS = ['FFTv4', 'VT3']
YC_PER_TEST_SECONDS = 120
YC_MEM_FRACTION = 0.7

# HCI: a szabad RAM ennyiedét osztjuk szét; egy 32 bites (LAA) példány legfeljebb ennyit kap.
HCI_MEM_FRACTION = 0.85
# Terepen (Build 396, 64 GB): 3397 MB-nál "Could not allocate 3397 MB" - a HCI szerint a Windows
# 2-3,5 GB közé korlátozza az összefüggő foglalást. A MemTestHelper is 2048-at használ.
HCI_MAX_MB_PER_INSTANCE = 2048
HCI_MIN_MB_PER_INSTANCE = 256

_NO_ERROR_RE = re.compile(r'\b(no|0)\s+errors?\b|errors?\s+is\s+not\s+detected|no\s+error', re.I)


def _is_error_line(line):
    return bool(re.search(r'error|crash|fail', line, re.I)) and not _NO_ERROR_RE.search(line)


def parse_tm5_log(text):
    """A TM5 Log.txt LEGUTOLSÓ futásának állapota. Visszaad: {'state', 'errors', 'detail'}.
    state: 'running' / 'ok' / 'errors' / 'stopped' (hiba nélkül, kívülről leállítva) /
    'tool_error' (nem a RAM hibája: kevés memória, hiányzó konfig) / 'empty' (még nincs napló)."""
    if not text or not text.strip():
        return {'state': 'empty', 'errors': 0, 'detail': ''}
    blocks = re.split(r'^-{4,}\s*TestMem5.*$', text, flags=re.M)
    lines = [l.strip() for l in blocks[-1].splitlines() if l.strip()]
    low = '\n'.join(lines).lower()
    last = lines[-1] if lines else ''
    if 'failed to allocate' in low or 'configuration file' in low and 'not found' in low \
            or 'was not started' in low:
        return {'state': 'tool_error', 'errors': 0, 'detail': last}
    err_lines = [l for l in lines if _is_error_line(l)]
    counted = [int(n) for n in re.findall(r'(\d+)\s*error\(s\)', low)]
    errors = max([len(err_lines)] + counted)
    if errors:
        return {'state': 'errors', 'errors': errors, 'detail': err_lines[-1] if err_lines else last}
    if 'testing completed' in low or 'completed' in last.lower():
        return {'state': 'ok', 'errors': 0, 'detail': last}
    if 'testing stopped' in low:
        return {'state': 'stopped', 'errors': 0, 'detail': last}
    return {'state': 'running', 'errors': 0, 'detail': last}


def parse_ycruncher_output(text):
    """Az y-cruncher stress kimenete: {'passed': N, 'errors': [sorok], 'finished': bool}."""
    text = text or ''
    passed = len(re.findall(r'Running\s+\S+:\s+Passed', text))
    errors = [l.strip() for l in text.splitlines()
              if _is_error_line(l) and not re.search(r'stop on error', l, re.I)]
    return {'passed': passed, 'errors': errors, 'finished': 'Test Finished' in text}


def parse_hci_status(text):
    """A HCI 1999-es Static-ja (' [/]   5.1% Coverage, 0 Errors') -> (lefedettség %, hibák),
    vagy None, ha a szöveg nem ilyen (pl. még nem indult el)."""
    m = re.search(r'([\d.]+)\s*%\s*Coverage,\s*(\d+)\s*Errors?', text or '', re.I)
    return (float(m.group(1)), int(m.group(2))) if m else None


def hci_alloc_failed(text):
    """A HCI állapotsora / felugró ablaka: 'Could not allocate 3397 MB' (terepen, Build 396)."""
    return 'could not allocate' in (text or '').lower()


# Windows kilépési kódok (előjel nélküli alakban), hogy a napló ne csak egy számot mutasson.
EXIT_CODE_NAMES = {
    0xC000013A: 'STATUS_CONTROL_C_EXIT - Ctrl+C, vagy a konzolablakát bezárták',
    0xC0000005: 'STATUS_ACCESS_VIOLATION - összeomlás (instabil CPU/RAM mellett gyakori)',
    0xC0000017: 'STATUS_NO_MEMORY - elfogyott a memória',
    0xC000001D: 'STATUS_ILLEGAL_INSTRUCTION - a CPU nem ismeri az utasítást',
    0xC0000409: 'STATUS_STACK_BUFFER_OVERRUN - összeomlás',
    1: 'taskkill /F (kilőve) vagy általános hiba',
}


def describe_exit_code(rc):
    """'3221225786 (0xC000013A: STATUS_CONTROL_C_EXIT - …)' alakú leírás a naplóba."""
    if rc is None:
        return 'nincs (még fut)'
    u = rc & 0xFFFFFFFF
    return f"{rc} (0x{u:08X}: {EXIT_CODE_NAMES.get(u, 'ismeretlen kód')})"


def parse_hci_title_errors(title):
    """A HCI címsorából ('[0 Errors] MemTest') a hibaszám, vagy None."""
    m = re.search(r'\[\s*(\d+)\s*errors?\s*\]', title or '', re.I)
    return int(m.group(1)) if m else None


def hci_plan(avail_mb, threads):
    """(példányszám, MB/példány) a HCI-hoz: legalább annyi példány, ahány logikai szál van
    (a gyártó ajánlása), és annyi, hogy egy példány se lépje túl a 32 bites korlátot.
    None, ha nincs érdemi szabad memória."""
    threads = max(1, int(threads or 1))
    total = int((avail_mb or 0) * HCI_MEM_FRACTION)
    if total < HCI_MIN_MB_PER_INSTANCE:
        return None
    n = max(threads, math.ceil(total / HCI_MAX_MB_PER_INSTANCE))
    n = min(n, total // HCI_MIN_MB_PER_INSTANCE)
    return n, total // n


def hci_grid(n, w, h, area):
    """A HCI-ablakok bal-felső sarkai rácsban, ÁTMÉRETEZÉS NÉLKÜL (explicit user decision,
    2026-10-26: "nem átméretezni kell csak berakni egymás mellé … az összes példány rajta
    legyen a screenen"; az ablak amúgy sem méretezhető, mérve 326×377, nincs WS_THICKFRAME).
    Annyi oszlop, amennyi átfedés nélkül elfér; ha a sorok nem férnek ki, egyenletesen
    egymásra csúsznak, de mindegyik a munkaterületen belül marad. area = (bal, felső, jobb, alsó).
    Visszaad: [(x, y, sor, oszlop)] az i. példányra."""
    left, top, right, bottom = area
    W, H = right - left, bottom - top
    cols = max(1, min(n, W // max(1, w)))
    rows = math.ceil(n / cols)
    step_x = w
    step_y = h if rows * h <= H or rows == 1 else max(0, (H - h)) / (rows - 1)
    # Ami elfér, középre kerül; ami nem, a munkaterület széléig ér.
    off_x = max(0, (W - (cols - 1) * step_x - w) // 2)
    off_y = max(0, int(H - (rows - 1) * step_y - h) // 2)
    return [(left + off_x + (i % cols) * step_x, top + off_y + int((i // cols) * step_y), i // cols, i % cols)
            for i in range(n)]


def ycruncher_args(seconds, avail_gb):
    """Az y-cruncher parancssora (az exe nélkül)."""
    mem_gb = max(1, int((avail_gb or 2) * YC_MEM_FRACTION))
    return (['skip-warnings', 'pause:-2', 'stress', f'-M:{mem_gb}G', f'-D:{YC_PER_TEST_SECONDS}',
             f'-TL:{int(seconds)}'] + YC_TESTS)


def tm5_config_text(source_text, cycles):
    """A gyári anta777 konfig másolata a kért ciklusszámmal. A sorvégekhez (CRLF) nem nyúl:
    LF-es konfigot a TM5 "nem talál" (Build 396, terepen)."""
    if not re.search(r'^Cycles=\d+', source_text, flags=re.M):
        raise ValueError('A TM5 konfigban nincs Cycles= sor')
    text = re.sub(r'^Cycles=\d+', f'Cycles={int(cycles)}', source_text, count=1, flags=re.M)
    return re.sub(r'^Config Name=[^\r\n]*', 'Config Name=DriverVarazslo', text, count=1, flags=re.M)


# ---------------------------------------------------------------------------
# WHEA (Windows Hardware Error Architecture) - Rendszer napló
# ---------------------------------------------------------------------------

def whea_query_ps(since_local):
    """PowerShell, ami a since_local (datetime, helyi idő) óta keletkezett WHEA-Logger
    eseményeket listázza. Kimenet: 'EV|id|idő|szint|első sor' sorok, a végén 'DONE';
    a lekérdezés hibája 'ERR|…' (nem "nincs esemény" - 3. elv)."""
    stamp = since_local.strftime('%Y%m%d%H%M%S')
    return (
        f"$s=[datetime]::ParseExact('{stamp}','yyyyMMddHHmmss',$null); "
        "try { $ev = Get-WinEvent -FilterHashtable @{LogName='System';"
        "ProviderName='Microsoft-Windows-WHEA-Logger';StartTime=$s} -ErrorAction Stop; "
        "foreach($e in $ev){ $m=(($e.Message -split \"`r?`n\")[0]) -replace '\\|','/'; "
        "'EV|'+$e.Id+'|'+$e.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')+'|'+$e.LevelDisplayName+'|'+$m }; "
        "'DONE' } catch { if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { 'DONE' } "
        "else { 'ERR|'+$_.Exception.Message } }"
    )


def parse_whea_output(stdout):
    """A whea_query_ps kimenete -> események listája ([] = bizonyítottan nincs), vagy None,
    ha a lekérdezés elbukott / nem ért végig."""
    events = []
    done = False
    for line in (stdout or '').splitlines():
        line = line.strip()
        if line.startswith('ERR|'):
            return None
        if line == 'DONE':
            done = True
        elif line.startswith('EV|'):
            parts = line.split('|', 4)
            if len(parts) == 5 and parts[1].isdigit():
                events.append({'id': int(parts[1]), 'time': parts[2], 'level': parts[3], 'message': parts[4]})
    return events if done else None


WHEA_EXPLAIN = {
    19: 'korrigált hardverhiba (Ryzenen jellemzően Infinity Fabric / FCLK) - a SOC-feszültség vagy az FCLK instabil',
    18: 'VÉGZETES hardverhiba (CPU / memóriavezérlő) - ilyenkor a gép kékhalállal vagy újraindulással áll meg',
    17: 'korrigált PCIe-hiba (bővítőkártya, NVMe, kábel/foglalat) - nem a RAM-ra utal',
    47: 'korrigált memóriahiba (ECC) - a RAM hibás vagy instabil',
    1: 'hardverhiba-bejegyzés (WHEA)',
}


def whea_summary(events):
    """Eseményazonosítónként összesít: [(id, darab, magyarázat)] darabszám szerint csökkenően."""
    counts = {}
    for ev in events:
        counts[ev['id']] = counts.get(ev['id'], 0) + 1
    return [(i, n, WHEA_EXPLAIN.get(i, 'hardverhiba-bejegyzés (WHEA)'))
            for i, n in sorted(counts.items(), key=lambda kv: -kv[1])]


# ---------------------------------------------------------------------------
# Záró verdikt
# ---------------------------------------------------------------------------

STEP_NAMES = {'tm5': 'TestMem5 (anta777 Extreme)', 'ycruncher': 'y-cruncher (FFTv4 + VT3)',
              'hcimemtest': 'HCI MemTest'}


def ramtest_verdict(results, whea_events):
    """results: {lépés: {'state': 'ok'|'errors'|'tool_error'|'skipped'|'cancelled', 'detail': str}}
    whea_events: lista, vagy None (a napló nem volt olvasható).
    Visszaad: {'ok': bool, 'headline': str, 'advice': [teendők]}. ✅ CSAK akkor, ha minden
    lépés 'ok' ÉS a WHEA-napló bizonyítottan üres (4. elv: nincs néma hamis siker)."""
    states = {k: (v or {}).get('state') for k, v in results.items()}
    failed = [k for k, s in states.items() if s == 'errors']
    not_run = [k for k in STEP_NAMES if states.get(k) != 'ok' and k not in failed]
    whea_ids = {ev['id'] for ev in (whea_events or [])}
    advice = []
    if 'tm5' in failed or 'hcimemtest' in failed:
        advice.append('A RAM vagy a beállításai instabilak. Teendő: XMP/EXPO kikapcsolása (vagy lazább timing / '
                      'kicsit több DRAM-feszültség), utána újra teszt; ha gyári beállításon is hibás, a modulokat '
                      'egyenként kell tesztelni, és a hibás modult cserélni.')
    if 'ycruncher' in failed:
        advice.append('Az y-cruncher hibája a CPU-ra vagy a memóriavezérlőre (SOC/FCLK) utal. Teendő: PBO / Curve '
                      'Optimizer / undervolt visszaállítása gyárira, FCLK lejjebb vagy SOC-feszültség kicsit feljebb.')
    if 19 in whea_ids:
        advice.append('WHEA 19 (Infinity Fabric): a SOC-feszültséget kicsit emelni (Ryzenen 1,10-1,15 V) vagy az '
                      'FCLK-t csökkenteni.')
    if 18 in whea_ids:
        advice.append('WHEA 18: végzetes hardverhiba - a CPU/memóriavezérlő tuningját gyárira kell állítani, és ha '
                      'gyárin is előjön, a CPU vagy az alaplap gyanús.')
    if 47 in whea_ids:
        advice.append('WHEA 47 (ECC-javítás): a RAM hibás vagy instabil - modulonként tesztelni.')
    if failed or whea_ids & {18, 19, 47}:
        names = ', '.join(STEP_NAMES[k] for k in failed) or 'a WHEA-napló'
        return {'ok': False, 'headline': f'❌ NEM STABIL - hibát talált: {names}', 'advice': advice}
    if not_run or whea_events is None:
        why = []
        if not_run:
            why.append('nem futott végig: ' + ', '.join(STEP_NAMES[k] for k in not_run))
        if whea_events is None:
            why.append('a WHEA-napló nem volt olvasható')
        return {'ok': False, 'headline': '⚠️ NEM ELDÖNTHETŐ - ' + '; '.join(why), 'advice': advice}
    if whea_ids:
        return {'ok': False, 'headline': '⚠️ A tesztek hibátlanok, de a WHEA-naplóban hardverhiba-bejegyzés van',
                'advice': advice}
    return {'ok': True, 'headline': '✅ STABIL - mindhárom teszt hibátlan, a WHEA-napló tiszta', 'advice': []}
