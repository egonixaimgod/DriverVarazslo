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


def get_machine_id():
    """Stabil gép-azonosító a ranglista deduplikálásához: ugyanarról a gépről újra
    feltöltve a felhő a machine_id alapján a MEGLÉVŐ sort frissíti (nem duplikál). A
    Windows MachineGuid-ját használjuk (a registry 64 bites nézetéből, hogy 32 bites
    Pythonból is a valódi értéket kapjuk); ha nem olvasható, a gépnévre esünk vissza."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            if guid:
                return str(guid)
    except Exception as e:
        logging.debug(f"[BENCHMARK] MachineGuid olvasási hiba (gépnévre esünk vissza): {e}")
    return os.environ.get('COMPUTERNAME', 'PC')


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
