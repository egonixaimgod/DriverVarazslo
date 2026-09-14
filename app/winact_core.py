"""Windows & Office aktiválás - közös mag (állapot, gyári kulcs, kulcstelepítés, aktiválás).

MIÉRT WMI-BŐL OLVAS ÉS CSAK ÍRÁSRA HASZNÁL slmgr-t: az `slmgr` kimenete LOKALIZÁLT - magyar
Windowson magyarul ír, és ennek a projektnek már van sebhelye a lokalizált konzol-szöveg
elemzéséből (lásd CLAUDE.md: a magyar pnputil "törlése nem sikerült" sora egy HIBÁS törlést
jelentett SIKERNEK). Ezért minden ÁLLAPOT a `SoftwareLicensingProduct`/`SoftwareLicensingService`
WMI-osztályokból jön, számkódként; az `slmgr` csak a három írási lépést végzi, és utána a
sikert megint a WMI mondja meg, nem a parancs kimenete.

MIÉRT `cscript //nologo`: az `slmgr` egy VBScript, és a `wscript` motorral MODÁLIS ABLAKOKAT
dob fel eredményenként - egy felügyelet nélküli GUI-folyamat ott örökre megállna. A
`cscript //nologo` ugyanazt stdout-ra írja.

A KMS-HOSTRÓL: a `/skms` a szervezet SAJÁT KMS-kiszolgálójára mutat (volume licenc), ezt a
technikus adja meg - a programban nincs, és nem is lehet, előre beírt kiszolgáló.
"""
import os
import re
import json
import shutil
import zipfile
import logging
import subprocess

from app.common import _app_data_dir
from app.common import download_with_cert_fallback

# A Windows licenc-alkalmazás azonosítója a SoftwareLicensingProduct-ban (fix GUID,
# minden Windowson ez). Enélkül a lekérdezés az összes terméket visszaadná (Office is).
WINDOWS_APP_ID = '55c92734-d682-4d71-983e-d6ec3f16059f'

# A SoftwareLicensingProduct.LicenseStatus számkódjai. A szöveget MI adjuk (magyarul),
# így nem függünk a rendszer nyelvétől.
# A LICENCÁLLAPOT emberi szövege és SZÍNE.
#
# A SZÍN AZT MONDJA MEG, MŰKÖDIK-E MOST - NEM AZT, HOGY TÖKÉLETES-E (2026-09-03, explicit
# user decision: *"ha aktiválva van és működik akkor legyen az zöld ne piros... írja azt is
# h a termék licencelt és működik, akkor az legyen inkább írva zölddel, ne az h aktiválás
# szükséges mert le fog járni - most épp aktív, ez a lényeg"*).
#
# A terepi eset: egy Office 16 `LicenseStatus=5` (Notification) állapotban volt, okként
# `0xC004F00F` ("a kulcs a hardverhez van kötve, és a hardver megváltozott"). A termék
# LICENCELT és MŰKÖDIK - a Word is aktiváltnak mutatta magát -, csak egy idő után
# figyelmeztetni fog. A program viszont PIROSSAL, "aktiválás szükséges" felirattal írta
# ki, ami egy működő terméket hibásnak mutatott, és pont az ellenkezőjét érte el, mint
# amiért ez a nézet létezik: a technikus nem tudta, mit higgyen.
#
# AZ ÁLLAPOT KÉTÁLLAPOTÚ A KÉPERNYŐN: AKTIVÁLVA VAGY NINCS (2026-09-07, explicit user
# decision). A felhasználó szavai: *"annyit írjon ki h aktiválva van vagy nincs és kész…
# most sárgán írja h aktivaljam ujra miközben be van aktiválva ez nem megtévesztő?
# feleslegesen írja sárgával ha működik írja ki zölden h aktiválva és kész"*.
#
# EZ A 2026-08-31-i és 09-03-i FINOMHANGOLÁS RÉSZLEGES VISSZAVONÁSA, és az indoklás
# fontos, hogy ne kerüljön vissza. Az akkori probléma valós volt (a nézet PIROSSAL,
# "aktiválás szükséges"-sel írt ki egy működő Office-t, miközben a Word "Aktivált
# termék"-et mutatott), de a megoldás - egy HARMADIK, sárga állapot "újraaktiválás
# ajánlott" felirattal - ugyanabba a hibába esett a másik irányból: egy aktivált,
# hibátlanul működő gépen sárga figyelmeztetést és teendőt mutatott. A technikus
# szempontjából a kérdés kétállapotú, és a képernyőnek is annak kell lennie.
#
# A HATÁR: MŰKÖDIK-E MOST A TERMÉK, aktivált licenccel?
#   'ok'    (zöld)  = 1 (Licensed), 5 (Notification) és - 2026-09-14 óta - 2/6 (türelmi
#                     idő). Az 5-ös nem azt jelenti, hogy nincs licenc: a termék licencelt
#                     és FUT, aktiváltnak is mutatja magát (mérve, 2026-08-31:
#                     Office24ProPlus2024VL_MAK_AE1, ok 0xC004F009) - csak az aktiválás
#                     nem végleges. Zöld.
#   'error' (piros) = minden más (0/3/4): ott a termék TÉNYLEG nem használható.
#
# A 2/6 (TÜRELMI IDŐ) 2026-09-14 ÓTA ZÖLD, "Aktív" felirattal - MAGYARÁZÓ ZÁRÓJEL NÉLKÜL
# (explicit user decision, képernyőképpel: *"ha be van aktiválva az office akkor ne
# pirossal írja már nekem ilyen faszságokat, írja ki azt h aktív és zöld legyen"*, majd a
# zárójeles "(türelmi időben)" változatra: *"ne is legyen piros meg attol megh türelmi idő
# van attól még aktív az aktiválás basszus - legyen zöld és aktív ha aktív"*). A terepi
# eset: egy Office 2019 Retail két terméke `LicenseStatus=2`, ok `0x4004F00C` ("türelmi
# időben, aktiválható") - a gépen az Office MŰKÖDIK és aktiváltnak mutatja magát, a nézet
# viszont PIROSSAL, "Nincs aktiválva" felirattal írta ki.
#
# EZ A 2026-09-07-i SZABÁLY SZŰKÍTÉSE. A régi indoklás ténybeli része igaz marad, ezért
# marad itt leírva: a 2/6 állapotban a termék szigorú értelemben NINCS aktiválva, csak
# türelmi időben működik. A régi megoldás (piros + "Nincs aktiválva") viszont ezt az
# árnyalatot úgy közölte, hogy közben egy MŰKÖDŐ terméket hibásnak mutatott - ugyanabba a
# hibába esett, amiért a sárga "újraaktiválás ajánlott" szint megszűnt, csak a másik
# irányból. A felhasználó döntése egyértelmű: a felületen a kérdés kétállapotú.
#
# AZ ÁRNYALAT NEM VÉSZ EL, CSAK NEM A JELVÉNYEN VAN - és ez az, ami miatt ez nem hazugság:
# az `activated` mező (és vele az aktiválás sikerének verdiktje) VÁLTOZATLANUL `code == 1`,
# a `LICENSE_STATUS_DETAIL` továbbra is "türelmi idő (2) – még nincs aktiválva"-ként írja
# le, és ez megy a naplóba (Rule 0) meg az aktiválás utáni hibaüzenetbe is. Vagyis a
# program soha nem fog SIKERT jelenteni egy türelmi idős aktiválásra - csak a felületen
# nem riogat egy működő termék miatt.
#
# A 'warning' szint megszűnt. A RÉSZLETES OK NEM VÉSZ EL: a `LicenseStatusReason` továbbra
# is kimegy (`reason_hex`/`reason_text`), a felület viszont CSAK a nem aktivált eseteknél
# írja ki - sikernél csend, hibánál magyarázat.
LICENSE_STATUS = {
    0: ('Nincs aktiválva', 'error'),
    1: ('Aktiválva', 'ok'),
    2: ('Aktív', 'ok'),
    3: ('Nincs aktiválva (lejárt türelmi idő)', 'error'),
    4: ('Nincs aktiválva (nem eredetinek jelölt)', 'error'),
    5: ('Aktiválva', 'ok'),
    6: ('Aktív', 'ok'),
}

# A MŰVELET-VERDIKTHEZ tartozó RÉSZLETES állapotszöveg - NEM a jelvényhez.
#
# MIÉRT KELL KÜLÖN: a jelvény kétállapotú lett (fent), az aktiválás sikerét viszont
# továbbra is a szigorú `code == 1` dönti el - az 5-ös állapot az `slmgr /ato` UTÁN
# valódi félsiker (a licenc működik, de az aktiválás nem véglegesült). E nélkül a
# kettő ellentmondana egymásnak a képernyőn: "❌ NEM sikerült - a Windows állapota:
# Aktiválva". A jelvény marad egyszerű, a művelet üzenete marad pontos.
LICENSE_STATUS_DETAIL = {
    0: 'nincs licenc (0)',
    1: 'aktiválva (1)',
    2: 'türelmi idő (2) – még nincs aktiválva',
    3: 'lejárt türelmi idő (3)',
    4: 'nem eredetinek jelölt (4)',
    5: 'értesítési mód (5) – a licenc működik, de az aktiválás nem véglegesült',
    6: 'meghosszabbított türelmi idő (6) – még nincs aktiválva',
}

# Azok az állapotok, amikben a termék MOST HASZNÁLHATÓ. A nézet ezt írja ki egy külön,
# megnyugtató sorban - egy "licencelt és működik" mondat mellett egy piros jelvény
# önmagában ellentmondás.
WORKING_STATUSES = (1, 2, 5, 6)

# A LICENCÁLLAPOT OKA (`LicenseStatusReason`), emberi nyelven.
#
# MIÉRT KELL (terepi visszajelzés, 2026-08-31): a nézet kiírta, hogy az Office
# "Értesítési állapot (aktiválás szükséges)", miközben a Wordben ott állt, hogy
# "Aktivált termék" - a technikus jogosan hitte a programot hibásnak. A gépen mérve a
# valódi állapot: `Office24ProPlus2024VL_MAK_AE1`, státusz 5, ok **0xC004F009**, azaz a
# MAK-kulcs TÜRELMI IDEJE JÁRT LE. Az 5-ös állapot nem azt jelenti, hogy nincs licenc:
# a termék licencelt, csak újraaktiválás kell - ezért fut és mutatja magát aktiváltnak
# az Office is, amíg be nem áll a csökkentett működés. A puszta állapotszöveg tehát
# igaz volt, de megmagyarázhatatlan; az OK az, ami a két képernyő ellentmondását
# feloldja, és megmondja a teendőt.
LICENSE_REASON = {
    0x00000000: 'rendben',
    0xC004F009: 'a türelmi idő lejárt - újraaktiválás kell (a termék licencelt, ezért még működik)',
    0xC004F00F: 'a kulcs a hardverhez van kötve, és a hardver megváltozott',
    0xC004F014: 'nincs telepítve termékkulcs ehhez a kiadáshoz',
    0xC004F034: 'a licenc kiadása sikertelen (jellemzően nem érhető el a KMS-kiszolgáló)',
    0xC004C060: 'a kulcsot a Microsoft aktiválási szolgáltatása blokkolta',
    0xC004C003: 'a kulcsot az aktiválási kiszolgáló blokkolta',
    0x4004F00C: 'türelmi időben, aktiválható',
    0x4004F040: 'aktiválva (KMS-kiszolgálóval)',
    0x4004F041: 'aktiválva (KMS-kiszolgálóval)',
}


def license_reason_text(raw):
    """A `LicenseStatusReason` szám -> (hexa kód, magyar magyarázat vagy '').

    A kód akkor is kimegy a felületre, ha nem ismerjük a szövegét: egy `0xC004xxxx`
    kereshető, egy hiányzó sor nem."""
    try:
        code = int(raw) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return '', ''
    return f'0x{code:08X}', LICENSE_REASON.get(code, '')

# A licenc CSATORNÁJA - ez dönti el, hogy újratelepítés után magától aktiválódik-e.
CHANNEL_HINTS = {
    'oem': 'OEM (gyári, a BIOS-ban van a kulcs)',
    'retail': 'Retail (dobozos/letöltött, átvihető másik gépre)',
    'volume': 'Volume (céges, KMS/MAK)',
}

# A Microsoft NYILVÁNOSAN közzétett KMS-kliens telepítőkulcsai (GVLK). Ezek nem titkosak
# és nem "feltört" kulcsok: a Microsoft pont azért teszi közzé őket, hogy a KMS-es gépekre
# fel lehessen tenni. Önmagukban NEM aktiválnak semmit - ahhoz kell egy elérhető KMS-host.
# Forrás: Microsoft "KMS client setup keys" dokumentáció.
GVLK_KEYS = (
    # (a kiadás felismerhető darabja a Description/Caption mezőben, GVLK, olvasható név)
    ('professional workstation', 'NRG8B-VKK3Q-CXVCJ-9G2XF-6Q84J', 'Windows 10/11 Pro for Workstations'),
    ('professionaln',            'MH37W-N47XK-V7XM9-C7227-GCQG9', 'Windows 10/11 Pro N'),
    ('professional education',   '6TP4R-GNPTD-KYYHQ-7B7DP-J447Y', 'Windows 10/11 Pro Education'),
    ('professional',             'W269N-WFGWX-YVC9B-4J6C9-T83GX', 'Windows 10/11 Pro'),
    ('enterprise ltsc 2021',     'M7XTQ-FN8P6-TTKYV-9D4CC-J462D', 'Windows 10 Enterprise LTSC 2021'),
    ('enterprisen',              'DPH2V-TTNVB-4X9Q3-TJR4H-KHJW4', 'Windows 10/11 Enterprise N'),
    ('enterprise',               'NPPR9-FWDCX-D2C8J-H872K-2YT43', 'Windows 10/11 Enterprise'),
    ('educationn',               '2WH4N-8QGBV-H22JP-CT43Q-MDWWJ', 'Windows 10/11 Education N'),
    ('education',                'NW6C2-QMPVW-D7KKK-3GKT6-VCFB2', 'Windows 10/11 Education'),
    ('coren',                    '3KHY7-WNT83-DGQKR-F7HPR-844BM', 'Windows 10/11 Home N'),
    ('core single language',     '7HNRX-D7KGG-3K4RQ-4WPJ4-YTDFH', 'Windows 10/11 Home Single Language'),
    ('core',                     'TX9XD-98N7V-6WMQ6-BX7FG-H8Q99', 'Windows 10/11 Home'),
)

# ===========================================================================
#  IDE ÍRD BE A KMS-KISZOLGÁLÓ CÍMÉT
# ---------------------------------------------------------------------------
#  Például:  KMS_HOST = 'kms.sajatceg.hu'
#            KMS_HOST = '192.168.1.50'
#            KMS_HOST = 'kms.sajatceg.hu:1688'      (ha nem az alap 1688-as port)
#
#  MIRE KELL: csak akkor jut szerephez, ha a gépnek NINCS gyári kulcsa a BIOS-ban.
#  Olyankor a kiadáshoz tartozó nyilvános KMS-kulcs (GVLK) megy fel, és az ehhez a
#  kiszolgálóhoz fordul aktiválásért.
#
#  HA VAN GYÁRI KULCS A BIOS-BAN, EZ AZ ÉRTÉK NEM SZÁMÍT: ott mindig a gyári kulcsot
#  használjuk, és az aktiválás a Microsoft felé megy - KMS-kiszolgáló nélkül.
#
#  Üresen hagyva a program nem állít be KMS-kiszolgálót; a felület ilyenkor kiírja,
#  hogy gyári kulcs nélküli gépet nem tud aktiválni.
# ===========================================================================
KMS_HOST = 'kms8.msguides.com'


# ===========================================================================
#  IDE ÍRD BE AZ OFFICE-AKTIVÁLÓ CSOMAG ADATAIT (2 SOR, ALUL)
# ---------------------------------------------------------------------------
#  MIT CSINÁL A GOMB (a nézet "Office aktiválása" gombja):
#     1. letölti az alábbi URL-en lévő ZIP-et,
#     2. kicsomagolja a C:\DriverVarazslo\office_aktivalo\csomag mappába,
#     3. elindítja benne az alább megnevezett .bat fájlt, SAJÁT, LÁTHATÓ konzolablakban.
#
#  1) OFFICE_ACTIVATOR_URL - a GitHub release KÖZVETLEN letöltési linkje.
#     Ezt a release oldalán a fájl nevére jobb klikk -> "Hivatkozás címének másolása"
#     adja meg, és MINDIG a /releases/download/ alakú (nem a /releases/tag/ oldal!):
#
#         OFFICE_ACTIVATOR_URL = 'https://github.com/<fiók>/<repo>/releases/download/<tag>/<fájl>.zip'
#
#     Példa (a block.bat-nál már működő alak):
#         'https://github.com/egonixaimgod/DriverVarazslo/releases/download/office/office.zip'
#
#  2) OFFICE_ACTIVATOR_BAT - a ZIP-en BELÜL lévő elindítandó fájl NEVE (csak a név,
#     útvonal nélkül - almappában is megtaláljuk):
#
#         OFFICE_ACTIVATOR_BAT = 'aktival.bat'
#
#  HA NEM TUDOD, MI A BAT NEVE: hagyd üresen, és nyomd meg a gombot. A program
#  letölti + kicsomagolja a ZIP-et, és KIÍRJA a benne talált .bat/.cmd fájlok nevét -
#  onnan már csak be kell másolnod ide. Futtatni ilyenkor semmit nem futtat.
#
#  HA AZ URL ÜRES: a gomb le van tiltva, és a felület megmondja, hogy ide kell írni.
# ===========================================================================
OFFICE_ACTIVATOR_URL = 'https://github.com/egonixaimgod/DriverVarazslo/releases/tag/mas.zip'
OFFICE_ACTIVATOR_BAT = 'mas.bat'


# Az `slmgr /ato` egy elérhetetlen KMS-hostra hosszan próbálkozik - időkorlát nélkül a
# folyamat percekig állna. A _run a TimeoutExpired-t elnyeli (CMD_TIMEOUT_RETURNCODE).
SLMGR_TIMEOUT = 180

# Egy KMS-aktiválás 180 napra szól, és a kliens magától megújítja. Ezt kiírjuk, mert a
# technikusnak tudnia kell, hogy ez NEM örökre szóló aktiválás.
KMS_RENEWAL_NOTE = ('A KMS-aktiválás 180 napra szól, és a gép magától megújítja, amíg eléri '
                    'a KMS-kiszolgálót. Ha az ügyfél kiviszi a hálózatból, lejár.')

# Az Office aktiválás-állapotát az ospp.vbs mondja meg; a helye Office-verziónként más.
OSPP_DIRS = (
    r'Microsoft Office\Office16', r'Microsoft Office\Office15', r'Microsoft Office\Office14',
    r'Microsoft Office\root\Office16', r'Microsoft Office\root\Office15',
)


WINDOWS_STATUS_PS = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$o = [ordered]@{}; "
    "try { $s = Get-CimInstance SoftwareLicensingService; "
    "  $o.OemKey = $s.OA3xOriginalProductKey; $o.KmsHost = $s.KeyManagementServiceMachine; "
    "  $o.KmsPort = $s.KeyManagementServicePort } catch {}; "
    # Csak a TELEPÍTETT Windows-termék érdekel: PartialProductKey nem null.
    f"try {{ $p = Get-CimInstance SoftwareLicensingProduct -Filter \"ApplicationId='{WINDOWS_APP_ID}' "
    "AND PartialProductKey IS NOT NULL\" | Select-Object -First 1; "
    "  if ($p) { $o.Name = $p.Name; $o.Description = $p.Description; $o.Status = $p.LicenseStatus; "
    "    $o.StatusReason = $p.LicenseStatusReason; $o.PartialKey = $p.PartialProductKey; "
    "    $o.GraceMinutes = $p.GracePeriodRemaining; $o.ProductKeyChannel = $p.ProductKeyChannel; "
    "    $o.KmsMachine = $p.KeyManagementServiceMachine } } catch {}; "
    "try { $os = Get-CimInstance Win32_OperatingSystem; "
    "  $o.OsCaption = $os.Caption; $o.OsVersion = $os.Version; $o.OsBuild = $os.BuildNumber } catch {}; "
    "$o | ConvertTo-Json -Compress"
)


def _ps_json(run, script, timeout=120, tag='WINACT'):
    """Egy PowerShell-lekérdezés JSON-válasza dictként. Hibánál üres dict + WARNING."""
    try:
        res = run(["powershell", "-NoProfile", "-Command", script],
                  encoding='utf-8', timeout=timeout)
        raw = (getattr(res, 'stdout', '') or '').strip()
        if not raw:
            logging.warning(f"[{tag}] A lekérdezés üres választ adott (rc="
                            f"{getattr(res, 'returncode', '?')}).")
            return {}
        return json.loads(raw) or {}
    except Exception as e:
        logging.warning(f"[{tag}] A lekérdezés sikertelen: {e}")
        return {}


def collect_windows_activation(run):
    """A Windows aktiválási állapota. MINDIG ad vissza dictet.

    Kulcsok: activated, status_code, status_text, status_color, edition, os_caption,
    os_build, partial_key, oem_key, channel, channel_text, kms_host, grace_days,
    gvlk (a kiadáshoz illő nyilvános KMS-kulcs), gvlk_name."""
    info = _ps_json(run, WINDOWS_STATUS_PS)
    try:
        code = int(info.get('Status'))
    except (TypeError, ValueError):
        code = -1
    text, color = LICENSE_STATUS.get(code, ('Ismeretlen állapot', 'unknown'))

    channel = str(info.get('ProductKeyChannel') or '').strip()
    channel_text = ''
    for key, label in CHANNEL_HINTS.items():
        if key in channel.lower():
            channel_text = label
            break

    grace = info.get('GraceMinutes')
    try:
        grace_days = int(grace) // 1440 if grace else 0
    except (TypeError, ValueError):
        grace_days = 0

    desc = str(info.get('Description') or '')
    name = str(info.get('Name') or '')
    gvlk, gvlk_name = gvlk_for_edition(f"{name} {desc} {info.get('OsCaption') or ''}")

    out = {
        'activated': code == 1,
        'status_code': code,
        'status_text': text,
        'status_color': color,
        # Csak a művelet-verdikthez (aktiválás után), sosem a jelvényhez.
        'status_detail': LICENSE_STATUS_DETAIL.get(code, f'ismeretlen ({code})'),
        'edition': name or desc,
        'os_caption': str(info.get('OsCaption') or ''),
        'os_build': str(info.get('OsBuild') or ''),
        'partial_key': str(info.get('PartialKey') or ''),
        # A gyári (OEM) kulcs a UEFI MSDM táblájából. Újratelepítés után ez az, ami
        # miatt a gép magától aktiválódik - ha nem tette, ezzel kézzel megoldható.
        'oem_key': str(info.get('OemKey') or '').strip(),
        'channel': channel,
        'channel_text': channel_text,
        'kms_host': str(info.get('KmsMachine') or info.get('KmsHost') or '').strip(),
        'grace_days': grace_days,
        'gvlk': gvlk,
        'gvlk_name': gvlk_name,
    }
    # A RÉSZLETES állapot a naplóba megy, mert a KÉPERNYŐRŐL 2026-09-07-én levettük: ott
    # már csak "Aktiválva / Nincs aktiválva" látszik (explicit user decision). Egy terepi
    # jelentésnél viszont pont a kód és az ok a kérdés, ezért itt a helye (Rule 0).
    logging.info(f"[WINACT] Windows: '{out['edition']}' | állapot={code} "
                 f"({out['status_detail']}) | csatorna='{channel}' | "
                 f"részkulcs={out['partial_key'] or '-'} | "
                 f"OEM-kulcs a BIOS-ban: {'IGEN' if out['oem_key'] else 'nincs'} | "
                 f"KMS-host='{out['kms_host'] or '-'}'")
    return out


def gvlk_for_edition(text):
    """A kiadáshoz illő NYILVÁNOS KMS-kliens kulcs (GVLK). (kulcs, olvasható név).

    A sorrend számít: a 'professional workstation' előbb van, mint a 'professional',
    különben a rövidebb minta elnyelné a hosszabbat."""
    low = (text or '').lower()
    for needle, key, name in GVLK_KEYS:
        if needle in low:
            return key, name
    # Ismeretlen kiadásnál a Pro a legvalószínűbb a szervizben - de megmondjuk, hogy tipp.
    return GVLK_KEYS[3][1], GVLK_KEYS[3][2] + ' (feltételezett kiadás)'


def normalize_key(key):
    """A beírt kulcs egységesítése: nagybetű, csak A-Z0-9, 5-ös csoportokban kötőjellel.
    A technikus vágólapról másol, ahol simán lehet szóköz vagy sortörés."""
    raw = re.sub(r'[^A-Za-z0-9]', '', str(key or '')).upper()
    if len(raw) != 25:
        return ''
    return '-'.join(raw[i:i + 5] for i in range(0, 25, 5))


def is_valid_kms_host(host):
    """Elfogadható-e KMS-host névnek. Szándékosan megengedő (belső gépnév, FQDN, IP),
    de a szemetet (szóköz, séma, útvonal) kiszűri - az `slmgr /skms` amúgy is némán
    elfogadná, és utána az aktiválás hasalna el érthetetlenül."""
    h = str(host or '').strip()
    if not h or len(h) > 255:
        return False
    # Opcionális :port
    if ':' in h:
        h, _, port = h.rpartition(':')
        if not port.isdigit() or not (0 < int(port) < 65536):
            return False
    return bool(re.match(r'^[A-Za-z0-9]([A-Za-z0-9\-\.]*[A-Za-z0-9])?$', h))


def _slmgr(run, *args, timeout=SLMGR_TIMEOUT):
    """Egy slmgr-hívás. `cscript //nologo`-val, hogy ne modális ablakot kapjunk."""
    slmgr_path = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'System32', 'slmgr.vbs')
    cmd = ['cscript', '//nologo', slmgr_path] + list(args)
    return run(cmd, timeout=timeout)


def install_product_key(run, key):
    """slmgr /ipk - a termékkulcs telepítése. Visszatérés: (siker, kimenet)."""
    key = normalize_key(key)
    if not key:
        return False, 'A kulcs formátuma hibás (25 karakter kell, 5x5 csoportban).'
    # A kulcsot NEM titkosítjuk el a naplóban: a technikusnak pont ez kell egy hibánál,
    # és nem jelszó - a gép matricáján/BIOS-ában amúgy is ott van.
    logging.info(f"[WINACT] Termékkulcs telepítése: {key}")
    res = _slmgr(run, '/ipk', key)
    return _slmgr_ok(res), _slmgr_text(res)


def set_kms_host(run, host):
    """slmgr /skms - a KMS-kiszolgáló beállítása. Üres hostnál nem csinál semmit."""
    host = str(host or '').strip()
    if not host:
        return True, '(nincs megadva KMS-kiszolgáló, ez a lépés kimarad)'
    if not is_valid_kms_host(host):
        return False, f'A KMS-kiszolgáló neve érvénytelen: {host}'
    logging.info(f"[WINACT] KMS-kiszolgáló beállítása: {host}")
    res = _slmgr(run, '/skms', host)
    return _slmgr_ok(res), _slmgr_text(res)


def clear_kms_host(run):
    """slmgr /ckms - a beállított KMS-kiszolgáló törlése (vissza az alapértelmezettre)."""
    logging.info("[WINACT] A beállított KMS-kiszolgáló törlése (/ckms).")
    res = _slmgr(run, '/ckms')
    return _slmgr_ok(res), _slmgr_text(res)


def activate(run):
    """slmgr /ato - aktiválás. FIGYELEM: a kimenete NEM a végső szó, a hívónak WMI-ből
    kell ellenőriznie (lásd a modul fejlécét)."""
    logging.info("[WINACT] Aktiválás indítása (/ato).")
    res = _slmgr(run, '/ato')
    return _slmgr_ok(res), _slmgr_text(res)


def _slmgr_ok(res):
    from app.common import CMD_TIMEOUT_RETURNCODE
    rc = getattr(res, 'returncode', 1)
    if rc == CMD_TIMEOUT_RETURNCODE:
        logging.warning("[WINACT] Az slmgr időtúllépéssel leállt (elérhetetlen KMS-kiszolgáló?).")
        return False
    return rc == 0


# AZ SLMGR HIBAKÓDJAI, emberi nyelven.
#
# MIÉRT KELL (terepi visszajelzés, 2026-09-07, Dell laptop): a gépre azt írta a Windows,
# hogy 22 nap múlva lejár; a nézet kiírta, hogy VAN gyári kulcs a BIOS-ban; az aktiválás
# viszont elbukott, és a technikus ennyit látott a képernyőn:
#
#     Error: 0xC004F050 On a computer running Microsoft Windows non-core edition,
#     run 'slui.exe 0x2a 0xC004F050' to display the error text.
#
# Ez a nyers, angol slmgr-kimenet: se azt nem mondja meg, MI a baj, se azt, hogy mi a
# teendő - a technikus szó szerinti visszajelzése az volt, hogy "valami sumák error
# szöveget írt ki". A kódot tehát le KELL fordítani.
#
# A TÁBLA A MICROSOFT DOKUMENTÁLT KÓDJAIBÓL ÁLL, NEM MÉRÉSBŐL. Ezt fontos külön
# kimondani (lásd a CLAUDE.md-t a mért kontra következtetett adatról): a fenti terepi
# esetből NEM maradt naplónk, tehát azt, hogy ott PONTOSAN melyik kód jött, nem tudjuk.
# A 0xC004F050 a legvalószínűbb (a gyári BIOS-kulcs jellemzően Home, a gépen viszont Pro
# van), de ez hipotézis. Ismeretlen kódnál ezért nem találunk ki jelentést: a kód megy
# ki nyersen, az kereshető - egy kitalált magyarázat rosszabb lenne a hallgatásnál.
SLMGR_ERRORS = {
    0xC004F050: 'a kulcs érvénytelen ehhez a Windows-kiadáshoz (jellemzően MÁS KIADÁS kulcsa: pl. Home kulcs Pro rendszeren)',
    0xC004E016: 'a kulcs nem ehhez a kiadáshoz való',
    0xC004F035: 'Volume (KMS/MAK) kulcsot nem fogad el a gép, mert hiányzik hozzá a gyári OEM BIOS-jelölő',
    0xC004C008: 'a kulcsot már túl sokszor aktiválták (elérte a Microsoft aktiválási korlátját)',
    0xC004C003: 'a kulcsot az aktiválási kiszolgáló blokkolta',
    0xC004C060: 'a kulcsot a Microsoft aktiválási szolgáltatása blokkolta',
    0xC004B100: 'az aktiválási kiszolgáló nem aktiválta ezt a gépet',
    0xC004F074: 'nem sikerült elérni a KMS-kiszolgálót',
    0xC004F038: 'a KMS-kiszolgáló nem ad ki licencet (túl kevés gép jelentkezett be rá)',
    0xC004F00F: 'a kulcs a hardverhez van kötve, és a hardver megváltozott',
    0xC004F009: 'a türelmi idő lejárt - újraaktiválás kell',
    0xC004F014: 'nincs telepítve termékkulcs ehhez a kiadáshoz',
    0x8007232B: 'a KMS-kiszolgáló neve nem oldható fel (DNS-hiba)',
    0x8007007B: 'a megadott KMS-kiszolgáló neve hibás formátumú',
    0x80072EE7: 'nem sikerült elérni az aktiválási kiszolgálót (hálózati hiba)',
    0x80072EFD: 'nem sikerült elérni az aktiválási kiszolgálót (hálózati hiba)',
}


def slmgr_error_text(text):
    """Az slmgr kimenetéből a hibakód -> ('0xC004F050', magyar magyarázat vagy '').

    Kód nélküli kimenetnél ('', ''). Tiszta függvény, offline tesztelhető.

    KÉT RÉSZLET, AMI NÉLKÜL NÉMÁN ROSSZUL MŰKÖDNE:
      - PONTOSAN 8 hexa jegyre illesztünk. A slmgr ugyanazt a kódot kétszer írja ki, és
        közte ott a `slui.exe 0x2a` - egy laza `0x[0-9a-f]+` minta a `0x2a`-t is
        kódnak venné, ha az állna elöl.
      - A keresés SZÁM szerint megy, nem szöveg szerint. A projekt egyszer már beleesett
        abba, hogy egy `.upper()` a `0x` előtagot is `0X`-re alakította, és a tábla
        SOSEM talált (2026-09-07, `wu_search_error_text`)."""
    m = re.search(r'0x[0-9A-Fa-f]{8}\b', str(text or ''))
    if not m:
        return '', ''
    code = int(m.group(0), 16) & 0xFFFFFFFF
    return f'0x{code:08X}', SLMGR_ERRORS.get(code, '')


def _slmgr_text(res):
    """Az slmgr kimenete a technikusnak. Lokalizált, ezért CSAK megjelenítjük - soha nem
    hozunk belőle döntést (lásd a modul fejlécét)."""
    out = (getattr(res, 'stdout', '') or '').strip()
    err = (getattr(res, 'stderr', '') or '').strip()
    txt = '\n'.join(t for t in (out, err) if t)
    return txt or '(nincs kimenet)'


# ---------------------------------------------------------------------------
# OFFICE
# ---------------------------------------------------------------------------

def find_ospp(run=None):
    """Az ospp.vbs megkeresése (Office aktiválás-kezelő). None, ha nincs Office."""
    roots = [os.environ.get('ProgramFiles', r'C:\Program Files'),
             os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')]
    for root in roots:
        for sub in OSPP_DIRS:
            p = os.path.join(root, sub, 'ospp.vbs')
            if os.path.isfile(p):
                logging.info(f"[WINACT] Office aktiválás-kezelő: {p}")
                return p
    logging.info("[WINACT] Nem található ospp.vbs - vagy nincs Office, vagy Microsoft Store-os/M365 verzió.")
    return None


OFFICE_WMI_PS = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    f"Get-CimInstance SoftwareLicensingProduct -Filter \"ApplicationId<>'{WINDOWS_APP_ID}' "
    "AND PartialProductKey IS NOT NULL\" | "
    "Select-Object Name, Description, LicenseStatus, LicenseStatusReason, "
    "PartialProductKey | ConvertTo-Json -Compress"
)


def collect_office_activation(run):
    """A telepített (nem-Windows) licencelt termékek: jellemzően az Office.

    A WMI-t használjuk, nem az ospp.vbs kimenetét - ugyanaz az érv, mint a Windowsnál:
    az ospp szövege lokalizált. Az ospp.vbs helyét viszont visszaadjuk, mert az az
    eszköz, amivel a technikus kézzel tud beavatkozni."""
    out = {'products': [], 'ospp': find_ospp(run)}
    data = None
    try:
        res = run(["powershell", "-NoProfile", "-Command", OFFICE_WMI_PS],
                  encoding='utf-8', timeout=120)
        raw = (getattr(res, 'stdout', '') or '').strip()
        if raw:
            data = json.loads(raw)
    except Exception as e:
        logging.warning(f"[WINACT] Az Office-állapot lekérdezése sikertelen: {e}")
        return out

    if isinstance(data, dict):
        data = [data]
    for row in (data or []):
        try:
            code = int(row.get('LicenseStatus'))
        except (TypeError, ValueError):
            code = -1
        text, color = LICENSE_STATUS.get(code, ('Ismeretlen állapot', 'unknown'))
        reason_hex, reason_text = license_reason_text(row.get('LicenseStatusReason'))
        out['products'].append({
            'name': str(row.get('Name') or ''),
            'description': str(row.get('Description') or ''),
            'partial_key': str(row.get('PartialProductKey') or ''),
            'status_code': code, 'status_text': text, 'status_color': color,
            'status_detail': LICENSE_STATUS_DETAIL.get(code, f'ismeretlen ({code})'),
            'activated': code == 1,
            # AZ OK - a felulet CSAK a nem aktivalt termekeknel irja ki (2026-09-07),
            # ott viszont ez mondja meg a teendot (pl. 0xC004F00F = a kulcs a hardverhez
            # van kotve, es a hardver megvaltozott).
            'reason_hex': reason_hex, 'reason_text': reason_text,
            # MUKODIK-E MOST? A jelveny 2026-09-07 ota NEM ebbol dont (az ketallapotu
            # lett), de a kettő nem ugyanaz: a 2/6 (turelmi ido) "nincs aktivalva, de
            # meg mukodik" - ez a mezo orzi a kulonbseget a naplo es a CLI szamara.
            'working': code in WORKING_STATUSES,
        })
    # A RÉSZLETES állapot a naplóba: a képernyőn már csak "Aktiválva / Nincs aktiválva"
    # látszik, egy terepi jelentésnél viszont a kód és az ok a kérdés (Rule 0).
    logging.info(f"[WINACT] Office/egyéb licencelt termék: {len(out['products'])} db "
                 + ('; '.join(f"{p['name'][:40]} -> {p['status_detail']}"
                              + (f", ok={p['reason_hex']} {p['reason_text']}".rstrip()
                                 if p['reason_hex'] else '')
                              for p in out['products']) or 'nincs'))
    return out


# ---------------------------------------------------------------------------
# OFFICE-AKTIVÁLÓ CSOMAG: letöltés a GitHub release-ből -> kicsomagolás -> .bat indítása
# ---------------------------------------------------------------------------
#
# A LETÖLTÉS UGYANAZ A BEVÁLT ÚT, AMIT A stresstools.zip HASZNÁL (explicit user decision,
# 2026-09-14: *"a stresstools.zip nél hasonlo letöltés van, az jol mukodik olyan legyen
# mint az ez is"*): `common.download_with_cert_fallback` -> `zipfile` kicsomagolás ->
# fázisonkénti progress-callback. Konkrétan ezt jelenti, és ezért nem szabad "egyszerűbb"
# urllib-hívásra cserélni: a friss-Windows tanúsítvány-fallback (Python OpenSSL ->
# PowerShell schannel -> certutil) ott van beépítve, a régi gépeken pedig pontosan ez a
# lánc az egyetlen, ami egyáltalán le tud tölteni (lásd CLAUDE.md, Régi Windows-támogatás).
#
# AMIBEN KÜLÖNBÖZIK A stresstools-tól, és mind a kettő szándékos:
#   - NINCS CACHE. A stresstools egy stabil, saját kiadású programcsomag; ez viszont egy
#     aktiváló script, amit a felhasználó bármikor frissíthet a release-ben. Minden
#     kattintás friss letöltés - így mindig az megy, ami MOST a release-ben van.
#   - A ZIP-et KICSOMAGOLÁS UTÁN IS MEGTARTJUK? Nem: törlünk, mint a stresstools.
#   - Az adatmappába megy (`C:\DriverVarazslo\office_aktivalo`), nem a %TEMP%-be: a
#     program saját temp-takarítója a %TEMP%-et üríti, és egy félbehagyott aktiválás
#     közben nem szeretnénk kihúzni a script alól a mappát.
OFFICE_ACTIVATOR_DIRNAME = 'office_aktivalo'   # az adatmappán belüli gyökér
OFFICE_ACTIVATOR_SUBDIR = 'csomag'             # ide csomagolunk ki
OFFICE_ACTIVATOR_ZIPNAME = 'csomag.zip'        # a letöltött ZIP ideiglenes neve
BATCH_EXTS = ('.bat', '.cmd')                  # amit "indítható script"-nek tekintünk
OFFICE_DL_TIMEOUT = 60                         # kapcsolat-időkorlát (Python ág)
OFFICE_DL_PS_TIMEOUT = 900                     # a PowerShell/certutil tartalék ág kerete

# A VÍRUSIRTÓ A LEGVALÓSZÍNŰBB BUKÁSI OK, ÉS EZT KI KELL MONDANI.
# Az Office/Windows aktiváló scriptek gyakorlatilag kivétel nélkül fent vannak a Defender
# listáján (HackTool:Win32/AutoKMS és társai), ezért a fájl eltűnhet MÁR A LETÖLTÉSKOR
# vagy a kicsomagoláskor - a Windows ilyenkor `ERROR_VIRUS_INFECTED` (225) hibát ad.
# E nélkül a magyarázat nélkül a technikus egy értelmezhetetlen "hozzáférés megtagadva"
# hibát látna, és a programot hibáztatná.
ANTIVIRUS_HINT = (
    'Ezt szinte biztosan a VÍRUSIRTÓ (Windows Defender) tette: az aktiváló scripteket '
    'kivétel nélkül kártékonynak jelöli, és már letöltés/kicsomagolás közben kiveszi a '
    'fájlt. Teendő: a Defender beállításaiban vegyél fel kivételt a '
    'C:\\DriverVarazslo\\office_aktivalo mappára (Vírus- és veszélyforrás-kezelés > '
    'Beállítások kezelése > Kizárások), majd próbáld újra.')


def office_activator_dir():
    """Az Office-aktiváló csomag gyökérmappája az adatmappán belül."""
    return os.path.join(_app_data_dir(), OFFICE_ACTIVATOR_DIRNAME)


def office_activator_plan():
    """MIT FOG CSINÁLNI az Office-aktiválás gomb - a felület ezt írja ki, és ebből dönti
    el, hogy a gomb engedélyezett-e.

    Három állapot van, és mind a háromnak SAJÁT üzenete van, mert a teendő is más:
      ready=True,  mode='run'  -> URL és .bat név is megvan: letölt, kicsomagol, indít.
      ready=True,  mode='list' -> URL megvan, .bat név nincs: letölt, kicsomagol, és
                                  KIÍRJA a megtalált .bat/.cmd fájlok nevét. Ez nem
                                  hibaállapot, hanem a beállítás elvégzésének a módja.
      ready=False              -> nincs URL: nincs mit letölteni."""
    url = (OFFICE_ACTIVATOR_URL or '').strip()
    bat = (OFFICE_ACTIVATOR_BAT or '').strip()
    if not url:
        return {'ready': False, 'mode': 'unset', 'url': '', 'bat': bat, 'text': (
            'Az Office-aktiváló csomag letöltési linkje nincs beállítva. A GitHub release '
            'közvetlen ZIP-linkjét az app/winact_core.py fájl OFFICE_ACTIVATOR_URL sorába '
            'kell beírni (az elindítandó .bat nevét pedig az alatta lévő '
            'OFFICE_ACTIVATOR_BAT sorba).')}
    if not bat:
        return {'ready': True, 'mode': 'list', 'url': url, 'bat': '', 'text': (
            'A csomag letöltési linkje megvan, de az elindítandó .bat neve nincs beállítva. '
            'A gomb most letölti és kicsomagolja a csomagot, és kiírja, milyen .bat/.cmd '
            'fájlok vannak benne - a megfelelőt az app/winact_core.py '
            'OFFICE_ACTIVATOR_BAT sorába kell beírni. Futtatni addig semmit nem futtat.')}
    return {'ready': True, 'mode': 'run', 'url': url, 'bat': bat, 'text': (
        f'Letölti a csomagot, kicsomagolja, és elindítja benne a(z) {bat} fájlt egy '
        'külön, látható parancssori ablakban.')}


def virus_blocked(exc):
    """Igaz, ha a hibát a vírusirtó okozta (a fájlt kivették a kezünk alól).

    Elsősorban a Windows saját hibakódjából (`ERROR_VIRUS_INFECTED` = 225) dolgozunk, mert
    az nyelvfüggetlen; a szöveges minták csak tartalékok, és MINDKÉT nyelven kellenek - a
    magyar Windows üzenete ékezetes ('vírus'), ami az angol 'virus' mintára SOSEM
    illeszkedne (ugyanaz a csapda, ami a projektben a magyar pnputil-kimenetnél már
    egyszer hamis eredményt okozott)."""
    if getattr(exc, 'winerror', None) == 225:
        return True
    low = str(exc).lower()
    return any(m in low for m in ('virus', 'vírus', 'potentially unwanted',
                                  'nemkívánatos', 'kártev', 'malware'))


def find_activator_bat(root, name):
    """A megnevezett .bat/.cmd megkeresése a kicsomagolt fában (almappákban is).

    Csak a FÁJLNEVET nézzük, kis/nagybetűtől függetlenül: a ZIP jellemzően egy
    gyökérmappát tartalmaz, aminek a neve kiadásonként változhat, tehát egy útvonalra
    illesztés a következő release-nél némán elhasalna. Kiterjesztés nélkül megadott névre
    a .bat és a .cmd is jó."""
    want = os.path.basename(str(name or '').strip()).lower()
    if not want:
        return None
    wants = [want] if os.path.splitext(want)[1] else [want + e for e in BATCH_EXTS]
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower() in wants:
                return os.path.join(dirpath, fn)
    return None


def list_batch_files(root):
    """A kicsomagolt fában talált összes .bat/.cmd, a gyökérhez képest relatív úttal.

    Két helyen kell: a beállítatlan-név ágon ez MAGA a válasz a technikusnak (ezek közül
    kell választania), a "nem találom a megadott fájlt" ágon pedig ez mondja meg, mi van
    helyette - egy puszta "nem található" üzenetből nem derülne ki, hogy elgépelt nevet
    írt-e be, vagy a ZIP tartalma más, mint amire számított."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith(BATCH_EXTS):
                out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(out)


def download_office_activator(run_fn, progress=None, log=None):
    """Letölti és kicsomagolja az Office-aktiváló csomagot. Hibánál KIVÉTELT dob.

    progress: opcionális callback(fázis, kész, összes) - a fázis 'download' (bájtok, az
    összes lehet None, ha a szerver nem küld Content-Length-et) vagy 'extract' (fájldarab),
    pontosan úgy, mint a stresstools letöltésénél. log: opcionális callback(szöveg) a
    felületre szánt sorokhoz.

    Visszatérés: (kicsomagolt_mappa, talált_batch_fájlok_listája)."""
    def say(msg):
        logging.info(f"[OFFICEACT] {msg}")
        if log:
            try:
                log(msg)
            except Exception as e:
                logging.debug(f"[OFFICEACT] A log-callback hibára futott: {e}")

    plan = office_activator_plan()
    if not plan['ready']:
        raise RuntimeError(plan['text'])

    root = office_activator_dir()
    ext_dir = os.path.join(root, OFFICE_ACTIVATOR_SUBDIR)
    zip_path = os.path.join(root, OFFICE_ACTIVATOR_ZIPNAME)
    os.makedirs(root, exist_ok=True)

    # A RÉGI KICSOMAGOLÁS TÖRLÉSE A LETÖLTÉS ELŐTT. Enélkül egy korábbi csomag fájljai
    # összekeverednének az újakkal, és a .bat-keresés akár egy RÉGI, már nem létező
    # scriptet találna meg - vagyis a technikus azt hinné, az új csomag futott le.
    if os.path.exists(ext_dir):
        say('🧹 Korábbi csomag törlése...')
        shutil.rmtree(ext_dir, ignore_errors=True)

    say(f'⬇ Letöltés: {plan["url"]}')
    try:
        dl_cb = (lambda done, total: progress('download', done, total)) if progress else None
        download_with_cert_fallback(run_fn, plan['url'], zip_path,
                                    timeout=OFFICE_DL_TIMEOUT, ps_timeout=OFFICE_DL_PS_TIMEOUT,
                                    log_tag='OFFICEACT', progress_cb=dl_cb)
    except Exception as e:
        _cleanup_partial(zip_path)
        if virus_blocked(e):
            raise RuntimeError(f'A letöltés nem sikerült: {e}\n{ANTIVIRUS_HINT}') from e
        raise

    size = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
    say(f'✅ Letöltve: {size / 1048576:.1f} MB')

    # NEM ZIP? Két eset van, és külön kell kezelni őket, mert az egyik a felhasználó
    # legvalószínűbb elgépelése: ha az URL közvetlenül egy .bat/.cmd fájlra mutat, azt
    # simán elfogadjuk (nincs mit kicsomagolni). Minden más esetben viszont a letöltött
    # fájl jellemzően egy HTML hibaoldal - a GitHub a rossz linkre is 200-at ad -, és
    # erre a "nem sikerült kicsomagolni" üzenet félrevezető lenne.
    if not zipfile.is_zipfile(zip_path):
        url_low = plan['url'].lower()
        if url_low.endswith(BATCH_EXTS):
            os.makedirs(ext_dir, exist_ok=True)
            direct = os.path.join(ext_dir, os.path.basename(plan['url'].split('?')[0]))
            shutil.move(zip_path, direct)
            say(f'ℹ️ A link közvetlenül egy script-fájlra mutat, nem ZIP-re: {os.path.basename(direct)}')
            return ext_dir, list_batch_files(ext_dir)
        _cleanup_partial(zip_path)
        raise RuntimeError(
            f'A letöltött fájl nem ZIP ({size / 1024:.0f} KB). Ellenőrizd a linket az '
            'app/winact_core.py OFFICE_ACTIVATOR_URL sorában: a GitHub release oldalán a '
            'fájl nevére jobb klikk -> "Hivatkozás címének másolása" adja a jó, '
            '/releases/download/ alakú linket. A böngészőben megnyitható /releases/tag/ '
            'oldal címe NEM jó - arra a szerver egy HTML oldalt ad vissza.')

    say('📦 Kicsomagolás...')
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            members = zf.infolist()
            for i, member in enumerate(members):
                # A ZipFile.extract magától kiszűri az abszolút utat és a '..' elemeket
                # (zip-slip), ezért nem kell külön ellenőrzés.
                zf.extract(member, ext_dir)
                if progress:
                    try:
                        progress('extract', i + 1, len(members))
                    except Exception as cb_err:
                        logging.debug(f"[OFFICEACT] extract progress hiba: {cb_err}")
    except Exception as e:
        if virus_blocked(e):
            raise RuntimeError(f'A kicsomagolás nem sikerült: {e}\n{ANTIVIRUS_HINT}') from e
        raise
    finally:
        _cleanup_partial(zip_path)

    batches = list_batch_files(ext_dir)
    say(f'✅ Kicsomagolva: {ext_dir} ({len(members)} fájl, ebből {len(batches)} db .bat/.cmd)')
    if not batches:
        # NEM dobunk kivételt: lehet, hogy a csomagban .exe/.ps1 van, és a technikusnak
        # kézzel kell megnyitnia. A mappát viszont meg kell neveznünk, hogy odataláljon.
        logging.warning("[OFFICEACT] A kicsomagolt csomagban EGYETLEN .bat/.cmd fájl sincs.")
    return ext_dir, batches


def _cleanup_partial(path):
    """Fél-kész/már feldolgozott letöltés törlése. Nem dob: ez takarítás, nem művelet.
    (A fél-kész ZIP törlése nem kozmetika: tele lemeznél pont a maradvány foglalná a
    helyet a következő próbálkozás elől - terepen látott [Errno 28] a stresstools-nál.)"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logging.debug(f"[OFFICEACT] Az ideiglenes fájl törlése sikertelen ({path}): {e}")


def launch_activator_bat(path):
    """Elindítja a .bat-ot SAJÁT, LÁTHATÓ konzolablakban, a saját mappájából. Hibánál dob.

    HÁROM RÉSZLET, AMI NÉLKÜL NÉMÁN ROSSZUL MŰKÖDNE:

    1. `cmd /c start` KÖZBEIKTATÁSA KÖTELEZŐ, nem körülményeskedés. A program kilépése
       `cleanup_zombies()`-on át megy, ami `taskkill /F /T /PID <self>` - a TELJES
       folyamatfát kilövi. Egy közvetlenül indított gyerek ennek a fának a része lenne,
       tehát ha a technikus bezárja a DriverVarázslót, az AKTIVÁLÓ SCRIPT IS MEGHALNA -
       akár az aktiválás közepén. A `start` maga indítja el a folyamatot, majd a `cmd`
       azonnal kilép, így a script már nem tartozik a fánkba. (Ugyanaz az indoklás, mint
       a CLI módra váltásnál - lásd app/gui/climode.py.)
    2. A PARANCS SZTRINGKÉNT megy, nem listaként: listával a Python `list2cmdline`-ja
       visszaperjelezi a belső idézőjeleket, és a `cmd` a `\\"`-t útvonal-kezdetnek veszi
       (terepen ez a CLI-váltásnál "A hálózati elérési út nem található" hibát adott).
    3. A MUNKAKÖNYVTÁR A SCRIPT SAJÁT MAPPÁJA (`start /D`), és így a bat neve útvonal
       nélkül megy át: az aktiváló scriptek szinte mindig hivatkoznak a mellettük lévő
       fájlokra, más könyvtárból indítva pedig csendben mást csinálnának. Mellékhaszon,
       hogy így a szóközös útvonal sem tud elromlani a `cmd /k` idézőjel-szabályain.

    A `cmd /k` (és nem `/c`): a script végén az ablak NYITVA MARAD, hogy a technikus
    elolvashassa az eredményt - egy felvillanó és eltűnő ablak pont a lényeget vinné el.
    Bezárni az X-szel vagy az `exit` paranccsal lehet."""
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    name = os.path.basename(path)
    cmd = f'cmd /c start "" /D "{folder}" cmd /k "{name}"'
    logging.warning(f"[OFFICEACT] AKTIVÁLÓ SCRIPT INDÍTÁSA (külön ablak, rendszergazdaként): "
                    f"{path} | parancs: {cmd}")
    helper = subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS,
                              cwd=folder, close_fds=True)
    # Megvárjuk a segéd `cmd` kilépését - a NAPLÓ miatt. A `start` után az azonnal végez
    # (ezredmásodperc), cserébe naplózható a visszatérési kódja: a CLI-váltásnál kétszer
    # is pont egy nem nulla kód árulta volna el azonnal, miért "nem indul el semmi".
    try:
        helper.wait(timeout=10)
        logging.info(f"[OFFICEACT] A segéd cmd kilépett (kód={helper.returncode}) - az "
                     f"aktiváló ablak innentől önálló folyamat.")
    except Exception as e:
        logging.warning(f"[OFFICEACT] A segéd cmd nem lépett ki 10 mp alatt ({e}).")
