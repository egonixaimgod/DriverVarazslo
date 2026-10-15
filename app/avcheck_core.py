"""Vírusirtó-diagnózis - KÖZÖS mag: ha a program egy Windows-műveletnél BERAGAD, ez
mondja meg, hogy a vírusirtó tartja-e fel, vagy valami más (és akkor az nagy baj).

MIÉRT KELL (2026-10-15, explicit user decision: *"érzékelje a program hogyha a vírusírtó
miatt nem tud tovább működni és írja ki … ha meg nem virusirto miatt akadt el a program az
nagyon nagy baj"*). TEREPI ESET: Lenovo 20LE (ThinkPad), Win11 26100, Build 382 - a fix
törlési lába előtt MINDEN `pnputil /export-driver` 300 mp-ig lógott, a technikus 2 óra
után lőtte le. A naplóból NEM derült ki, hogy vírusirtó volt-e: semmi nem kérdezte meg.

3. elv (a nemtudás nem tény): három szint van, és a szöveg mindig megmondja, melyik.
  'defender'   - BIZONYÍTOTT: a Defender saját naplójában blokkolás/észlelés áll ránk
                 (a programra, a pnputil-ra vagy a PowerShellre) az akadás idejéből.
  'thirdparty' - VALÓSZÍNŰ: be van kapcsolva egy NEM Microsoft vírusirtó. A harmadik fél
                 termékeinek nincs közös naplója, tehát bizonyítani nem tudjuk.
  'none'       - nincs nyoma vírusirtónak: ez NEM vírusirtó, hanem a Windows driver-tára
                 vagy egy program-hiba. Ezt hangosan kell jelezni (ERROR + naplóküldés).
  'unknown'    - maga a lekérdezés is elbukott (pl. a PowerShell is beragadt): nem
                 eldönthető, és ezt ki is mondjuk.
"""

import json
import logging
import os

from app.common import CMD_TIMEOUT_RETURNCODE

AVCHECK_TIMEOUT = 90              # mp - ha a PowerShell is beragad, ne várjunk órákig
AVCHECK_EVENT_MINUTES = 240       # a Defender-napló ennyi percre visszamenőleg
# Defender Operational napló: 1006/1015/1116/1117 = észlelés/művelet, 1121 = ASR-blokk,
# 1123 = Védett mappák (Controlled Folder Access) blokk, 1126 = hálózatvédelmi blokk.
DEFENDER_EVENT_IDS = (1006, 1015, 1116, 1117, 1121, 1123, 1126)
# Ezekre a nevekre utaló Defender-esemény = minket blokkolt (a program, a gyerekfolyamatai
# és az adatmappája). A magyar név ékezetes és ékezet nélküli alakja is kell.
OWN_MARKERS = ('drivervarazslo', 'drivervarázsló', 'pnputil', 'powershell', 'dism')
# A driver-tárat író Windows-folyamatok: ha egy ilyen már régóta fut, az a beragadt
# eszköztelepítés jele (nem vírusirtó).
PNP_PROCESSES = ('drvinst', 'pnputil', 'DismHost', 'TiWorker')

AVCHECK_PS = r'''
$out = @{ Products = @(); ProductsOk = $false; Events = @(); EventsOk = $false; Procs = @() }
try {
    $out.Products = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop |
        ForEach-Object { @{ Name = [string]$_.displayName; State = [int]$_.productState } })
    $out.ProductsOk = $true
} catch {}
try {
    $ev = Get-WinEvent -ErrorAction Stop -FilterHashtable @{
        LogName = 'Microsoft-Windows-Windows Defender/Operational'; Id = @(%IDS%);
        StartTime = (Get-Date).AddMinutes(-%MIN%) }
    $out.Events = @($ev | Select-Object -First 50 | ForEach-Object {
        @{ Id = $_.Id; Time = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss');
           Msg = ([string]$_.Message).Substring(0, [Math]::Min(600, ([string]$_.Message).Length)) } })
    $out.EventsOk = $true
} catch {
    if ($_.FullyQualifiedErrorId -match 'NoMatchingEventsFound') { $out.EventsOk = $true }
}
$out.Procs = @(Get-Process -Name %PROCS% -ErrorAction SilentlyContinue | ForEach-Object {
    $age = $null; try { $age = [int]((Get-Date) - $_.StartTime).TotalSeconds } catch {}
    @{ Name = $_.ProcessName; Id = $_.Id; AgeS = $age } })
$out | ConvertTo-Json -Depth 4 -Compress
'''


def _script():
    return (AVCHECK_PS.replace('%IDS%', ','.join(str(i) for i in DEFENDER_EVENT_IDS))
            .replace('%MIN%', str(AVCHECK_EVENT_MINUTES))
            .replace('%PROCS%', ','.join(PNP_PROCESSES)))


def product_enabled(state):
    """A SecurityCenter2 `productState` középső bájtjának 0x10 bitje = valós idejű védelem BE."""
    try:
        return bool(int(state) & 0x1000)
    except (TypeError, ValueError):
        return False


def is_microsoft_av(name):
    low = (name or '').lower()
    return 'defender' in low or 'microsoft' in low


def evaluate(data):
    """A nyers lekérdezésből verdikt. `data` None = a lekérdezés elbukott.
    Visszatérés: {'verdict', 'active', 'events', 'procs'} (tiszta függvény, tesztelhető)."""
    if not isinstance(data, dict):
        return {'verdict': 'unknown', 'active': [], 'events': [], 'procs': []}
    products = data.get('Products') or []
    if isinstance(products, dict):          # ConvertTo-Json egy elemű tömböt kibont
        products = [products]
    active = [p.get('Name') or '?' for p in products if product_enabled(p.get('State'))]
    events = data.get('Events') or []
    if isinstance(events, dict):
        events = [events]
    own = [e for e in events if any(m in (e.get('Msg') or '').lower() for m in OWN_MARKERS)]
    procs = data.get('Procs') or []
    if isinstance(procs, dict):
        procs = [procs]
    if own:
        verdict = 'defender'
    elif any(not is_microsoft_av(n) for n in active):
        verdict = 'thirdparty'
    elif data.get('ProductsOk') and data.get('EventsOk'):
        verdict = 'none'
    else:
        verdict = 'unknown'
    return {'verdict': verdict, 'active': active, 'events': own, 'procs': procs}


def report_lines(result, what):
    """A technikusnak szóló képernyő-szöveg. `what` = mi akadt el (pl. 'a driver-mentés')."""
    v = result['verdict']
    exe = 'C:\\DriverVarazslo mappát és a DriverVarazslo.exe programot'
    if v == 'defender':
        e = result['events'][0]
        return [f'🛡️ A VÍRUSIRTÓ (Microsoft Defender) BLOKKOLJA A PROGRAMOT - ezért akadt el {what}.',
                f'   Bizonyíték a Defender naplójából ({e.get("Time")}, esemény {e.get("Id")}): '
                f'{" ".join((e.get("Msg") or "").split())[:300]}',
                f'👉 TEENDŐ: Windows Biztonság > Vírus- és veszélyforrás-védelem > Kizárások: vedd fel a '
                f'{exe}, majd indítsd újra az 1 kattintásos fixet.']
    if v == 'thirdparty':
        names = ', '.join(n for n in result['active'] if not is_microsoft_av(n))
        return [f'🛡️ VALÓSZÍNŰLEG A VÍRUSIRTÓ ({names}) TARTJA FEL A PROGRAMOT - ezért akadt el {what}.',
                '   (Ez nem bizonyított: ennek a vírusirtónak nincs olyan naplója, amit ki tudnánk olvasni, '
                'de be van kapcsolva, és a Windows driver-műveletei nem válaszolnak.)',
                f'👉 TEENDŐ: kapcsold ki ideiglenesen a(z) {names} védelmét (vagy vedd fel kivételnek a '
                f'{exe}), majd indítsd újra az 1 kattintásos fixet.']
    if v == 'none':
        lines = [f'❗ NEM A VÍRUSIRTÓ AKASZTOTTA MEG {what.upper()} - a Defender naplójában nincs blokkolás, '
                 'és más vírusirtó nincs bekapcsolva. Ez a Windows driver-tárának beragadása vagy program-hiba.']
        old = [p for p in result['procs'] if (p.get('AgeS') or 0) > 600]
        if old:
            lines.append('   Régóta futó Windows-telepítőfolyamat: ' + ', '.join(
                f'{p.get("Name")} (PID {p.get("Id")}, {int(p.get("AgeS") or 0) // 60} perce)' for p in old))
        lines.append('👉 TEENDŐ: indítsd újra a gépet, futtasd újra a fixet, és KÜLDD EL A NAPLÓT a fejlesztőnek.')
        return lines
    return [f'⚠️ Nem eldönthető, hogy a vírusirtó akasztotta-e meg {what}: a vírusirtó-lekérdezés '
            'maga is elbukott (a Windows parancsai most nem válaszolnak).',
            f'👉 TEENDŐ: ha van vírusirtó a gépen, kapcsold ki ideiglenesen (vagy vedd fel kivételnek a '
            f'{exe}), indítsd újra a gépet és a fixet. Ha így is elakad, küldd el a naplót.']


def _log_setupapi_tail(max_bytes=6000):
    """A `setupapi.dev.log` vége a naplóba: a második, független tanú arról, hogy a
    driver-tár min állt (CLAUDE.md 4. fejezet)."""
    path = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'INF', 'setupapi.dev.log')
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            tail = f.read().decode('utf-8', 'replace')
        logging.warning(f"[AVCHECK] setupapi.dev.log vége ({path}):\n{tail}")
    except Exception as e:
        logging.warning(f"[AVCHECK] A setupapi.dev.log nem olvasható: {e}")


def diagnose_hang(run_fn, what):
    """Egy beragadt Windows-művelet után: vírusirtó-e az ok. Mindent naplóz.
    Visszatérés: (eredmény-dict, képernyő-sorok). Soha nem dob."""
    data = None
    try:
        res = run_fn(['powershell', '-NoProfile', '-Command', _script()], timeout=AVCHECK_TIMEOUT)
        rc = getattr(res, 'returncode', None)
        out = (getattr(res, 'stdout', '') or '').strip()
        if rc == CMD_TIMEOUT_RETURNCODE:
            logging.error(f"[AVCHECK] A vírusirtó-lekérdezés is beragadt ({AVCHECK_TIMEOUT} mp).")
        elif out:
            data = json.loads(out[out.find('{'):])
        else:
            logging.warning(f"[AVCHECK] A vírusirtó-lekérdezés üres kimenetet adott (rc={rc}).")
    except Exception as e:
        logging.warning(f"[AVCHECK] A vírusirtó-lekérdezés kiértékelése elbukott: {e}", exc_info=True)
    result = evaluate(data)
    lines = report_lines(result, what)
    level = logging.ERROR if result['verdict'] in ('none', 'unknown') else logging.WARNING
    logging.log(level, f"[AVCHECK] Verdikt: {result['verdict']} (mi akadt el: {what}); "
                       f"bekapcsolt vírusirtók: {result['active']}; ránk vonatkozó Defender-esemény: "
                       f"{len(result['events'])}; PnP-folyamatok: {result['procs']}; "
                       f"nyers: {json.dumps(data, ensure_ascii=False)[:3000] if data else None}")
    _log_setupapi_tail()
    return result, lines
