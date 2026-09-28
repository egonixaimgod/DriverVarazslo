# -*- coding: utf-8 -*-
"""A DRIVEREKHEZ TARTOZÓ MICROSOFT STORE-ALKALMAZÁSOK SZINKRONJA (2026-09-27).

MIÉRT KELL EZ EGYÁLTALÁN - mért tény, nem feltevés:
  A modern (DCH) driverek a vezérlőpultjukat NEM a csomagban hozzák, hanem az INF-ben
  KÉRIK a Store-ból (Hardware Support App). Az INF-ben ez így néz ki (élőben kiolvasva
  a fejlesztői gép NVIDIA display-INF-jéből, oem9.inf):

      AddSoftware = NVIDIACorp.NVIDIAControlPanel, 0, nv_store_software_CPL_install64
      [nv_store_software_CPL_install64]
      SoftwareType = 2
      SoftwareVersion = 8.1.944.0
      SoftwareID = pfn://NVIDIACorp.NVIDIAControlPanel_56jybvy8sckqj

  Ezt a Windows MAGÁTÓL csak akkor telepíti, ha az "Eszköztelepítési beállítások"
  engedik a gyártói alkalmazások letöltését - a lánc viszont `SearchOrderConfig=0`-t ír
  (wusettings_core), ami PONTOSAN ezt a kapcsolót kapcsolja ki (szándékosan: különben a
  WU a lábak közt felülírná a driverkészletet). Vagyis a fix után a Windows SOHA nem
  rakja fel ezeket - ha mi nem, senki. A régi záró lépés (MDM UpdateScanMethod) csak a
  MÁR FENT LÉVŐ alkalmazásokat frissíti, hiányzót nem telepít, és a visszatérési
  értékét sem nézte (mindig "✅ kész"-t írt).

A LÁNC:
  1. a jelen lévő eszközök által HASZNÁLT INF-ekből kiolvassuk a SoftwareType=2 bejegyzéseket
     (csomagnév = PFN + minimális verzió);
  2. a PFN-t a Store saját katalógusából termékazonosítóra fordítjuk
     (displaycatalog.mp.microsoft.com, mérve 202 ms, NVIDIA -> 9NF8H0H7WMLT);
  3. telepítés/frissítés: elsőként `winget --source msstore` (mérve 0,5 mp egy naprakész
     alkalmazásra), tartalékként a Windows saját Store-telepítő API-ja (AppInstallManager,
     WinRT) - friss Windowson a winget alias még nem feltétlenül regisztrált;
  4. A VERDIKT A VISSZAOLVASÁS: Get-AppxPackage szerint fent van-e, a kért verzióval.
     A winget/Store visszatérési kódja csak tájékoztató.

A modul semmit nem töröl és semmit nem kapcsol ki - csak telepít."""
import json
import logging
import os
import re
import time

from app import wu_core

STORE_CATALOG_LOOKUP_URL = ('https://displaycatalog.mp.microsoft.com/v7.0/products/lookup'
                            '?alternateId=PackageFamilyName&Value={pfn}&market=US'
                            '&languages=en-US&fieldsTemplate=Details')
WINGET_TIMEOUT = 600          # egy alkalmazás telepítése legfeljebb ennyi mp
APPINSTALL_TIMEOUT = 600      # a WinRT-tartalék ugyanennyit kap
STORE_UPDATE_SCAN_TIMEOUT = 600

# winget-kódok (APPINSTALLER_CLI_ERROR_*), amik NEM hibák: "nincs újabb" / "már fent van".
# ELŐJEL NÉLKÜLI alakban tároljuk, és az összehasonlítás előtt a kódot is arra hozzuk:
# a Python subprocess a Windows DWORD-ot ELŐJEL NÉLKÜL adja (mérve: 2316632107), a
# PowerShell $LASTEXITCODE viszont előjelesen (-1978335189). Az első változat csak az
# előjeles alakot ismerte, és egy naprakész alkalmazást hibának vett - majd a tartalék úton
# fölöslegesen újratelepítette (élő teszt fogta meg, 2026-09-27).
WINGET_UPTODATE_CODES = {
    0x8A15002B,   # UPDATE_NOT_APPLICABLE (mérve: naprakész alkalmazásra)
    0x8A150061,   # PACKAGE_ALREADY_INSTALLED
}


# Store-hibakódok magyarázata. CSAK MÉRT kód kerül ide (kitalált jelentés nem):
STORE_ERROR_TEXT = {
    # 2026-09-27, mindkét úton (winget + AppInstallManager) ugyanez: a Realtek Audio
    # Console-t egy olyan gépre kértük, aminek a Realtek-drivere NEM deklarálja. A
    # gyártói támogató-alkalmazásokat (HSA) a Store csak a hozzájuk tartozó driverrel
    # rendelkező gépre engedi.
    '0x803FB005': 'a Store szerint ez az alkalmazás erre a gépre nem telepíthető '
                  '(a gyártó csak a hozzá tartozó driverrel engedi)',
}


def store_error_text(detail):
    m = re.search(r'0x[0-9A-Fa-f]{8}', detail or '')
    if m:
        txt = STORE_ERROR_TEXT.get('0x' + m.group(0)[2:].upper())
        if txt:
            return f"{txt} [{m.group(0)}]"
    return (detail or '')[:160]


def _u32(rc):
    try:
        return int(rc) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None

_SECTION_RE = re.compile(r'^\s*\[([^\]]+)\]\s*$')
_PFN_RE = re.compile(r'pfn://\s*([A-Za-z0-9][A-Za-z0-9.\-]*_[a-z0-9]{13})', re.I)


def _split_sections(text):
    sections, cur = {}, None
    for line in (text or '').splitlines():
        line = line.split(';', 1)[0]
        m = _SECTION_RE.match(line)
        if m:
            cur = m.group(1).strip().lower()
            sections.setdefault(cur, [])
            continue
        if cur is not None and line.strip():
            sections[cur].append(line.strip())
    return sections


def _kv(line):
    if '=' not in line:
        return None, None
    k, v = line.split('=', 1)
    return k.strip().lower(), v.strip().strip('"').strip()


def parse_inf_store_apps(text):
    """Egy INF szövegéből a Store-alkalmazás igények: [{'pfn', 'min_version'}].

    Szekciónként dolgozik: egy AddSoftware-szekció SoftwareType=2-vel és pfn:// ID-vel
    egy igény. A SoftwareType=1 (a csomagban hozott telepítő) nem ide tartozik - azt a
    Windows a driver telepítésekor maga futtatja. Tiszta függvény."""
    out = {}
    for _name, lines in _split_sections(text).items():
        kv = {}
        for line in lines:
            k, v = _kv(line)
            if k:
                kv[k] = v
        if kv.get('softwaretype', '').strip() != '2':
            continue
        m = _PFN_RE.search(kv.get('softwareid', ''))
        if not m:
            continue
        pfn = m.group(1)
        ver = kv.get('softwareversion', '').strip()
        key = pfn.lower()
        prev = out.get(key)
        if not prev or version_tuple(ver) > version_tuple(prev['min_version']):
            out[key] = {'pfn': pfn, 'min_version': ver}
    return list(out.values())


def version_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r'\d+', v or '')[:4])
    except Exception:
        return ()


def version_ok(installed, required):
    """A telepített verzió eléri-e a kértet. Kért verzió nélkül a jelenlét elég."""
    if not required:
        return bool(installed)
    return bool(installed) and version_tuple(installed) >= version_tuple(required)


_SWTYPE_MARKERS = (b'softwaretype', 'softwaretype'.encode('utf-16-le'))


def _mentions_software_type(path):
    """Gyors előszűrő: van-e egyáltalán 'SoftwareType' a fájlban (UTF-16 is). Mérve: 19 INF /
    5,8 MB = 0,02 mp, tehát egy nagy DriverStore-ral is olcsó."""
    try:
        with open(path, 'rb') as f:
            b = f.read().lower()
    except OSError:
        return False
    return any(m in b for m in _SWTYPE_MARKERS)


def collect_required_store_apps(published_infs, inf_dir=None, present_hwid_sets=None):
    """A gép driverei által KÉRT Store-alkalmazások, PFN szerint összevonva:
    {pfn_lower: {'pfn', 'min_version', 'infs': [...]}}.

    KÉT FORRÁS, mert az egyik vak (2026-09-27):
      1. `published_infs`: a jelen lévő eszközök FŐ (function) driverei - ezt adja a
         Win32_PnPSignedDriver. Csakhogy az EXTENSION INF-eket nem nevezi meg (CLAUDE.md:
         "only ever names a device's FUNCTION driver"), a gyártók pedig a vezérlőpultjukat
         jellemzően pont az extension INF-ben kérik.
      2. `present_hwid_sets` (ha megadták): MINDEN telepített gyári INF, amelyik Store-
         alkalmazást kér ÉS a hardver-azonosítói illeszkednek egy jelen lévő eszközre. Így
         az extension/komponens INF is számít, egy korábbi gépről ottmaradt INF (vándorló
         teszt-lemez!) viszont nem."""
    inf_dir = inf_dir or os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'INF')
    pubs = {p.lower() for p in (published_infs or [])}
    candidates = set(pubs)
    if present_hwid_sets:
        try:
            candidates |= {f.lower() for f in os.listdir(inf_dir)
                           if f.lower().startswith('oem') and f.lower().endswith('.inf')}
        except OSError as e:
            logging.warning(f"[STOREAPP] Az INF-mappa nem olvasható ({inf_dir}): {e}")
    req = {}
    skipped_foreign = []
    for pub in sorted(candidates):
        path = os.path.join(inf_dir, pub)
        if not os.path.isfile(path) or not _mentions_software_type(path):
            continue
        try:
            text = wu_core._read_text_best_effort(path)
            apps = parse_inf_store_apps(text)
        except Exception as e:
            logging.debug(f"[STOREAPP] {pub} nem értelmezhető: {e}")
            continue
        if not apps:
            continue
        if pub not in pubs:
            ids = wu_core.extract_inf_hardware_ids(text)
            hit = any(wu_core._hwid_matches(i, h) for i in ids
                      for s in present_hwid_sets for h in s)
            if not hit:
                skipped_foreign.append(pub)
                continue
            logging.info(f"[STOREAPP] {pub}: nem fő driver, de egy jelen lévő eszközre illik "
                         f"(extension/komponens) - a kért alkalmazásai számítanak.")
        for a in apps:
            k = a['pfn'].lower()
            cur = req.setdefault(k, {'pfn': a['pfn'], 'min_version': a['min_version'], 'infs': []})
            if version_tuple(a['min_version']) > version_tuple(cur['min_version']):
                cur['min_version'] = a['min_version']
            cur['infs'].append(pub)
    if skipped_foreign:
        logging.info(f"[STOREAPP] Store-alkalmazást kérő, de a gép egyetlen eszközére sem illő "
                     f"(más gépről maradt) INF-ek - kihagyva: {skipped_foreign}")
    return req


INSTALLED_APPX_PS = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "@(Get-AppxPackage | Select-Object PackageFamilyName,Version) | ConvertTo-Json -Compress; "
    "'DONE'")


def installed_store_apps(run):
    """{pfn_lower: verzió} a jelenlegi felhasználóra. Hiba esetén None (a nemtudás
    nem üres lista: egy üres halmaz azt állítaná, hogy semmi nincs fent)."""
    try:
        res = run(['powershell', '-NoProfile', '-Command', INSTALLED_APPX_PS],
                  encoding='utf-8', timeout=120)
        out = (getattr(res, 'stdout', '') or '').strip()
        if 'DONE' not in out:
            logging.warning(f"[STOREAPP] Az alkalmazás-lista lekérdezése nem futott végig "
                            f"(rc={getattr(res, 'returncode', '?')}).")
            return None
        body = out.rsplit('DONE', 1)[0].strip()
        data = json.loads(body) if body else []
        if isinstance(data, dict):
            data = [data]
        apps = {}
        for d in data:
            pfn = (d.get('PackageFamilyName') or '').lower()
            if pfn:
                v = str(d.get('Version') or '')
                if version_tuple(v) >= version_tuple(apps.get(pfn, '')):
                    apps[pfn] = v
        if not apps:
            logging.warning("[STOREAPP] Az alkalmazás-lista ÜRES - ez hiba, nem eredmény.")
            return None
        return apps
    except Exception as e:
        logging.warning(f"[STOREAPP] Az alkalmazás-lista lekérdezése elhasalt: {e}")
        return None


def lookup_store_product(pfn, fetch):
    """PFN -> (termékazonosító, cím) a Store katalógusából. Hiba/nincs találat: (None, None).
    `fetch(url) -> str` (a hívó a közös, régi-Windows-barát letöltőt adja)."""
    try:
        txt = fetch(STORE_CATALOG_LOOKUP_URL.format(pfn=pfn))
        data = json.loads(txt or '{}')
        for p in data.get('Products') or []:
            pid = p.get('ProductId')
            if pid:
                title = ''
                lp = p.get('LocalizedProperties') or []
                if lp:
                    title = lp[0].get('ProductTitle') or ''
                return pid, title
        logging.warning(f"[STOREAPP] A Store katalógusában nincs termék ehhez: {pfn}")
    except Exception as e:
        logging.warning(f"[STOREAPP] A Store-katalógus lekérdezése elhasalt ({pfn}): {e}")
    return None, None


def install_via_winget(run, product_id):
    """(eredmény, részlet): 'ok' | 'uptodate' | 'missing' (nincs winget) | 'fail'."""
    # ELŐBB MEGNÉZZÜK, VAN-E WINGET (2026-09-28, terepi napló, Build 344): a hiánya várt
    # eset (friss / lecsupaszított Windows), a Store-API tartalék pedig működik - a vak
    # hívás viszont a `_run`-ban "[ERROR] [CMD] Kivétel: [WinError 2]" sort hagyott, ami egy
    # hibátlan futás naplójában hamis riasztás.
    import shutil
    alias = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WindowsApps', 'winget.exe')
    winget = shutil.which('winget') or (alias if os.path.exists(alias) else None)
    if not winget:
        return 'missing', 'a winget nincs telepítve/regisztrálva (nem található a PATH-on)'
    cmd = [winget, 'install', '--id', product_id, '--source', 'msstore',
           '--accept-package-agreements', '--accept-source-agreements',
           '--silent', '--disable-interactivity']
    try:
        res = run(cmd, encoding='utf-8', timeout=WINGET_TIMEOUT,
                  ok_codes=(0,) + tuple(WINGET_UPTODATE_CODES)
                  + tuple(c - 0x100000000 for c in WINGET_UPTODATE_CODES))
    except FileNotFoundError:
        return 'missing', 'a winget nincs telepítve/regisztrálva'
    except Exception as e:
        return 'fail', str(e)
    rc = _u32(getattr(res, 'returncode', None))
    lines = [l.strip() for l in (getattr(res, 'stdout', '') or '').splitlines()]
    tail = ' | '.join(l for l in lines if l and not set(l) <= set('-\\|/█▒ '))[-240:]
    if rc == 0:
        return 'ok', tail
    if rc in WINGET_UPTODATE_CODES:
        return 'uptodate', tail
    if rc == 9009:
        return 'missing', 'a winget nem indítható'
    return 'fail', f"rc={rc} {tail}"


def build_appinstall_ps(product_id, timeout_s=APPINSTALL_TIMEOUT):
    """A Windows saját Store-telepítő API-ja (AppInstallManager) - a winget tartaléka.
    Sor-protokoll: STATE|<állapot>|<százalék>, a végén DONE|<állapot>|<hibakód>."""
    pid = re.sub(r'[^A-Za-z0-9]', '', product_id)
    return (
        "$ErrorActionPreference='Stop'\n"
        "try {\n"
        "Add-Type -AssemblyName System.Runtime.WindowsRuntime\n"
        "$null=[Windows.ApplicationModel.Store.Preview.InstallControl.AppInstallManager,"
        "Windows.ApplicationModel.Store.Preview,ContentType=WindowsRuntime]\n"
        "$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { "
        "$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and "
        "$_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]\n"
        "$m = New-Object Windows.ApplicationModel.Store.Preview.InstallControl.AppInstallManager\n"
        f"$op = $m.StartAppInstallAsync('{pid}','',$false,$false)\n"
        "$t = $asTask.MakeGenericMethod([Windows.ApplicationModel.Store.Preview.InstallControl."
        "AppInstallItem]).Invoke($null,@($op))\n"
        "$t.Wait(60000) | Out-Null\n"
        "$item = $t.Result\n"
        "if (-not $item) { 'DONE|NoItem|0'; exit 0 }\n"
        f"$deadline = (Get-Date).AddSeconds({int(timeout_s)})\n"
        "$last = ''\n"
        "while ((Get-Date) -lt $deadline) {\n"
        "  $s = $item.GetCurrentStatus()\n"
        "  $st = [string]$s.InstallState\n"
        "  $line = 'STATE|' + $st + '|' + [int]$s.PercentComplete\n"
        "  if ($line -ne $last) { $line; $last = $line }\n"
        "  if ($st -in @('Completed','Canceled','Error')) {\n"
        "    'DONE|' + $st + '|' + ('0x{0:X8}' -f $s.ErrorCode.HResult); exit 0 }\n"
        "  Start-Sleep -Milliseconds 1000\n"
        "}\n"
        "'DONE|Timeout|0'\n"
        "} catch { 'DONE|Exception|' + $_.Exception.Message }\n")


def install_via_appinstaller(run, product_id):
    """(eredmény, részlet): 'ok' | 'fail'. Az 'ok' csak azt jelenti, hogy a Store
    'Completed'-et mondott - a valódi verdiktet a hívó a visszaolvasással mondja ki."""
    try:
        res = run(['powershell', '-NoProfile', '-Command', build_appinstall_ps(product_id)],
                  encoding='utf-8', timeout=APPINSTALL_TIMEOUT + 90)
    except Exception as e:
        return 'fail', str(e)
    out = getattr(res, 'stdout', '') or ''
    done = [l for l in out.splitlines() if l.startswith('DONE|')]
    if not done:
        return 'fail', f"rc={getattr(res, 'returncode', '?')} (nincs DONE sor)"
    parts = done[-1].split('|', 2)
    state = parts[1] if len(parts) > 1 else '?'
    code = parts[2] if len(parts) > 2 else ''
    if state == 'Completed':
        return 'ok', 'Completed'
    return 'fail', f"{state} {code}".strip()


def missing_store_apps(run, published_infs, inf_dir=None, present_hwid_sets=None):
    """Hálózat NÉLKÜL: a használt driverek által kért, de hiányzó / túl régi alkalmazások.
    Visszatérés: lista [{'pfn','name','min_version','installed'}], vagy None, ha a telepített
    alkalmazások listája nem volt lekérdezhető (a nemtudás nem "nincs hiány")."""
    req = collect_required_store_apps(published_infs, inf_dir=inf_dir,
                                      present_hwid_sets=present_hwid_sets)
    if not req:
        return []
    have = installed_store_apps(run)
    if have is None:
        return None
    out = []
    for key, info in req.items():
        now = have.get(key)
        if not version_ok(now, info['min_version']):
            out.append({'pfn': info['pfn'], 'name': info['pfn'].split('_', 1)[0],
                        'min_version': info['min_version'], 'installed': now})
    logging.info(f"[STOREAPP] Hiányzó/elavult driver-alkalmazás: {len(out)}/{len(req)} "
                 + ', '.join(f"{o['pfn']} (fent: {o['installed'] or '-'})" for o in out))
    return out


def sync_driver_store_apps(run, log, published_infs, fetch, check_cancel=None,
                           inf_dir=None, present_hwid_sets=None):
    """A driverekhez tartozó Store-alkalmazások telepítése/frissítése.

    Visszatérés: {'required': [...], 'ok': [...], 'installed': [...], 'failed': [...],
    'unverified': bool} - minden lista elem {'pfn','title','min_version',...}.
    `log(msg)` a képernyő-sor (GUI: task_progress, CLI: print)."""
    report = {'required': [], 'ok': [], 'installed': [], 'failed': [], 'unverified': False}
    req = collect_required_store_apps(published_infs, inf_dir=inf_dir,
                                      present_hwid_sets=present_hwid_sets)
    logging.info(f"[STOREAPP] A használt driverek {len(req)} Store-alkalmazást kérnek: "
                 + ', '.join(f"{v['pfn']} (>= {v['min_version'] or '?'}, {','.join(v['infs'])})"
                             for v in req.values()))
    if not req:
        return report
    before = installed_store_apps(run)
    if before is None:
        report['unverified'] = True
        before = {}
    winget_missing = False
    for key, info in req.items():
        if check_cancel and check_cancel():
            break
        pfn, need = info['pfn'], info['min_version']
        pid, title = lookup_store_product(pfn, fetch)
        name = title or pfn.split('_', 1)[0]
        entry = {'pfn': pfn, 'title': name, 'min_version': need,
                 'product_id': pid, 'before': before.get(key)}
        report['required'].append(entry)
        if not pid:
            if version_ok(before.get(key), need):
                entry['result'] = 'mar_fent'
                report['ok'].append(entry)
                log(f"   ✅ {name}: fent van ({before.get(key)}) - frissítést nem tudtunk "
                    f"keresni (a Store katalógusa nem adott termékazonosítót).")
            else:
                entry['result'] = 'nincs_termek'
                report['failed'].append(entry)
                log(f"   ❌ {name}: a driver kéri, de a Store katalógusában nem található "
                    f"({pfn}).")
            continue
        was = before.get(key)
        log(f"   ⬇️ {name} ({pid}) - " + (f"fent: {was}, frissítés keresése..." if was
                                          else "HIÁNYZIK, telepítés..."))
        how, detail = ('missing', '') if winget_missing else install_via_winget(run, pid)
        if how == 'missing':
            winget_missing = True
            logging.info(f"[STOREAPP] winget nem érhető el ({detail}) - a Windows Store-API-ja jön.")
            if was and version_ok(was, need):
                # Naprakész-e: ezt a winget nélkül nem tudjuk megkérdezni; a fent lévő,
                # a driver kérését teljesítő alkalmazást a záró Store-frissítés viszi tovább.
                how, detail = 'uptodate', 'winget nélkül nincs külön frissítés-keresés'
            else:
                how, detail = install_via_appinstaller(run, pid)
        elif how == 'fail':
            if was and version_ok(was, need):
                # A fent lévő, a driver kérését teljesítő alkalmazást a tartalék út NEM
                # telepíti újra (a StartAppInstallAsync egy fent lévő alkalmazást is
                # újrarak - fölösleges munka, mérve 2026-09-27). A frissítését a záró
                # Store-frissítés (run_store_update_scan) viszi.
                logging.warning(f"[STOREAPP] winget sikertelen ({pid}): {detail} - az alkalmazás "
                                f"fent van ({was}), újratelepítést nem indítunk.")
            else:
                logging.warning(f"[STOREAPP] winget sikertelen ({pid}): {detail} - a Windows Store-API-ja jön.")
                how, detail = install_via_appinstaller(run, pid)
        entry['method_result'] = f"{how}: {detail}"[:300]
        logging.info(f"[STOREAPP] {name} ({pfn}): {how} - {detail[:300]}")
    # VISSZAOLVASÁS: ez a verdikt, nem a telepítők kódja.
    after = installed_store_apps(run)
    if after is None:
        report['unverified'] = True
        after = {}
    for entry in report['required']:
        if entry.get('result'):
            continue
        key = entry['pfn'].lower()
        now = after.get(key)
        entry['after'] = now
        if version_ok(now, entry['min_version']):
            if entry['before'] and version_tuple(now) <= version_tuple(entry['before']):
                entry['result'] = 'naprakesz'
                report['ok'].append(entry)
                log(f"   ✅ {entry['title']}: naprakész ({now}).")
            else:
                entry['result'] = 'telepitve'
                report['installed'].append(entry)
                log(f"   ✅ {entry['title']}: " + (f"frissítve {entry['before']} → {now}."
                                                   if entry['before'] else f"telepítve ({now})."))
        else:
            entry['result'] = 'hiba'
            report['failed'].append(entry)
            log(f"   ❌ {entry['title']}: NINCS fent a kért verzióban "
                f"(most: {now or 'nincs telepítve'}, kell: {entry['min_version'] or 'bármely'}). "
                f"Ok: {store_error_text(entry.get('method_result', ''))}")
    logging.info(f"[STOREAPP] Mérleg: {len(report['required'])} kért, {len(report['ok'])} rendben, "
                 f"{len(report['installed'])} telepítve/frissítve, {len(report['failed'])} hiba"
                 + (" (a visszaolvasás nem sikerült)" if report['unverified'] else ''))
    return report


UPDATE_SCAN_PS = (
    "$ErrorActionPreference='Stop'; try { "
    "$r = Get-CimInstance -Namespace 'Root\\cimv2\\mdm\\dmmap' "
    "-ClassName 'MDM_EnterpriseModernAppManagement_AppManagement01' | "
    "Invoke-CimMethod -MethodName UpdateScanMethod; 'RV=' + $r.ReturnValue "
    "} catch { 'ERR=' + $_.Exception.Message }")


def run_store_update_scan(run, timeout=STORE_UPDATE_SCAN_TIMEOUT):
    """Az ÖSSZES fent lévő Store-alkalmazás frissítés-keresése (MDM UpdateScanMethod).
    Visszatérés: ('ok'|'timeout'|'fail', részlet). A régi hívás a visszatérési értéket nem
    nézte, és mindig "kész"-t írt."""
    from app.common import CMD_TIMEOUT_RETURNCODE
    try:
        res = run(['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command',
                   UPDATE_SCAN_PS], timeout=timeout)
    except Exception as e:
        return 'fail', str(e)
    if getattr(res, 'returncode', None) == CMD_TIMEOUT_RETURNCODE:
        return 'timeout', ''
    out = (getattr(res, 'stdout', '') or '').strip()
    m = re.search(r'RV=(\d+)', out)
    if m and m.group(1) == '0':
        return 'ok', out[-200:]
    return 'fail', out[-300:] or f"rc={getattr(res, 'returncode', '?')}"
