r"""EGY MAG ARRA A KÉRDÉSRE, HOGY EGY DRIVER-CSOMAG HASZNÁLATBAN VAN-E.

MIÉRT LÉTEZIK EZ A MODUL (explicit user decision, 2026-09-17): a technikus a Driverek
nézetben és az 1 kattintásos fix törlési előnézetében UGYANAZT a kérdést teszi fel -
"mi az, ami kell a gépnek, és mi az, ami mehet a levesbe?" -, ezért ugyanannak a
függvénynek kell rá válaszolnia. Két külön implementáció előbb-utóbb eltérne, és a
technikus az egyik képernyőn kitörölne valamit, amit a másik védettnek mutatott.

A TEREPI ESET, AMI EZT KIKÉNYSZERÍTETTE: egy távoli asztali program ("ninja...")
kernel-drivere törlődött, és ezzel megszűnt a távoli elérés - a driveren keresztül
kommunikált a program. Az ilyen drivernek NINCS saját eszköz-csomópontja, ezért a régi
felderítés (`Win32_PnPSignedDriver`) SEMMIT nem tudott róla: a listában pontosan úgy
nézett ki, mint egy rég nem használt telefon-driver.

=============================================================================
NÉGY FÜGGETLEN JEL - MÉRVE A FEJLESZTŐI GÉPEN (2026-09-17, 144 third-party csomag)
=============================================================================

| jel                                  | mit talált          | idő    |
|--------------------------------------|---------------------|--------|
| jelenlévő eszköz használja           | 20 csomag           | 0,6 mp |
| FUTÓ kernel-szolgáltatás  (ÚJ)       | +6 csomag           | 0,4 mp |
| nem jelenlévő (ghost) eszköz  (ÚJ)   | +27 csomagra van    | 0,7 mp |
| filter-regisztráció  (ÚJ)            | ssudmdm, winusb     | 0,9 mp |
| SEMMI JEL -> nyugodtan törölhető     | 118 csomag          |        |

A "+6 csomag" nem elméleti nyereség: az ESET vírusirtó NÉGY kernel-drivere
(`eamonm`, `ehdrv`, `epfw`, `epfwwfp`) pontosan ide esett - futnak, a gép védelme
rajtuk áll, és a régi felderítés mindegyiket "használatlannak" mutatta volna. Ez a
ninja-eset ugyanaz a hibaosztály.

=============================================================================
AMI NEM LÉTEZIK: "MIKOR HASZNÁLTA UTOLJÁRA" - MÉRVE, NE PRÓBÁLD MEG ÚJRA
=============================================================================

A kérdés természetes, de a Windows nem tárolja, és a három kézenfekvő út mind zsákutca
(mind a három LEMÉRVE ezen a gépen, 2026-09-17):

  1. `DEVPKEY_Device_LastArrivalDate`: a nevével ellentétben NEM az utolsó használat.
     A gép MINDEN eszközére a legutóbbi rendszerindítás időpontját adta (8:45:13 -
     8:45:23, egyetlen 10 másodperces sávban), mert bootkor minden eszköz újra
     "megérkezik". Kiírva ez a legrosszabb fajta adat lenne: pontosnak látszó hazugság.
  2. Ugyanez REGISTRYBŐL: nincs is ott. 339 eszköz-példányt végigjárva NULLA esetben
     szerepelt a `Properties\{83da6326-...}\00000066` érték - a PnP futásidőben tartja.
     A `Get-PnpDeviceProperty` pedig eszközönként ~1,1 mp, 204 ghost eszközre ~4 PERC.
  3. A `.sys` fájl utolsó hozzáférési ideje: `fsutil behavior query disablelastaccess`
     -> `DisableLastAccess = 2 (System Managed, Disabled)`, tehát a Windows NEM
     frissíti. Egy régi driver fájlján a dátum a telepítés napja marad.

Amit HELYETTE adunk, és ami a technikus valódi kérdésére válaszol: a MOST fennálló
állapot, bizonyítékkal (melyik eszköz, melyik futó szolgáltatás), plusz a "van hozzá
eszköz, de épp nincs bedugva" köztes állapot - ez utóbbi a kihúzott nyomtató/telefon
esete, ahol a törlés csak a következő bedugáskor fáj.

=============================================================================
EZ NEM SZŰRŐ, ÉS SOHA NEM IS LEHET AZ
=============================================================================

A CLAUDE.md szabálya ("MINDEN DRIVERT LEHESSEN TÖRÖLNI - a törlésbe SOHA ne tegyél új
szűrőt") érvényben marad: ez a modul CSAK BESOROL és MEGMAGYARÁZ. Semmit nem rejt el,
semmit nem tilt le, egyetlen csomagot sem véd meg a törléstől. A technikus továbbra is
bármit kitörölhet - csak most már tudja, mit töröl. Az offline javítás (másik Windowsból
bootolva) emiatt sértetlen marad.
"""

# === AUTO-IMPORTS ===
import os
import re
import json
import glob
import logging
# === /AUTO-IMPORTS ===


# --- ÁLLAPOTOK -------------------------------------------------------------
# A felület ezt a három értéket csoportosítja; a sorrendjük a fontossági sorrend is.
USAGE_ACTIVE = 'active'      # 🔴 most is használja a gép
USAGE_STANDBY = 'standby'    # 🟡 tartozik hozzá valami, de épp nem aktív
USAGE_UNUSED = 'unused'      # ⚪ semmilyen jel - szabadon törölhető
USAGE_UNKNOWN = 'unknown'    # a felderítés nem futott le (offline mód, hiba)

# A rendezéshez: az "aktív" kerül felülre. A felület is ezt használja.
USAGE_ORDER = {USAGE_ACTIVE: 0, USAGE_STANDBY: 1, USAGE_UNUSED: 2, USAGE_UNKNOWN: 3}

USAGE_LABELS = {
    USAGE_ACTIVE: 'Használatban',
    USAGE_STANDBY: 'Készenlétben',
    USAGE_UNUSED: 'Nem használt',
    USAGE_UNKNOWN: 'Ismeretlen',
}

# A felderítés időkorlátja. Mérve 2,9 mp a teljes lekérdezés egy 144 csomagos gépen;
# a bőséges keret egy lassú, sok eszközös gépre szól, nem a normál esetre.
USAGE_QUERY_TIMEOUT = 180

# A published INF-ek helye a futó rendszeren. Offline módban ez MÁS lenne - ezért nem
# hívjuk offline (lásd a modul tetején: a futó gép állapota semmit nem mond egy másik
# lemezen lévő Windows csomagjairól).
def _inf_dir():
    return os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'INF')


# =============================================================================
# 1) A RENDSZER ÁLLAPOTA - EGY POWERSHELL HÍVÁS
# =============================================================================
# MIÉRT REGISTRYBŐL ÉS NEM `Win32_PnPSignedDriver`-BŐL (mérve, 2026-09-17):
#   - a jelenlévő eszközökre BIZONYÍTOTTAN ugyanazt adja: mindkét út pontosan ugyanazt
#     a 64 INF-et hozta ki, 0 eltéréssel MINDKÉT irányban;
#   - viszont a `Win32_PnPSignedDriver` CSAK a jelenlévő eszközöket ismeri, a registry
#     a nem jelenlévőket is: +27 csomagra talált ghost eszközt (köztük egy "ThinkPad
#     UltraNav driver"-t és egy BenQ monitort), és épp ezek azok, amiket a technikus
#     tévedésből használatlannak hinne;
#   - ráadásul gyorsabb: a teljes lekérdezés 2,9 mp, míg a `Win32_PnPSignedDriver`
#     önmagában 3,1 mp volt.
# A `Win32_PnPEntity` a jelenlévőség eldöntésére kell (a projekt máshol is ezt
# használja: WU_PNP_QUERY_PS) - a nevet is onnan vesszük, mert az FELOLDOTT szöveg,
# míg a registry `DeviceDesc`-je gyakran `@oem14.inf,%str%;Név` alakú.
#
# Az `[Console]::OutputEncoding` SZÁNDÉKOSAN nincs beleírva: a `_run` minden
# PowerShell `-Command` hívást UTF-8-ra kényszerít (common.ps_force_utf8), és aki
# maga állítja be, azt kihagyja - egy második beállítás itt csak elfedné a közös
# szabályt.
DRIVER_USAGE_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'

# 1) driver-kulcs ({class-guid}\NNNN) -> published INF
$keyInf = @{}
foreach ($c in (Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class')) {
  foreach ($n in (Get-ChildItem $c.PSPath)) {
    $ip = (Get-ItemProperty $n.PSPath -Name InfPath).InfPath
    if ($ip) { $keyInf[($c.PSChildName + '\' + $n.PSChildName).ToLower()] = $ip.ToLower() }
  }
}

# 2) jelenlévő eszközök (PNPDeviceID -> feloldott név)
$present = @{}
foreach ($p in (Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true -and $_.ConfigManagerErrorCode -ne 45 })) {
  if ($p.PNPDeviceID) { $present[$p.PNPDeviceID.ToLower()] = $p.Name }
}

# 3) MINDEN telepített eszköz-példány (a nem jelenlévők is) -> INF
$devices = @()
$filters = @{}
foreach ($e in (Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum')) {
  foreach ($d in (Get-ChildItem $e.PSPath)) {
    foreach ($i in (Get-ChildItem $d.PSPath)) {
      $props = Get-ItemProperty $i.PSPath
      foreach ($fk in 'UpperFilters', 'LowerFilters') {
        foreach ($x in $props.$fk) { if ($x) { $filters[$x.ToLower()] = 1 } }
      }
      if (-not $props.Driver) { continue }
      $inf = $keyInf[$props.Driver.ToLower()]
      if (-not $inf) { continue }
      $id = ($e.PSChildName + '\' + $d.PSChildName + '\' + $i.PSChildName)
      $isPresent = $present.ContainsKey($id.ToLower())
      $name = $present[$id.ToLower()]
      if (-not $name) { $name = $props.FriendlyName }
      if (-not $name) { $name = $props.DeviceDesc }
      $devices += [pscustomobject]@{ inf = $inf; name = $name; present = $isPresent }
    }
  }
}

# 4) class-szintű filter-regisztrációk
foreach ($c in (Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class')) {
  foreach ($fk in 'UpperFilters', 'LowerFilters') {
    foreach ($x in (Get-ItemProperty $c.PSPath -Name $fk).$fk) { if ($x) { $filters[$x.ToLower()] = 1 } }
  }
}

# 5) kernel-szolgáltatások állapota
$services = @()
foreach ($s in (Get-WmiObject Win32_SystemDriver)) {
  $file = ''
  $repo = ''
  if ($s.PathName) {
    $file = [System.IO.Path]::GetFileName($s.PathName)
    if ($s.PathName -match '(?i)FileRepository\\([^\\]+?\.inf)_') { $repo = $Matches[1] }
  }
  $services += [pscustomobject]@{ name = $s.Name; state = $s.State; start = $s.StartMode; file = $file; repo = $repo }
}

[pscustomobject]@{
  devices  = $devices
  services = $services
  filters  = @($filters.Keys)
} | ConvertTo-Json -Depth 4 -Compress
"""


# =============================================================================
# 2) TISZTA FÜGGVÉNYEK (offline tesztelhetők, subprocess nélkül)
# =============================================================================

# Az INF `AddService=` sorai. A `%TOKEN%` alak feloldása a [Strings] szekcióból megy:
# mérve a fejlesztői gépen az ESET mind az öt csomagja `AddService=%ServiceName%`-et
# ír, tehát feloldás nélkül PONTOSAN a legfontosabb eset (futó vírusirtó-driver)
# maradna felismeretlen.
_ADD_SERVICE_RE = re.compile(r'^\s*AddService\s*=\s*([^,;\s]+)', re.IGNORECASE | re.MULTILINE)
_STRING_DEF_RE = re.compile(r'^\s*([A-Za-z0-9_\-.]+)\s*=\s*"([^"]*)"', re.IGNORECASE | re.MULTILINE)
_SYS_FILE_RE = re.compile(r'([A-Za-z0-9_\-.]+\.sys)', re.IGNORECASE)


def parse_inf_facts(text):
    r"""Egy INF szövegéből: a telepített szolgáltatások nevei és a hozzá tartozó .sys
    fájlnevek. TISZTA függvény - ez a modul offline tesztelhető magja.

    Visszatérés: {'services': [...], 'sys_files': [...]}, mindkettő kisbetűsítve.

    A .sys fájlnevekre azért van szükség, mert a szolgáltatásnév ÖNMAGÁBAN TÉVES
    párosítást ad (mérve 2026-09-17): az `e1d.inf` és az `e1d68x64.inf` UGYANAZT az
    `e1dexpress` szolgáltatást deklarálja, tehát a névre hagyatkozva mindkét csomag
    "futónak" látszana, holott a futó szolgáltatás bináris útvonala
    (`...\FileRepository\e1d.inf_amd64_...\e1d.sys`) egyértelműen az egyiket nevezi meg.
    A 145 futó szolgáltatásból 143-nak EGYEDI a .sys fájlneve, tehát ez a jó horgony."""
    if not text:
        return {'services': [], 'sys_files': []}
    strings = {m.group(1).lower(): m.group(2) for m in _STRING_DEF_RE.finditer(text)}
    services = []
    for m in _ADD_SERVICE_RE.finditer(text):
        val = m.group(1).strip()
        if val.startswith('%') and val.endswith('%') and len(val) > 2:
            val = strings.get(val[1:-1].lower(), '')
        val = val.strip().strip('"').lower()
        if val and val not in services:
            services.append(val)
    sys_files = []
    for m in _SYS_FILE_RE.finditer(text):
        f = m.group(1).lower()
        if f not in sys_files:
            sys_files.append(f)
    return {'services': services, 'sys_files': sys_files}


def _norm_inf(name):
    return (name or '').strip().lower()


def build_usage(raw, inf_facts, published_names=None, originals=None):
    """A nyers rendszerállapotból csomagonkénti besorolás. TISZTA függvény.

    `raw`: a DRIVER_USAGE_PS kimenete dict-ként (devices/services/filters).
    `inf_facts`: {published_inf: {'services': [...], 'sys_files': [...]}} - a published
        INF-ekből kiolvasott tények (lásd read_inf_facts).
    `published_names`: mely csomagokra kérünk sort. None esetén az `inf_facts` kulcsai.
    `originals`: {published: original} - opcionális, a csomaglistából. Ezzel a futó
        szolgáltatás DriverStore-mappája PONTOSAN egy csomaghoz köthető (a mappa neve
        az EREDETI INF-névvel kezdődik, nem a publikálttal). Enélkül is helyes marad a
        besorolás, csak a .sys-horgonyra támaszkodik.

    Minden sor megnevezi a BIZONYÍTÉKOT is (`reasons`), nem csak a verdiktet - a
    CLAUDE.md Rule 0 szellemében: egy besorolás, aminek nem látszik az indoka, a
    technikus számára ugyanolyan vak, mint a puszta INF-név volt."""
    devices = raw.get('devices') or []
    services = raw.get('services') or []
    filters = {str(f).lower() for f in (raw.get('filters') or []) if f}

    # INF -> eszközök (jelenlévő / nem jelenlévő külön)
    dev_present, dev_absent = {}, {}
    for d in devices:
        inf = _norm_inf(d.get('inf'))
        if not inf:
            continue
        name = (d.get('name') or '').strip()
        # A registry DeviceDesc gyakran `@oem14.inf,%str%;ThinkPad UltraNav` alakú -
        # a technikusnak a pontosvessző UTÁNI, feloldott név kell.
        if name.startswith('@') and ';' in name:
            name = name.split(';', 1)[1].strip()
        if not name:
            continue
        bucket = dev_present if d.get('present') else dev_absent
        names = bucket.setdefault(inf, [])
        if name not in names:
            names.append(name)

    # szolgáltatás-index: név, .sys fájlnév és DriverStore-mappa szerint is
    svc_by_name, svc_by_file, svc_by_repo = {}, {}, {}
    for s in services:
        nm = (s.get('name') or '').strip().lower()
        entry = {'name': s.get('name') or '', 'state': s.get('state') or '',
                 'start': s.get('start') or '', 'file': (s.get('file') or '').lower(),
                 'repo': _norm_inf(s.get('repo'))}
        if nm:
            svc_by_name[nm] = entry
        if entry['file']:
            svc_by_file.setdefault(entry['file'], entry)
        if entry['repo']:
            svc_by_repo.setdefault(entry['repo'], entry)

    names = list(published_names) if published_names is not None else list(inf_facts.keys())
    orig_map = {_norm_inf(k): _norm_inf(v) for k, v in (originals or {}).items()}
    out = {}
    for pub_raw in names:
        pub = _norm_inf(pub_raw)
        if not pub:
            continue
        facts = inf_facts.get(pub) or {'services': [], 'sys_files': []}
        own_origin = orig_map.get(pub)
        present = list(dev_present.get(pub, []))
        absent = list(dev_absent.get(pub, []))

        def _belongs(e):
            """Ez a futó szolgáltatás TÉNYLEG ehhez a csomaghoz tartozik?

            Két csomag deklarálhatja ugyanazt a szolgáltatásnevet (mérve: `e1d.inf` és
            `e1d68x64.inf` -> mindkettő `e1dexpress`), ezért a puszta névegyezés
            kevés - különben a régi verzió is "futónak" látszana. Két pontosító jel,
            és mindkettő csak akkor vétózik, ha VAN mihez hasonlítani."""
            if e['repo'] and own_origin and e['repo'] != own_origin:
                return False
            if e['file'] and facts['sys_files'] and e['file'] not in facts['sys_files']:
                return False
            return True

        # A csomaghoz tartozó szolgáltatások összegyűjtése. A .sys fájlnév a
        # legerősebb horgony (lásd parse_inf_facts), a szolgáltatásnév a tartalék.
        matched, seen = [], set()
        for svc_name in facts['services']:
            e = svc_by_name.get(svc_name)
            if not e or e['name'].lower() in seen or not _belongs(e):
                continue
            seen.add(e['name'].lower())
            matched.append(e)
        for sysf in facts['sys_files']:
            e = svc_by_file.get(sysf)
            if e and e['name'].lower() not in seen:
                seen.add(e['name'].lower())
                matched.append(e)

        running = [e for e in matched if (e['state'] or '').lower() == 'running']
        stopped = [e for e in matched if (e['state'] or '').lower() != 'running']
        filt = [s for s in facts['services'] if s in filters]

        reasons = []
        if present:
            reasons.append('Jelenlévő eszköz használja: ' + ', '.join(present[:4])
                           + (f' (+{len(present) - 4})' if len(present) > 4 else ''))
        if running:
            reasons.append('Fut a kernel-szolgáltatása: ' + ', '.join(e['name'] for e in running))
        if absent:
            reasons.append('Tartozik hozzá eszköz, de most NINCS csatlakoztatva: '
                           + ', '.join(absent[:3]) + (f' (+{len(absent) - 3})' if len(absent) > 3 else ''))
        if stopped and not running:
            reasons.append('Telepített, de épp nem futó szolgáltatás: '
                           + ', '.join(e['name'] for e in stopped))
        if filt:
            reasons.append('Szűrő-driverként be van jegyezve (akkor lép működésbe, '
                           'ha a hozzá tartozó eszköz csatlakozik): ' + ', '.join(filt))

        if present or running:
            state = USAGE_ACTIVE
        elif absent or stopped or filt:
            state = USAGE_STANDBY
        else:
            state = USAGE_UNUSED
            reasons.append('Semmi nem hivatkozik rá: nincs hozzá eszköz a gépben, '
                           'és nem fut hozzá szolgáltatás.')

        out[pub] = {
            'state': state,
            'devices': present,
            'absent_devices': absent,
            'services': [{'name': e['name'], 'state': e['state']} for e in matched],
            'running_services': [e['name'] for e in running],
            'filter_services': filt,
            'reasons': reasons,
            'summary': _summary(state, present, running, absent, stopped, filt),
        }
    return out


def _summary(state, present, running, absent, stopped, filt):
    """A táblázat "Használat" oszlopának RÖVID szövege. Egy sor, ami azonnal megmondja,
    MIÉRT az adott állapot - a puszta "Használatban" felirat ugyanolyan vak lenne, mint
    a régi INF-név."""
    if state == USAGE_ACTIVE:
        if present and running:
            return f'{present[0]} + fut: {running[0]["name"]}'
        if present:
            return present[0] + (f' (+{len(present) - 1})' if len(present) > 1 else '')
        return 'FUT: ' + ', '.join(e['name'] for e in running[:2])
    if state == USAGE_STANDBY:
        if absent:
            return f'kihúzva: {absent[0]}' + (f' (+{len(absent) - 1})' if len(absent) > 1 else '')
        if filt:
            return 'szűrő-driver: ' + filt[0]
        if stopped:
            return 'leállítva: ' + stopped[0]['name']
        return 'készenlétben'
    if state == USAGE_UNKNOWN:
        return 'nem vizsgálva'
    return 'semmi nem használja'


def summarize_counts(usage):
    """Állapotonkénti darabszám - a felület fejlécéhez és a naplóhoz. Tiszta függvény."""
    counts = {USAGE_ACTIVE: 0, USAGE_STANDBY: 0, USAGE_UNUSED: 0, USAGE_UNKNOWN: 0}
    for entry in (usage or {}).values():
        st = (entry or {}).get('state', USAGE_UNKNOWN)
        counts[st] = counts.get(st, 0) + 1
    return counts


def present_device_map(usage):
    """A régi `{INF: [jelenlévő eszköznevek]}` alak.

    MIÉRT VAN KÜLÖN FÜGGVÉNYBEN: a lánc három helye (a törlés előtti pillanatkép, a
    "nem jött vissza" jelentés és a törlési előnézet) pontosan ezt az alakot várja, és
    szemantikailag is a JELENLÉVŐ eszközökre kérdez rá ("a gépben van-e még?"). Így a
    régi hívók változatlanul működnek, miközben EGY felderítés fut mindenki alatt."""
    return {inf: list(entry.get('devices') or [])
            for inf, entry in (usage or {}).items() if entry.get('devices')}


# =============================================================================
# 3) BELÉPÉSI PONTOK
# =============================================================================

def read_inf_facts(published_names=None, inf_dir=None, reader=None):
    """A published INF-ek beolvasása és a tények kinyerése.

    Mérve (2026-09-17): a gép mind a 144 third-party csomagjának published INF-je ott
    van a `%WINDIR%\\INF` mappában, tehát ez TISZTA FÁJLOLVASÁS - se `dism`, se
    `pnputil`, se WMI. `published_names` nélkül a mappa `oem*.inf` fájljait olvassuk,
    így a felderítés a csomaglistától is független.

    `reader`: a fájlolvasó (teszteléshez cserélhető). Alapból a projekt kódolás-felismerő
    olvasója, mert az INF-ek hol ANSI-k, hol BOM NÉLKÜLI UTF-16-osak - ez utóbbi némán
    nulla találatot adna (a CLAUDE.md-ben dokumentált NVIDIA-eset)."""
    if reader is None:
        from app.wu_core import _read_text_best_effort
        reader = _read_text_best_effort
    folder = inf_dir or _inf_dir()
    if published_names is None:
        try:
            published_names = [os.path.basename(p) for p in glob.glob(os.path.join(folder, 'oem*.inf'))]
        except Exception as e:
            logging.warning(f"[USAGE] A(z) {folder} mappa nem listázható: {e}")
            published_names = []
    facts, missing = {}, []
    for name in published_names:
        pub = _norm_inf(name)
        if not pub:
            continue
        path = os.path.join(folder, pub)
        text = reader(path)
        if not text:
            missing.append(pub)
        facts[pub] = parse_inf_facts(text)
    if missing:
        # Nem hiba: a csomagnak nem kötelező kernel-szolgáltatást telepítenie, és a
        # published INF is hiányozhat. De ki kell mondani, mert ilyenkor a csomag CSAK
        # az eszköz-oldali jelre támaszkodhat - a "miért nincs szolgáltatás-adat?"
        # kérdés máshonnan megválaszolhatatlan lenne.
        logging.debug(f"[USAGE] {len(missing)} published INF nem volt olvasható "
                      f"({folder}): {missing[:10]}")
    return facts


def collect_usage_raw(run_fn):
    """A rendszer állapota egyetlen PowerShell hívásban. Hibánál None."""
    try:
        res = run_fn(["powershell", "-NoProfile", "-Command", DRIVER_USAGE_PS],
                     encoding='utf-8', timeout=USAGE_QUERY_TIMEOUT)
        text = (getattr(res, 'stdout', '') or '').strip()
        if not text:
            logging.warning("[USAGE] A rendszerállapot-lekérdezés üres kimenetet adott "
                            f"(returncode={getattr(res, 'returncode', '?')}).")
            return None
        data = json.loads(text)
        if not isinstance(data, dict):
            logging.warning(f"[USAGE] Váratlan válasz-alak: {type(data).__name__}")
            return None
        # A ConvertTo-Json EGYETLEN elemű tömböt objektummá laposít - a lista-mezőket
        # ezért vissza kell listásítani, különben egyetlen eszközös gépen elszállna.
        for key in ('devices', 'services', 'filters'):
            val = data.get(key)
            if val is None:
                data[key] = []
            elif isinstance(val, dict):
                data[key] = [val]
        return data
    except Exception as e:
        logging.warning(f"[USAGE] A rendszerállapot lekérdezése sikertelen: {e}")
        return None


def collect_package_usage(run_fn, packages=None, inf_dir=None):
    """A MODUL FŐ BELÉPÉSI PONTJA: csomagonkénti használat-besorolás.

    `packages`: a már betöltött csomaglista (a `dism`/`pnputil` dict-jei `published` +
    `original` mezővel), vagy puszta published nevek listája, vagy None (ilyenkor a
    `%WINDIR%\\INF\\oem*.inf` fájlokból dolgozunk). A dict-alak a legjobb, mert az
    `original` nevekkel a futó szolgáltatások pontosabban köthetők csomaghoz.

    Visszatérés: {published_inf_kisbetűvel: {state, devices, absent_devices, services,
    reasons, summary, ...}}. A felderítés bukásakor ÜRES dict - a hívó ilyenkor
    `unknown` állapotot mutat, ami őszinte: az "ismeretlen" nem ugyanaz, mint a
    "nem használt", és egy törlési döntést nem szabad egy elbukott lekérdezésre
    alapozni."""
    published_names, originals = None, {}
    if packages:
        published_names = []
        for p in packages:
            if isinstance(p, dict):
                pub = _norm_inf(p.get('published'))
                if not pub:
                    continue
                published_names.append(pub)
                if p.get('original'):
                    originals[pub] = p['original']
            elif p:
                published_names.append(_norm_inf(p))
    raw = collect_usage_raw(run_fn)
    if raw is None:
        return {}
    facts = read_inf_facts(published_names, inf_dir)
    usage = build_usage(raw, facts, published_names or list(facts.keys()), originals)
    counts = summarize_counts(usage)
    logging.info(
        f"[USAGE] {len(usage)} csomag besorolva: "
        f"{counts[USAGE_ACTIVE]} használatban, {counts[USAGE_STANDBY]} készenlétben, "
        f"{counts[USAGE_UNUSED]} nem használt "
        f"(forrás: {len(raw.get('devices') or [])} eszköz-példány, "
        f"{len(raw.get('services') or [])} kernel-szolgáltatás, "
        f"{len(raw.get('filters') or [])} szűrő-bejegyzés)")
    # A "használatban" sorok NEVESÍTVE a naplóba: ha a technikus később arra panaszkodik,
    # hogy egy törlés után elromlott valami, ez a sor mondja meg, mit tudott a program a
    # törlés pillanatában.
    for pub, entry in sorted(usage.items()):
        if entry['state'] == USAGE_ACTIVE:
            logging.debug(f"[USAGE] {pub}: HASZNÁLATBAN - {'; '.join(entry['reasons'])}")
    return usage
