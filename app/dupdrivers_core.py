"""DriverStore duplikátum-takarítás (RAPR / Driver Store Explorer elv) - KÖZÖS mag
(GUI panel + CLI menüpont).

Ugyanabból a driverből (azonos EREDETI inf-név) a DriverStore-ban több verzió is
felhalmozódhat (minden frissítés otthagyja a régit) - ezek gigákat foglalhatnak.
A csoportosítás, a biztonsági szabályok és a törlés EGY példányban itt él:
  - a jelenlévő eszközök által AKTÍVAN használt publikált inf-ek (Win32_PnPSignedDriver
    InfName) SOSEM törölhetők - hiába régebbi a verziójuk, egy eszköz épp azon fut;
  - ha az aktív-lista lekérdezése hibázik (None), SEMMI nem törölhető (biztonságos irány);
  - csak oemXX.inf publikált nevű (third-party) csomagot törlünk, gyárit soha;
  - először /force nélkül próbálkozunk, és csak sikertelen törlésnél adunk /force-ot;
  - törlés előtt az aktív-lista ÚJRA lekérdezendő (a felület/menü állapota elavulhatott).
Csak élő rendszeren fut (offline cél-OS-nél a hívók elutasítják)."""

# === AUTO-IMPORTS ===
import os
import re
import json
import logging
from app.wu_core import _iso_date_or_none
from app.wu_core import _parse_driver_version
from app.drivers_core import delete_blocked_in_use
from app.drivers_core import delete_failure_text
# === /AUTO-IMPORTS ===


def dup_version_key(vstr):
    """Verzió-string ('31.0.15.5222') -> int-tuple a rendezéshez. Értelmezhetetlen -> (0,)."""
    try:
        parts = tuple(int(p) for p in re.findall(r'\d+', vstr or ''))
        return parts if parts else (0,)
    except Exception:
        return (0,)


def get_active_published_infs(run):
    """A jelenlévő eszközök által ténylegesen használt publikált inf-nevek halmaza
    kisbetűvel (pl. {'oem12.inf'}). Hiba esetén None: a hívó ilyenkor NEM törölhet
    (inkább nem takarítunk, mint hogy egy aktív drivert lőjünk ki).

    ÜRES HALMAZ SOSEM TÉRHET VISSZA - az is hiba, nem eredmény (2026-08-05, offline
    teszt találta). A régi kód csak KIVÉTELRE adott None-t: ha a PowerShell némán
    elhasalt (üres stdout, vagy nem-JSON kimenet), a `data = []` ág simán lefutott,
    és a hívó egy ÜRES aktív-listát kapott - amiben MINDEN csomag használatlannak
    látszik. Élő Windowson mindig több tucat INF aktív, tehát a nulla elem
    definíció szerint sikertelen lekérdezés. Ez a hívók "None -> nem törlünk"
    biztonsági ágát csendben kikerülte volna (duplikátum-takarítás, elhalasztott
    INF-kivezetés).

    A returncode-ot szándékosan NEM tekintjük ítéletnek: a kimenet dönt. Egy
    non-terminating PowerShell hiba (pl. olvashatatlan WMI-példány) 1-es kóddal
    tér vissza, miközben a lista maga hibátlan - ilyenkor kár lenne eldobni."""
    try:
        ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
              "Get-WmiObject Win32_PnPSignedDriver | Where-Object { $_.InfName } | "
              "Select-Object InfName | ConvertTo-Json -Compress")
        res = run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=120)
        raw = (res.stdout or '') if res else ''
        if not raw.strip():
            logging.error(f"[DUPDRV] Az aktív inf-lista lekérdezése ÜRES kimenetet adott "
                          f"(rc={getattr(res, 'returncode', '?')}) - ez hiba, nem üres lista.")
            return None
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        active = {str(d.get('InfName') or '').strip().lower() for d in data}
        active.discard('')
        if not active:
            logging.error("[DUPDRV] Az aktív inf-lista értelmezhető, de EGYETLEN inf-et sem "
                          "tartalmaz - élő rendszeren ez lehetetlen, hibaként kezeljük.")
            return None
        logging.info(f"[DUPDRV] Aktívan használt inf-ek: {len(active)} db")
        return active
    except Exception as e:
        logging.error(f"[DUPDRV] Aktív inf-lista lekérdezése sikertelen: {e}")
        return None


def dup_release_key(d):
    """Rendezési kulcs egy DriverStore-csomaghoz: DÁTUM elsődleges, verzió a holtverseny-
    döntő (közös szabály: wu_core.release_rank).

    Korábban tisztán verzió-alapú volt, és egy gyártói verziósémaváltásnál a RÉGEBBI
    csomagot tartotta meg 'keep'-ként (a frissebbet meg törölhetőnek jelölte) - ugyanaz
    a hibaosztály, ami a katalógus-kaput is érintette. Az aktív-INF védelem emiatt sem
    sérülhet: azt a build_duplicate_groups külön, ettől függetlenül érvényesíti.

    A verzió-tagra a szigorúbb `_parse_driver_version` fut (min. 3 tagú szám-sorozat), és
    ha az nem ad eredményt, a régi, megengedőbb `dup_version_key`-re esünk vissza - így a
    holtverseny-döntés sosem lesz gyengébb, mint a dátum bevezetése előtt volt."""
    date_iso = _iso_date_or_none(d.get('date')) or ''
    ver = _parse_driver_version(d.get('version')) or dup_version_key(d.get('version'))
    return (date_iso, ver)


def build_duplicate_groups(drivers, active_infs):
    """Third-party driver-lista -> duplikátum-csoportok. Egy csoport = azonos eredeti
    inf-név; a legújabb KIADÁS marad meg ('keep' - dátum elsődleges, verzió holtversenynél),
    a többi törölhető jelölt ('dups'), kivéve az aktívan használtakat ('active': True,
    nem törölhető; active_infs None esetén MINDEN aktívnak számít).
    Visszatérés: (csoport-lista, törölhetők száma)."""
    groups = {}
    for d in drivers:
        orig = (d.get('original') or '').strip().lower()
        if not orig or not (d.get('published') or '').lower().startswith('oem'):
            continue
        groups.setdefault(orig, []).append(d)

    result = []
    for orig, items in groups.items():
        if len(items) < 2:
            continue
        items_sorted = sorted(items, key=dup_release_key, reverse=True)
        keep, rest = items_sorted[0], items_sorted[1:]
        # A DÖNTÉS BIZONYÍTÉKA A LOGBA, ha a dátum és a verzió NEM ugyanazt mondja: ez az
        # egyetlen eset, ahol a megtartott csomag verziószáma kisebb a törölhetőkénél, és
        # egy "miért a régebbi verziót tartotta meg?" bejelentésre csak ez a sor válaszol.
        keep_v = _parse_driver_version(keep.get('version')) or dup_version_key(keep.get('version'))
        for d in rest:
            d_v = _parse_driver_version(d.get('version')) or dup_version_key(d.get('version'))
            if d_v > keep_v:
                logging.info(f"[DUPDRV] {orig}: a DÁTUM dönt, nem a verzió - megtartva "
                             f"{keep.get('published')} v{keep.get('version')} [{keep.get('date') or '?'}], "
                             f"törölhető {d.get('published')} v{d.get('version')} [{d.get('date') or '?'}] "
                             f"(magasabb verziószám, de régebbi kiadás).")
        dups = []
        for d in rest:
            pub_l = (d.get('published') or '').lower()
            dups.append({
                'published': d.get('published', ''), 'version': d.get('version', ''),
                'date': d.get('date', ''),
                'provider': d.get('provider', ''), 'class': d.get('class', ''),
                # active_infs None (lekérdezési hiba) -> mindent aktívnak
                # jelölünk = semmi sem törölhető (biztonságos irány).
                'active': (active_infs is None) or (pub_l in active_infs),
            })
        result.append({
            'original': orig,
            'keep': {'published': keep.get('published', ''), 'version': keep.get('version', ''),
                     'date': keep.get('date', '')},
            'provider': keep.get('provider', ''), 'class': keep.get('class', ''),
            'dups': dups,
        })
    result.sort(key=lambda g: (g['provider'].lower(), g['original']))
    deletable = sum(1 for g in result for d in g['dups'] if not d['active'])
    logging.info(f"[DUPDRV] {len(result)} duplikátum-csoport, {deletable} törölhető régi verzió")
    return result, deletable


def auto_cleanup_duplicates(run, log, get_drivers, check_cancel=None):
    """A záró DriverStore-takarítás két fázisa: (1) azonos eredeti INF-nevű régi verziók,
    (2) átnevezett INF-fel kiváltott régi csomagok (`retire_superseded_packages`).
    Mind a három hívó (GUI kézi telepítés, GUI és CLI AutoFix) ezt hívja, tehát a
    második fázis is mindenhol ugyanúgy fut. Visszatérés: (törölt, sikertelen, kihagyott)."""
    a = _cleanup_duplicate_groups(run, log, get_drivers, check_cancel)
    if check_cancel and check_cancel():
        return a
    b = retire_superseded_packages(run, log, get_drivers, check_cancel)
    return tuple(x + y for x, y in zip(a, b))


def _cleanup_duplicate_groups(run, log, get_drivers, check_cancel=None):
    """FELÜGYELET NÉLKÜLI duplikátum-takarítás - a driver-telepítések záró lépése
    (GUI manuális telepítés + GUI/CLI AutoFix hívja): egy frissen telepített driver
    után a régi verzió(k) ottmaradnak a DriverStore-ban, ez a lépés azonnal el is
    takarítja őket. Ugyanazokkal a biztonsági szabályokkal dolgozik, mint a kézi panel
    (aktívan használt inf soha nem törlődik; ha az aktív-lista nem kérdezhető le, NEM
    törlünk semmit; csak oemXX.inf) - ezért felügyelet nélkül is biztonságos.

    get_drivers: 0-argumentumos callable, a third-party driver-listát adja vissza
    (GUI: self._get_third_party_drivers). Minden hiba fail-silent (log + visszatérés):
    egy takarítási hiba SOSEM buktathatja el magát a telepítést.
    Visszatérés: (törölt, sikertelen, kihagyott) darabszám."""
    try:
        drivers = get_drivers() or []
        active = get_active_published_infs(run)
        if active is None:
            log('  ⚠️ Az aktív driver-lista nem kérdezhető le - a duplikátum-takarítás kimarad (biztonsági szabály).')
            return 0, 0, 0
        groups, deletable = build_duplicate_groups(drivers, active)
        if not deletable:
            # "AZONOS NEVŰ": utána még jöhet a kiváltott-csomag fázis, és a régi "nincs
            # törölhető régi verzió" mondat annak törlésével ellentmondásba került (2026-09-27).
            log('  ✅ Nincs azonos nevű régi driver-verzió a DriverStore-ban.')
            return 0, 0, 0
        names = [d['published'] for g in groups for d in g['dups'] if not d['active'] and d['published']]
        # MINDEN TÖRLENDŐ CSOMAGOT NEVÉN NEVEZÜNK, MIELŐTT HOZZÁNYÚLNÁNK (2026-09-08).
        # A napló eddig ennyit mondott: "[DUPDRV] 2 duplikátum-csoport, 2 törölhető régi
        # verzió", majd "pnputil /delete-driver oem5.inf". Az `oemNN.inf` szám önmagában
        # SEMMIT nem árul el (ráadásul újratelepítéskor átszámozódik), tehát a "hova lett a
        # driverem?" kérdés a naplóból megválaszolhatatlan volt - miközben ez a projekt
        # egyik alapszabálya, hogy minden romboló lépés a CÉLJÁT is kiírja. Az adat végig
        # ott volt a `groups`-ban, csak nem ment ki.
        details = duplicate_delete_details(groups)
        for g in groups:
            for d in g['dups']:
                if d['active'] or not d['published']:
                    continue
                logging.warning(
                    f"[DUPDRV] TÖRLENDŐ: {d['published']} ({g['original']}) "
                    f"{d.get('provider') or '?'} v{d.get('version') or '?'} [{d.get('date') or '?'}] "
                    f"- MEGMARAD helyette: {g['keep'].get('published') or '?'} "
                    f"v{g['keep'].get('version') or '?'} [{g['keep'].get('date') or '?'}]")
        log(f'  🧹 {len(names)} elavult driver-verzió törlése a DriverStore-ból...')
        ok, fail, skipped = delete_duplicate_packages(run, log, names, active,
                                                      check_cancel=check_cancel, details=details)
        log(f'  🧹 DriverStore-takarítás kész: {ok} törölve' + (f', {fail} sikertelen' if fail else '') + (f', {skipped} kihagyva' if skipped else '') + '.')
        return ok, fail, skipped
    except Exception as e:
        logging.warning(f"[DUPDRV] Automatikus duplikátum-takarítás hiba (a telepítést nem érinti): {e}")
        try:
            log(f'  ⚠️ DriverStore-takarítási hiba (a telepítést nem érinti): {e}')
        except Exception:
            pass
        return 0, 0, 0


# ===== ÁTNEVEZETT INF-FEL KIVÁLTOTT CSOMAGOK (2026-09-27, terepen mérve) =====
#
# A duplikátum-csoportok kulcsa az EREDETI INF-név - ez szándékosan szűk (egy téves
# összevonás egy KELLŐ drivert törölne). A gyártók viszont néha ÁTNEVEZIK az INF-et,
# és akkor a régi verzió örökre bent marad. Terepen, ASRock B450M, Build 339: a lánc
# első WU-köre felrakta az `smbusamd.inf` 5.12.0.38-at (2017), két perccel később a
# katalógus az `amdinterface.inf` 2.0.0.29-et (2026), a Windows arra kötötte az AMD
# SMBUS eszközt, és a 2017-es csomag "nem használt"-ként maradt a Driverek nézetben
# - a technikus joggal kérdezte, hogy "fos drivert rakott fel a program?".
#
# A SZABÁLY BIZONYÍTÉKON ÁLL, NEM NÉVEN - mind a négy feltétel kell:
#   1. a csomag a KÖZÖS használat-magban `unused` (se eszköz, se futó szolgáltatás,
#      se szűrő, se nyomtató) - `unknown` esetén semmi;
#   2. nem Extension / SoftwareComponent / nyomtató osztályú: ezek kötése a használat-
#      magban NEM látszik (nincs mért jel rá), tehát náluk az `unused` nem bizonyíték;
#   3. a csomag saját (nem osztálykódos) hardver-azonosítói legalább egy JELENLÉVŐ
#      eszközre illeszkednek - vagyis a hardver itt van, a Windows mégsem ezt választotta;
#   4. MINDEN ilyen eszköz egy MÁSIK, ugyanattól a gyártótól való, legalább ugyanolyan
#      friss third-party csomagon fut.
# Ha a csomag egyetlen jelenlévő eszközre sem illik (pl. az NVIDIA laptopos `nvpcf.inf`
# egy asztali gépen), NEM nyúlunk hozzá: az egy máshová való, de ártalmatlan
# komponens, és a törlése a WU szemében megváltoztathatná a csomag állapotát.
# A törlés sima `pnputil /delete-driver` (se /uninstall, se /force): ha mégis bármi
# használja, a pnputil megtagadja, és a csomag marad.

SUPERSEDED_SKIP_CLASSES = {'extension', 'softwarecomponent', 'printer', 'printqueue'}


def _provider_key(p):
    """Gyártónév összevetéshez: az első szó, kisbetűvel ('Advanced Micro Devices, Inc'
    és 'Advanced Micro Devices Inc.' ugyanaz)."""
    w = re.findall(r'[a-z0-9]+', (p or '').lower())
    return w[0] if w else ''


def find_superseded_packages(drivers, usage, nodes, inf_hwids, match_fn, rank_fn):
    """Átnevezett INF-fel KIVÁLTOTT, használaton kívüli csomagok. TISZTA függvény.

    drivers:   third-party csomagok (published/original/provider/version/date/class)
    usage:     {published: {'state': ...}} - a `driverusage_core` besorolása
    nodes:     az eszközfa (`win32.enumerate_device_nodes`)
    inf_hwids: {published: [a csomag INF-jének hardver-azonosítói]}
    match_fn:  (inf_hwid, dev_hwid) -> bool   (wu_core._hwid_matches)
    rank_fn:   (date, version) -> rendezési kulcs (wu_core.release_rank)

    Visszatérés: [{'published', 'original', 'provider', 'version', 'date', 'devices',
    'replaced_by': [{'published', 'original', 'version', 'date'}]}]."""
    by_pub = {(d.get('published') or '').strip().lower(): d for d in drivers or []}
    present = [n for n in nodes or [] if n.get('present')]
    out = []
    for pub, d in by_pub.items():
        if not pub.startswith('oem'):
            continue
        if (usage.get(pub) or {}).get('state') != 'unused':
            continue
        if (d.get('class') or '').strip().lower() in SUPERSEDED_SKIP_CLASSES:
            continue
        ids = [h for h in (inf_hwids.get(pub) or []) if '&CC_' not in h.upper()]
        if not ids:
            continue
        hit = []
        for n in present:
            dev_ids = list(n.get('hwids') or []) + list(n.get('compat') or [])
            if any(match_fn(i, x) for i in ids for x in dev_ids):
                hit.append(n)
        if not hit:
            continue
        cur_rank = rank_fn(d.get('date'), d.get('version'))
        repl, ok = [], True
        for n in hit:
            b = (n.get('inf') or '').strip().lower()
            q = by_pub.get(b)
            if (not b.startswith('oem') or b == pub or q is None
                    or _provider_key(q.get('provider')) != _provider_key(d.get('provider'))
                    or rank_fn(q.get('date'), q.get('version')) < cur_rank):
                ok = False
                break
            repl.append(q)
        if not ok:
            continue
        uniq = {(q.get('published') or '').lower(): q for q in repl}
        out.append({
            'published': d.get('published', ''), 'original': d.get('original', ''),
            'provider': d.get('provider', ''), 'version': d.get('version', ''),
            'date': d.get('date', ''),
            'devices': [n.get('friendly') or n.get('desc') or n.get('id') for n in hit],
            'replaced_by': [{'published': q.get('published', ''), 'original': q.get('original', ''),
                             'version': q.get('version', ''), 'date': q.get('date', '')}
                            for q in uniq.values()],
        })
    return out


def retire_superseded_packages(run, log, get_drivers, check_cancel=None):
    """A `find_superseded_packages` találatainak törlése, felügyelet nélkül.

    Minden hiba elnyelve (a telepítést nem buktathatja). Nem tud dönteni -> nem töröl.
    Visszatérés: (törölt, sikertelen, kihagyott)."""
    try:
        from app import driverusage_core
        from app.wu_core import _hwid_matches, release_rank, _read_text_best_effort
        drivers = get_drivers() or []
        ctx = driverusage_core.collect_usage_context(run, drivers)
        if not ctx or not ctx.get('raw') or ctx['raw'].get('nodes') is None:
            logging.info("[DUPDRV] Kiváltott-csomag vizsgálat kimarad: a használat-felderítés "
                         "vagy az eszközfa nem futott le (nemtudásra nem törlünk).")
            return 0, 0, 0
        usage = ctx['usage']
        inf_dir = driverusage_core._inf_dir()
        inf_hwids = {}
        for pub, e in usage.items():
            if e.get('state') != 'unused':
                continue
            text = _read_text_best_effort(os.path.join(inf_dir, pub))
            inf_hwids[pub] = driverusage_core.parse_inf_models(text)['hwids'] if text else []
        found = find_superseded_packages(drivers, usage, ctx['raw']['nodes'], inf_hwids,
                                         _hwid_matches, release_rank)
        logging.info(f"[DUPDRV] Kiváltott-csomag vizsgálat: {len(inf_hwids)} nem használt csomag "
                     f"megnézve, {len(found)} bizonyítottan kiváltott.")
        if not found:
            return 0, 0, 0
        active = get_active_published_infs(run)
        if active is None:
            log('  ⚠️ Az aktív driver-lista nem kérdezhető le - a kiváltott csomagok törlése kimarad.')
            return 0, 0, 0
        ok = fail = skipped = 0
        for s in found:
            if check_cancel and check_cancel():
                break
            pub = s['published']
            uj = ', '.join(f"{r['original'] or r['published']} v{r['version']} [{r['date'] or '?'}]"
                           for r in s['replaced_by'])
            cimke = f"{pub} ({s['original']} {s['provider']} v{s['version']} [{s['date'] or '?'}])"
            if pub.lower() in active:
                skipped += 1
                logging.info(f"[DUPDRV] Kiváltott csomag időközben aktív lett, marad: {cimke}")
                continue
            logging.warning(f"[DUPDRV] KIVÁLTOTT csomag törlése: {cimke} - eszköz(ök): "
                            f"{s['devices']} - helyette fut: {uj}")
            res = run(['pnputil', '/delete-driver', pub], ok_codes=(0, 3010))
            if res and res.returncode in (0, 3010):
                ok += 1
                log(f'  ✅ {cimke} törölve - kiváltotta: {uj}')
            elif delete_blocked_in_use(res):
                skipped += 1
                logging.info(f"[DUPDRV] Kiváltott csomag marad (használatban): {cimke}")
            else:
                fail += 1
                why = delete_failure_text(res)
                logging.warning(f"[DUPDRV] Kiváltott csomag törlése SIKERTELEN: {cimke} - {why}")
                log(f'  ❌ {cimke} törlése sikertelen: {why}')
        if ok or fail:
            log(f'  🧹 Átnevezett INF-fel kiváltott régi csomag: {ok} törölve'
                + (f', {fail} sikertelen' if fail else '') + '.')
        return ok, fail, skipped
    except Exception as e:
        logging.warning(f"[DUPDRV] Kiváltott-csomag takarítás hiba (a telepítést nem érinti): {e}",
                        exc_info=True)
        return 0, 0, 0


def duplicate_delete_details(groups):
    """published (oemNN.inf) -> emberi címke ("eredeti.inf, Gyártó vX [dátum]").

    A törlési naplósorok és a képernyő-szöveg ebből tudják megnevezni, MI tűnt el:
    az `oemNN.inf` szám önmagában semmitmondó és újratelepítéskor átszámozódik."""
    out = {}
    for g in (groups or []):
        for d in g.get('dups') or []:
            pub = (d.get('published') or '').strip()
            if not pub:
                continue
            reszek = [g.get('original') or '?']
            if d.get('provider'):
                reszek.append(str(d['provider']))
            if d.get('version'):
                reszek.append('v' + str(d['version']))
            if d.get('date'):
                reszek.append(f"[{d['date']}]")
            out[pub.lower()] = ' '.join(reszek)
    return out


def delete_duplicate_packages(run, log, names, active_infs, check_cancel=None, details=None):
    """A kijelölt régi duplikátum-verziók törlése (pnputil /delete-driver, sikertelen
    törlésnél második kör /force-szal). A names listát a hívónak már oemXX-re szűrve
    kell átadnia; az active_infs a TÖRLÉS ELŐTT frissen lekérdezett aktív-halmaz.

    `details`: published -> emberi címke (duplicate_delete_details). Nem kötelező, de
    ADD MEG, ha van: enélkül a napló és a képernyő is csak egy `oemNN.inf` számot mutat,
    amiből utólag senki nem tudja megmondani, melyik driver tűnt el.
    Visszatérés: (ok, fail, skipped)."""
    ok = fail = skipped = 0
    total = len(names)
    details = details or {}

    def _cimke(n):
        d = details.get((n or '').lower())
        return f'{n} ({d})' if d else n

    for i, name in enumerate(names):
        if check_cancel and check_cancel():
            log('\n❗ Megszakítva!')
            break
        if name.lower() in active_infs:
            skipped += 1
            log(f'  ⏭ {_cimke(name)} - időközben aktív lett, kihagyva')
            continue
        # ROMBOLÓ LÉPÉS: a cél a MŰVELET ELŐTT megy a naplóba, névvel.
        logging.warning(f"[DUPDRV] Törlés indul: {_cimke(name)}")
        res = run(['pnputil', '/delete-driver', name], ok_codes=(0, 3010))
        deleted = bool(res) and (res.returncode == 0 or 'deleted' in (res.stdout or '').lower() or 'törölve' in (res.stdout or '').lower())
        if not deleted:
            # Második kör /force-szal: a nem használt, de valamihez még bejegyzett
            # régi verziókat csak így engedi el a pnputil.
            res = run(['pnputil', '/delete-driver', name, '/force'], ok_codes=(0, 3010))
            deleted = bool(res) and (res.returncode == 0 or 'deleted' in (res.stdout or '').lower() or 'törölve' in (res.stdout or '').lower())
        if deleted:
            ok += 1
            logging.warning(f"[DUPDRV] Törölve: {_cimke(name)}")
            log(f'  ✅ {_cimke(name)} törölve ({i + 1}/{total})')
        else:
            # A VALÓDI okot írjuk ki, nem a pnputil fejlécét (`Microsoft PnP Utility`) -
            # lásd `drivers_core.delete_failure_text`. A naplóban marad a teljes kimenet.
            why = delete_failure_text(res)
            # AZ IN-USE ESET A TAKARÍTÁSBAN NORMÁLIS KIMENETEL, NEM HIBA: a sima
            # /delete-driver szándékosan nem bánt semmit, amit egy eszköz használ (a
            # CLAUDE.md ezt a takarítás "tervezett viselkedésének" nevezi). Ezért a
            # `skipped` számlálóba megy, nem a `fail`-be: a záró sor különben azt írta,
            # hogy "1 sikertelen", miközben a tétel sora azt, hogy "ez normális" -
            # két egymásnak ellentmondó állítás egy képernyőn. A három számláló így
            # egyértelmű: ok = törölve, skipped = használatban van (nem bántjuk),
            # fail = VALÓDI hiba, amivel foglalkozni kell.
            if delete_blocked_in_use(res):
                skipped += 1
                logging.info(f"[DUPDRV] MARAD (használatban): {_cimke(name)} - {why}")
                log(f'  ⏭ {_cimke(name)} marad: {why} - ez a takarításban normális, '
                    f'a régi verzió a következő újraindítás után törölhető.')
            else:
                fail += 1
                logging.warning(f"[DUPDRV] SIKERTELEN törlés: {_cimke(name)} - {why} "
                                f"| nyers: {(res.stdout or '')[:200] if res else '?'}")
                log(f'  ❌ {_cimke(name)} törlése sikertelen: {why}')
    return ok, fail, skipped
