"""Driver listázás/törlés - KÖZÖS mag (GUI + CLI, online + offline).

A `dism /Get-Drivers` szöveg-parzolás korábban 4 közel azonos példányban élt
(GUI online/offline az app/gui/drivers.py-ban, CLI online/offline az
app/cli/drivers.py-ban) - most EGY példányban itt. Ugyanígy a "phantom csomag"
szűrés (force-delete-elt bejegyzések), az egy-driveres törlés és az agresszív
force-törlés fallback is.

FONTOS (CLAUDE.md): a dism hívásoknak /English-sel KELL futniuk, mert a lenti
angol kulcsszavas parzolás ("Published Name" stb.) más nyelvű Windows/WinPE
alatt különben némán üres listát ad. A driver-verzió a valódi kimenetben önálló
"Version :" sor (NEM "Date and Version").
"""

# === AUTO-IMPORTS ===
import os
import re
import time
import shutil
import json
import glob
import logging
from app.common import CMD_TIMEOUT_RETURNCODE
# === /AUTO-IMPORTS ===


# EGY driver-csomag törlésének felső korlátja (mp). A pnputil a PnP query-remove-ra vár;
# egy beragadt eszközverem (terepen: Intel RST tárolóvezérlő) esetén a WINDOWS SAJÁT belső
# időkorlátja ~143 mp - a mienknek EZ ALATT kell lennie, különben sosem lép közbe (a 180-as
# első próbálkozás pont ezért volt hatástalan). A sikeres törlések a terepi logokban
# 0,1-7 mp közt vannak, tehát a 60 mp ~8x tartalék még lassú HDD-s gépen is. Ha mégis
# elvágnánk egy lassú, de élő törlést, az nem okoz kárt: a hívó ilyenkor újraindít és
# FOLYTATJA a törlést, tehát a csomag a következő lábon úgyis sorra kerül.
DELETE_DRIVER_TIMEOUT = 60

# A pnputil visszatérési kódjai, amikor a PnP query-remove időtúllépésbe fut (a terepi
# logban 480 és 482 - "A művelet túllépte az időkorlátot, miközben arra várakozott, hogy
# az eszköz végrehajtson egy PnP-lekérdezéstörlési kérelmet ... Lehet, hogy a rendszert
# újra kell indítani"). A saját timeoutunk alatt ezek már ritkán jönnek elő, de ha a
# Windows előbb ad fel, ezekről ismerjük fel ugyanazt az állapotot.
PNP_REMOVAL_STALL_CODES = (480, 482)


def _dism_date_to_iso(val):
    """A DISM `Date :` mezője -> 'YYYY-MM-DD' (rendezhető), különben ''.

    >>> A `/English` CSAK A KULCSSZAVAKAT ANGOLOSÍTJA, A DÁTUM-ÉRTÉKET NEM. <<<
    Ez a függvény korábban az ellenkezőjét állította ("a `/English` kimenet mindig
    M/D/YYYY alakú - a lokalizált formátumokra nem számítunk"), és ez a feltevés
    TEREPEN MEGDŐLT (2026-09-21, Dell Latitude 5480, Build 326). A napló szó szerint:

        Published Name : oem0.inf
        Date : 2006. 06. 21.          <- MAGYAR formátum az angol kulcsszó mellett
        Version : 10.0.19041.1806

    A DISM a dátumot a RENDSZER rövid-dátum mintájával formázza; magyar Windowson az
    `yyyy. MM. dd.` (élőben mérve: `(Get-Culture).DateTimeFormat.ShortDatePattern` ->
    `yyyy. MM. dd.`). A régi regex (`\\d{1,2}[/.-]\\d{1,2}[/.-]\\d{4}`) erre nem
    illeszkedett, tehát a `date` mező MINDEN magyar gépen üresen maradt - vagyis a
    duplikátum-takarítás "dátum elsődleges, verzió csak holtversenynél" szabálya
    (a projekt 2026-07-27 óta egységes kiadás-rendezése) a bolt ÖSSZES gépén némán
    verzió-alapú maradt. Pontosan az a hibaosztály, ami miatt az a szabály született:
    egy gyártói verziósémaváltásnál a verzió-rendezés a FRISSEBB csomagot jelöli meg
    törölhetőnek. A hiba egy éve némán élt, mert semmi nem jelezte a hiányzó dátumot -
    ezért logol a `parse_dism_driver_list` összegző sora.

    KÉT ALAK, ÉS A MÁSODIK KÉTÉRTELMŰ:
      - NÉGYJEGYŰ ÉV ELÖL (`2006. 06. 21.`, `2006-06-21`, `2006/06/21`): egyértelmű,
        utána hónap, majd nap. Ez a magyar/ISO/kelet-ázsiai alak.
      - NÉGYJEGYŰ ÉV HÁTUL (`6/21/2006`, `21.06.2006`): a hónap/nap sorrend nem
        olvasható ki a számokból. Amit el tudunk dönteni, azt eldöntjük (ha az egyik
        szám > 12, az csak nap lehet), a maradékra a szeparátor szokása dönt:
        `/` -> amerikai M/D, `.` vagy `-` -> európai D/M.

    A kétértelműség ára kicsi és NEM ROMLÓ: a rendezés relatív, a heurisztika pedig
    determinisztikus, tehát egy csoport minden csomagját UGYANÚGY értelmezi - egy
    esetleges hónap/nap csere az évet és a hónap-szintű sorrendet nem forgatja fel.
    Érvénytelen (hónap>12, nap>31) értékre üres stringet adunk, mert egy rossz dátum
    rosszabb, mint a hiányzó: a hiányzót a `dup_release_key` a verzióval pótolja."""
    s = (val or '').strip()
    # 1) Négyjegyű év ELÖL - egyértelmű sorrend (ide tartozik a magyar `yyyy. MM. dd.`).
    m = re.match(r'^(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})\.?$', s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        # 2) Négyjegyű év HÁTUL - a hónap/nap sorrend kétértelmű.
        m = re.match(r'^(\d{1,2})([.\-/])\s*(\d{1,2})[.\-/]\s*(\d{4})\.?$', s)
        if not m:
            return ''
        a, sep, b, y = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
        if a > 12:        # az első szám nem lehet hónap
            mo, d = b, a
        elif b > 12:      # a második szám nem lehet hónap
            mo, d = a, b
        elif sep == '/':  # amerikai szokás: M/D/YYYY
            mo, d = a, b
        else:             # európai szokás: D.M.YYYY
            mo, d = b, a
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ''
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_dism_driver_list(stdout):
    """`dism /English ... /Get-Drivers` kimenet -> driver-dict lista
    ({'published','original','provider','class','version','date'} kulcsokkal).

    A 'date' ISO alakú ('YYYY-MM-DD') és a DISM `Date :` sorából jön: a
    DriverStore-takarítás ez alapján dönti el, melyik a csomagcsalád LEGÚJABB kiadása
    (dátum elsődleges, verzió csak holtversenynél - lásd wu_core.release_rank)."""
    drivers = []
    current = {}
    for line in (stdout or '').splitlines():
        line = line.strip()
        if not line:
            if current and "published" in current:
                drivers.append(current)
                current = {}
            continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            key, val = parts[0].strip(), parts[1].strip()
            if "Published Name" in key:
                current["published"] = val
            elif "Original File Name" in key:
                current["original"] = val
            elif "Provider Name" in key:
                current["provider"] = val
            elif "Class Name" in key:
                current["class"] = val
            elif key == "Date":
                current["date"] = _dism_date_to_iso(val)
                if val and not current["date"]:
                    # A NYERS ÉRTÉKET IS MEGTARTJUK, hogy a napló megnevezhesse, mit nem
                    # sikerült értelmezni. E nélkül a hiányzó dátum egy éven át NÉMA volt
                    # (lásd `_dism_date_to_iso`): minden magyar gépen elveszett, és a
                    # duplikátum-rendezés dátum-elsőbbsége csendben verzió-alapúvá esett.
                    current["date_raw"] = val
            elif "Version" in key:
                current["version"] = val
    if current and "published" in current:
        drivers.append(current)
    # ÉRTELMEZHETETLEN DÁTUM-FORMÁTUM: EGY összegző sor (nem csomagonként - a lista
    # 100+ tételes is lehet, és a Rule 0 ellen-szabálya szerint a hot loop nem logol).
    # Ez az a jelzés, ami a 2026-09-21-i terepi leletet egy pillantással megadta volna.
    unparsed = sorted({d['date_raw'] for d in drivers if d.get('date_raw')})
    if unparsed:
        logging.warning(
            f"[DRIVERS] {sum(1 for d in drivers if d.get('date_raw'))}/{len(drivers)} csomag "
            f"DÁTUMÁT nem sikerült értelmezni - a duplikátum-takarítás ezeknél a verzióra "
            f"esik vissza. Előforduló nyers alakok: {unparsed[:5]} "
            f"(a DISM a rendszer rövid-dátum mintáját használja, a /English csak a "
            f"kulcsszavakat angolosítja).")
    # NÉMA PARZOLÁSI HIBA elleni jelzés: ha a DISM adott kimenetet, de egyetlen csomagot
    # sem sikerült kinyerni, az szinte biztosan a /English hiánya (más nyelvű Windows/
    # WinPE) vagy megváltozott kimeneti formátum - és eddig ez egy csendes "0 driver"
    # eredményként jelent meg a felületen, mindenféle nyom nélkül (CLAUDE.md-ben
    # dokumentált hibaosztály). Ez a sor teszi a logból azonnal felismerhetővé.
    if not drivers and (stdout or '').strip():
        # A DISM SAJÁT HIBÁJÁT NEM SZABAD PARZOLÁSI HIBÁNAK LÁTNI (2026-09-07, terepi
        # naplóból). A régi szöveg egyetlen okot kínált ("/English hiányzik vagy változott
        # a formátum?"), és pontosan akkor is azt írta ki, amikor a DISM világosan
        # megmondta a valódi okot: `The specified image is currently being serviced by
        # another DISM operation`. A technikus egy üres driver-listát látott, hamis
        # magyarázattal - ez a fájl egyik legrégebbi hibaosztálya (rossz ok = rossz nyom).
        why = dism_failure_reason(stdout)
        logging.warning(f"[DRIVERS] DISM-parzolás NULLA csomagot adott "
                        f"{len((stdout or '').splitlines())} sornyi kimenetből - "
                        f"{why or 'ismeretlen ok (/English hiányzik vagy változott a formátum?)'}. "
                        f"Első 300 kar.: {(stdout or '')[:300]!r}")
    return drivers


# A DISM ISMERT HIBÁI, emberi nyelven. Kulcs: a kimenet ANGOL töredéke (a hívásaink mind
# `/English`-sel mennek); érték: (rövid ok, átmeneti-e). Az "átmeneti" azt jelenti, hogy
# egy újrapróbálásnak van értelme - lásd get_offline_drivers/get_third_party_drivers.
DISM_ERROR_HINTS = (
    ('currently being serviced by another dism operation',
     'egy MÁSIK DISM művelet dolgozik ugyanezen a lemezképen - meg kell várni', True),
    ('another dism operation', 'egy másik DISM művelet fut - meg kell várni', True),
    ('error: 740', 'a DISM nem rendszergazdaként futott (Error: 740)', False),
    ('elevated permissions', 'a DISM nem rendszergazdaként futott', False),
    ('error: 87', 'érvénytelen DISM-paraméter (Error: 87) - futó Windowsra mutató '
                  '/Image útvonal? Arra a /Online való', False),
    ('points to a running windows installation',
     'az /Image a FUTÓ Windowsra mutat - arra a /Online való', False),
    ('does not appear to be a valid windows directory',
     'a megadott útvonal nem érvényes Windows-mappa', False),
    ('error: 2', 'a DISM nem találja a megadott útvonalat (Error: 2)', False),
    ('error: 50', 'a művelet ezen a lemezképen nem támogatott (Error: 50)', False),
)


def dism_failure_reason(stdout, stderr=''):
    """A DISM kimenetéből a HIBA OKA emberi nyelven, vagy '' ha nem ismerjük fel.

    Tiszta függvény, offline tesztelhető. Csak MAGYARÁZ - nem dönt semmiről."""
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    for needle, why, _transient in DISM_ERROR_HINTS:
        if needle in text:
            return why
    return ''


def dism_is_busy(stdout, stderr=''):
    """Igaz, ha a DISM azért nem adott listát, mert ÁTMENETILEG foglalt.

    Ez az egyetlen olyan hiba a fentiek közül, amit egy rövid várakozás megold - a többi
    (jogosultság, rossz útvonal) újrapróbálástól sem lesz jobb."""
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    return any(n in text for n, _w, transient in DISM_ERROR_HINTS if transient)


def _file_repository_path(target_os_path=None):
    """A DriverStore FileRepository útvonala (online: futó rendszer, offline: cél-OS)."""
    if target_os_path:
        return os.path.join(target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
    return os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")


def _inf_dir_path(target_os_path=None):
    """A Windows\\INF mappa útvonala (online/offline)."""
    if target_os_path:
        return os.path.join(target_os_path, "Windows", "INF")
    return os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "INF")


def filter_phantom_packages(drivers, target_os_path=None):
    """Szellem (korábban force-delete-elt) csomagok kiszűrése: egy nem-oem publikált
    nevű bejegyzés csak akkor valódi, ha még van hozzá tartozó mappa a DriverStore
    FileRepository-jában."""
    rep = _file_repository_path(target_os_path)
    valid_drivers = []
    dropped = []
    for d in drivers:
        pub = d.get("published", "")
        if not pub:
            dropped.append('(névtelen bejegyzés)')
            continue
        if pub.lower().startswith("oem"):
            valid_drivers.append(d)
            continue
        if glob.glob(os.path.join(rep, f"{pub}_*")):
            valid_drivers.append(d)
        else:
            dropped.append(f"{pub} ({d.get('original', '?')})")
    # A kiszűrt csomagok EDDIG némán tűntek el a listából: ha valaki azt jelenti, hogy
    # "hiányzik egy driver a listáról", enélkül semmi nyom nem maradt róla. Nem hiba
    # (pont ez a szűrő dolga), de látszania kell.
    if dropped:
        logging.info(f"[DRIVERS] Szellem-szűrő: {len(dropped)} csomag kihagyva (nincs FileRepository mappájuk): "
                     f"{', '.join(dropped[:10])}{' ...' if len(dropped) > 10 else ''}")
    return valid_drivers


# Hányszor próbáljuk újra a driver-listát, ha a DISM ÉPP FOGLALT, és mennyit várunk közte.
# Terepen (2026-09-04, Lenovo) a lista kétszer üresen jött vissza, mert egy másik DISM
# művelet dolgozott ugyanazon a lemezképen - a felület "0 driver"-t mutatott. Egy másik
# DISM tipikusan másodpercek alatt végez (gyakran a SAJÁT másik szálunk az), ezért egy
# rövid, korlátos várakozás megoldja; ha mégsem, a hívó továbbra is üres listát kap, de
# a napló ekkor már a VALÓDI okot nevezi meg.
DISM_BUSY_RETRIES = 3
DISM_BUSY_WAIT_S = 4


def _run_dism_list(run, cmd):
    """Egy `dism ... /Get-Drivers` hívás, a FOGLALTSÁG rövid kivárásával.

    Csak akkor próbálkozik újra, ha a DISM maga mondja, hogy foglalt (`dism_is_busy`) -
    egy jogosultsági vagy útvonal-hibán az újrapróbálás csak időt pazarolna."""
    res = run(cmd)
    for attempt in range(1, DISM_BUSY_RETRIES + 1):
        if not dism_is_busy(res.stdout, getattr(res, 'stderr', '')):
            return res
        logging.warning(f"[DRIVERS] A DISM foglalt (másik művelet dolgozik a lemezképen) - "
                        f"{attempt}/{DISM_BUSY_RETRIES}. újrapróbálás {DISM_BUSY_WAIT_S} mp múlva.")
        time.sleep(DISM_BUSY_WAIT_S)
        res = run(cmd)
    if dism_is_busy(res.stdout, getattr(res, 'stderr', '')):
        logging.warning("[DRIVERS] A DISM a várakozás után is foglalt - a lista üres marad. "
                        "Ez NEM azt jelenti, hogy nincs driver a gépen.")
    return res


def get_third_party_drivers(run, problems=None):
    """Third-party driverek listája (online). /English: kényszerített angol DISM
    kimenet, függetlenül a Windows nyelvi beállításától.

    `problems`: opcionális lista. Ha a DISM hibázott, a felismert OK bekerül ide - így a
    hívó meg tudja különböztetni a "tényleg nincs driver" és a "nem sikerült lekérdezni"
    esetet, ami eddig mindkettő egyformán `0 driver`-ként jelent meg a felületen."""
    res = _run_dism_list(run, ['dism', '/English', '/Online', '/Get-Drivers'])
    drivers = parse_dism_driver_list(res.stdout)
    if problems is not None and not drivers:
        why = dism_failure_reason(res.stdout, getattr(res, 'stderr', ''))
        if why:
            problems.append(why)
    return drivers


def get_all_drivers(run):
    """Összes driver listája (online, veszélyes mód). JSON-hiba esetén kivételt dob -
    a hívó dönt (a GUI hibát jelenít meg, a CLI üres listával folytat)."""
    cmd = ['powershell', '-NoProfile', '-Command',
           '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-WindowsDriver -Online -All | Select-Object ProviderName, ClassName, Version, Driver, OriginalFileName | ConvertTo-Json -Depth 2 -WarningAction SilentlyContinue']
    res = run(cmd, encoding='utf-8')
    out = (res.stdout or '').strip()
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    parsed_drivers = [{"published": d.get("Driver", ""), "original": d.get("OriginalFileName", ""),
                       "provider": d.get("ProviderName", ""), "class": d.get("ClassName", ""),
                       "version": d.get("Version", "")} for d in data]
    return filter_phantom_packages(parsed_drivers)


def get_offline_drivers(run, target_os_path, all_drivers=False, problems=None):
    """Offline cél-OS drivereinek listája (dism /Image:...).

    `problems`: lásd get_third_party_drivers - a DISM felismert hibája ide kerül."""
    cmd = ['dism', '/English', f'/Image:{target_os_path}', '/Get-Drivers']
    if all_drivers:
        cmd.append('/all')
    res = _run_dism_list(run, cmd)
    drivers = parse_dism_driver_list(res.stdout)
    if problems is not None and not drivers:
        why = dism_failure_reason(res.stdout, getattr(res, 'stderr', ''))
        if why:
            problems.append(why)
    return filter_phantom_packages(drivers, target_os_path)


# A törlés-kimenet SIKERTELENSÉGÉT jelző töredékek. Ezeket a pozitív tövek ELŐTT kell
# nézni, különben a magyar HIBAüzenet is sikernek látszik - lásd delete_succeeded.
# Ékezet nélküli előtagokkal dolgozunk ('nem siker'), mert a pnputil ANSI-ban ír
# (CLAUDE.md: a kimenete terepen mojibake-ként érkezik), és egy elrontott 'ü' miatt a
# teljes szóra illesztés némán elhasalna.
DELETE_FAILURE_MARKERS = (
    'nem siker',        # "nem sikerült" (a 'siker' pozitív tő ELŐTT kell illeszkednie!)
    'sikertelen',
    'nem lehet',
    'nem tudta',
    'failed', 'failure', 'unable', 'cannot', 'could not', 'denied',
    'access is denied', 'in use',
)

# A SIKERT jelző töredékek. A magyar ragozás két külön tövet ad ('törl'-és/-ése, de
# 'töröl'-ve/-t), egyetlen minta nem fedi mindkettőt; az ékezet nélküli párok
# ('torl'/'torol'/'eltavolit') a mojibake-es kimenetre valók.
DELETE_SUCCESS_MARKERS = (
    'deleted', 'successfully', 'removed',
    'törl', 'töröl', 'torl', 'torol',
    'sikerült', 'sikerul', 'siker',
    'eltávolít', 'eltavolit',
)


def delete_succeeded(res):
    """Egy driver-törlő parancs eredményéből eldönti, sikeres volt-e (a returncode
    mellett a lokalizált szövegeket is figyelembe véve, kis/nagybetű-függetlenül).

    A 3010 (ERROR_SUCCESS_REBOOT_REQUIRED) MAGÁBAN sikernek számít: a csomag törlődött,
    csak a lezáráshoz kell újraindítás - az AutoFix úgyis újraindul.

    A SORREND LÉNYEGI, nem stílus: a szöveges ág CSAK akkor fut, ha a returncode NEM
    0/3010 - vagyis pont a HIBÁS esetben. A magyar pnputil hibaüzenete viszont
    "A(z) oem12.inf illesztőprogram-csomag TÖRLÉSE nem sikerült" alakú, azaz tartalmazza
    a 'törl' pozitív tövet -> a régi, csak-pozitív szűrő egy bukott törlést SIKERNEK
    minősített. Ez pontosan az a néma hamis siker, amit a Build 218 óta kerülünk: az
    AutoFix 'nem törölhető csomagok' listája (app/gui/autofix.py) némán üres maradt, és a
    fázis tiszta '✅ Driverek eltávolítva'-t írt ki, miközben csomagok maradtak vissza.
    Ezért előbb a TAGADÓ alakokra nézünk, és csak utána a sikertövekre."""
    if res.returncode in (0, 3010):
        return True
    out = (res.stdout or '').lower()
    if not out:
        # Nincs mihez nyúlni: a nem-nulla returncode marad az egyetlen jel -> hiba.
        return False
    if any(k in out for k in DELETE_FAILURE_MARKERS):
        return False
    return any(k in out for k in DELETE_SUCCESS_MARKERS)


def delete_stalled(res):
    """Igaz, ha a törlés a BERAGADT ESZKÖZVERMEN akadt el (saját időtúllépésünk vagy a
    pnputil PnP query-remove hibakódja), nem pedig "rendes" hibával bukott.

    Terepen bizonyított ok-okozat (Dell OptiPlex, több futásban azonosan): amint az Intel
    RST tárolóvezérlő csomagja "3010 - reboot kell a befejezéshez"-zel törlődik, a rákövetkező
    csomagok query-remove kérése beragad, és DARABONKÉNT ~143 mp-et eszik meg. ÚJRAINDÍTÁS
    UTÁN ugyanaz a csomag 0,5 mp alatt törlődik (16:48-as terepi futás) - tehát a helyes
    válasz nem a további őrlés, hanem: kör vége, reboot, törlés folytatása."""
    rc = getattr(res, 'returncode', None)
    return rc == CMD_TIMEOUT_RETURNCODE or rc in PNP_REMOVAL_STALL_CODES


def delete_driver_package(run, pub, target_os_path=None, timeout=None):
    """Egy driver-csomag törlése (online: pnputil, offline: dism). A nyers eredményt
    adja vissza - a sikert a hívó delete_succeeded()-del dönti el.

    timeout (mp): felső korlát EGY csomag törlésére. Kell, mert a pnputil a PnP
    query-remove-ra vár, és egy nem válaszoló eszközverem (terepen: Intel RST
    tárolóvezérlő) percekig lógatja - timeout nélkül ez az egész AutoFix lábat
    megakasztja. Időtúllépéskor a _run CMD_TIMEOUT_RETURNCODE-dal tér vissza."""
    if target_os_path:
        return run(['dism', f'/Image:{target_os_path}', '/Remove-Driver', f'/Driver:{pub}'], timeout=timeout)
    # ok_codes 3010: siker, de reboot kell a lezáráshoz - a delete_succeeded sikeresnek veszi.
    return run(['pnputil', '/delete-driver', pub, '/uninstall', '/force'], ok_codes=(0, 3010), timeout=timeout)


# ============================================================================
# "A CSOMAGOT HASZNÁLJA EGY TELEPÍTETT ESZKÖZ" - a nyomtató-csomagok esete (2026-09-07)
# ============================================================================
# Terepi napló (Lenovo, 2026-09-04): a technikus KIKAPCSOLTA a nyomtató-védelmet, tehát
# kifejezetten törölni akarta a nyomtató-csomagokat, a pnputil mégis mind a négyet
# elutasította. A kimenet két sora együtt mondja meg, mi történt:
#
#   Driver package uninstalled.
#   Failed to delete driver package: Legalább egy olyan eszköz van telepítve jelenleg,
#   amely a megadott INF fájlt használja.        (returncode 3758096957 = 0xE000023D)
#
# Vagyis a `/uninstall` LEFUTOTT (a driver lekerült az eszközről), csak a CSOMAG maradt,
# mert egy telepített eszköz még hivatkozik rá. Nyomtató-INF-eknél ez az eszköz a
# nyomtatósor, amit a Nyomtatásisor-kezelő (Spooler) szolgáltatás tart életben - amíg az
# fut, a csomag nem törölhető, akárhányszor próbáljuk.
#
# A SZOLGÁLTATÁS LEÁLLÍTÁSA NEM MINDIG ELÉG - MÉRVE, NE TEKINTSD GARANCIÁNAK
# (2026-09-07, ASRock B450M, Build 303). Ugyanaz a lánc a Spoolert bizonyítottan
# leállította ("The Print Spooler service was stopped successfully."), a törlés mégis
# ugyanezzel a 0xE000023D kóddal bukott a `prnms009.inf` / `prnms006.inf` csomagokon.
# Az ok: a csomagot nem a szolgáltatás fájl-zárolása tartja, hanem a nyomtatósor
# `SWD\PRINTENUM\{...}` ESZKÖZ-CSOMÓPONTJA, ami a szolgáltatás leállítása után is
# bejegyezve marad a PnP-ben. Ez a kettő a Windows saját virtuális nyomtatója (Print to
# PDF / XPS Document Writer), amit a Windows amúgy is visszatesz - a megmaradásuk tehát
# ártalmatlan. A kör ezért megmarad (ahol tényleg a szolgáltatás az akadály, ott
# megoldja), de a hívó KÜLÖN JELENTI ezt a kimenetelt, mert egy "nem sikerült" a valódi
# ok nélkül pont az a fajta félrevezetés, amiből ez a fájl gyűjt.
#
# A megoldás ugyanaz a minta, amit a temp-takarítás már használ a szolgáltatás-zárolta
# mappáknál: a szolgáltatást EGYSZER állítjuk le a köteg elején, nem csomagonként, és a
# végén MINDENKÉPP visszaindítjuk.
DELETE_IN_USE_RETURNCODE = 0xE000023D          # 3758096957

# A kimenet szövege ugyanezt mondja, lokalizáltan. Ékezet nélküli töredékek, mert a
# pnputil ANSI-ban ír és terepen mojibake-ként érkezik (lásd DELETE_FAILURE_MARKERS).
DELETE_IN_USE_MARKERS = (
    'inf f',                    # "a megadott INF fájlt használja" / "INF file"
    'currently installed',
    'is in use', 'in use by',
)


def delete_blocked_in_use(res):
    """Igaz, ha a törlés azért bukott, mert egy TELEPÍTETT eszköz még használja a csomagot.

    Ez nem ugyanaz, mint a `delete_stalled` (beragadt eszközverem, timeout): ott várni
    kell, itt a használót kell megszüntetni. Tiszta függvény, offline tesztelhető."""
    if res is None or delete_succeeded(res):
        return False
    if getattr(res, 'returncode', None) == DELETE_IN_USE_RETURNCODE:
        return True
    text = f"{getattr(res, 'stdout', '') or ''}\n{getattr(res, 'stderr', '') or ''}".lower()
    return 'failed to delete driver package' in text and any(m in text for m in DELETE_IN_USE_MARKERS)


# A nyomtatósort életben tartó szolgáltatás. A `/force` sem segít rajta: nem jogosultsági
# kérdés, hanem az, hogy a szolgáltatás valóban használja az INF-et.
PRINT_SPOOLER_SERVICE = 'Spooler'


def set_service_state(run, service, start):
    """Egy szolgáltatás indítása/leállítása. Visszatérés: sikerült-e.

    A `net stop` 2-es kódja ("már áll") és a `net start` 2182-e ("már fut") NEM hiba -
    ezért mennek `ok_codes`-ban, különben minden futás hamis WARNING-ot írna."""
    verb = 'start' if start else 'stop'
    codes = (0, 2182) if start else (0, 2, 2184)
    res = run(['net', verb, service], ok_codes=codes, timeout=90)
    ok = res.returncode in codes
    logging.log(logging.INFO if ok else logging.WARNING,
                f"[DRIVERS] A(z) {service} szolgáltatás {'indítása' if start else 'leállítása'}: "
                f"{'OK' if ok else 'SIKERTELEN'} (returncode={res.returncode})")
    return ok


def retry_in_use_deletes(run, items, log=None, check_cancel=None, timeout=None,
                         target_os_path=None, log_tag='DELETE'):
    r"""MÁSODIK KÖR: a "egy telepített eszköz használja" hibával bukott csomagok
    újrapróbálása a nyomtatósor-kezelő (Spooler) ÁTMENETI leállításával.

    `items`: a bukott csomagok dict-jei (`published` + lehetőleg `original`, `provider`,
    `class`). `log(msg)`: a képernyőre menő sorok (GUI: emit-lambda, CLI: print) - None
    esetén csak a napló kap sort. `check_cancel()`: igazra a kör megszakad.

    Visszatérés: {'deleted': [dict...], 'still_in_use': ['név (eredeti)'...],
                  'failed': ['név (eredeti)'...]}.

    EGY MAG, KÉT HÍVÓ (2026-09-18, explicit user decision): eddig csak az AutoFix
    törlési fázisa ismerte ezt a kört, a KÉZI törlés nem - ott a technikus annyit
    látott, hogy "❌ sikertelen", ok nélkül. Terepen ez 14 nyomtató-csomagnál fordult
    elő egyszerre. Két külön példány előbb-utóbb eltérne, és a két képernyő mást mondana
    ugyanarról a csomagról.

    HÁROM SZABÁLY, ami nélkül ez többet ártana, mint használ:
      1. A szolgáltatást EGYSZER állítjuk le az egész kötegre, nem csomagonként.
      2. A visszaindítás `finally`-ben van, tehát kivétel, megszakítás vagy törlési hiba
         után is megtörténik - egy Spooler nélkül visszaadott gép nem tud nyomtatni, ami
         sokkal rosszabb, mint egy megmaradt driver-csomag.
      3. NEM szűrünk osztályra: amit a technikus törölni akart, azt törölni akarjuk
         (CLAUDE.md: a törlésbe soha ne kerüljön új szűrő).

    A KÖR MÉRT HATÁRA (2026-09-07, ASRock B450M, Build 303): a Spooler bizonyítottan
    leállt, a törlés mégis ugyanazzal a 0xE000023D kóddal bukott a `prnms009.inf` /
    `prnms006.inf` csomagokon - azokat nem a SZOLGÁLTATÁS tartja, hanem a nyomtatósor
    `SWD\PRINTENUM\{...}` ESZKÖZ-csomópontja, ami a leállítás után is bejegyezve marad.
    Ezért van külön `still_in_use` lista: a hívó ezt nevén nevezheti a technikusnak.
    NEM megoldás a nyomtatósor-eszköz eltávolítása - az az ügyfél nyomtatóját venné ki a
    gépből, amit senki nem kért."""
    out = {'deleted': [], 'still_in_use': [], 'failed': []}
    items = [d for d in (items or []) if (d or {}).get('published')]
    if not items:
        return out

    def _say(msg):
        if log:
            log(msg)

    names = [f"{d.get('published')} ({d.get('original', '')})" for d in items]
    logging.warning(f"[{log_tag}] {len(items)} csomagot használ egy telepített eszköz - a(z) "
                    f"{PRINT_SPOOLER_SERVICE} leállításával újrapróbáljuk: {names}")
    _say(f'\n🖨️ {len(items)} csomagot még használ egy telepített eszköz (jellemzően a '
         f'nyomtatósor) - a nyomtatósor átmeneti leállításával újrapróbáljuk...')
    spooler_stopped = set_service_state(run, PRINT_SPOOLER_SERVICE, start=False)
    try:
        for drv in items:
            if check_cancel and check_cancel():
                break
            nm = drv.get('published', '')
            label = f"{nm} ({drv.get('original', '')})"
            res = delete_driver_package(run, nm, target_os_path, timeout=timeout)
            if delete_succeeded(res):
                out['deleted'].append(drv)
                logging.info(f"[{log_tag}] Törölve (2. kör): {nm} ({drv.get('original', '')}) - "
                             f"{drv.get('provider', '?')} [{drv.get('class', '?')}]")
                _say(f'  ✅ {nm} törölve (a nyomtatósor leállítása után)')
                continue
            out['failed'].append(label)
            if delete_blocked_in_use(res):
                # Leállt Spooler MELLETT is "eszköz használja" -> nem a szolgáltatás volt
                # az akadály, hanem egy eszköz-csomópont.
                out['still_in_use'].append(label)
                logging.warning(
                    f"[{log_tag}] A 2. körben sem sikerült: {label}, "
                    f"returncode={res.returncode} - a leállított {PRINT_SPOOLER_SERVICE} "
                    f"mellett is egy ESZKÖZ-csomópont (nyomtatósor) tartja, nem a szolgáltatás.")
            else:
                logging.warning(f"[{log_tag}] A 2. körben sem sikerült: {label}, "
                                f"returncode={res.returncode}")
    finally:
        # MINDENKÉPP vissza: ez a gép nyomtatási képessége.
        if spooler_stopped:
            if set_service_state(run, PRINT_SPOOLER_SERVICE, start=True):
                _say('✅ A nyomtatósor visszaindítva.')
            else:
                # Ezt LÁTNIA KELL a technikusnak - nem hallgatható el.
                _say('⚠️ A nyomtatósor (Spooler) szolgáltatást nem sikerült visszaindítani! '
                     'Indítsd el kézzel: services.msc → Nyomtatásisor-kezelő → Indítás '
                     '(vagy: net start Spooler).')
    return out


def delete_failure_text(res, limit=140):
    """Egy sikertelen `pnputil /delete-driver` VALÓDI okának emberi szövege.

    MIÉRT KELL: a pnputil kimenete a saját FEJLÉCÉVEL kezdődik, tehát a nyers stdout
    első N karaktere a hibaüzenet helyett ezt adja - terepen mérve (Dell Latitude 5480,
    Build 326) a technikus ennyit látott a képernyőn:

        ❌ oem41.inf (iigd_ext.inf Intel Corporation v30.0.101.1069) törlése sikertelen:
           Microsoft PnP Utility

    ...miközben a valódi ok a következő bekezdésben állt (`Failed to delete driver
    package: One or more devices are presently installed using the specified INF`,
    rc=0xE000023D). A "Microsoft PnP Utility" mint hibaüzenet semmit nem mond, és pont
    az a fajta néma hamis jelentés, amit ez a projekt mindenhol üldöz.

    A sorrend: (1) az in-use eset SAJÁT, MAGYAR szövege - erre van értelmes
    magyarázatunk, és a felület magyar (a pnputil `Failed to delete driver package: One
    or more devices are presently installed using the specified INF.` sora 100 karakter
    angolul, és a technikusnak nem mond többet); (2) a pnputil `Failed to ...` sora, ha
    az ok más; (3) végszükségben az első NEM fejléc-sor. Tiszta függvény."""
    if res is None:
        return '?'
    text = f"{getattr(res, 'stdout', '') or ''}\n{getattr(res, 'stderr', '') or ''}"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if delete_blocked_in_use(res):
        return 'egy telepített eszköz még használja ezt az INF-et'
    for ln in lines:
        if ln.lower().startswith('failed to'):
            return ln[:limit]
    for ln in lines:
        if 'pnp utility' not in ln.lower():
            return ln[:limit]
    rc = getattr(res, 'returncode', '?')
    return f'a pnputil nem adott magyarázatot (kód {rc})'


def in_use_explanation(count):
    """A "nyomtatósor-eszköz tartja" kimenetel magyarázata - EGY szöveg, hogy a kézi
    törlés és az AutoFix ugyanazt mondja róla."""
    return (f'   Ebből {count} db-ot a nyomtatósor ESZKÖZ-csomópontja tart, nem a Spooler '
            f'szolgáltatás - ezért a leállítása sem segített rajtuk. Ez akkor fordul elő, '
            f'ha a driverhez telepített nyomtató tartozik: előbb a nyomtatót kell '
            f'eltávolítani (Beállítások → Bluetooth és eszközök → Nyomtatók és lapolvasók), '
            f'és utána törölhető a driver. A Windows saját virtuális nyomtatóinál '
            f'(Print to PDF, XPS Document Writer) ez normális, és nincs is vele teendő.')


def force_delete_driver_files(run, pub, target_os_path=None):
    """Agresszív force-törlés fallback (takeown/icacls/rmtree a FileRepository +
    Windows\\INF alól). CSAK "összes driver" módban, nem-oem csomagra hívható -
    third-party nézetben egy sikertelen törlés egyszerűen sikertelen marad.
    Visszatérési érték: talált-e (és törölt-e) bármit."""
    rep = _file_repository_path(target_os_path)
    inf_dir = _inf_dir_path(target_os_path)
    found_any = False
    # A takeown/icacls/rmdir a _run-on át naplózódik, de a Python-oldali shutil.rmtree /
    # os.remove NEM hagyna semmi nyomot - pedig ez a legdestruktívabb művelet a
    # programban (a DriverStore FileRepository mappáit tünteti el). Ha egy force-törlés
    # olyat visz el, amit nem kellett volna, a logból pontosan látszania kell, MIT.
    logging.warning(f"[DRIVERS] FORCE-TÖRLÉS indul: {pub} (repo={rep}, inf={inf_dir})")

    dirs = glob.glob(os.path.join(rep, f"{pub}_*"))
    logging.info(f"[DRIVERS] Force-törlés: {len(dirs)} FileRepository mappa illeszkedik a(z) {pub} névre.")
    if dirs:
        for d in dirs:
            logging.warning(f"[DRIVERS] Force-törlés - mappa: {d}")
            run(f'takeown /f "{d}" /r /A', shell=True)
            run(f'icacls "{d}" /grant *S-1-5-32-544:F /t', shell=True)
            try:
                shutil.rmtree(d)
                logging.info(f"[DRIVERS] Force-törlés - rmtree OK: {d}")
            except Exception as e:
                logging.warning(f"[DRIVERS] Force-törlés - rmtree sikertelen ({d}): {e} - rmdir következik")
            run(f'rmdir /s /q "{d}"', shell=True)
            logging.info(f"[DRIVERS] Force-törlés - mappa maradt-e: {os.path.exists(d)} ({d})")
        found_any = True

    bname = os.path.splitext(pub)[0]
    for ext in ['.inf', '.pnf', '.INF', '.PNF']:
        fpath = os.path.join(inf_dir, bname + ext)
        if os.path.exists(fpath):
            logging.warning(f"[DRIVERS] Force-törlés - fájl: {fpath}")
            run(f'takeown /f "{fpath}" /A', shell=True)
            run(f'icacls "{fpath}" /grant *S-1-5-32-544:F', shell=True)
            try:
                os.remove(fpath)
                logging.info(f"[DRIVERS] Force-törlés - fájl törölve: {fpath}")
                found_any = True
            except OSError as e:
                logging.warning(f"[DRIVERS] Force-törlés - os.remove sikertelen ({fpath}): {e} - del /f /q következik")
                run(f'del /f /q "{fpath}"', shell=True)
                found_any = True
    logging.warning(f"[DRIVERS] FORCE-TÖRLÉS vége: {pub} - talált/törölt: {found_any}")
    return found_any


# ---------------------------------------------------------------------------
# A CSOMAGLISTA GYORSÍTÓTÁRA (lásd app/gui/drivers.py: _get_third_party_drivers)
# ---------------------------------------------------------------------------
# Másodlagos védőháló arra az esetre, ha a DriverStore-t rajtunk kívül írná valami
# (Windows Update, egy másik program). Az ELSŐDLEGES érvénytelenítés eseményalapú:
# minden módosító parancs eldobja a gyorsítótárat, lásd `mutates_driver_store`.
DRIVER_LIST_TTL = 120

# Egy driver-csomag TELEPÍTÉSÉNEK felső határa. Terepen mérve (2026-08-31, ThinkPad T14
# Gen 1, Win11 26200): egy `pnputil /add-driver ... /install` 31 percig futott, egy másik
# 15,5 percig - a lánc 22 telepítése összesen 60 percet vitt el. A határ bőkezű, mert egy
# nagy chipset-csomag telepítése valóban lehet több perc: itt nem a lassú, hanem a
# VÉGTELEN telepítést kell megfogni.
INSTALL_DRIVER_TIMEOUT = 900

# Azok a parancsrészletek, amik MEGVÁLTOZTATJÁK a DriverStore tartalmát. Ha egy parancs
# ezek bármelyikét tartalmazza, a csomaglista-gyorsítótár elavult.
#
# SZÁNDÉKOSAN BŐVEN MERÍTVE: egy tévesen eldobott gyorsítótár csak egy fölösleges dism-et
# jelent, egy tévesen MEGTARTOTT viszont elavult listát ad egy előtte/utána
# összehasonlításnak (pl. verify_failed_installs) - vagyis néma hamis eredményt.
_DRIVER_STORE_MUTATORS = (
    '/add-driver', '/delete-driver', '/remove-device', '/scan-devices',
    '/add-package', '/remove-driver', '/export-driver',
)


def mutates_driver_store(cmd):
    """Igaz, ha a parancs a DriverStore tartalmát megváltoztat(hat)ja."""
    try:
        text = (cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)).lower()
    except Exception:
        return False
    if not ('pnputil' in text or 'dism' in text):
        return False
    return any(m in text for m in _DRIVER_STORE_MUTATORS)
