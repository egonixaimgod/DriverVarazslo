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
import re
import json
import logging
from app.wu_core import _iso_date_or_none
from app.wu_core import _parse_driver_version
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
            log('  ✅ Nincs törölhető régi driver-verzió a DriverStore-ban.')
            return 0, 0, 0
        names = [d['published'] for g in groups for d in g['dups'] if not d['active'] and d['published']]
        log(f'  🧹 {len(names)} elavult driver-verzió törlése a DriverStore-ból...')
        ok, fail, skipped = delete_duplicate_packages(run, log, names, active, check_cancel=check_cancel)
        log(f'  🧹 DriverStore-takarítás kész: {ok} törölve' + (f', {fail} sikertelen' if fail else '') + (f', {skipped} kihagyva' if skipped else '') + '.')
        return ok, fail, skipped
    except Exception as e:
        logging.warning(f"[DUPDRV] Automatikus duplikátum-takarítás hiba (a telepítést nem érinti): {e}")
        try:
            log(f'  ⚠️ DriverStore-takarítási hiba (a telepítést nem érinti): {e}')
        except Exception:
            pass
        return 0, 0, 0


def delete_duplicate_packages(run, log, names, active_infs, check_cancel=None):
    """A kijelölt régi duplikátum-verziók törlése (pnputil /delete-driver, sikertelen
    törlésnél második kör /force-szal). A names listát a hívónak már oemXX-re szűrve
    kell átadnia; az active_infs a TÖRLÉS ELŐTT frissen lekérdezett aktív-halmaz.
    Visszatérés: (ok, fail, skipped)."""
    ok = fail = skipped = 0
    total = len(names)
    for i, name in enumerate(names):
        if check_cancel and check_cancel():
            log('\n❗ Megszakítva!')
            break
        if name.lower() in active_infs:
            skipped += 1
            log(f'  ⏭ {name} - időközben aktív lett, kihagyva')
            continue
        res = run(['pnputil', '/delete-driver', name], ok_codes=(0, 3010))
        deleted = bool(res) and (res.returncode == 0 or 'deleted' in (res.stdout or '').lower() or 'törölve' in (res.stdout or '').lower())
        if not deleted:
            # Második kör /force-szal: a nem használt, de valamihez még bejegyzett
            # régi verziókat csak így engedi el a pnputil.
            res = run(['pnputil', '/delete-driver', name, '/force'], ok_codes=(0, 3010))
            deleted = bool(res) and (res.returncode == 0 or 'deleted' in (res.stdout or '').lower() or 'törölve' in (res.stdout or '').lower())
        if deleted:
            ok += 1
            log(f'  ✅ {name} törölve ({i + 1}/{total})')
        else:
            fail += 1
            log(f'  ❌ {name} törlése sikertelen: {(res.stdout or "")[:120] if res else "?"}')
    return ok, fail, skipped
