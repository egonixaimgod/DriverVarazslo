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
    (inkább nem takarítunk, mint hogy egy aktív drivert lőjünk ki)."""
    try:
        ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
              "Get-WmiObject Win32_PnPSignedDriver | Where-Object { $_.InfName } | "
              "Select-Object InfName | ConvertTo-Json -Compress")
        res = run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=120)
        data = json.loads(res.stdout) if res and (res.stdout or '').strip() else []
        if isinstance(data, dict):
            data = [data]
        active = {str(d.get('InfName') or '').strip().lower() for d in data}
        active.discard('')
        logging.info(f"[DUPDRV] Aktívan használt inf-ek: {len(active)} db")
        return active
    except Exception as e:
        logging.error(f"[DUPDRV] Aktív inf-lista lekérdezése sikertelen: {e}")
        return None


def build_duplicate_groups(drivers, active_infs):
    """Third-party driver-lista -> duplikátum-csoportok. Egy csoport = azonos eredeti
    inf-név; a legújabb verzió megmarad ('keep'), a többi törölhető jelölt ('dups'),
    kivéve az aktívan használtakat ('active': True, nem törölhető; active_infs None
    esetén MINDEN aktívnak számít). Visszatérés: (csoport-lista, törölhetők száma)."""
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
        items_sorted = sorted(items, key=lambda d: dup_version_key(d.get('version')), reverse=True)
        keep, rest = items_sorted[0], items_sorted[1:]
        dups = []
        for d in rest:
            pub_l = (d.get('published') or '').lower()
            dups.append({
                'published': d.get('published', ''), 'version': d.get('version', ''),
                'provider': d.get('provider', ''), 'class': d.get('class', ''),
                # active_infs None (lekérdezési hiba) -> mindent aktívnak
                # jelölünk = semmi sem törölhető (biztonságos irány).
                'active': (active_infs is None) or (pub_l in active_infs),
            })
        result.append({
            'original': orig,
            'keep': {'published': keep.get('published', ''), 'version': keep.get('version', '')},
            'provider': keep.get('provider', ''), 'class': keep.get('class', ''),
            'dups': dups,
        })
    result.sort(key=lambda g: (g['provider'].lower(), g['original']))
    deletable = sum(1 for g in result for d in g['dups'] if not d['active'])
    logging.info(f"[DUPDRV] {len(result)} duplikátum-csoport, {deletable} törölhető régi verzió")
    return result, deletable


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
