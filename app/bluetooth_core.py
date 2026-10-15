"""Bluetooth tiszta lap - KÖZÖS mag (GUI + CLI): minden párosítás és minden Bluetooth-
eszközcsomópont törlése, hogy a Windows Bluetooth-része nulláról induljon.

MIÉRT KELL KÜLÖN (2026-10-14, explicit user decision: *"legyen egy olyan rész is ami a
bluetoothos cuccokat cleareli ki teljesen es tiszta uj lappal tudok indulni"*). MÉRVE a
felhasználó gépén (ASUS USB-BT500): a Beállítások két "Forcell Master Pro"-t mutatott -
két KÜLÖN párosítás két MAC-címmel (3B9ED7071C80, D52C42B1345B), mindkettő ~12
csomóponttal; mind a 40 BT-csomópont NEM jelenlévő volt. A szellemeszköz-törlés ezen nem
segít, mert (1) a `System`/`Net` osztályt kihagyja, és (2) a Beállítások listája a
párosítás-adatbázisból jön (`BTHPORT\\Parameters\\Devices` + `Keys`), amit semmi nem töröl.
Az eszközönkénti "Eszköz eltávolítása" pedig percekig pörög, mert a verem közben a
rádión keresztül próbál beszélni az eszközzel.

A SORREND ezért: szolgáltatások le -> párosítások törlése SYSTEM-ként (a `Keys` ACL-je csak
SYSTEM-nek ad jogot, mérve: admin nélkül "Requested registry access is not allowed") ->
csomópontok törlése -> visszaolvasás (a csomópontok ÉS a rádió) -> szolgáltatások vissza
(`finally`, megszakításnál is).

A RÁDIÓT NEM TILTJUK LE (2026-10-15, visszavonva): a `pnputil /disable-device` 3010-nél a
KÖVETKEZŐ indulásra ütemezi a letiltást, az `/enable-device` ezt nem vonja vissza ("pending
system reboot", rc=50) - újraindítás után a rádió Code 22-vel halott maradt (mérve a
felhasználó gépén). Ugyanez lett volna egy letiltás és visszakapcsolás közötti
programbezárás vége. Élő rádió mellett a törlés mérve 29/29 csomópont, mind 0,0 mp.

A HIBÁS BT-T MEGJAVÍTJA (2026-10-15, explicit user decision: *"hogyha el van baszva a
bluetooth akkor csinálja meg a program"*): a kör végén (`finally`) a letiltott rádiót
engedélyezi, a más hibakódosat újraindítja, a letiltott `bthserv`-et kézire állítja és
elindítja. A verdikt a visszaolvasott hibakód.

1. elv: ez TÖRLŐ funkció, NINCS benne kivétel - minden BT-párosítás megy, a technikus BT-
egere/billentyűzete is (a megerősítő ezt kimondja). A rádió (adapter) maga nem párosítás,
annak a csomópontja és a drivere marad.
"""

# === AUTO-IMPORTS ===
import logging
import os
import re
import time
from app.common import CMD_TIMEOUT_RETURNCODE, _app_data_dir, _ps_quote
from app.win32 import PC_CONTAINER_ID
# === /AUTO-IMPORTS ===

# A Bluetooth-verem saját enumerátorai. A `BTH\MS_*` protokoll-csomópontokat (RFCOMM,
# PAN, LE-enumerátor) a rádió indulásakor a Windows újra létrehozza.
BT_ENUMERATORS = ('BTHENUM\\', 'BTHLE\\', 'BTHLEDEVICE\\', 'BTHHFENUM\\', 'BTH\\')
# A felhasználói BT-szolgáltatás neve felhasználónként egyedi utótagot kap.
BT_SERVICES = ('bthserv', 'BTAGService', 'BthAvctpSvc')
BT_STEP_TIMEOUT = 60          # mp - egy pnputil / szolgáltatás-lépés felső határa
BT_MAX_STUCK = 3              # ennyi egymás utáni beragadt törlés után feladjuk (újraindítás kell)
BT_RADIO_SETTLE = 8           # mp - ennyit várunk, hogy a javított rádió hibakód nélkül elinduljon (nem mért)
CM_PROB_DISABLED = 22
BT_CLEAR_TASK = 'DriverVarazsloBtClear'


def _is_bt_enum(dev_id):
    return (dev_id or '').upper().startswith(BT_ENUMERATORS)


def select_bluetooth_nodes(nodes):
    """(rádiók, törlendő csomópontok) a teljes eszközfából.

    Rádió = `Bluetooth` osztályú, nem BT-enumerátoros csomópont (USB/PCI adapter).
    Törlendő = minden BT-enumerátoros csomópont + ami egy BT-eszköz KONTÉNERÉBEN van (a
    `HID\\{00001124-…}` játékvezérlő-csomópont, hang-végpontok). A gép konténere és a
    rádiók konténere KIMARAD: mérve a USB-s rádiónak saját konténere van, és a `BTH\\MS_*`
    csomópontok abban ülnek - enélkül a kibontás a rádiót is törölné."""
    radios = [n for n in nodes
              if (n.get('cls') or '').lower() == 'bluetooth' and not _is_bt_enum(n.get('id'))]
    radio_ids = {n['id'] for n in radios}
    skip_cont = {PC_CONTAINER_ID} | {n.get('container') for n in radios}
    bt = [n for n in nodes if _is_bt_enum(n.get('id'))]
    conts = {n.get('container') for n in bt if n.get('container')} - skip_cont
    targets = [n for n in nodes if n['id'] not in radio_ids
               and (_is_bt_enum(n['id']) or (n.get('container') and n.get('container') in conts))]
    # A gyerek-csomópontok (HID, szolgáltatások) előbb, a `Dev_` szülők és a `BTH\` utána.
    targets.sort(key=lambda n: (_is_bt_enum(n['id']), '\\DEV_' in n['id'].upper(),
                                n['id'].upper().startswith('BTH\\')))
    return radios, targets


def node_label(n):
    return n.get('friendly') or n.get('desc') or n.get('id')


# --- Párosítások törlése SYSTEM-ként ---------------------------------------------------
# A kimenet a result-fájlba megy (a SYSTEM-feladat kimenetét másképp nem látjuk):
#   DEL: <hely> | <eszköznév>    HIBA: <szöveg>    DELETED: n    LEFT: n    DONE
CLEAR_PAIRINGS_PS = r'''
$res = '__RES__'
$base = 'HKLM:\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters'
$mac = '^[0-9A-Fa-f]{12}$'
$out = New-Object System.Collections.Generic.List[string]
function DevName($m) {
  try { $b = (Get-ItemProperty -Path "$base\Devices\$m" -Name Name -ErrorAction Stop).Name
        return ([Text.Encoding]::UTF8.GetString($b)).TrimEnd([char]0) } catch { return '' }
}
function Pass($delete) {
  $n = 0
  foreach ($k in @(Get-ChildItem "$base\Devices" -ErrorAction SilentlyContinue)) {
    if ($k.PSChildName -notmatch $mac) { continue }
    $n++
    if ($delete) {
      $nm = DevName $k.PSChildName
      try { Remove-Item -Path $k.PSPath -Recurse -Force -ErrorAction Stop; $out.Add("DEL: Devices\" + $k.PSChildName + " | " + $nm) }
      catch { $out.Add("HIBA: Devices\" + $k.PSChildName + ": " + $_.Exception.Message) }
    }
  }
  foreach ($r in @(Get-ChildItem "$base\Keys" -ErrorAction SilentlyContinue)) {
    foreach ($k in @(Get-ChildItem $r.PSPath -ErrorAction SilentlyContinue)) {
      if ($k.PSChildName -notmatch $mac) { continue }
      $n++
      if ($delete) {
        try { Remove-Item -Path $k.PSPath -Recurse -Force -ErrorAction Stop; $out.Add("DEL: Keys\" + $r.PSChildName + "\" + $k.PSChildName + " | LE-kulcs") }
        catch { $out.Add("HIBA: Keys\" + $k.PSChildName + ": " + $_.Exception.Message) }
      }
    }
    foreach ($v in @($r.Property)) {
      if ($v -notmatch $mac) { continue }
      $n++
      if ($delete) {
        try { Remove-ItemProperty -Path $r.PSPath -Name $v -Force -ErrorAction Stop; $out.Add("DEL: Keys\" + $r.PSChildName + "[" + $v + "] | kapcsolat-kulcs") }
        catch { $out.Add("HIBA: Keys\" + $v + ": " + $_.Exception.Message) }
      }
    }
  }
  return $n
}
try {
  $out.Add("DELETED: " + (Pass $true))
  $out.Add("LEFT: " + (Pass $false))
} catch { $out.Add("HIBA: " + $_.Exception.Message) }
$out.Add("DONE")
$out | Out-File -FilePath $res -Encoding utf8
'''


def parse_clear_result(text):
    """A SYSTEM-szkript result-fájlja -> dict. `left` None, ha a szkript nem ért végig
    (nincs DONE / nincs LEFT sor): az "nem ellenőrizhető", nem "0 maradt"."""
    out = {'deleted': [], 'errors': [], 'left': None, 'done': False}
    for line in (text or '').lstrip('﻿').splitlines():
        line = line.strip()
        if line.startswith('DEL:'):
            out['deleted'].append(line[4:].strip())
        elif line.startswith('HIBA:'):
            out['errors'].append(line[5:].strip())
        elif line.startswith('LEFT:'):
            m = re.search(r'\d+', line)
            out['left'] = int(m.group()) if m else None
        elif line == 'DONE':
            out['done'] = True
    if not out['done']:
        out['left'] = None
    return out


def clear_pairings_as_system(run, log):
    """A párosítás-adatbázis törlése egy egyszer futó SYSTEM-feladattal. A verdikt a
    szkript SAJÁT visszaolvasása (LEFT sor), nem a feladat kilépési kódja."""
    d = _app_data_dir()
    ps1 = os.path.join(d, 'bt_clear.ps1')
    res = os.path.join(d, 'bt_clear_result.txt')
    logging.warning(f"[BT] PÁROSÍTÁSOK TÖRLÉSE (BTHPORT\\Parameters\\Devices + Keys) SYSTEM-feladattal: "
                    f"'{BT_CLEAR_TASK}' -> {ps1}")
    try:
        if os.path.exists(res):
            os.remove(res)
        with open(ps1, 'w', encoding='utf-8-sig', newline='\r\n') as fh:
            fh.write(CLEAR_PAIRINGS_PS.replace('__RES__', _ps_quote(res)).strip() + '\n')
    except OSError as e:
        logging.error(f"[BT] A SYSTEM-szkript nem írható: {e}")
        return {'deleted': [], 'errors': [f'a szkript nem írható: {e}'], 'left': None, 'done': False}
    q_res = _ps_quote(res)
    ps = (
        f"$tn = '{BT_CLEAR_TASK}'; "
        "try { $a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
        f"'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{_ps_quote(ps1)}\"'; "
        "$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 2); "
        "Register-ScheduledTask -TaskName $tn -Action $a -Principal $p -Settings $s -Force -ErrorAction Stop | Out-Null; "
        "Start-ScheduledTask -TaskName $tn -ErrorAction Stop; "
        "$sw = [Diagnostics.Stopwatch]::StartNew(); "
        f"while ($sw.Elapsed.TotalSeconds -lt 90 -and -not ((Test-Path '{q_res}') -and "
        f"((Get-Content '{q_res}' -Raw -ErrorAction SilentlyContinue) -match 'DONE'))) {{ Start-Sleep -Milliseconds 500 }}; "
        "'WAITED:' + [int]$sw.Elapsed.TotalSeconds "
        "} catch { 'FAIL:' + $_.Exception.Message } "
        "finally { Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue }"
    )
    r = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps], timeout=150)
    launch = ((getattr(r, 'stdout', '') or '') + (getattr(r, 'stderr', '') or '')).strip()
    logging.info(f"[BT] SYSTEM-feladat kimenete: {launch[:400]}")
    text = ''
    try:
        with open(res, 'r', encoding='utf-8-sig', errors='replace') as fh:
            text = fh.read()
    except OSError as e:
        logging.error(f"[BT] A SYSTEM-szkript eredménye nem olvasható ({res}): {e}")
    out = parse_clear_result(text)
    if not out['done']:
        out['errors'].append('a SYSTEM-feladat nem ért végig' + (f' ({launch[:200]})' if launch else ''))
    for p in (ps1, res):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError as e:
            logging.warning(f"[BT] {p} nem törölhető: {e}")
    for line in out['deleted']:
        logging.info(f"[BT] Párosítás törölve: {line}")
    for line in out['errors']:
        logging.warning(f"[BT] Párosítás-törlési hiba: {line}")
    return out


# --- Szolgáltatások ---------------------------------------------------------------------
def stop_bt_services(run):
    """A futó BT-szolgáltatások leállítása. Visszaad: a leállított nevek listája (csak
    ezeket indítjuk vissza - amit a felhasználó maga állított le, az úgy marad)."""
    names = ', '.join(f"'{s}'" for s in BT_SERVICES)
    ps = (f"$n = @({names}) + @(Get-Service -Name 'BluetoothUserService_*' -ErrorAction SilentlyContinue | "
          "ForEach-Object Name); foreach ($x in $n) { $s = Get-Service -Name $x -ErrorAction SilentlyContinue; "
          "if ($s -and $s.Status -eq 'Running') { try { Stop-Service -Name $x -Force -ErrorAction Stop; "
          "'STOPPED:' + $x } catch { 'STOPFAIL:' + $x + ':' + $_.Exception.Message } } }")
    r = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps], timeout=BT_STEP_TIMEOUT)
    out = getattr(r, 'stdout', '') or ''
    stopped = [l[8:].strip() for l in out.splitlines() if l.startswith('STOPPED:')]
    for l in out.splitlines():
        if l.startswith('STOPFAIL:'):
            logging.warning(f"[BT] Szolgáltatás nem állt le: {l[9:].strip()}")
    logging.info(f"[BT] Leállított szolgáltatások: {stopped or 'nincs (egyik sem futott)'}")
    return stopped


def start_bt_services(run, names, ensure=(), log=None):
    """A leállított szolgáltatások (`names`) + az `ensure` szolgáltatások indítása, visszaolvasva;
    ami le van tiltva, azt előbb kézire állítja (a `bthserv` alapja is kézi). Visszaad: ami NEM fut."""
    names = list(dict.fromkeys(list(ensure) + list(names)))
    if not names:
        return []
    logging.info(f"[BT] Szolgáltatások indítása (ha le van tiltva: kézire állítva): {names}")
    lst = ', '.join(f"'{_ps_quote(s)}'" for s in names)
    ps = (f"foreach ($x in @({lst})) {{ $s = Get-Service -Name $x -ErrorAction SilentlyContinue; "
          "if ($s -and $s.StartType -eq 'Disabled') { try { Set-Service -Name $x -StartupType Manual -ErrorAction Stop; "
          "'UNDISABLED:' + $x } catch { 'UNDISFAIL:' + $x + ':' + $_.Exception.Message } }; "
          "try { Start-Service -Name $x -ErrorAction Stop } catch { }; "
          "$s = Get-Service -Name $x -ErrorAction SilentlyContinue; "
          "if (-not $s -or $s.Status -ne 'Running') { 'NOTRUNNING:' + $x } }")
    r = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps], timeout=BT_STEP_TIMEOUT)
    if getattr(r, 'returncode', 1) == CMD_TIMEOUT_RETURNCODE:
        return names
    lines = (getattr(r, 'stdout', '') or '').splitlines()
    for l in lines:
        if l.startswith('UNDISABLED:'):
            logging.warning(f"[BT] Letiltott szolgáltatás kézire állítva: {l[11:].strip()}")
            if log:
                log(f'🔧 A(z) {l[11:].strip()} szolgáltatás le volt tiltva - visszakapcsolva.')
        elif l.startswith('UNDISFAIL:'):
            logging.warning(f"[BT] A szolgáltatás tiltása nem oldható fel: {l[10:].strip()}")
    bad = [l[11:].strip() for l in lines if l.startswith('NOTRUNNING:')]
    if bad:
        logging.warning(f"[BT] Nem indult vissza: {bad}")
    return bad


# --- A rádió (adapter) javítása ---------------------------------------------------------
def _radio_states(enum_nodes, radios):
    """{id: csomópont | None}, vagy None, ha az eszközfa nem olvasható (az nem "rendben van")."""
    try:
        now = {n['id'].upper(): n for n in enum_nodes()}
    except Exception as e:
        logging.error(f"[BT] A rádió állapota nem olvasható: {e}", exc_info=True)
        return None
    return {r['id']: now.get(r['id'].upper()) for r in radios}


def _radio_ok(n):
    return bool(n and n.get('present') and not n.get('problem'))


def repair_radios(run, log, enum_nodes, radios):
    """A hibás rádiók javítása: letiltott (Code 22) -> engedélyezés, más hibakód -> újraindítás.
    Visszaad: (megjavítottak, továbbra is hibásak) név szerint; a verdikt a visszaolvasás."""
    st = _radio_states(enum_nodes, radios)
    tried = []
    for r in (radios if st is not None else []):
        n = st[r['id']]
        if _radio_ok(n) or not n or not n.get('present'):
            continue  # ép, vagy közben kihúzták (azt nem tudjuk javítani; a visszaolvasás jelenti)
        code, name = n.get('problem'), node_label(r)
        verb = '/enable-device' if code == CM_PROB_DISABLED else '/restart-device'
        logging.warning(f"[BT] HIBÁS RÁDIÓ (Code {code}) -> pnputil {verb}: {r['id']} ({name})")
        log(f"🔧 Az adapter {'le volt tiltva - visszakapcsolás' if code == CM_PROB_DISABLED else f'hibát jelez (Code {code}) - újraindítás'}: {name}")
        res = run(['pnputil', verb, r['id']], timeout=BT_STEP_TIMEOUT, ok_codes=(0,))
        rc = getattr(res, 'returncode', 1)
        if rc != 0:
            logging.warning(f"[BT] pnputil {verb} rc={rc}: {(getattr(res, 'stdout', '') or '')[-300:]}")
        tried.append((r, rc))
    # Ha a pnputil sikert mondott, az indulásra várunk; különben egy visszaolvasás elég.
    deadline = time.monotonic() + (BT_RADIO_SETTLE if any(rc == 0 for _, rc in tried) else 0)
    while tried:
        st = _radio_states(enum_nodes, radios)
        if (st is not None and all(_radio_ok(st[r['id']]) for r, _ in tried)) or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if st is None:
        return [], [node_label(r) for r in radios]
    fixed = [node_label(r) for r, _ in tried if _radio_ok(st[r['id']])]
    bad = [node_label(r) for r in radios if not _radio_ok(st[r['id']])]
    for name in fixed:
        logging.info(f"[BT] Rádió megjavítva (visszaolvasva): {name}")
    for r in radios:
        if not _radio_ok(st[r['id']]):
            n = st[r['id']]
            logging.error(f"[BT] A rádió NEM működik: {r['id']} ({node_label(r)}) - "
                          f"{'eltűnt' if not n or not n.get('present') else 'Code ' + str(n.get('problem'))}")
    return fixed, bad


# --- A teljes kör -----------------------------------------------------------------------
def clear_bluetooth(run, log, enum_nodes, check_cancel=None, system_clear=None):
    """A Bluetooth tiszta lap. `log(sor)` a képernyőre ír, `enum_nodes()` a teljes eszközfa
    (win32.enumerate_device_nodes), `system_clear(run, log)` a SYSTEM-es registry-lépés
    (tesztben csonkolható). Visszaad: dict, `ok` csak akkor igaz, ha a visszaolvasás szerint
    nem maradt se párosítás, se BT-csomópont, és a rádió + a szolgáltatások visszaálltak."""
    check_cancel = check_cancel or (lambda: False)
    system_clear = system_clear or clear_pairings_as_system
    rep = {'ok': False, 'cancelled': False, 'pairings': [], 'pairings_left': None,
           'removed': 0, 'total': 0, 'left_nodes': [], 'radio_fixed': [], 'radio_problems': [],
           'services_down': [], 'errors': [], 'stuck': False}
    try:
        nodes = enum_nodes()
    except Exception as e:
        logging.error(f"[BT] Az eszközfa nem olvasható: {e}", exc_info=True)
        rep['errors'].append(f'az eszközfa nem olvasható: {e}')
        return rep
    radios, targets = select_bluetooth_nodes(nodes)
    rep['total'] = len(targets)
    logging.info(f"[BT] Rádiók: {[(r['id'], node_label(r), r.get('problem')) for r in radios]}; "
                 f"törlendő csomópont: {len(targets)}")
    for n in targets:
        logging.debug(f"[BT]   törlendő: {n['id']} | {node_label(n)} | jelen={n.get('present')}")
    log(f"📶 Bluetooth-adapter: {', '.join(node_label(r) for r in radios) or 'nincs a gépben'}")
    log(f"🔎 {len(targets)} Bluetooth-eszközcsomópont (párosított eszközök és szolgáltatásaik).")

    stopped = []
    # A rádiót nem tiltjuk le (lásd a modul elején); a végén a hibásat javítjuk - az eleve
    # hibásat is (pl. Build 382-385 óta letiltott), különben halott BT mellé ✅ jönne.
    present_radios = [r for r in radios if r.get('present')]
    try:
        log('⏸️ Bluetooth-szolgáltatások leállítása...')
        stopped = stop_bt_services(run)
        if check_cancel():
            rep['cancelled'] = True
            return rep

        log('🗝️ Párosítások törlése (a Windows párosítás-adatbázisa, SYSTEM-jogon)...')
        pr = system_clear(run, log)
        rep['pairings'] = pr['deleted']
        rep['pairings_left'] = pr['left']
        rep['errors'] += pr['errors']
        for line in pr['deleted']:
            name = line.split('|', 1)[-1].strip()
            if line.startswith('Devices'):
                log(f'  🗑 {name or line.split("|")[0].strip()}')
        if check_cancel():
            rep['cancelled'] = True
            return rep

        log(f'🧹 {len(targets)} eszközcsomópont eltávolítása...')
        stuck_row = 0
        for n in targets:
            if check_cancel():
                rep['cancelled'] = True
                return rep
            res = run(['pnputil', '/remove-device', n['id']], timeout=BT_STEP_TIMEOUT, ok_codes=(0, 1, 3010))
            rc = getattr(res, 'returncode', 1)
            if rc == CMD_TIMEOUT_RETURNCODE:
                stuck_row += 1
                logging.warning(f"[BT] Beragadt törlés ({BT_STEP_TIMEOUT}s): {n['id']}")
                log(f'  ⏱️ Beragadt: {node_label(n)}')
                if stuck_row >= BT_MAX_STUCK:
                    rep['stuck'] = True
                    log(f'  ❗ {BT_MAX_STUCK} egymás utáni beragadás - a többit kihagyom, újraindítás kell.')
                    break
                continue
            stuck_row = 0
            if rc in (0, 3010):
                rep['removed'] += 1
            # Az 1-es kód tipikusan: a csomópont már nincs meg (a szülőjével együtt ment) -
            # a végső döntés úgyis a lenti visszaolvasás.

        # Visszaolvasás a szolgáltatások visszaindítása ELŐTT: utána a Windows a `BTH\MS_*`
        # protokoll-csomópontokat jogosan újra létrehozza.
        try:
            ids = {n['id'].upper() for n in enum_nodes()}
            rep['left_nodes'] = [node_label(n) for n in targets if n['id'].upper() in ids]
        except Exception as e:
            logging.error(f"[BT] A visszaolvasás nem sikerült: {e}", exc_info=True)
            rep['left_nodes'] = None
            rep['errors'].append('a csomópontok visszaolvasása nem sikerült')
    finally:
        if present_radios:
            rep['radio_fixed'], rep['radio_problems'] = repair_radios(run, log, enum_nodes, present_radios)
        # A `bthserv` nélkül nincs Bluetooth: rádiós gépen akkor is fusson, ha nem mi állítottuk le.
        rep['services_down'] = start_bt_services(run, stopped, ensure=('bthserv',) if present_radios else (), log=log)

    rep['ok'] = (not rep['cancelled'] and not rep['errors'] and not rep['stuck']
                 and rep['pairings_left'] == 0 and rep['left_nodes'] == []
                 and not rep['radio_problems'] and not rep['services_down'])
    logging.info(f"[BT] Eredmény: {rep}")
    return rep
