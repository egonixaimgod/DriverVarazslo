"""DriverVarázsló GUI - Driver Keresés és Telepítés nézet: hardver-szken, WU/Catalog keresés, kiválasztott driverek telepítése."""

# === AUTO-IMPORTS ===
import os
import sys
import platform
import subprocess
import re
import threading
import time
import logging
import shutil
import json
import glob
import traceback
import queue
from app.common import _ps_quote, _app_data_dir
from app import dupdrivers_core
from app.wu_core import WU_PNP_QUERY_PS
from app.wu_core import WuProcessAborted
from app.wu_core import wu_search_error_text
from app.wu_core import _build_wu_install_ps
from app.wu_core import _filter_wu_scan_devices
from app.wu_core import _is_inbox_driver
from app.wu_core import _iso_date_or_none
from app.wu_core import _iter_process_lines
from app.wu_core import _match_wu_updates_to_devices
from app.wu_core import _parse_driver_version
from app.wu_core import is_newer_release
from app.wu_core import release_rank
from app.wu_core import base_vendor_hwid
from app.wu_core import mark_generic_replace_candidates
from app.wu_core import deep_catalog_candidates as wu_core_deep_candidates
from app.wu_core import inf_package_applies
from app.wu_core import select_applicable_infs
from app.wu_core import unoffered_requested_titles
from app.wu_core import is_specific_hwid
from app.wu_core import driver_model_rank
from app.wu_core import catalog_title_family
from app.wu_core import package_bound_to_device_family
from app.wu_core import is_composite_parent
from app.wu_core import device_risk_marker
from app.wu_core import mark_device_risk
from app.wu_core import is_firmware_update
from app.wu_core import filter_autofix_risky_devices
from app.wu_core import FIRMWARE_CLASS_LABEL
from app.wu_core import FIRMWARE_CLASS_WARNING
from app.wu_core import no_source_is_actionable
from app.drivers_core import DELETE_DRIVER_TIMEOUT
from app.drivers_core import INSTALL_DRIVER_TIMEOUT
from app.common import CMD_TIMEOUT_RETURNCODE
from app.common import CommandResult
# === /AUTO-IMPORTS ===


# Eszközkezelő-hibakódok emberi olvasatban (a "Problémás eszközök" szekcióhoz).
# Csak a gyakoriak - ismeretlen kódra általános szöveg megy.
PNP_ERROR_CODE_DESCRIPTIONS = {
    1: 'Nincs megfelelően konfigurálva',
    3: 'A driver sérült vagy kevés az erőforrás',
    10: 'Az eszköz nem tud elindulni',
    12: 'Nincs elég szabad erőforrás',
    14: 'Újraindítás szükséges a működéshez',
    18: 'A drivert újra kell telepíteni',
    19: 'A registry-bejegyzése sérült',
    21: 'A Windows épp eltávolítja az eszközt',
    22: 'Az eszköz le van tiltva',
    24: 'Az eszköz nincs jelen vagy hibás',
    28: 'NINCS TELEPÍTVE DRIVER',
    31: 'Nem működik megfelelően (driver-hiba)',
    32: 'A szolgáltatása le van tiltva',
    37: 'A driver inicializálása sikertelen',
    39: 'A driver sérült vagy hiányzik',
    43: 'Az eszköz hibát jelzett és leállt',
    52: 'A driver aláírása nem ellenőrizhető',
}

# MIT KELL CSINÁLNI, HOGY A HIBAKÓD ELTŰNJÖN (2026-09-03, explicit user decision:
# *"ez mit jelent pl h code 24? irja mar le a program h mit kell vele csinalni h
# eltunjon"*).
#
# A puszta leírás ("Az eszköz nincs jelen vagy hibás") pontos, de a technikusnak
# nem mondja meg, mi a következő mozdulat - a felületen ezért a leírás MELLETT a
# teendő is megjelenik. A szövegek szándékosan a PROGRAM SAJÁT menüire hivatkoznak
# ("Szellemeszközök" nézet, "Eszközök újrakötése" gomb), mert azok egy kattintásra
# vannak; ahol tényleg fizikai beavatkozás kell (kihúzás-visszadugás, kábel), ott
# azt mondjuk ki. Ahol nincs értelmes teendő a program keretein belül (28/39:
# egyszerűen nincs driver), ott a keresés/gyártói oldal a válasz - ez a
# leggyakoribb eset, és a felület enélkül csak annyit mondana, hogy "hibás".
#
# A kulcshalmaznak NEM kell fednie a PNP_ERROR_CODE_DESCRIPTIONS-t: hiányzó kódnál
# a felület egyszerűen nem ír teendőt, ami jobb, mint egy általános semmitmondás.
PNP_ERROR_CODE_REMEDIES = {
    1:  'Futtasd a szkennelést és telepítsd a talált drivert. Ha nincs találat, a gyártó oldaláról kell driver.',
    3:  'Telepítsd újra a drivert (szkennelés → telepítés). Ha marad, kevés a memória vagy sérült a driver-fájl.',
    10: 'Próbáld az "Eszközök újrakötése" gombot, majd telepíts újabb drivert. Ha marad, hardverhiba is lehet.',
    12: 'Két eszköz ugyanazt az erőforrást kéri. BIOS-ban tiltsd le a nem használt eszközöket, vagy tedd másik PCIe-portba a kártyát.',
    14: 'Indítsd újra a gépet — ez a hibakód ettől magától eltűnik.',
    # A 18-as kód a DCH-driverek KÍSÉRŐ komponensein (Intel Graphics Command Center /
    # Control Panel, SoftwareComponent osztály) a leggyakoribb - ott a driver-fájl maga
    # hiányzik a DriverStore-ból. Build 294-ig ezt a program SAJÁT MAGA okozta: a
    # telepítés utáni INF-takarítás kivezette a kísérő INF-eket, mielőtt a Windows
    # létrehozta volna a hozzájuk tartozó eszközöket (lásd _cleanup_unused_staged_infs).
    18: 'Telepítsd újra a drivert: szkennelés → jelöld be az eszközt → Telepítés. '
        '(Videokártya kísérő-komponenseinél — Intel Graphics Command Center, Control Panel — '
        'ez a driver újratelepítésével rendbe jön; a hozzájuk tartozó alkalmazást a Microsoft '
        'Store hozza le magától.)',
    19: 'Sérült registry-bejegyzés. Az "Eszközök újrakötése" gomb újraépíti; ha nem elég, az 1 kattintásos fix megoldja.',
    21: 'A Windows épp eltávolítja — várj pár másodpercet, majd szkennelj újra. Ha marad, indítsd újra a gépet.',
    22: 'Az eszköz le van tiltva. Kattints a sor melletti "Engedélyezés" gombra.',
    24: 'A készülék nincs a gépben (kihúzott/leszerelt eszköz maradványa). Ha kell: dugd vissza. Ha nem kell: Szellemeszközök menü → törlés, ettől eltűnik a listáról.',
    28: 'Nincs rá driver. Futtasd a szkennelést; ha az sem talál, a gép/alaplap gyártójának oldaláról kell letölteni.',
    31: 'Telepíts újabb drivert, vagy nyomd meg az "Eszközök újrakötése" gombot, hogy a Windows újraválassza.',
    32: 'A driver szolgáltatása le van tiltva. Telepítsd újra a drivert — ez visszaállítja az indítási módot.',
    37: 'A driver nem tudott elindulni. Telepíts újabb verziót; ha nincs, az "Eszközök újrakötése" gomb visszateheti a gyárira.',
    39: 'Hiányzó vagy sérült driver. Szkennelj és telepítsd a találatot; ha nincs, a gyártó oldaláról kell driver.',
    43: 'Az eszköz maga jelzett hibát. Indítsd újra a gépet; ha marad, telepíts újabb drivert. Gyakran valódi hardverhiba.',
    52: 'Aláíratlan driver. Vagy a gyártó hivatalos (aláírt) csomagját telepítsd, vagy kapcsold ki az aláírás-kényszerítést.',
}


# A WUA-keresés (`_search_wu_api`) időkorlátja másodpercben.
#
# 300 -> 180 (2026-09-01, explicit user decision). MÉRVE ugyanabban a láncban (Dell
# Latitude 5580, Build 288): a SIKERES keresések **48, 55 és 92 mp** alatt lefutottak, a
# bukott viszont pontosan a teljes 300-ig ment, majd IDŐTÚLLÉPÉS. A 180 tehát még mindig
# a mért maximum kétszerese - de a bukott esetben 2 percet megspórol.
#
# A HATÁR CSÖKKENTÉSE ÖNMAGÁBAN NEM ELÉG, ÉS NEM IS EZ A LÉNYEGI JAVÍTÁS: ugyanennek a
# futásnak a diagnózisa szerint a keresés azért futott bele az időkorlátba, mert a
# NÉVFELOLDÁS még nem állt fel a driver-törlés utáni booton (a régi `_check_internet` egy
# nyers `8.8.8.8` IP-próbával elégedett meg, ami DNS nélkül is sikerül). Az OKOT a
# `GuiBaseMixin._check_internet`/`_wait_for_internet` DNS-ellenőrzése szünteti meg; ez a
# konstans már csak a büntetést rövidíti, ha a WUA mégis beragad.
#
# NE MENJ ENNÉL LEJJEBB mérés nélkül: a projekt elve itt "inkább hosszabb, mint rövidebb"
# - egy elvágott, egyébként sikeres keresés azt jelenti, hogy a gép WU-s driverei
# kimaradnak, és azt a naplóból utólag alig lehet megkülönböztetni a valódi WU-hibától.
WU_SEARCH_TIMEOUT = 180

# --- Microsoft Update Catalog: lapozás, rendezés, holtverseny-kezelés ---
#
# A katalógus laponként 25 sort ad (mérve: "1 - 25 of 546 (page 1 of 22)"), a lapozás
# és a rendezés pedig sima query-paraméter - a nevek a katalógus saját
# SiteConstants.aspx-éből: QueryStringPageIndex='p', QueryStringSortColumn='scol',
# QueryStringSortDirection='sdir'. Rendezhető oszlopok (a fejléc data-columnName-jei):
# Title, Products, ClassificationComputed, DateComputed, DriverVerVersion, SizeInBytes.
CATALOG_PAGE_SIZE = 25
# Legfeljebb ennyi lapot kérünk le EGY HWID-re. A dátum szerinti rendezés miatt az 1. lap
# már a legfrissebb 25 sort tartalmazza; a többi lap csak akkor kell, ha a legfrissebb
# dátumú holtverseny átlóg a lap végén. 3 lap = 75 sor, bőven fedi a mért eseteket.
CATALOG_MAX_PAGES = 3

# IDEGEN-GYÁRTÓS JELÖLT KIPRÓBÁLÁSÁNAK MÉRETHATÁRA (MB), 2026-09-03, explicit user decision.
#
# A helyzet: a gép saját `SUBSYS_`-kulcsán már a legfrissebb csomag van fent, az általános
# kulcson viszont vannak frissebb sorok - azok ugyanannak a chipnek MÁS gépgyártókhoz
# készült változatai. Hogy egy ilyen mégis illik-e, azt CSAK letöltés után, az INF-ből
# lehet megtudni. A felhasználó döntése: a kicsit próbáljuk ki (úgyis az az igazság
# egyetlen útja), a nagyot ne.
#
# Miért pont 100: a mért hangdriver-csomagok 11-30 MB körül vannak (érdemes kipróbálni),
# a videokártya-csomagok 240 MB - 1,2 GB (nem éri meg egy olyan csomagért, amiről a gyártó
# saját kulcsa már megmondta, hogy nem ide való). A méret a katalógus találati sorának
# Size oszlopából jön, tehát a döntés LETÖLTÉS NÉLKÜL meghozható.
CATALOG_FOREIGN_TRY_MAX_MB = 100

# Hány jelölt részletlapját kérdezzük le az ALKALMAZHATÓSÁG letöltés előtti eldöntéséhez.
# Egy lap 44-86 KB / ~2 mp (mérve), tehát néhány jelöltnél ez nagyságrendekkel olcsóbb egy
# fölösleges letöltésnél (11 MB - 1,2 GB) - egy 25 soros holtversenynél viszont már 25
# kérés lenne, ezért van felső korlát. A holtverseny amúgy is dátum szerint rendezett,
# tehát az első néhány a releváns.
#
# 6 -> 12 (2026-09-03, terepen mérve): a HID billentyűzetre **12** azonos című, azonos
# dátumú AlpsAlpine-jelölt jött, tehát a 6-os korláttal a szűrő KIMARADT, és a program
# újra felajánlotta a bizonyítottan nem ide való csomagot. 12 lap ~24 mp - egy olyan
# eszköznél, ahol enélkül minden szken újra felajánlana valamit, ez megéri.
CATALOG_HWID_PROBE_MAX = 12
CATALOG_SORT_QS = '&scol=DateComputed&sdir=desc'
# Ennyi holtverseny-jelöltnél kérjük le a részletlapot a "Driver Model" mezőért
# (letöltés előtti, pár KB-os alkalmasság-jelzés - lásd _catalog_driver_models). PONTOS
# névegyezésnél azonnal megállunk, tehát a jó esetben ennél jóval kevesebb kérés fut. A
# felső korlát azért kell, mert a holtverseny nagy is lehet (mérve: 45 sor a Realtek
# NIC-re) - viszont bőven megéri: pár KB-os kérésekkel kerülünk el egy rossz, akár
# 1,2 GB-os letöltést, és csak azoknál az eszközöknél fut, ahol tényleg telepítenénk.
CATALOG_MODEL_PROBE_MAX = 10
# ---------------------------------------------------------------------------
# A KATALÓGUS-SOROK GYORSÍTÓTÁRA (2026-08-31, terepi mérésre)
# ---------------------------------------------------------------------------
# MIÉRT: a katalógus-zárókör MINDEN LÁBON újra lefut, lényegében ugyanazzal az
# eszközlistával, és eszközönként max 4 HWID-et kérdez le. Mérve egy 3 órás láncon
# (ThinkPad T14 Gen 1, Build 284): **952 HTTP-lekérdezés, ebből mindössze 194 EGYEDI
# HWID-re** - vagyis a kérések 80%-a szó szerint ugyanaz a kérdés volt, összesen
# ~11 PERCET elvéve. Az öt zárókörből három NULLA csomagot eredményezett, a negyedik
# egyet (amiről addigra már tudtuk, hogy nem köt rá).
#
# MIÉRT BIZTONSÁGOS: ez TISZTA HÁLÓZATI OLVASÁS-gyorsítótár. Nem dönt semmiről, csak a
# szerver válaszát teszi el; a döntés (dátum-elsőbbség, kiadás-kapu, INF-vizsgálat,
# no-bind lista) változatlanul lefut a sorokon, a TELEPÍTETT állapotot pedig továbbra is
# frissen kérdezzük le. Amit a gyorsítótár nem láthat, az egy MENET KÖZBEN közzétett új
# katalógus-csomag - a gyártók hetes-havi ritmusban publikálnak, egy lánc pedig 1-3 óra.
#
# MIÉRT FÁJLBAN ÉS NEM MEMÓRIÁBAN: a lánc minden lába KÜLÖN FOLYAMAT, és a mérés szerint
# az ismétlődések szinte kizárólag lábak KÖZÖTT vannak (láboknként egy zárókör). Egy
# memóriabeli gyorsítótár tehát pont a valódi veszteséget nem fogná meg.
#
# HIBÁT SOSEM TESZÜNK EL: `_catalog_fetch_rows` kivételt dob, ha a lekérdezés elhasal
# (pl. a láncban terepen mért hálózat-kiesés alatt), és a hívó kapja el - így egy üres
# válasz csak akkor kerül a tárba, ha a szerver tényleg azt mondta: nincs találat.
CATALOG_ROWS_TTL = 6 * 3600
# Ennél több bejegyzést nem tartunk (a legrégebbiek esnek ki): egy gép ~200 egyedi HWID-et
# kérdez, tehát ez több gép nyomát is elbírja, a fájl mérete mégis kordában marad.
CATALOG_ROWS_MAX = 2000
# A gyorsítótárat 10 szál írja/olvassa (a katalógus-kör szálkészlete), ezért zárral.
_CATALOG_ROWS_LOCK = threading.Lock()
# ---------------------------------------------------------------------------
# A RÉSZLETLAP TÁMOGATOTT-AZONOSÍTÓ LISTÁINAK GYORSÍTÓTÁRA (2026-09-08, terepi mérésre)
# ---------------------------------------------------------------------------
# Ugyanaz az érv, mint a sor-gyorsítótárnál, csak a másik kérés-fajtára: a letöltés előtti
# alkalmazhatóság-ellenőrzés (`_catalog_supported_hwids`) GUID-onként egy 44-86 KB-os
# részletlapot húz le, és a `_catalog_detail_cache` CSAK MEMÓRIÁBAN él - a lánc lábai
# viszont külön folyamatok, tehát minden láb újraszedi ugyanazt az 50-70 lapot. Mérve egy
# ASRock B450M láncon (Build 304): a katalógus-zárókörök 74 / 27 / 19 / 14 mp-e szinte
# teljes egészében ez volt, eszközönként 24 mp (NVIDIA, 12 lap).
#
# CSAK SIKERES, NEM ÜRES EREDMÉNYT TESZÜNK EL. A `None` (a lap nem jött le, VAGY nincs
# rajta ilyen szekció) nem kerül a tárba: egy hálózati hiba különben tartósan "nem
# eldönthető"-vé betonozná a csomagot - ugyanaz a szabály, mint a sor-gyorsítótárnál
# ("hibás lekérdezést sosem teszünk el").
CATALOG_HWIDS_TTL = 7 * 24 * 3600
CATALOG_HWIDS_MAX = 3000
_CATALOG_HWIDS_LOCK = threading.Lock()


def _device_stem(pnp_id):
    """Egy eszköz TARTÓS azonosítója: a példány-azonosító utolsó `\\` előtti része.

    MIÉRT NEM A TELJES PÉLDÁNY-AZONOSÍTÓ (2026-08-31, mérve a fejlesztői gépen): annak a
    farkában ott a PnP példányszám, amit a Windows LÉPTET, ha egy eszköz-csomópontot
    eltávolítanak és újra felderítenek - vagyis pontosan akkor, amikor ez a program
    `pnputil /remove-device`-t futtat (újrakötés-kör, regresszió-javítás). A tartós
    no-bind tár emiatt a saját újrafelderítéseinktől vált halottá: a 4 feljegyzésből 2
    már nem illeszkedett semmire, így ugyanazt a bizonyítottan NEM alkalmazható csomagot
    a felület újra előre bejelölve ajánlotta, az AutoFix pedig újra letöltötte.

        USB\\VID_041E&PID_3274&MI_03\\7&887C350&0&0003 -> ...&1&0003
        HDAUDIO\\...&REV_1003\\5&337FEF56&0&0001       -> ...&1&0001

    A törzs maga a hardver-azonosító, ami újrafelderítés után is ugyanaz. Két azonos
    eszköznél közös - ez helyes: ami az egyikre nem alkalmazható, az az ikertestvérére
    sem. Régi bejegyzésekhez is működik, nem kell séma-váltás.

    IDEMPOTENS KELL LEGYEN, ÉS 2026-09-03-IG NEM VOLT AZ - ez volt a "sose lesz minden
    naprakész" érzés fő oka (terepen mérve, HP EliteDesk 800 G2). A régi kód
    `rsplit('\\', 1)[0]`-t használt, ami a TELJES példány-azonosítón helyes:

        HDAUDIO\\FUNC_01&...&REV_1001\\5&337FEF56&0&0001  ->  HDAUDIO\\FUNC_01&...&REV_1001

    csakhogy a tartós no-bind tár a MÁR LETÖRZSELT értéket menti el (`_no_bind_record`
    a `k[0]`-t írja ki), és az olvasó oldal (`_no_bind_load` hívói) MÉGEGYSZER
    lefuttatta rajta - egy egy-backslashes törzsből pedig a puszta BUSZNEVET csinálta:

        HDAUDIO\\FUNC_01&...&REV_1001  ->  HDAUDIO        <- olvasási kulcs
        (a keresési kulcs közben a helyes teljes törzs maradt)

    A kettő SOHA nem egyezett, tehát a tár gyakorlatilag halott volt. Három dolog
    romlott el tőle egyszerre: (a) a `prev_no_bind` jelölés sosem került fel, így a
    bizonyítottan NEM alkalmazható csomagot a felület minden szken után újra ELŐRE
    BEJELÖLVE ajánlotta; (b) a bejegyzések nem deduplikálódtak (élőben mérve: ugyanaz
    az Intel-bejegyzés NÉGYSZER a fájlban); (c) a sikeres kötés utáni KIVEZETÉS sem
    talált rá semmire, tehát a tár csak hízott.

    A javítás az első KÉT útvonal-elem megtartása, ami teljes azonosítóra és törzsre
    egyaránt ugyanazt adja - vagyis akárhányszor futtatható."""
    parts = (pnp_id or '').upper().split('\\')
    return '\\'.join(parts[:2])


# A tartós no-bind tár CÍM-oldali kulcsa. A megjelenítéshez a katalógus-találat címe elé
# `MS Katalógus: ` kerül (lásd `_catalog_find_driver`), a tartalék-jelöltek viszont a
# NYERS címmel jegyződnek fel - ugyanaz a csomag így KÉT kulcson landolt. Élőben mérve
# (2026-09-03, HP EliteDesk): ugyanaz az Intel-csomag egyszerre szerepelt
# `MS Katalógus: Intel(R) Corporation - System - 9.21.0.4561` és
# `Intel(R) Corporation - System - 9.21.0.4561` néven, tehát a dedup és a maszkolás is
# kettévált rajta. Egy FELÜLETI előtag nem lehet része egy azonosító kulcsnak.
_NB_TITLE_PREFIX = 'ms katalógus:'


def _nb_title(title):
    """A no-bind kulcshoz használt, megjelenítési előtag nélküli cím."""
    t = (title or '').strip()
    return t[len(_NB_TITLE_PREFIX):].strip() if t.lower().startswith(_NB_TITLE_PREFIX) else t
# Ha az INF-vizsgálat elveti a nyertes csomagot, ennyi TARTALÉK jelöltet próbálunk még
# (a nyertessel együtt ennyi letöltés lehet összesen). Csak azonos dátumú jelöltek
# jönnek szóba, tehát a tartalék sosem lehet régebbi kiadás - lásd _catalog_find_driver.
CATALOG_MAX_CANDIDATES = 3
# WINDOWS-ALAPDRIVEREN ülő eszköznél ennél többet is megpróbálunk, és ott RÉGEBBI kiadás
# is szóba jön (explicit user decision, 2026-08-26: "az inbox drivernél jobb a régi driver
# milliószor"). Terepen mérve (ASRock B450M + Realtek ALC897) a helyes csomag a
# LEGSPECIFIKUSABB kulcs első találata volt, de RÉGEBBI kiadás, mint az általános kulcson
# nyert (és az eszközre nem illő) Acer-változat - a 3-as korláttal és az azonos-dátum
# szabállyal soha nem jutottunk el hozzá.
#
# Miért nem drágább a gyakorlatban: a tartalékok a legspecifikusabb kulcs felől jönnek,
# tehát a helyes csomag jellemzően az ELSŐ tartalék; a telepítő ráadásul URL szerint
# deduplikál (mérve: 25 katalógus-bejegyzés ugyanarra az 1,2 GB-os cab-ra mutatott), és a
# korábban megbukott csomagokat le sem tölti (tartós no-bind tár).
CATALOG_INBOX_FALLBACK_CANDIDATES = 6
# EGY ESZKÖZRE ENNYI BÁJTNÁL TÖBBET NEM TÖLTÜNK LE A TARTALÉKOK VÉGIGPRÓBÁLÁSÁRA.
#
# MIÉRT KELL (2026-09-01): a fenti 6-os korlát a hangkártyán ártalmatlan - a helyes
# ASRock-csomag mérve **11,2 MB**, tehát mind a 6 jelölt végigpróbálása pár perc. Egy
# Windows-alapdriveren (Microsoft Basic Display Adapter) ragadt VIDEOKÁRTYA viszont
# ugyanezen az ágon megy, és ott egy csomag **1,2 GB** (ezt a fájl korábban mérte) -
# 6 jelölt akár 7 GB és órák lennének, ami az "1 kattintásos fix" elfogadási
# feltételét (20-60 perc, kávészünet) borítaná fel.
#
# A korlát a MÁR ELKÖLTÖTT bájtokra néz, a következő jelölt indítása ELŐTT - az első
# jelöltet tehát mindig végigvisszük. Így: kis csomagoknál mind a 6 jelölt sorra kerül,
# egy 1,2 GB-osnál viszont a második után megállunk. Nem néma: a technikus látja, hogy
# a program azért hagyta abba, mert elérte a letöltési korlátot.
CATALOG_FALLBACK_MAX_BYTES = 2 * 1024 * 1024 * 1024


class GuiHwScanMixin:
    """Driver Keresés és Telepítés nézet: hardver-szken, WU/Catalog keresés, kiválasztott driverek telepítése. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def start_hw_scan(self, deep=True, allow_storage=False, allow_firmware=False, use_catalog=True):
        """Hardver-szken. deep=True (alapértelmezés): a Microsoft Update Catalogot MINDEN
        olyan eszközre megkérdezzük, amire a WU Agent nem adott ajánlatot - nem csak a
        hibakódosakra és a Windows-alapdriveren futókra. Lassabb (eszközönként max 4 HTTP
        lekérdezés, 10 szálon), cserébe ez az egyetlen mód, amivel egy RÉGI, de hibátlanul
        működő gyári driver is frissülni tud. deep=False: a korábbi, szűk kiegészítés.

        A FELÜLETEN EZ NEM "MÉLY KERESÉS"-KÉNT SZEREPEL (2026-09-03, explicit user
        decision: *"ezt meg en se ertem nemhogy egy ugyfel"*). A felirat GUI-ban
        "🔄 MEGLÉVŐ DRIVEREKHEZ IS KERES ÚJABBAT" / kikapcsolva "🎯 CSAK A HIÁNYZÓ ÉS
        HIBÁS DRIVEREK", a CLI-ben "A MÁR MŰKÖDŐ gyári driverekhez is keressek újabbat?".
        A régi név két okból volt rossz: (a) azt sugallta, hogy a katalóguson BELÜL keres
        mélyebben, holott azt dönti el, HÁNY ESZKÖZRE kérdezünk rá; (b) a katalóguson
        belüli mélyebb lapozás LÉTEZIK IS - `_catalog_fetch_rows(deep=True)`, Windows-
        alapdriveres eszköz saját SUBSYS-kulcsán, automatikusan, kapcsoló nélkül -, tehát
        két KÜLÖNBÖZŐ dolgot hívtunk "mély"-nek. A paraméter neve szándékosan maradt
        `deep`: a `wu_core.deep_catalog_candidates`, az AUTOFIX_DEEP_CATALOG és a CLI is
        ezt használja, az átnevezés hívási helyek tucatját érintené nulla haszonért -
        de ha a kódban "mély szken"-t olvasol, a felületen EZT a feliratot keresd.

        allow_storage / allow_firmware: **2026-09-02 ÓTA MINDIG False, A PARAMÉTERTŐL
        FÜGGETLENÜL** (explicit user decision: "ne lehessen bekapcsolni sose... inkább ne
        tudják bekapcsolni az ügyfelek mert abbol sok baj lehet"). A felületről eltűnt a két
        jelölőnégyzet, itt pedig a paraméter felül van írva, hogy a képességet se a JS-ből,
        se máshonnan ne lehessen visszahozni. A két hibalehetőség, ami miatt: rossz
        tároló-driver után a Windows EL SEM INDUL (INACCESSIBLE_BOOT_DEVICE, csak
        helyreállító médiával javítható), a firmware-írás pedig visszafordíthatatlan.
        A paraméter és a mögötte lévő teljes logika SZÁNDÉKOSAN a helyén maradt: ha valaha
        újra kapcsolhatóvá kell tenni, az alábbi két sor törlése + a felületi kapcsoló
        visszatétele elég, semmi mást nem kell újraírni.
        (Előzmény: 2026-08-28-tól két, alapból kikapcsolt jelölőnégyzet volt itt.)

        use_catalog (2026-09-02, explicit user decision - "gyors mód"): alapból BE. Kikapcsolva
        a szken CSAK a WU Agentet kérdezi meg, és a Microsoft Update Catalog mindhárom ága
        (hibakódos + Windows-alapdriveres + mélykeresés) kimarad. Ez a szken leghosszabb
        szakasza - mérve 90+ eszközön 10 szálon ~1,5-2 perc, a találatok telepítésével együtt
        jóval több -, de VELE EGYÜTT VÉSZ EL a katalógus-only találat is: azon a Dell-en,
        amiről a mérés készült, a WU időtúllépésbe futott, és mind a 16 driver a katalógusból
        jött. Ha a WU elhasal ÉS a katalógus ki van kapcsolva, a szken semmit nem talál -
        ezt ilyenkor ki is mondjuk a felületen, hogy ne tűnjön hibának."""
        logging.info(f"[API] start_hw_scan(deep={deep}, use_catalog={use_catalog}) hívás")
        deep = bool(deep)
        # LEZÁRVA (2026-09-02, explicit user decision). A kapott értéket SZÁNDÉKOSAN eldobjuk:
        # a felületen nincs kapcsoló, és a képességet innen sem szabad visszahozni. Ha a hívó
        # mégis True-t küld, azt kilogoljuk - az már hívási hiba, nem felhasználói döntés.
        if allow_storage or allow_firmware:
            logging.warning("[HW_SCAN] Tároló/firmware engedélyt kért a hívó "
                            f"(allow_storage={allow_storage}, allow_firmware={allow_firmware}), "
                            "de ez a program egészében véglegesen tiltva van - figyelmen kívül hagyva.")
        allow_storage = False
        allow_firmware = False
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Hardver keresés csak Élő rendszeren működik!', 'type': 'error'})
            self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Offline módban nem elérhető', 'time': ''})
            return

        if self._hw_scanning:
            logging.warning("[HW_SCAN] Már fut egy scan!")
            return
        if self._task_busy:
            # Megosztjuk a _safe_thread-alapú feladatokkal ugyanazt a "busy" jelzőt, mert
            # a scan és egy driver-telepítés/törlés egyszerre futva ugyanazt a
            # self.hw_updates_pool listát írná-olvasná (race condition).
            logging.warning(f"[HW_SCAN] Elutasítva - már fut egy másik feladat ({self._task_busy}).")
            self.emit('toast', {'message': f'⚠️ Már folyamatban van egy másik művelet ({self._task_busy}), várd meg amíg befejeződik!', 'type': 'warning'})
            # A JS oldal a scan gomb megnyomásakor azonnal "folyamatban" állapotba kapcsol -
            # e nélkül az emit nélkül elutasítás esetén a progress sáv örökre "Scannelés
            # folyamatban..." állapotban ragadna, hiszen sosem indul valódi scan-szál.
            self.emit('hw_scan_result', {'pool': self.hw_updates_pool, 'installed': self._hw_installed_devs,
                                          'sys_info': f'⚠️ Másik művelet ({self._task_busy}) fut, próbáld újra pár másodperc múlva', 'time': ''})
            return
        self._hw_scanning = True
        self._task_busy = 'hw_scan'
        logging.info("[HW_SCAN] Hardver scan indítása...")

        def worker():
            try:
                _start = time.monotonic()
                
                # Internet ellenőrzés
                self.emit('hw_scan_progress', {'status': '1/6 · Internetkapcsolat ellenőrzése', 'detail': '', 'determinate': False})
                if not self._check_internet():
                    self.emit('toast', {'message': '❌ Nincs internetkapcsolat! Telepíts egy hálózati drivert!', 'type': 'error'})
                    self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Nincs Internet!', 'time': ''})
                    return
                
                # Hardver változások frissítése szkennelés előtt
                logging.info("[HW_SCAN] Eszközök újra-szkennelése (PnP)...")
                self.emit('hw_scan_progress', {'status': '1/6 · Hardver-változások keresése', 'detail': 'pnputil /scan-devices'})
                self._run(['pnputil', '/scan-devices'])
                time.sleep(2)
                
                sys_info_text = "Ismeretlen PC / Laptop"
                logging.info("[HW_SCAN] Rendszer info lekérdezése...")
                self.emit('hw_scan_progress', {'status': '2/6 · Rendszer-információk lekérdezése', 'detail': ''})

                # System info
                try:
                    ps_cmd = (
                        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                        "$cs = Get-WmiObject Win32_ComputerSystem | Select-Object Manufacturer, Model, PCSystemType; "
                        "$bb = Get-WmiObject Win32_BaseBoard | Select-Object Manufacturer, Product; "
                        "$enc = Get-WmiObject Win32_SystemEnclosure | Select-Object ChassisTypes; "
                        "@{CS=$cs; BB=$bb; ENC=$enc} | ConvertTo-Json -Depth 3"
                    )
                    res = self._run(["powershell", "-NoProfile", "-Command", ps_cmd], encoding='utf-8')
                    if res.stdout.strip():
                        data = json.loads(res.stdout.strip())
                        cs = data.get("CS", {}) or {}
                        bb = data.get("BB", {}) or {}
                        enc = data.get("ENC", {}) or {}

                        man = (cs.get("Manufacturer") or "").strip()
                        mod = (cs.get("Model") or "").strip()
                        pct = cs.get("PCSystemType", -1)

                        # Fallback: ha OEM placeholder, használjuk az alaplap infót
                        oem_junk = {"to be filled by o.e.m.", "default string", "system manufacturer",
                                    "system product name", "not applicable", ""}
                        if man.lower() in oem_junk:
                            man = (bb.get("Manufacturer") or "").strip()
                        if mod.lower() in oem_junk:
                            mod = (bb.get("Product") or "").strip()
                        if man.lower() in oem_junk:
                            man = "Ismeretlen gyártó"
                        if mod.lower() in oem_junk:
                            mod = "Ismeretlen modell"

                        # Chassis-alapú laptop/desktop detekció (pontosabb mint PCSystemType)
                        chassis = enc.get("ChassisTypes", []) or []
                        if isinstance(chassis, int):
                            chassis = [chassis]
                        laptop_chassis = {8, 9, 10, 11, 14, 30, 31, 32}  # Portable, Laptop, Notebook, Sub Notebook, etc.
                        is_laptop = pct == 2 or any(c in laptop_chassis for c in chassis)
                        prefix = "💻 Laptop" if is_laptop else "🖥️ Asztali (Desktop)"

                        sys_info_text = f"{prefix} | {man} - {mod}"
                except Exception as e:
                    logging.debug(e)
                self.emit('hw_scan_progress', {'sys_info': sys_info_text, 'status': '2/6 · Csatlakoztatott eszközök felderítése', 'detail': ''})

                # PnP devices - a szűrés/kategorizálás a KÖZÖS _filter_wu_scan_devices-ben él
                # (az AutoFix ugyanezt használja - ne ide írj eszköz-szűrési logikát!)
                pnp_data = []
                try:
                    res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS], encoding='utf-8')
                    if res.stdout:
                        pnp_data = json.loads(res.stdout)
                except Exception as ex:
                    logging.error(f"PNP Query error: {ex}")

                self.emit('hw_scan_progress', {'status': '2/6 · Eszközlista szűrése', 'detail': ''})

                devices_to_check = _filter_wu_scan_devices(pnp_data)

                # KOCKÁZATOS OSZTÁLYOK KAPUJA - A TELJES ESZKÖZLISTÁRA, EGY HELYEN.
                # Ugyanaz a lecke, mint az AutoFix katalógus-zárókörében (CLAUDE.md,
                # 2026-07-28): ez a szken HÁROM forrásból tölti a katalógus-kört (hibakódos
                # + generikus-driveres + mély szken), és ha a kapu ágakként ülne, egyetlen
                # hibakód elég lenne a megkerüléséhez. Ezért itt, a legelején, mielőtt
                # bármelyik ág hozzáérne a listához.
                #
                # A visszaadott `risky_dropped` bontást 2026-09-03 óta SEHOL NEM HASZNÁLJUK
                # (a "N tároló-eszköz kihagyva" képernyő-sor törölve, lásd lentebb) - ezért
                # nem is vesszük át változóba: egy soha nem olvasott érték csak azt a
                # látszatot keltené, hogy még van rá funkció. A kizárt eszközöket maga a
                # `filter_autofix_risky_devices` naplózza, névvel, `[HW-SCAN]` cimkével.
                if not (allow_storage and allow_firmware):
                    devices_to_check, _ = filter_autofix_risky_devices(
                        devices_to_check, allow_storage=allow_storage,
                        allow_firmware=allow_firmware, log_tag='HW-SCAN',
                        context='a kézi driver-keresésből')

                logging.info(f"PnP szürés: {len(devices_to_check)} eszköz átment")
                total_devs = len(devices_to_check)
                # WU COM API search
                self.emit('hw_scan_progress', {'status': f'✅ {total_devs} hardverelem azonosítva, WU keresés indul...',
                                               'sys_info': f'{sys_info_text} | ⏳ Driver keresés...'})

                self.hw_updates_pool = []
                self._hw_installed_devs = []
                self.wu_api_mode = True

                # Telepített driver-verziók/dátumok egyszeri felmérése: a találatok melletti
                # "Telepítve: X" kijelzéshez ÉS a katalógus-út már-telepítve szűréséhez.
                self.emit('hw_scan_progress', {'status': '3/6 · Telepített driver-verziók felmérése', 'detail': 'dism /Get-Drivers — 15-50 mp'})
                inst_info = self._get_installed_driver_info()

                # Közvetlen WU API lekérdezés (a COM objektum ezen kulcs módosítása nélkül is látja a drivereket)
                self.emit('hw_scan_progress', {'status': '4/6 · Windows Update kérdezése', 'detail': 'Ez a leghosszabb szakasz — akár 2-5 perc is lehet, közben a Windows nem ad jelzést. Az óra fut: a program dolgozik.', 'determinate': False})
                wu_results = self._search_wu_api()
                wu_api_success = wu_results is not None

                if wu_results is None:
                    wu_results = []

                self.emit('hw_scan_progress', {'status': '5/6 · A Windows Update válaszának feldolgozása', 'detail': ''})

                # Párosítás a KÖZÖS _match_wu_updates_to_devices-szel (HWID prefix + név-tartalék,
                # az AutoFix is pontosan ezt hívja - ne ide írj párosítási logikát!)
                wu_by_uid = {w.get('UpdateID'): w for w in wu_results if w.get('UpdateID')}
                matches = _match_wu_updates_to_devices(wu_results, devices_to_check)
                matched_hwids = set()
                matched_uids = set()
                for m in matches:
                    dev = m['device']
                    # A `matched_uids` itt frissül (a csomag TÉNYLEG párosult, akkor is, ha
                    # utána kiszűrjük), a `matched_hwids` viszont csak lentebb, a
                    # firmware-kapu UTÁN: az kizárja az eszközt a katalógus-körből, és egy
                    # kiszűrt firmware-csomag miatt nem eshet el az eszköz VALÓDI drivere.
                    matched_uids.add(m['uid'])
                    inst = inst_info.get((dev.get('pnp_id') or '').upper()) or {}
                    wu_date = _iso_date_or_none((wu_by_uid.get(m['uid']) or {}).get('DriverVerDate')) or ''
                    inst_date = _iso_date_or_none(inst.get('date')) or ''
                    # KOCKÁZATI JELÖLÉS A WU-TALÁLATOKRA IS (2026-07-28). A manuális szken
                    # szerződése: tároló-/firmware-találat PIROSAN és ELŐRE BE NEM JELÖLVE
                    # jelenik meg, mert emberi döntést kíván. Ezt eddig CSAK a mély
                    # katalógus-szken tette rá (deep_catalog_candidates), így ugyanaz az
                    # NVMe-vezérlő pirosan VAGY némán, előre bepipálva jelent meg attól
                    # függően, melyik forrás találta meg - egy "mindet telepít" kattintás
                    # pedig csendben tett fel boot-kritikus drivert.
                    # A csomag-szintű firmware-vizsgálat sem elhagyható: egy SSD-firmware a
                    # TÁROLÓVEZÉRLŐHÖZ, egy dokkoló-firmware egy USB-eszközhöz párosul,
                    # tehát az eszközosztály önmagában nem fogná meg (lásd
                    # wu_core.filter_firmware_updates ugyanezt az AutoFix oldalán).
                    risky, risk_label, risk_reason = device_risk_marker(dev)
                    pkg_firmware = is_firmware_update(wu_by_uid.get(m['uid']) or {})
                    if not allow_firmware and pkg_firmware:
                        # A CSOMAG firmware, akkor is, ha az ESZKÖZ osztálya nem az - és a
                        # fenti eszköz-kapu ezt nem foghatja meg. Kihagyva, nevesítve.
                        logging.info(f"[HW-SCAN] Firmware-csomag kihagyva (a kapcsoló ki van "
                                     f"kapcsolva): '{m['title']}' -> {dev['name']}")
                        continue
                    matched_hwids.add(dev['id'])
                    if not risky and pkg_firmware:
                        risky, risk_label, risk_reason = True, FIRMWARE_CLASS_LABEL, FIRMWARE_CLASS_WARNING
                    if risky:
                        logging.warning(f"[HW_SCAN] KOCKÁZATOS WU-találat, előre BE NEM jelölve: "
                                        f"{dev['name']} [{dev.get('pclass') or '?'}] - '{m['title']}'")
                    self.hw_updates_pool.append({
                        "name": dev['name'], "cat": dev['cat'], "hwid": dev['id'],
                        "wu_title": m['title'], "pnp_id": dev.get('pnp_id', ''),
                        "installed_version": inst.get('version', ''),
                        "installed_date": inst_date,
                        "wu_date": wu_date,
                        # Downgrade-jelzés a felületnek: a WU néha a telepítettnél RÉGEBBI
                        # csomagot ajánl (pl. friss gyári NVIDIA driver után) - a manuális
                        # listából nem rejtjük el, csak megjelöljük, a döntés a felhasználóé.
                        # (Az AutoFix ezzel szemben automatikusan kihagyja az ilyet, lásd
                        # wu_core._filter_wu_downgrades.)
                        # (_is_inbox_driver: a beépített generikus driver frissebb dátuma
                        # nem downgrade-jelzés - lásd wu_core._filter_wu_downgrades.)
                        "downgrade": bool(wu_date and inst_date and wu_date < inst_date
                                          and not dev.get('err_code')
                                          and not _is_inbox_driver(inst)),
                        # A pontos WU UpdateID a telepítéshez: e nélkül a telepítő csak
                        # HWID-prefix alapján tudna szűrni, ami azonos HWID-jű csomagoknál
                        # (pl. Realtek Extension + MEDIA ugyanazon hdaudio ID-n) többet
                        # telepítene, mint amit a felhasználó kijelölt.
                        "update_id": m['uid'],
                        "risky": risky, "risk_label": risk_label, "risk_reason": risk_reason
                    })
                # A párosítatlan (ghost) WU-találatok kimaradnak a poolból
                for wu in wu_results:
                    if wu.get('UpdateID') not in matched_uids:
                        logging.debug(f"[WU_API] Ghost / Unmatched eszköz kihagyva: {wu.get('Title')}")

                # "GYÁRI DRIVER A GENERIKUS HELYETT": megjelöljük azokat az eszközöket,
                # amik a Windows beépített driverén futnak, pedig a chipgyártónak van
                # sajátja. A jelölést a KÖZÖS wu_core.mark_generic_replace_candidates
                # végzi - ugyanez fut az AutoFix katalógus-zárókörében is, hogy a két út
                # pontosan ugyanazokat az eszközöket találja meg.
                # allow_storage/allow_firmware: a kockázatos eszközöket a FENTI kapu már
                # kiszűrte a `devices_to_check`-ből, ha a technikus nem engedélyezte őket.
                # Itt ezért a kapcsolók értékét adjuk tovább (nem fix True-t): bekapcsolva a
                # találat pirosan és ELŐRE BE NEM JELÖLVE jelenik meg - itt ember dönt.
                generic_devs = mark_generic_replace_candidates(
                    devices_to_check, inst_info,
                    allow_storage=allow_storage, allow_firmware=allow_firmware)
                if generic_devs:
                    logging.info(f"[CATALOG] Generikus driveren futó eszközök: {[d['name'] for d in generic_devs]}")

                if not use_catalog:
                    # GYORS MÓD: a katalógus mindhárom ága kimarad. Ha a WU is elhasalt,
                    # akkor NINCS forrás - ezt ki kell mondani, különben a technikus a
                    # program hibájának hiszi az üres eredményt.
                    logging.info("[CATALOG] A katalógus-keresés KIHAGYVA (gyors mód, use_catalog=False).")
                    if not wu_api_success:
                        self.wu_api_mode = False
                        self.emit('hw_scan_progress', {
                            'status': '6/6 · Katalógus kihagyva (gyors mód)',
                            'detail': 'A Windows Update NEM válaszolt, a katalógus pedig ki van kapcsolva - így most nincs forrás.'})
                    else:
                        self.emit('hw_scan_progress', {
                            'status': '6/6 · Katalógus kihagyva (gyors mód)',
                            'detail': 'Csak a Windows Update találatai látszanak.'})
                elif not wu_api_success:
                    # Teljes katalógus-fallback: a WU API elhasalt, minden eszközt a
                    # katalógusban keresünk.
                    self.wu_api_mode = False
                    self.emit('hw_scan_progress', {'status': f'6/6 · Microsoft Update Catalog — {total_devs} eszköz',
                                                   'detail': 'A Windows Update nem válaszolt, ezért mindent a katalógusban keresünk.',
                                                   'determinate': True, 'current': 0, 'total': total_devs})
                    self._catalog_search(devices_to_check, installed_info=inst_info)
                else:
                    # HIBRID KIEGÉSZÍTÉS: a hibakódos (driver nélküli / hibás) eszközökre,
                    # amikre a WU nem adott semmit, még ráengedjük a katalógus-keresést is -
                    # két forrás egyesítve, hogy tényleg MINDENT megtaláljunk. A pool vegyes
                    # lesz (WU-s elemek update_id-vel, katalógusosak url-lel), a telepítő
                    # diszpécser (install_selected_wu) elemenként dönti el a módot.
                    # A hibakódos eszközök mellé a generikus driveren futók is bekerülnek:
                    # ezekre a WU szerint "minden rendben" (ezért nem ajánl semmit), a
                    # katalógusban viszont ott a chipgyártó csomagja.
                    # A hibakódos ág eszközei is átmennek a KÖZÖS kockázati jelölőn
                    # (mark_device_risk): egy hibakódos tárolóvezérlő/firmware-eszköz eddig
                    # jelöletlenül, tehát PIROS FIGYELMEZTETÉS NÉLKÜL és ELŐRE BEJELÖLVE
                    # került a listára - miközben a mély szken ugyanazt az eszközt pirossal
                    # hozta. A jelölés így nem attól függ, melyik ág találta meg.
                    leftover = [mark_device_risk(d) for d in devices_to_check
                                if d.get('err_code') and d['id'] not in matched_hwids]
                    # MÉLY SZKEN (deep=True, alapértelmezés): a katalógust MINDEN olyan
                    # eszközre megkérdezzük, amire a WU nem adott ajánlatot - nem csak a
                    # hibakódosakra és a generikus driveresekre. Enélkül egy eszköz, ami
                    # hibátlanul fut egy RÉGI gyári driveren, sosem kapott újabbat: a WU
                    # szerint rendben van, inbox-jelölt nem lévén a katalógust meg se
                    # kérdeztük rá. A csomagok szűrése változatlan (a _catalog_find_driver
                    # verzió-kapuja csak SZIGORÚAN újabb csomagot enged át), tehát a mély
                    # szken nem hoz downgrade-et, csak lefedettséget.
                    # include_risky/include_firmware: a technikus kapcsolói (2026-08-28).
                    # Bekapcsolva a tárolóvezérlő/lemez/firmware eszközök is bekerülnek -
                    # de `risky` jelzővel, piros figyelmeztetéssel és ELŐRE BE NEM JELÖLVE.
                    # Itt ember dönt, és a szerelőnek látnia kell, HOGY LÉTEZIK csomag, még
                    # ha a telepítése mérlegelendő is. Lásd wu_core.DEEP_CATALOG_RISKY_CLASSES.
                    rest = wu_core_deep_candidates(
                        [d for d in devices_to_check if d['id'] not in matched_hwids],
                        inst_info, include_risky=allow_storage,
                        include_firmware=allow_firmware) if deep else []
                    todo, todo_ids = [], set()
                    for d in leftover + generic_devs + rest:
                        if d['id'] in matched_hwids or d['id'] in todo_ids:
                            continue
                        todo_ids.add(d['id'])
                        todo.append(d)
                    if todo:
                        parts = []
                        if leftover:
                            parts.append(f'{len(leftover)} problémás')
                        if generic_devs:
                            parts.append(f'{len(generic_devs)} generikus driveres')
                        primary_ids = {d['id'] for d in leftover + generic_devs}
                        extra = sum(1 for d in todo if d['id'] not in primary_ids)
                        if extra:
                            parts.append(f'{extra} mélykeresés')
                        self.emit('hw_scan_progress', {'status': f'6/6 · Microsoft Update Catalog — {len(todo)} eszköz',
                                                       'detail': f'Forrás: {" + ".join(parts)}',
                                                       'determinate': True, 'current': 0, 'total': len(todo)})
                        self._catalog_search(todo, installed_info=inst_info)

                # A "telepített/naprakész" lista: minden eszköz, amire végül nincs találat.
                #
                # A SORHOZ ODAKERÜL A TÉNYLEGESEN TELEPÍTETT DRIVER IS (2026-09-03, explicit
                # user decision: *"írja ki azt is a program hogy jelenleg mik vannak
                # feltelepítve"*). Eddig ezek a sorok csak a nevet és a "Naprakész" szót
                # mutatták, vagyis a szken 91 eszközből 89-ről semmi érdemit nem mondott -
                # pedig a `inst_info` már a memóriában van (a találatok "telepítve: X"
                # cimkéjéhez amúgy is lekérdeztük), tehát ez nulla extra munka.
                pool_hwids = {p.get('hwid') for p in self.hw_updates_pool}
                self._hw_installed_devs = []
                for dev in devices_to_check:
                    if dev['id'] in pool_hwids:
                        continue
                    inst = inst_info.get((dev.get('pnp_id') or '').upper()) or {}
                    self._hw_installed_devs.append({
                        **dev,
                        'installed_version': inst.get('version') or '',
                        'installed_date': inst.get('date') or '',
                        'installed_provider': inst.get('provider') or '',
                        'installed_inf': inst.get('inf') or '',
                    })
                # SZÁNDÉKOSAN NINCS "is_inbox" JELÖLŐ EZEKEN A SOROKON. Kézenfekvő lenne
                # (az `_is_inbox_driver` egy hívás innen), de az a 2026-09-03-án TÖRÖLT
                # "N eszköz fut a Windows beépített driverén" funkció visszacsempészése
                # lenne más néven - lásd a payload-építésnél az indoklást. A gyártónév
                # (`installed_provider`) amúgy is kimondja: ha ott "Microsoft" áll, az
                # eszköz a Windows saját driverén fut. Ez tény a telepített driverről,
                # nem külön teendő-lista.

                # PROBLÉMÁS ESZKÖZÖK: hibakódos eszközök kiemelése, hogy sose maradjon
                # észrevétlen lyuk - akkor is látszik, ha egyik forrás sem adott rá drivert.
                problems = []
                for dev in devices_to_check:
                    code = dev.get('err_code') or 0
                    if not code:
                        continue
                    problems.append({
                        'name': dev['name'], 'hwid': dev['id'], 'code': code,
                        'pnp_id': dev.get('pnp_id', ''),
                        'desc': PNP_ERROR_CODE_DESCRIPTIONS.get(code, f'Hibakód: {code}'),
                        # MIT KELL VELE CSINÁLNI (2026-09-03): a leírás megmondja, MI a baj,
                        # a technikusnak viszont az kell, hogy MIT tegyen. Ismeretlen kódnál
                        # üres marad - egy általános semmitmondás rosszabb, mint a hallgatás.
                        'remedy': PNP_ERROR_CODE_REMEDIES.get(code, ''),
                        'cat': dev.get('cat') or '',
                        'has_fix': dev['id'] in pool_hwids,
                    })
                if problems:
                    logging.info(f"[HW_SCAN] Problémás eszközök: {[(p['name'], p['code'], p['has_fix']) for p in problems]}")

                elapsed = int(time.monotonic() - _start)
                _m, _s = divmod(elapsed, 60)
                time_str = f"{_m} perc {_s} mp" if _m else f"{_s} mp"
                mode = "WU API" if self.wu_api_mode else "Katalógus"
                found = len(self.hw_updates_pool)
                final_sys = f"{sys_info_text} | ✅ Kész ({mode})! {found} frissítés ({total_devs} eszköz)"

                # A KIHAGYOTT TÁROLÓ-/FIRMWARE-ESZKÖZÖK MÁR NEM MENNEK KI A KÉPERNYŐRE
                # (2026-09-03, explicit user decision: *"azt se írja ki nekem h a tároló és
                # firmware driverek kihagyva, senkit se érdekel minek van ott"*). VISSZAVONJA
                # a 2026-08-28-i szabályt, ami szerint a kihagyott darabszámnak látszania kell
                # ("különben a 'miért nem talált az SSD-mhez drivert?' hibának tűnik").
                #
                # MIÉRT VÁLLALHATÓ A VISSZAVONÁS: 2026-09-02 óta ez nem a technikus egy
                # elfelejtett pipája, hanem a program RÖGZÍTETT szabálya - nincs is hozzá
                # kapcsoló, tehát nincs mit "észrevennie". A régi szöveg tehát egy olyan
                # döntést magyarázott minden szken végén, amit senki nem hozott meg.
                # A NAPLÓBAN VÁLTOZATLANUL BENNE VAN (`filter_autofix_risky_devices` minden
                # kizárt eszközt névvel logol `[HW-SCAN]` cimkével), tehát egy terepi
                # jelentésből a kérdés továbbra is megválaszolható - csak a képernyőt nem
                # terheli. A `skipped_risky` mező ezért kikerült a payloadból is: egy senki
                # által nem olvasott mező pont az az élőnek látszó holt kód, amit ez a
                # projekt máshol is irt.

                # MI MARADT A WINDOWS BEÉPÍTETT (INBOX) DRIVERÉN?
                #
                # >>> EZ 2026-09-03 ÓTA CSAK A NAPLÓBA MEGY, A FELÜLETRE NEM. <<<
                # Explicit user decision: *"ez hogy mennyi eszköz fut a beépített driveren
                # ez se kell ki lehet onnan törölni a faszba"*. VISSZAVONJA a 2026-08-31-i
                # kérést (*"írja már ki a program hogy hány driver fut a windows beépített
                # generikus driverén"*), ami ugyanettől a felhasználótól jött - a szken
                # nézete időközben öt külön dobozzá hízott, és ez volt az egyik, ami a
                # valódi teendőt (a találati lista) kiszorította a képernyőről.
                #
                # A SZÁMÍTÁS ÉS A NAPLÓZÁS SZÁNDÉKOSAN MARAD, a `hw_scan_result` payloadból
                # viszont az `inbox` mező KIKERÜLT (a felület nem rendereli). Miért éri meg
                # így: a "mely eszközök nem kapnak gyári drivert?" a projekt egyik
                # visszatérő terepi kérdése, és a válasz e nélkül a naplósor nélkül egy
                # visszaadott gépen már megválaszolhatatlan lenne (Rule 0). A költsége
                # ~nulla: memóriabeli szűrés a már meglévő listákon, hálózat és
                # alfolyamat nélkül. Az 1 kattintásos fix ZÁRÓ JELENTÉSE változatlanul
                # KIÍRJA ezt a képernyőre - ott a technikus a lánc végén áll, és pont az
                # a "mi maradt" pillanata; a kézi szkennél viszont a találati lista a
                # lényeg.
                #
                # UGYANAZ A SZŰRŐ, amit a záró jelentés használ (`_health_report_worth_listing`,
                # app/gui/autofix.py) - két külön lista előbb-utóbb ellentmondana egymásnak
                # ugyanarról a gépről. A szűrő SZÁNDÉKOSAN szűkebb, mint a keresésé: a
                # katalógust mindenre megkérdezzük, de csak azt naplózzuk külön, amivel a
                # szerelőnek TEENDŐJE lehet. Mérve (dev gép): 98 eszközből 71 fut inbox
                # driveren, és ebből 3 az érdekes.
                inbox_worth, inbox_by_design = [], 0
                for dev in devices_to_check:
                    if dev.get('err_code'):
                        continue        # a hibakódosakat a "Problémás eszközök" listázza
                    inst = inst_info.get((dev.get('pnp_id') or '').upper()) or {}
                    if not inst or not _is_inbox_driver(inst):
                        continue
                    # `pkg_devices` nélkül hívjuk (alapértelmezés: None), tehát a beépített
                    # HID/beviteli eszközök itt NEM kerülnek a listára. Szándékos: ez a
                    # számítás csak a naplóba megy (a felületről 2026-09-03-án kikerült),
                    # és a "van-e hozzá stage-elt gyári csomag" felmérés egy `dism`-hívás -
                    # egy naplósorért nem éri meg. A lánc záró jelentése átadja.
                    if self._health_report_worth_listing(dev, inst):
                        inbox_worth.append({
                            'name': dev.get('name') or dev.get('id'),
                            'cat': dev.get('cat') or '', 'hwid': dev.get('id') or '',
                            'inf': inst.get('inf') or '', 'provider': inst.get('provider') or '',
                            # Van-e rá MOST találat a listán? Ha igen, a technikusnak
                            # nincs teendője: elég feltelepíteni, amit a szken talált.
                            'has_offer': (dev.get('id') in pool_hwids),
                        })
                    else:
                        inbox_by_design += 1
                logging.info(f"[HW_SCAN] Windows-alapdriveren: {len(inbox_worth) + inbox_by_design} "
                             f"eszköz ({len(inbox_worth)} érdemi, {inbox_by_design} ehhez gyári "
                             f"driver nem is létezik). Érdemiek: {[i['name'] for i in inbox_worth]}")

                self.emit('hw_scan_result', {
                    'pool': self.hw_updates_pool, 'installed': self._hw_installed_devs,
                    'problems': problems, 'sys_info': final_sys, 'time': time_str,
                    # A FEJLÉC-CSÍK STRUKTURÁLT ADATAI (2026-09-03). A `sys_info` egyetlen,
                    # egyre hosszabbra toldott mondat volt ("gép | kész | N frissítés | ⛔
                    # kihagyva..."), amiből a felület csak egy csíkot tudott csinálni - és
                    # a gépnév ráadásul MÉGEGYSZER megjelent a gyártói kártyán is. A nézet
                    # most külön mezőkből rakja össze az összefoglalót, a gyártói link
                    # pedig ugyanabba a csíkba olvad be, tehát a gépnév egyszer szerepel.
                    'machine': sys_info_text, 'dev_count': total_devs, 'mode': mode,
                    # A felület ebből tudja kiírni, hogy a szűk eredmény a GYORS MÓD
                    # következménye, nem hiány. `wu_failed` mellé téve különösen fontos:
                    # a kettő együtt azt jelenti, hogy egyik forrás sem futott.
                    'catalog_skipped': (not use_catalog),
                    'wu_failed': (not wu_api_success),
                })
                self._hw_loaded = True

                # A VIDEOKÁRTYA-GYÁRTÓI ELLENŐRZÉSEK (NVIDIA/AMD/Intel) ITT VOLTAK, ÉS
                # TELJESEN KIKERÜLTEK (explicit user decision, 2026-09-02): *"ez a
                # videokartya cucc nem is kell a programba, a program amugyis felrak egy
                # videokartya drivert igyis ugyis"*. Törölve `app/gui/nvidia.py` és
                # `app/gui/vendorgpu.py`, a mixinek mindkét API-osztályból, a CLI
                # menüpontjai. AMIT EZZEL ELENGEDTÜNK, TUDVA: az NVIDIA-ág nem csak
                # linkelt, hanem le is töltötte és csendben telepítette a gyári drivert,
                # ami jellemzően hónapokkal újabb a WU-énál (mért: GT 710 -> 475.14,
                # RTX 3060 -> 610.74) - NVIDIA-s gépen mostantól a WU/katalógus verziója
                # marad. Az Intel-ág amúgy is halott volt: az intel.com 403 Forbidden-t
                # ad minden úton (mérve 2026-09-02, PowerShell-fallbackkel is).
                # ...ÉS 2026-09-18-ÁN A GÉP/ALAPLAP-GYÁRTÓI LINK-KÁRTYA IS KIKERÜLT
                # (explicit user decision): *"az a gomb nem kell teljesen feleslegesen van
                # ott, szedd ki, nem kell semmilyen alaplapnak se h elvigyen a gyartoi
                # oldalra, a program megtalal minden frissítést a géphez"*. Törölve
                # `app/gui/oemdrivers.py` (vele az `open_vendor_driver_page`, ami
                # 2026-09-02-kor épp azért költözött ide a vendorgpu.py-ból), a mixin
                # mindkét API-osztályból, a CLI menüpontja, és a felület három eleme
                # (gomb, `openVendorPage`, `renderOemCard`).
                #
                # AMIT EZZEL TUDVA ELENGEDTÜNK: a gyártói oldalon lévő csomag néha
                # frissebb a katalógusénál, van hozzá vezérlőpult/segédszoftver, és marad
                # pár eszköz (BIOS-segédek, RGB/ventilátor-vezérlés), amire tényleg nincs
                # katalógus-csomag - azokhoz mostantól kézzel kell a gyártó oldalára menni.
                # A felhasználó ezt ismerve döntött így: ugyanezen a gépen (ASRock B450M)
                # a lánc 2026-09-04 óta megtalálja és fel is rakja a gyári hang- és
                # LAN-drivert, tehát a link a gyakorlatban már nem az egyetlen forrás.
                # Nyereség: eggyel kevesebb WMI-lekérdezés minden szken végén.
                #
                # A záró jelentés `no_source` ága TOVÁBBRA IS a gyártó driver-oldalára
                # irányít - az szöveg, nem link, és ott valóban az a teendő.
            except Exception as e:
                logging.error(f"hw_scan crash: {e}")
                logging.error(traceback.format_exc())
                self.emit('hw_scan_progress', {'status': '❌ Hiba történt!'})
                self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Scan hiba', 'time': ''})
            finally:
                self._hw_scanning = False
                self._task_busy = None

        try:
            threading.Thread(target=worker, daemon=True, name="hw-scan").start()
        except Exception as e:
            logging.error(f"[HW_SCAN] Thread indítási hiba: {e}")
            self._hw_scanning = False
            self._task_busy = None
            self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Thread hiba', 'time': ''})

    def _search_wu_api(self):
        logging.info("[WU_API] _search_wu_api() indult...")
        try:
            ps_cmd = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try {
        $SM = New-Object -ComObject Microsoft.Update.ServiceManager
        $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null
    } catch {}
    $Searcher.ServerSelection = 3
    $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    $updates = @()
    foreach ($U in $Result.Updates) {
        $dvd = ''; try { $dvd = ([datetime]$U.DriverVerDate).ToString('yyyy-MM-dd') } catch {}
        $updates += [PSCustomObject]@{
            Title = $U.Title; DriverModel = $U.DriverModel; HardwareID = $U.DriverHardwareID
            DriverClass = $U.DriverClass; DriverProvider = $U.DriverProvider
            UpdateID = $U.Identity.UpdateID; Size = $U.MaxDownloadSize; DriverVerDate = $dvd
        }
    }
    if ($updates.Count -eq 0) { Write-Output "[]" }
    else { $updates | ConvertTo-Json -Depth 2 -Compress }
} catch {
    # A HIBAKÓD A LÉNYEG, ÉS A `Write-Error` ELTAKARTA (2026-09-07, terepi naplóból).
    # A Write-Error a hibás parancs KONTEXTUSÁT is kiírja - a napló stderr-jébe így a
    # TELJES szkript bekerült (~30 sor zaj), a végén egyetlen érdemi mondattal:
    # "A kivétel HRESULT-értéke: 0x80240438". A program pedig csak annyit mondott a
    # technikusnak, hogy "időtúllépés vagy WUA hiba" - miközben a pontos ok ott volt.
    # A [Console]::Error.WriteLine nem fűz hozzá kontextust, a kódot pedig strukturáltan,
    # gépi úton is olvashatóan adjuk vissza.
    # A `-f 'X8'` a NEGATÍV int32-t is helyesen, kettes komplemensben formázza
    # (-2145123272 -> 80240438), tehát nincs szükség maszkolásra. Élőben mérve: a
    # kézenfekvőnek tűnő `[uint32]($h -band 0xFFFFFFFF)` ELSZÁLL, mert a PowerShell a
    # `0xFFFFFFFF` literált int32-ként -1-nek veszi, így a maszk nem csinál semmit, és
    # az uint32-konverzió a negatív értéken kivételt dob - ami épp azt a kontextus-zajt
    # gyártaná vissza, ami miatt ez a blokk átíródott.
    $h = 0
    try { $h = $_.Exception.HResult } catch {}
    $hex = ('0x{0:X8}' -f $h)
    [Console]::Error.WriteLine("WUERROR|$hex|" + $_.Exception.Message)
}
"""
            res = self._run(["powershell", "-NoProfile", "-Command", ps_cmd],
                            timeout=WU_SEARCH_TIMEOUT, encoding='utf-8')
            out = res.stdout.strip()
            if not out and res.stderr:
                # A HIBAKÓDOT KIMONDJUK - eddig egy 200 karakterre vágott, kontextussal
                # teli stderr-részlet ment a naplóba, amiben a lényeg (a HRESULT) épp
                # nem fért bele. A `self._wu_search_error` a hívó ágaknak szól, hogy a
                # KÉPERNYŐN is a konkrét ok jelenjen meg, ne csak "WUA hiba".
                code, hint = wu_search_error_text(res.stderr)
                self._wu_search_error = (code, hint)
                if code:
                    logging.error(f"[WU_API] A WU-keresés hibakóddal állt le: {code}"
                                  f"{(' - ' + hint) if hint else ''}")
                else:
                    logging.warning(f"[WU_API] A WU-keresés hiba nélküli kód nélkül bukott el. "
                                    f"Stderr: {res.stderr[:400]}")
                return None
            if out:
                data = json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                logging.info(f"[WU_API] Talált frissítések: {len(data) if isinstance(data, list) else 0}")
                return data if isinstance(data, list) else None
        except subprocess.TimeoutExpired:
            logging.error(f"[WU_API] WU API timeout ({WU_SEARCH_TIMEOUT}s) - szolgáltatás-újraindítás, "
                          f"majd azonnali továbblépés (nincs második keresési kör)...")
            self.emit('hw_scan_progress', {'status': f'⚠️ A Windows Update nem válaszolt ({WU_SEARCH_TIMEOUT // 60} perc) — áttérés a katalógusra', 'detail': ''})
            # A 'autofix' csatornára CSAK akkor írunk, ha tényleg AutoFix fut: kézi
            # szkennelésnél ez a sor a logban ([EMIT:]) az AutoFix-hez tartozónak látszott,
            # és egy terepi bejelentés kivizsgálásakor pont ez viszi félre a nyomot.
            if getattr(self, '_task_busy', None) == 'autofix' or getattr(self, 'resume_mode', False) or getattr(self, 'resume_step1', False):
                self.emit('task_progress', {'task': 'autofix', 'log': '⚠️ Windows Update API időtúllépés! Szolgáltatások újraindítása...'})

            # A WU szolgáltatások újraindítása a GÉPET gyógyítja (a következő keresés már
            # jó eséllyel másodpercek alatt lefut), de az EREDMÉNYRE itt már nem várunk újra.
            reset_ps = r"""
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Stop-Service bits -Force -ErrorAction SilentlyContinue
            Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
            Start-Service cryptsvc -ErrorAction SilentlyContinue
            Start-Service bits -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", reset_ps])
            # SZÁNDÉKOSAN NINCS újrapróbálkozás (korábban volt még egy teljes keresési kör):
            # terepen bizonyított (klónozott rendszer vadonatúj AM5 hardveren, 2026-07, két
            # egymás utáni szkennél is), hogy a szolgáltatás-újraindítás utáni retry ugyanúgy
            # időtúllépésbe fut - a felhasználó ~10,5 percet várt ~5,5 helyett, nulla
            # többlet-eredményért. A None visszatérésre a hívók maguktól váltanak: a manuális
            # szken a katalógus-fallbackre (start_hw_scan), az AutoFix a kör lezárására.
        except Exception as e:
            logging.error(f"[WU_API] WU API error: {e}")
        return None

    def _get_installed_driver_info(self):
        """A jelenleg telepített driverek verziója ÉS dátuma eszközönként
        (Win32_PnPSignedDriver): UPPER(eszköz instance ID) -> {'version': str, 'date':
        'yyyy-MM-dd', 'provider': str, 'inf': str} map. Fogyasztói: a katalógus-fallback már-telepítve szűrése, a
        találatok melletti "telepítve: X" kijelzés, és az AutoFix downgrade-védelme
        (wu_core._filter_wu_downgrades). A WU API útnál a szerver maga szűr az
        IsInstalled=0 feltétellel (terepen látott hiba e nélkül: a 3 perccel korábban
        telepített Realtek LAN drivert a következő szken újra felajánlotta).
        Hiba esetén üres map-pel (szűrés nélkül) folytatjuk - inkább ajánljunk fel egy már
        meglévő drivert, mint hogy elrejtsünk egy hiányzót."""
        info = {}
        try:
            # DriverProviderName + InfName is kell: ebből derül ki, hogy a jelenlegi driver
            # egy Windows-beépített (inbox) generikus-e. A downgrade-védelem ezt használja -
            # egy inbox driver "újabb dátuma" nem lehet indok a gyári csomag eldobására
            # (wu_core._filter_wu_downgrades).
            ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                  "Get-WmiObject Win32_PnPSignedDriver | Where-Object { $_.DeviceID -and $_.DriverVersion } | "
                  "Select-Object DeviceID, DriverVersion, DriverDate, DriverProviderName, InfName | ConvertTo-Json -Compress")
            res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=120)
            data = json.loads(res.stdout) if res and res.stdout.strip() else []
            if isinstance(data, dict):
                data = [data]
            for d in data:
                did = (d.get('DeviceID') or '').upper()
                if not did:
                    continue
                # DriverDate WMI CIM_DATETIME formátumban jön: "20230115000000.000000+000"
                raw_date = str(d.get('DriverDate') or '')
                date = ''
                if len(raw_date) >= 8 and raw_date[:8].isdigit():
                    date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                info[did] = {'version': d.get('DriverVersion') or '', 'date': date,
                             'provider': d.get('DriverProviderName') or '',
                             'inf': d.get('InfName') or ''}
            logging.info(f"[CATALOG] Telepített driver-infó: {len(info)} eszköz")
        except Exception as e:
            logging.warning(f"[CATALOG] Telepített driver-infó lekérdezése sikertelen (verzió-szűrés nélkül folytatjuk): {e}")
        return info

    def _catalog_row_score(self, row_text_lower):
        """Egy katalógus-találati sor pontozása az AKTUÁLIS rendszerhez illés szerint (a
        sor teljes szövege alapján, ami a Products oszlopot is tartalmazza). A katalógus
        ugyanarra a HWID-re Windows 10/11/Server és amd64/arm64 sorokat is visszaad; a
        puszta "legmagasabb verzió" választás korábban rossz OS-hez/architektúrához
        tartozó csomagot is kiválaszthatott (a pnputil ezt ugyan visszadobta, de az
        eszköz "sikertelen telepítés"-ként végezte egy amúgy megtalálható driver helyett).
        None = kizárt sor (biztosan nem alkalmazható); egyébként minél nagyobb, annál jobb.
        A katalógus-szken csak élő rendszeren fut (start_hw_scan offline-t elutasít),
        ezért a host OS/architektúra a mérce."""
        t = row_text_lower
        machine = (platform.machine() or '').upper()
        if 'arm64' in t and not machine.startswith('ARM'):
            return None
        build = getattr(sys.getwindowsversion(), 'build', 0)
        if build >= 22000:  # Windows 11 host
            if 'windows 11' in t:
                return 3
            if 'windows 10' in t and 'later' in t:
                return 2  # "Windows 10 and later drivers" - Win11-re is érvényes, ez a leggyakoribb driver-sor
            if 'windows 10' in t:
                return 1
            if 'server' in t:
                return 0
            return 1
        else:  # Windows 10 host
            if 'windows 10' in t:
                return 3
            if 'windows 11' in t:
                return None  # Win11-only csomag Win10-re nem applikálható
            if 'server' in t:
                return 0
            return 1

    @staticmethod
    def _catalog_parse_rows(html):
        """A találati lap sorainak kibontása: [(guid, cím, sor_szöveg_kisbetűs, dátum_iso)].
        A sor_szöveg a teljes <tr> tag-mentesítve (Products oszloppal, a pontozáshoz), a
        dátum a "Last Updated" oszlopból (m/d/yyyy -> yyyy-MM-dd). Külön (statikus)
        függvény, hogy egy elmentett lapon offline is tesztelhető legyen."""
        rows = []
        for row_m in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.S):
            row_html = row_m.group(1)
            link = re.search(r"id=['\"]([a-fA-F0-9\-]+)_link['\"][^>]*>(.*?)</a>", row_html, re.S)
            if not link:
                continue
            guid = link.group(1)
            title = ' '.join(re.sub(r'<[^>]+>', ' ', link.group(2)).split())
            row_text = ' '.join(re.sub(r'<[^>]+>', ' ', row_html).split())
            date_iso = ''
            dm = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', row_text)
            if dm:
                date_iso = f"{dm.group(3)}-{int(dm.group(1)):02d}-{int(dm.group(2)):02d}"
            rows.append((guid, title, row_text.lower(), date_iso))
        return rows

    @staticmethod
    def _catalog_row_size_mb(row_text):
        """A csomag mérete MB-ban a találati sor szövegéből, vagy None.

        A katalógus táblázatának van egy Size oszlopa ("11.2 MB", "1.2 GB"), és a
        `row_text` a teljes `<tr>` tag-mentesítve - tehát a méret INGYEN megvan, még a
        letöltés (sőt a DownloadDialog-hívás) ELŐTT. Erre épül a méret-alapú döntés:
        egy 11 MB-os hangdrivert érdemes kipróbálni akkor is, ha valószínűleg más
        gépgyártóé, egy 1,2 GB-os videokártya-csomagot viszont nem.

        A dátum-mintát (m/d/yyyy) szándékosan nem zavarja: külön egységet keresünk."""
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*(kb|mb|gb)\b', row_text or '', re.IGNORECASE)
        if not m:
            return None
        try:
            ertek = float(m.group(1).replace(',', '.'))
        except ValueError:
            return None
        return {'kb': ertek / 1024.0, 'mb': ertek, 'gb': ertek * 1024.0}[m.group(2).lower()]

    # ------------------------------------------------------------------
    # A katalógus-sorok gyorsítótára (lásd CATALOG_ROWS_TTL fenti indoklását)
    # ------------------------------------------------------------------
    def _catalog_rows_store_path(self):
        return os.path.join(_app_data_dir(), 'catalog_rows.json')

    def _catalog_rows_cache(self):
        """A lemezről egyszer beolvasott gyorsítótár {HWID: {'t': idő, 'rows': [...]}}.

        A hívó a `_CATALOG_ROWS_LOCK`-ot tartja: `_catalog_fetch_rows` 10 szálon fut."""
        cache = getattr(self, '_cat_rows', None)
        if cache is not None:
            return cache
        cache = {}
        try:
            with open(self._catalog_rows_store_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cache = data
            logging.info(f"[CATALOG] Katalógus-gyorsítótár beolvasva: {len(cache)} HWID "
                         f"({self._catalog_rows_store_path()}).")
        except FileNotFoundError:
            pass
        except Exception as e:
            # Nem hiba: gyorsítótár nélkül a régi viselkedést kapjuk (minden lekérdezés kimegy).
            logging.debug(f"[CATALOG] catalog_rows.json nem olvasható: {e}")
        self._cat_rows = cache
        self._cat_rows_dirty = False
        return cache

    def _catalog_rows_flush(self):
        """A gyorsítótár kiírása. A katalógus-kör VÉGÉN hívjuk, nem lekérdezésenként:
        egy kör 200-950 találatot ad, körönként egy írás bőven elég."""
        with _CATALOG_ROWS_LOCK:
            cache = getattr(self, '_cat_rows', None)
            if not cache or not getattr(self, '_cat_rows_dirty', False):
                return
            try:
                # Túlcsordulásnál a LEGRÉGEBBI bejegyzések esnek ki - azok a legkevésbé
                # frissek, tehát úgyis ők járnának le leghamarabb.
                if len(cache) > CATALOG_ROWS_MAX:
                    keep = sorted(cache.items(), key=lambda kv: kv[1].get('t', 0),
                                  reverse=True)[:CATALOG_ROWS_MAX]
                    cache = dict(keep)
                    self._cat_rows = cache
                with open(self._catalog_rows_store_path(), 'w', encoding='utf-8') as f:
                    json.dump(cache, f)
                self._cat_rows_dirty = False
                logging.info(f"[CATALOG] Katalógus-gyorsítótár kiírva: {len(cache)} HWID.")
            except Exception as e:
                # A gyorsítótár kényelmi funkció: ha nem megy, a keresés attól még helyes.
                logging.debug(f"[CATALOG] catalog_rows.json nem írható: {e}")

    def _catalog_fetch_rows(self, hwid, ssl_ctx, max_pages=CATALOG_MAX_PAGES, deep=False):
        """Egy HWID katalógus-keresése, DÁTUM SZERINT CSÖKKENŐ sorrendben, szükség esetén
        LAPOZVA. Visszatérés: [(guid, cím, sor_szöveg_kisbetűs, dátum_iso)].

        GYORSÍTÓTÁRAZOTT (2026-08-31): ugyanarra a HWID-re a szerver válasza órákon belül
        nem változik, a lánc viszont MINDEN LÁBON újra végigkérdezi ugyanazt a ~190
        eszközt. Mérve: 952 lekérdezésből 758 (80%) szó szerinti ismétlés, ~11 perc.
        Részletes indoklás a CATALOG_ROWS_TTL konstansnál.

        MIÉRT NEM ELÉG EGY LAPOT LEKÉRNI (terepi log + élő mérés, 2026-08-06):
        a katalógus laponként 25 sort ad, a régi kód pedig egyetlen, RENDEZETLEN lapot
        kért le - vagyis a döntésünk azon a 25 soron állt, amit a szerver épp elsőnek
        adott. Mérve ugyanezen a gépen:
            PCI\\VEN_10DE&DEV_2504            -> "1 - 25 of 546"   (22 lap)
            HDAUDIO\\FUNC_01&VEN_10EC&DEV_0892 -> "1 - 25 of 1000"  (40 lap)
        tehát a videokártyára a csomagok 4,6%-át láttuk. Rosszabb: a rendezetlen lap
        ÖSSZETÉTELE nem állandó. A 2026-08-05-i futásban ugyanarra a hangeszközre, ugyanazzal
        a 3 HWID-del, négy körben KÉT KÜLÖNBÖZŐ nyertes jött ki (6.0.9992.1 [2026-05-18]
        háromszor, 6.0.10007.1 [2026-06-22] egyszer) - a nálunk 5 héttel frissebb csomag
        háromszor egyszerűen nem került bele a mintába. Vagyis a "legfrissebb dátum nyer"
        szabály helyes volt, csak nem a teljes listára alkalmaztuk.

        A megoldás nem 22 lap letöltése (91 eszköz × 4 HWID mellett az kezelhetetlen),
        hanem a SZERVER OLDALI RENDEZÉS: a lapozás egyszerű GET (`&p=<0-alapú lapindex>`),
        a rendezés `&scol=DateComputed&sdir=desc` - mindkét paraméternév a katalógus saját
        SiteConstants.aspx-éből való (QueryStringPageIndex='p', QueryStringSortColumn='scol').
        Így az 1. lap MÁR a 25 legfrissebb sort tartalmazza, ami pontosan az, amire a
        dátum-elsődlegű döntésnek szüksége van. Következő lapot csak akkor kérünk, ha a
        legfrissebb dátumú csoport ÁTLÓG a lap végén (különben a holtverseny egy részét
        nem látnánk) - a mért két esetben ez 1 lapot jelent.

        A rendezés determinisztikus: háromszor egymás után lekérve azonos az eredmény
        (mérve). Ha a rendezett kérés bármiért elhasal, visszaesünk a rendezetlenre -
        az a régi viselkedés, nem rosszabb a mainál."""
        import urllib.request, urllib.parse
        # A gyorsítótár csak az ALAPÉRTELMEZETT lapszámra érvényes: más max_pages más
        # sorhalmazt adna, és egy szűkebb találatot nem szabad bőségesként visszaadni.
        # A gyorsítótár kulcsa tartalmazza a LEKÉRÉS MÉLYSÉGÉT is: egy sekélyebb
        # lekérdezés eredményét nem szabad bőségesként visszaadni egy mélyebb kérésre.
        cache_key = f"{(hwid or '').upper()}|{max_pages}|{1 if deep else 0}"
        # VALÓDI IDŐBÉLYEG, ezért time.time() és nem monotonic: a tár FOLYAMATOKON ÁTÍVELŐ
        # (a lánc minden lába külön folyamat), márpedig a monotonic óra folyamatonként
        # nullázódik. Ugyanaz az eset, mint a gui/nvidia.py fájl-mtime frissességénél.
        # Óraugrás legrosszabb esetben egy fölösleges lekérdezést jelent, hibát nem.
        now = time.time()
        if cache_key:
            with _CATALOG_ROWS_LOCK:
                ent = self._catalog_rows_cache().get(cache_key)
                if ent and 0 <= (now - ent.get('t', 0)) < CATALOG_ROWS_TTL:
                    rows = [tuple(r) for r in (ent.get('rows') or [])]
                    logging.debug(f"[CATALOG] Gyorsítótárból: {hwid} -> {len(rows)} sor "
                                  f"({(now - ent['t']) / 60:.0f} perce kérdeztük le).")
                    return rows
        base = ('https://www.catalog.update.microsoft.com/Search.aspx?q='
                + urllib.parse.quote(hwid))

        def fetch(url):
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            return urllib.request.urlopen(req, context=ssl_ctx, timeout=30).read().decode('utf-8')

        try:
            html = fetch(base + CATALOG_SORT_QS)
            sorted_ok = True
        except Exception as e:
            # A rendezés elvesztése nem végzetes: a régi (rendezetlen) viselkedést kapjuk.
            logging.debug(f"[CATALOG] A rendezett lekérdezés elhasalt ({hwid}): {e} - rendezetlenül próbáljuk.")
            html = fetch(base)
            sorted_ok = False

        rows = self._catalog_parse_rows(html)
        total_m = re.search(r'(\d+)\s*-\s*(\d+)\s+of\s+(\d+)', html)
        total = int(total_m.group(3)) if total_m else len(rows)
        pages, stop = 1, 'egy lap elég'
        if sorted_ok and rows:
            top_date = rows[0][3] or ''
            # Amíg a lap UTOLSÓ sora is a legfrissebb dátumú csoportba tartozik, a csoport
            # átlóghat a következő lapra - azt is le kell kérni, különben a holtverseny
            # egy részét (és talán épp az eszközhöz valót) nem is látjuk.
            # A `deep` ág a DÁTUM-HOLTVERSENY feltételt hagyja el. Alaphelyzetben csak
            # akkor kérünk következő lapot, ha a legfrissebb dátumú csoport ÁTLÓG a lap
            # végén - és ez pont ott állítja meg a lapozást, ahol a legtöbbet érne:
            # mérve az ASRock ALC892-n a saját SUBSYS-kulcs **41 sort** ad 2 lapon, az
            # 1. lap viszont 2019-03-04 .. 2021-03-22 (különböző dátumok), tehát a
            # program a 41-ből csak 25-öt látott. Ez az egyetlen ág, ahol a régebbi
            # sorok érnek is valamit: Windows-alapdriveren futó eszköz SAJÁT, gyártó-
            # specifikus kulcsán MINDEN sor ehhez a géphez való, csak régebbi kiadás -
            # márpedig a régi gyári driver jobb a generikusnál (explicit user decision).
            # Az általános kulcsra SOSEM mélyítünk: ott 1000 sor van, mind más gyártó
            # OEM-változata, tehát csak kérésekbe kerülne (ezt a fájl korábban is mérte).
            while (pages < max_pages and len(rows) >= CATALOG_PAGE_SIZE * pages
                   and (deep or (rows[-1][3] or '') == top_date) and len(rows) < total):
                try:
                    more = self._catalog_parse_rows(fetch(f"{base}{CATALOG_SORT_QS}&p={pages}"))
                except Exception as e:
                    stop = f'a {pages + 1}. lap nem jött le ({e})'
                    break
                if not more:
                    stop = f'a {pages + 1}. lap üres'
                    break
                seen = {r[0] for r in rows}
                rows += [r for r in more if r[0] not in seen]
                pages += 1
            else:
                if pages >= max_pages:
                    stop = f'lapkorlát ({max_pages})'
        # LOGOLÁS: ez egy FORRÓ ÚT - eszközönként max 4 hívás, 126 eszközre és 2 körre
        # ez négyszáznál is több sor. Terepen mérve (2026-08-06, Build 264) 448 ilyen sor
        # keletkezett, amiből 412 "-> 0 sor" volt, azaz 92% tartalmatlan zaj a forgó
        # logban (lásd CLAUDE.md: a maximális logolás maximális INFORMÁCIÓT jelent, nem
        # maximális sorszámot). A nulla találat amúgy sem vész el: közvetlenül a hívás
        # ELŐTT kimegy a "[CATALOG] Keresés: <eszköz> (<hwid>)" sor a _catalog_find_driver
        # -ből, a végén pedig a "Döntés:" összegzés; Döntés-sor hiánya = nulla sor minden
        # kulcsra (CLAUDE.md 8. lépés). FIGYELEM: ez a mondat 2026-09-07-ig a [CALL]-
        # rétegre hivatkozott, de az azóta NEM csomagolja ezt a függvényt (mérve: 260 KB
        # / 1792 sor egyetlen láncban, lásd common._CALL_LOG_EXCLUDE) - a bizonyíték
        # tehát a Keresés-sor, ne töröld azt a _catalog_find_driver-ből.
        # Marad tehát: van találat (DEBUG), illetve a ritka és érdekes esetek (INFO):
        # ha ténylegesen lapoztunk, vagy ha a rendezés kiesett.
        if rows or pages > 1 or not sorted_ok:
            msg = (f"[CATALOG] Lekérdezés: {hwid} -> {len(rows)} sor "
                   f"(a katalógusban összesen {total}, {pages} lap, "
                   f"{'dátum szerint csökkenő' if sorted_ok else 'RENDEZETLEN'}; {stop})")
            (logging.info if (pages > 1 or not sorted_ok) else logging.debug)(msg)
        # Eltesszük - de CSAK a rendezett (teljes értékű) választ. Egy rendezetlen
        # tartalék-lekérdezés a régi, gyengébb mintát adja; azt hat órára bebetonozni
        # rosszabb lenne, mint legközelebb újra megkérdezni.
        if cache_key and sorted_ok:
            with _CATALOG_ROWS_LOCK:
                self._catalog_rows_cache()[cache_key] = {'t': now, 'rows': [list(r) for r in rows]}
                self._cat_rows_dirty = True
        return rows

    def _catalog_detail_page(self, guid, ssl_ctx):
        """Egy katalógus-tétel részletlapja, GUID szerint gyorsítótárazva (memóriában).

        MIÉRT KÖZÖS (2026-09-03): ugyanezt a lapot kérdezi a "Driver Model" holtverseny-
        döntő ÉS az új, letöltés előtti alkalmazhatóság-ellenőrzés
        (`_catalog_supported_hwids`) - két külön kérés ugyanarra a 44-86 KB-os oldalra
        pazarlás lenne. A tár a példányon él, tehát egy szken erejéig érvényes: a
        részletlap tartalma amúgy sem változik egy futás alatt."""
        tar = getattr(self, '_catalog_detail_cache', None)
        if tar is None:
            tar = self._catalog_detail_cache = {}
        if guid in tar:
            return tar[guid]
        import urllib.request
        try:
            req = urllib.request.Request(
                'https://www.catalog.update.microsoft.com/ScopedViewInline.aspx?updateid=' + guid,
                headers={'User-Agent': 'Mozilla/5.0'})
            html = urllib.request.urlopen(req, context=ssl_ctx, timeout=30).read().decode('utf-8', 'replace')
        except Exception as e:
            logging.debug(f"[CATALOG] Részletlap nem jött le ({guid}): {e}")
            html = ''
        tar[guid] = html
        return html

    def _catalog_supported_hwids(self, guid, ssl_ctx):
        """A csomag TÁMOGATOTT HARDVER-AZONOSÍTÓI a részletlapról, kisbetűsen.
        `None`, ha a lap nem jött le vagy nincs rajta ilyen szekció (= nem eldönthető).

        EZ A LEGFONTOSABB ADAT, AMIT LETÖLTÉS NÉLKÜL MEG LEHET TUDNI (2026-09-03, élőben
        mérve). A katalógus a TÖRZS-HWID-re (`...&DEV_0221`) más gépgyártók változatait is
        visszaadja, és eddig CSAK a letöltött csomag INF-jéből derült ki, hogy nem ide
        valók - vagyis a program felajánlott valamit, letöltötte, elvetette, majd a
        technikus joggal kérdezte, hogy akkor minek ajánlotta fel. A részletlapon viszont
        ott a pontos lista, `<div id="driverhwIDs">` blokkban. Mérve ezen a gépen
        (HP EliteDesk 800 G2, Realtek ALC221, eszköz: ...&SUBSYS_103C8054):

            HP  6.0.1.8335  ->  44 KB, 1,9 mp,  16 azonosító, a gépé BENNE VAN
            GEN 6.0.9980.1  ->  86 KB, 2,1 mp, 149 azonosító, a gépé NINCS köztük

        Vagyis a "más gépgyártó változata" eset MÁR A KERESÉSNÉL kiszűrhető, letöltés,
        kicsomagolás és INF-vizsgálat nélkül.

        A `None` és az üres lista KÜLÖNBÖZIK, és ez fontos: `None` = nem tudjuk (a lapon
        nincs ilyen szekció, vagy nem jött le) -> SOHA nem vetünk el semmit emiatt, ugyanaz
        az elv, mint az `inf_package_applies` None-jánál. Csak a NEM ÜRES lista alapján
        szabad kizárni."""
        # LEMEZ-GYORSÍTÓTÁR: a lánc lábai külön folyamatok, a `_catalog_detail_page`
        # memóriabeli tára tehát lábanként újraszedeti ugyanazt az 50-70 lapot
        # (lásd CATALOG_HWIDS_TTL indoklását).
        with _CATALOG_HWIDS_LOCK:
            ent = self._catalog_hwids_cache().get(guid)
            if ent and 0 <= (time.time() - ent.get('t', 0)) < CATALOG_HWIDS_TTL:
                ids = ent.get('ids') or None
                if ids:
                    return list(ids)
        html = self._catalog_detail_page(guid, ssl_ctx)
        if not html:
            return None
        m = re.search(r'id="driverhwIDs"[^>]*>(.*?)</div>\s*</div>', html, re.S | re.I)
        if not m:
            return None
        import html as _html
        ids = [_html.unescape(' '.join(x.split())).lower()
               for x in re.findall(r'<div[^>]*>(.*?)</div>', m.group(1), re.S) if x.strip()]
        if ids:
            with _CATALOG_HWIDS_LOCK:
                self._catalog_hwids_cache()[guid] = {'t': time.time(), 'ids': ids}
                self._cat_hwids_dirty = True
        return ids or None

    # ------------------------------------------------------------------
    # A támogatott-azonosító listák gyorsítótára (lásd CATALOG_HWIDS_TTL)
    # ------------------------------------------------------------------
    def _catalog_hwids_store_path(self):
        return os.path.join(_app_data_dir(), 'catalog_hwids.json')

    def _catalog_hwids_cache(self):
        """{GUID: {'t': idő, 'ids': [...]}}. A hívó a `_CATALOG_HWIDS_LOCK`-ot tartja."""
        cache = getattr(self, '_cat_hwids', None)
        if cache is not None:
            return cache
        cache = {}
        try:
            with open(self._catalog_hwids_store_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cache = data
            logging.info(f"[CATALOG] Támogatott-azonosító gyorsítótár beolvasva: {len(cache)} csomag.")
        except FileNotFoundError:
            pass
        except Exception as e:
            logging.debug(f"[CATALOG] catalog_hwids.json nem olvasható: {e}")
        self._cat_hwids = cache
        self._cat_hwids_dirty = False
        return cache

    def _catalog_hwids_flush(self):
        """Kiírás a katalógus-kör VÉGÉN (nem lekérdezésenként), a sor-gyorsítótár mintájára."""
        with _CATALOG_HWIDS_LOCK:
            cache = getattr(self, '_cat_hwids', None)
            if not cache or not getattr(self, '_cat_hwids_dirty', False):
                return
            try:
                if len(cache) > CATALOG_HWIDS_MAX:
                    cache = dict(sorted(cache.items(), key=lambda kv: kv[1].get('t', 0),
                                        reverse=True)[:CATALOG_HWIDS_MAX])
                    self._cat_hwids = cache
                with open(self._catalog_hwids_store_path(), 'w', encoding='utf-8') as f:
                    json.dump(cache, f)
                self._cat_hwids_dirty = False
                logging.info(f"[CATALOG] Támogatott-azonosító gyorsítótár kiírva: {len(cache)} csomag.")
            except Exception as e:
                logging.debug(f"[CATALOG] catalog_hwids.json nem írható: {e}")

    def _catalog_driver_models(self, guid, ssl_ctx):
        """Egy katalógus-tétel részletlapjáról a "Driver Model" mező (a TÁMOGATOTT
        ESZKÖZÖK neve), kisbetűsen. Üres string, ha nincs vagy nem jött le.

        Miért éri meg egy külön kérés: a részletlap pár KB, a csomag viszont akár 1,2 GB.
        A 2026-08-05-i futásban a videokártyára 10 azonos című, azonos dátumú sor volt
        holtversenyben ('NVIDIA Display Driver Update (32.0.15.9595)'), a program vaktában
        vitte el az egyiket, 1,2 GB-ot töltött, és az INF-vizsgálat kiderítette, hogy a
        csomag nem ismeri ezt a kártyát. A részletlap viszont NÉVSZERINT felsorolja:
        "Driver Model: NVIDIA GeForce RTX 3090,...,NVIDIA GeForce RTX 3060,..." - ez az
        egyetlen olyan adat, amiből LETÖLTÉS ELŐTT eldönthető, melyik holtverseny-sor való
        ehhez a géphez."""
        html = self._catalog_detail_page(guid, ssl_ctx)
        if not html:
            return ''
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ',
                                          re.sub(r'<script.*?</script>', '', html, flags=re.S)))
        m = re.search(r'Driver Model:\s*(.*?)\s*(?:Driver Provider|Driver Version|Driver Class|'
                      r'Supported products|Supported languages|Company|Architecture|Classification|$)', text)
        return (m.group(1) if m else '').strip().lower()

    def _catalog_download_url(self, guid, ssl_ctx, name=''):
        """A letöltési link feloldása egy katalógus-tétel GUID-jából (DownloadDialog).
        Külön függvény, mert a holtverseny-tartalék (lásd _install_catalog_sync) is ezen
        keresztül kéri le a KÖVETKEZŐ jelölt URL-jét, amikor az elsőt az INF-vizsgálat
        elvetette. None, ha nem sikerült."""
        import urllib.request
        dl_body = f'updateIDs=[{{"size":0,"languages":"","uidInfo":"{guid}","updateID":"{guid}"}}]'
        dl_req = urllib.request.Request(
            'https://www.catalog.update.microsoft.com/DownloadDialog.aspx',
            data=dl_body.encode('utf-8'),
            headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            dl_html = urllib.request.urlopen(dl_req, context=ssl_ctx, timeout=30).read().decode('utf-8')
        except Exception as e:
            logging.debug(f"[CATALOG] DownloadDialog hiba ({name or guid}): {e}")
            return None
        cab_link = re.search(r'downloadInformation\[0\]\.files\[0\]\.url\s*=\s*[\"\']([^\"\']+)[\"\']', dl_html)
        return cab_link.group(1) if cab_link else None

    @staticmethod
    def _catalog_row_is_microsoft(title):
        """Microsoft saját csomagja-e a katalógus-sor? A katalógusban a cím a
        szolgáltató nevével kezdődik ("Realtek Semiconductor Corp. - MEDIA - ..."),
        külön provider-oszlop nincs. Inbox driver cseréjénél egy Microsoft-csomag
        nem hoz semmit (az van fent), ezért az ilyen sorokat kihagyjuk."""
        return (title or '').strip().lower().startswith('microsoft')

    def _catalog_find_driver(self, item, installed_info, ssl_ctx, known_no_bind=None):
        """Egy eszköz legjobb katalógus-találatának felkutatása.

        known_no_bind: {PNP_ID_NAGYBETŰS: {korábban megbukott katalógus-GUID-ok}} - a
        tartós no-bind tárból, EGYSZER beolvasva a hívóban (nem szálanként/eszközönként).

        Az eszköz ÖSSZES hardver-azonosítóját lekérdezi a legspecifikusabbtól
        (VEN&DEV&SUBSYS&REV) az általánosabbig (VEN&DEV), max 4-et (hálózat-kímélés),
        és a sorokat EGY HALMAZBA gyűjti, abból választ.

        Miért unió, és miért nem áll meg az első találó HWID-nél: mérve (2026-07-24,
        Realtek ALC892) a specifikus és az általános azonosító MÁS csomagot ad, és
        történetesen a specifikus a régebbit:
            HDAUDIO\\...&DEV_0892&SUBSYS_18496893 -> 6.0.9136.1  (2021-03-22)
            HDAUDIO\\...&DEV_0892                 -> 6.0.9992.1  (2026-05-18)
        A régi kód az első találó azonosítónál megállt, tehát a 2021-es drivert
        választotta volna, és a kommentje szerint az általánosabb HWID "ugyanazt adná
        vissza" - ez tévedés volt.

        Visszatérés: pool-elem dict vagy None."""
        hwids, seen_hwid, generic_skipped = [], set(), []
        for h in ([item['id']] if item.get('id') else []) + list(item.get('all_hwids') or []):
            hl = (h or '').strip().lower()
            if not h or hl in seen_hwid:
                continue
            seen_hwid.add(hl)
            # TÍPUSKÓDDAL NEM KÉRDEZÜNK (wu_core.is_specific_hwid). Egy ACPI\PNP0501
            # ("soros port") vagy USB\ROOT_HUB30 kulcsra a katalógus BÁRMELYIK gyártó
            # arra a fajtára szánt csomagját visszaadja - terepen mérve egy Intel gép
            # USB-gyökérhubjára így jött vissza egy AMD-csomag, egy COM-portra pedig egy
            # LG-s. Az eszköz maga NINCS kizárva: a specifikus azonosítóival kérdezzük.
            if not is_specific_hwid(h):
                generic_skipped.append(h)
                continue
            hwids.append(h)
        if not hwids:
            # Nincs egyetlen konkrét azonosító sem - a keresés értelmetlen lenne. Ezt ki
            # KELL írni, különben a "miért nem kapott az X eszköz drivert?" kérdésre nincs
            # válasz a terepi logban (CLAUDE.md Rule 0).
            logging.debug(f"[CATALOG] Kihagyva (csak típuskódos azonosítói vannak, "
                          f"azokra bármely gyártó csomagja illeszkedne): {item['name']} {generic_skipped}")
            return None
        # A gyártó+eszköz TÖRZS-azonosító pótlása (SUBSYS/REV/CC nélkül): az eszköz saját
        # HWID-listája sokszor csak alrendszer-kötött ID-ket tartalmaz, a gyártó friss
        # csomagja viszont a törzsön van indexelve (lásd wu_core.base_vendor_hwid).
        # A keresés így is max 4 lekérdezés marad, de a törzs mindig köztük van.
        hwids = hwids[:4]
        # A törzs is átmegy a típuskód-vizsgálaton: az item['id'] lehet általános
        # azonosító is, és egy típuskódos törzzsel ugyanúgy más gyártó csomagját hoznánk be.
        base = base_vendor_hwid(hwids[0] if hwids else '')
        if base and is_specific_hwid(base) and base.lower() not in {h.lower() for h in hwids}:
            hwids = hwids[:3] + [base]

        inst = (installed_info or {}).get((item.get('pnp_id') or '').upper()) or {}
        inst_ver_str = inst.get('version', '')
        inst_ver = _parse_driver_version(inst_ver_str)
        # "Gyári driver a generikus helyett": CSAK a mark_generic_replace_candidates
        # által megjelölt eszközöknél lép életbe (lásd ott, hogy miért nem globális).
        replace_inbox = bool(item.get('generic_ok')) and _is_inbox_driver(inst)

        rows_by_guid = {}
        # MELYIK KULCS HOZTA A SORT? A `hwids` lista SPECIFIKUS -> ÁLTALÁNOS sorrendű (az
        # eszköz saját azonosítói, végül a szintetizált törzs-ID), így az index maga a
        # "mennyire pontosan szól ez a sor ennek az eszköznek" mérőszáma: 0 = a legpontosabb.
        #
        # MIÉRT KELL (terepen mérve, 2026-08-26, ASRock B450M + Realtek ALC897): az eszköz
        # SAJÁT kulcsára (`...DEV_0897&SUBSYS_18494897`) a katalógus első találata a
        # 'Realtek - MEDIA - 6.0.9360.1' csomag, aminek az INF-je PONTOSAN ezt az eszközt
        # listázza. Az ÁLTALÁNOS kulcsra (`...DEV_0897`) viszont egy ÚJABB, 6.0.10007.1-es
        # csomag jött - ami viszont Acer gépekre való (mind a 332 DEV_0897 bejegyzése
        # SUBSYS_1025xxxx). Mivel a sorokat egy halmazba öntöttük és csak a dátum döntött,
        # az Acer-csomag nyert, az INF-ellenőrzés jogosan elvetette, a helyes (de régebbi)
        # csomaghoz pedig soha nem jutottunk el - a hangkártya a Windows alapdriverén maradt.
        spec_by_guid = {}
        # MÉLYEBB LAPOZÁS a Windows-alapdriveren futó eszköz SAJÁT, SUBSYS-es kulcsain
        # (lásd `_catalog_fetch_rows(deep=...)`). Csak ott, mert csak ott van értelme:
        # egy SUBSYS-re szűkített kulcs minden sora ehhez a géphez való, tehát a régebbi
        # kiadások is valódi jelöltek - az általános kulcson viszont ezerszám állnak más
        # gyártók OEM-változatai, ott a mélyítés puszta kéréspazarlás lenne.
        deep_ok = _is_inbox_driver(inst)
        for spec, hwid in enumerate(hwids[:4]):
            deep = deep_ok and 'SUBSYS_' in (hwid or '').upper()
            try:
                logging.debug(f"[CATALOG] Keresés: {item['name']} ({hwid}{', MÉLY' if deep else ''})")
                for (g, t, row_l, d) in self._catalog_fetch_rows(hwid, ssl_ctx, deep=deep):
                    rows_by_guid.setdefault(g, (t, row_l, d))
                    if spec < spec_by_guid.get(g, 99):
                        spec_by_guid[g] = spec
            except Exception as e:
                logging.debug(f"[CATALOG] Lekérdezési hiba ({hwid}): {e}")
        if not rows_by_guid:
            return None

        # OS/architektúra pontozás - ha minden sor kizárt, visszaesünk a teljes
        # listára (régi viselkedés), mert egy "rossz OS-ű" driver is jobb lehet a semminél.
        all_rows = [(g, t, row_l, d) for g, (t, row_l, d) in rows_by_guid.items()]
        scored = [(sc, g, t, d) for (g, t, row_l, d) in all_rows
                  if (sc := self._catalog_row_score(row_l)) is not None]
        if not scored:
            scored = [(0, g, t, d) for (g, t, _row_l, d) in all_rows]
        if replace_inbox:
            vendor_rows = [c for c in scored if not self._catalog_row_is_microsoft(c[2])]
            if not vendor_rows:
                logging.debug(f"[CATALOG] {item['name']}: csak Microsoft-csomag van a katalógusban, a generikus csere értelmetlen - kihagyva.")
                return None
            scored = vendor_rows

        # A GÉP SAJÁT `SUBSYS_`-KULCSÁRÓL VALÓ SOROK ELSŐBBSÉGE A NYERTES-VÁLASZTÁSNÁL
        # (2026-09-03, terepen mérve, HP EliteDesk 800 G2 + Realtek ALC221).
        #
        # A `spec_by_guid` eddig CSAK a tartalékok sorrendjét adta, a NYERTEST tisztán a
        # dátum döntötte el - az összes kulcs sorait egy közös halmazba öntve. Ez volt az
        # utolsó láncszem a *"felrakom, működik, mégis újra felajánlja"* körben, és a
        # napló pontosan kimutatja:
        #
        #   a gép SAJÁT HP-kulcsa (&SUBSYS_103C8054) -> 14 sor, mind 6.0.1.8xxx,
        #       a legfrissebb: 6.0.1.8335 [2017-12-26]  <- PONT EZ VAN FENT A GÉPEN
        #   az ÁLTALÁNOS kulcs (DEV_0221)             -> 25 sor a 264-ből, 2026-osak
        #
        # Dátum szerint a 2026-os "újabb", tehát az nyert - csakhogy az MÁS gépgyártó
        # OEM-változata, és az INF-vizsgálat mind a hármat elvetette. A gép valójában
        # NAPRAKÉSZ: a HP a saját kulcsán 2017 óta nem adott ki újabbat, és az fent van.
        #
        # A SZABÁLY: ha a gyártó publikál EHHEZ A KONKRÉT GÉPHEZ (SUBSYS) szóló csomagot,
        # akkor arra a kulcsra nézve kell eldönteni, van-e újabb. Egy általános kulcsról
        # jött, "frissebb" sor definíció szerint egy MÁSIK gép változata. Ettől a
        # kiadás-kapu (`is_newer_release`) helyes választ ad: nincs újabb -> nincs
        # ajánlat -> az eszköz végre "naprakész" lesz, nem pedig örökké felajánlott.
        #
        # KIVÉTEL: HIBAKÓDOS eszköznél nem szűkítünk. Ott bármilyen driver jobb a
        # semminél, tehát minden sor jelölt marad (ugyanaz az elv, mint a downgrade-
        # védelemnél). A tartalék-lista amúgy is a TELJES `scored`-ból épül, tehát ha a
        # szűkített nyertes INF-je mégsem illik, a többi sor továbbra is sorra kerül.
        own_specs = {i for i, h in enumerate(hwids[:4]) if 'SUBSYS_' in (h or '').upper()}
        own_rows = ([c for c in scored if spec_by_guid.get(c[1], 99) in own_specs]
                    if own_specs and not item.get('err_code') else [])
        pool = own_rows or scored
        if own_rows and len(own_rows) != len(scored):
            logging.info(f"[CATALOG] {item['name']}: a gyártó ehhez a géptípushoz "
                         f"({[h for h in hwids[:4] if 'SUBSYS_' in (h or '').upper()][:1]}) "
                         f"{len(own_rows)} csomagot publikál - a nyertest ezek közül "
                         f"választjuk, a további {len(scored) - len(own_rows)} általános "
                         f"sor más gépgyártók változata (tartaléknak megmaradnak).")
        # ===== ALKALMAZHATÓSÁG-ELLENŐRZÉS LETÖLTÉS ELŐTT (2026-09-03) =====
        #
        # EZ SZÜNTETI MEG A "felajánlja -> letölti -> elveti -> mégis felajánlja" KÖRT.
        # A katalógus a törzs-HWID-re más gépgyártók változatait is visszaadja, és eddig
        # CSAK a letöltött csomag INF-jéből derült ki, hogy nem ide való. A részletlap
        # viszont (44-86 KB, ~2 mp) NÉVSZERINT felsorolja a támogatott hardver-
        # azonosítókat - lásd `_catalog_supported_hwids`. Mérve ezen a gépen:
        #
        #   HP  6.0.1.8335  ->  16 azonosító, a gép ...&SUBSYS_103C8054-e BENNE VAN
        #   GEN 6.0.9980.1  -> 149 azonosító, a gépé NINCS köztük
        #
        # Így a nem ide való csomag már a KERESÉSNÉL kiesik: nem kerül a listára, nem
        # töltjük le, és a technikusnak nem kell azzal szembesülnie, hogy a program
        # felajánl valamit, amit aztán maga vet el.
        #
        # HÁROM SZABÁLY, AMI NÉLKÜL EZ TÖBBET ÁRTANA, MINT HASZNÁL:
        #  1. CSAK NEM ÜRES lista alapján szűrünk. `None` (nincs ilyen szekció a lapon,
        #     vagy nem jött le) = NEM ELDÖNTHETŐ -> a jelölt marad. Ugyanaz az elv, mint
        #     az `inf_package_applies` None-jánál: sosem vetünk el a nemtudás alapján.
        #  2. Ha a szűrés MINDENT kivágna, a szűrés eredményét eldobjuk. Egy üres lista
        #     azt jelentené, hogy "nincs hozzá driver" - amit csak bizonyítottan szabad
        #     kimondani, és egy szerveroldali formátumváltozás nem tehet ilyen állítást.
        #  3. Csak a néhány legjobb jelöltet ellenőrizzük (`CATALOG_HWID_PROBE_MAX`),
        #     hogy egy 25 soros holtverseny ne jelentsen 25 kérést.
        best_score = max(s for s, _g, _t, _d in pool)
        cands = [c for c in pool if c[0] == best_score]
        dev_ids = {str(h).lower() for h in (item.get('all_hwids') or []) if h}
        # A BIZONYÍTOTTAN KIZÁRT CSOMAGOK GUID-JAI - a TARTALÉK-listák is ezt használják.
        # Enélkül a szűrés eredménye elveszett: a tartalékok a teljes `scored` halmazból
        # épülnek, tehát ugyanaz a csomag, amiről az imént bizonyítottuk a gyártó SAJÁT
        # listájával, hogy nem ehhez az eszközhöz való, tartalékként visszakerült - és a
        # telepítő le is töltötte volna (hangnál 11 MB, videokártyánál 1,2 GB), hogy aztán
        # az INF-vizsgálat ugyanazt mondja ki még egyszer.
        probe_kizart_guids = set()
        if dev_ids:
            # A SORREND SZÁMÍT: a legfrissebb (és legspecifikusabb kulcsról való) jelölteket
            # kérdezzük le, mert a korlát miatt csak az első néhányat vizsgáljuk meg.
            sorrend = sorted(cands, key=lambda c: ((c[3] or ''), _parse_driver_version(c[2]) or ()),
                             reverse=True)
            sorrend.sort(key=lambda c: spec_by_guid.get(c[1], 99))
            # UGYANAZT A CSOMAGOT NEM KÉRDEZZÜK LE HATSZOR (2026-09-08, terepen mérve).
            #
            # A katalógus EGY csomagot több GUID alatt publikál (OS-ágankénti bejegyzések) -
            # ezt a CLAUDE.md már rögzíti az NVIDIA 25 bejegyzésénél. A `rows_by_guid` GUID
            # szerint dedupál, tehát ezek mind külön jelöltként kerülnek ide, és mindegyik
            # SAJÁT részletlap-letöltést kapott, holott a tartalmuk azonos. ÉLŐBEN MÉRVE
            # (2026-09-08, a katalógust olvasva, ugyanazokon a kulcsokon, amiket a Build
            # 304-es ASRock B450M lánc használt):
            #
            #   PCI\VEN_1022&DEV_148A  ->  75 bejegyzés =  4 tényleges csomag
            #                              (35x + 24x + 12x + 4x ugyanaz a cím+dátum)
            #   PCI\VEN_10DE&DEV_2504  ->  50 bejegyzés =  3 tényleges csomag (32x/14x/4x)
            #
            # ÉS A DEDUP BIZONYÍTOTTAN BIZTONSÁGOS: négy csoportban 3-3 testvér-GUID
            # részletlapját összehasonlítva a támogatott-azonosító lista MINDIG AZONOS volt
            # (5, 5, 15, illetve 70 azonosító) - a lista a CSOMAG tulajdonsága, nem a
            # bejegyzésé. A naplóban ez 256 sor / 52 KB volt, a legrosszabb 48x szó szerint
            # azonos sorral.
            #
            # HÁROM BAJ, ÉS A MÁSODIK ÉRDEMI (nem csak pazarlás):
            #  1. 12 részletlap-letöltés 3-4 tény megtudásához (mérve: 24 mp eszközönként,
            #     LÁBANKÉNT - a részletlap 44-86 KB, ~2 mp);
            #  2. ELFOGYASZTJA a CATALOG_HWID_PROBE_MAX keretet néhány valódi csomagon: a
            #     75 bejegyzésből az első 12 akár mind UGYANAZ a csomag lehet, a maradék
            #     63 pedig ELLENŐRZÉS NÉLKÜL megy tovább (`maradek`) - vagyis a nyertes
            #     lehet olyan sor, amit meg sem néztünk. A dedup után mind a 4 tényleges
            #     csomag belefér a keretbe;
            #  3. 256 azonos naplósor (Rule 0 ellen-szabálya: maximális INFORMÁCIÓ).
            #
            # A dedup kulcsa (cím, dátum): a katalógus a verziót a CÍMBEN hordozza, tehát az
            # azonos cím+dátum ugyanaz a kiadás. A verdikt a testvérekre is érvényes (a
            # támogatott-azonosító lista a CSOMAG tulajdonsága, nem a bejegyzésé), a
            # sorrend és minden későbbi lépés (URL-feloldás, letöltés) viszont VÁLTOZATLANUL
            # az összes GUID-dal dolgozik - nem veszítünk jelöltet, csak kérést spórolunk.
            csoport = {}
            for c in sorrend:
                csoport.setdefault(((c[2] or '').strip().lower(), c[3] or ''), []).append(c)
            kepviselok = [tagok[0] for tagok in csoport.values()][:CATALOG_HWID_PROBE_MAX]
            probed_keys = {((c[2] or '').strip().lower(), c[3] or '') for c in kepviselok}
            maradek = [c for c in sorrend
                       if ((c[2] or '').strip().lower(), c[3] or '') not in probed_keys]
            if len(csoport) != len(sorrend):
                logging.info(f"[CATALOG] {item['name']}: {len(sorrend)} katalógus-bejegyzés "
                             f"{len(csoport)} tényleges csomagot takar (a katalógus egy csomagot "
                             f"több GUID alatt publikál) - {len(kepviselok)} részletlapot kérdezünk le.")
            illik, eldonthetetlen, kizart = [], [], []
            for c in kepviselok:
                tagok = csoport[((c[2] or '').strip().lower(), c[3] or '')]
                tamogatott = self._catalog_supported_hwids(c[1], ssl_ctx)
                if tamogatott is None:
                    eldonthetetlen.extend(tagok)
                elif dev_ids & set(tamogatott):
                    illik.extend(tagok)
                else:
                    kizart.append((c, len(tamogatott), len(tagok)))
                    probe_kizart_guids.update(t[1] for t in tagok)
            if kizart:
                for (c, n, db) in kizart:
                    logging.info(f"[CATALOG] {item['name']}: '{c[2][:60]}' KIZÁRVA letöltés előtt - "
                                 f"a részletlap {n} támogatott azonosítója közt nincs ott az eszközé"
                                 + (f" (a katalógusban {db} bejegyzés alatt)." if db > 1 else "."))
                n_bejegyzes = sum(db for _c, _n, db in kizart)
                self.emit('task_progress', {'task': 'hw_scan', 'log':
                          f'  ⏭️ {item["name"]}: {len(kizart)} katalógus-csomag kizárva letöltés '
                          f'nélkül (a gyártó saját listája szerint nem ehhez az eszközhöz valók)'
                          + (f' - a katalógusban {n_bejegyzes} bejegyzés alatt szerepelnek.'
                             if n_bejegyzes != len(kizart) else '.')})
            # MI MARAD JELÖLTNEK: ami illik, ami nem volt eldönthető, és amit a korlát miatt
            # meg sem néztünk. A BIZONYÍTOTTAN kizártak nem.
            szurt = illik + eldonthetetlen + maradek
            if szurt:
                cands = szurt
            elif eldonthetetlen or not kizart:
                # Nem tudtunk semmit kiolvasni -> NEM szűrünk (a nemtudás sosem elvetés ok).
                logging.info(f"[CATALOG] {item['name']}: a részletlapokból nem derült ki semmi - "
                             f"a szűrést nem alkalmazzuk.")
            else:
                # MINDEN jelöltet MEGVIZSGÁLTUNK, és mindegyik listája KIZÁRJA az eszközt.
                # Ez nem feltételezés, hanem a gyártó saját, névszerinti listája - tehát
                # kimondható, hogy erre az eszközre a katalógusban nincs való csomag.
                # (Terepen mérve: a HID billentyűzetre 12 AlpsAlpine-jelölt jött, mindegyik
                # `hid\alp000d&col02` típusú azonosítókat támogat, az eszköz viszont
                # `hid\vid_044e&pid_1212&col02&col02` - egyik sem fedi.)
                logging.info(f"[CATALOG] {item['name']}: MINDEN megvizsgált jelölt ({len(kizart)} db) "
                             f"kizárja ezt az eszközt a saját támogatott-azonosító listájával - "
                             f"nincs való csomag, nem ajánljuk fel.")
                self.emit('task_progress', {'task': 'hw_scan', 'log':
                          f'  🚫 {item["name"]}: a katalógus {len(kizart)} jelöltje közül egyik sem '
                          f'támogatja ezt az eszközt (a gyártó saját listája szerint) - nem ajánljuk fel.'})
                return None
        # KORÁBBAN MÁR MEGBUKOTT CSOMAGOK KIHAGYÁSA: amit egy előző futásban ugyanerre az
        # eszközre letöltöttünk és az INF-vizsgálat elvetett, azt nem töltjük le újra
        # (egy videokártya-csomag 1,2 GB). A jelölést a tartós no-bind tár őrzi, GUID
        # szerint - a cím ehhez kevés, mert a katalógusban 10 azonos című sor is lehet.
        # A szűrés SZÁNDÉKOSAN csak a katalógus-GUID-ra megy, és csak akkor, ha marad
        # más jelölt: itt dől el, mi kerül a KÉZI SZKEN LISTÁJÁRA, és egy találatot sosem
        # tüntetünk el - a technikus egy kattintással újrapróbálhat egy korábban elvetett
        # csomagot is (megjelölve, be nem jelölve jelenik meg). A fölösleges LETÖLTÉST a
        # telepítő oldalán spóroljuk meg (_install_catalog_sync: _proven_wrong), ami a
        # csomagcsalád régebbi kiadását is felismeri.
        bad_guids = {rec.get('guid') for rec in
                     ((known_no_bind or {}).get(_device_stem(item.get('pnp_id'))) or [])
                     if rec.get('guid')}
        if bad_guids:
            usable = [c for c in cands if c[1] not in bad_guids]
            if usable and len(usable) != len(cands):
                logging.debug(f"[CATALOG] {item['name']}: {len(cands) - len(usable)} jelölt kihagyva "
                              f"(korábban letöltöttük, és nem erre az eszközre való volt).")
                cands = usable
            elif not usable:
                # HA A LEGJOBB PONTSZÁMÚ JELÖLTEK MIND ISMERTEN ROSSZAK, NEM ADJUK FEL,
                # HANEM LEJJEBB LÉPÜNK A PONTSZÁMBAN (2026-09-03, explicit user decision:
                # *"arra hogy fel se települ arra nem az a megoldás hogy akkor berakom egy
                # tiltolistaba hogy többet ne dobja be mert akkor a device nem fog kapni
                # drivert sose... az a cél hogy minden kaphon drivert"*).
                #
                # A régi `if usable and ...` feltétel pont ezt a helyzetet hagyta ki: ha a
                # szűrés ÜRESRE fogyott, a `cands` változatlan maradt, tehát a nyertes újra
                # a bizonyítottan alkalmazhatatlan csomag lett - a program minden szken
                # után ugyanazt ajánlotta, amiről már tudta, hogy nem megy. Terepen ez volt
                # a Realtek-eset: az általános kulcsról jött, MÁS gépgyártós csomag nyert,
                # az INF-vizsgálat elvetette, és a gép saját HP-kulcsán lévő 14 sort a
                # program soha meg sem nézte.
                #
                # A helyes válasz nem a találat elrejtése (az eszköz akkor SOSEM kapna
                # drivert), hanem a KÖVETKEZŐ valódi jelölt előhozása: a `scored` teljes
                # halmazából minden nem-tiltott sor, a szokásos sorrendben (legspecifikusabb
                # kulcs -> jobb OS-pontszám -> frissebb dátum). Ha így sem marad semmi,
                # akkor tényleg nincs csomag, és ezt a záró jelentés ki is mondja.
                fallback = [c for c in scored if c[1] not in bad_guids]
                if fallback:
                    fallback.sort(key=lambda c: (c[3] or ''), reverse=True)
                    fallback.sort(key=lambda c: c[0], reverse=True)
                    fallback.sort(key=lambda c: spec_by_guid.get(c[1], 99))
                    logging.info(f"[CATALOG] {item['name']}: a legjobb pontszámú jelöltek MIND "
                                 f"korábban megbukottak - lejjebb lépünk a pontszámban, hogy az "
                                 f"eszköz mégis kapjon esélyt. Új jelöltek: {len(fallback)} db, "
                                 f"első: '{fallback[0][2][:60]}' [{fallback[0][3] or '?'}]")
                    cands = fallback
                else:
                    logging.info(f"[CATALOG] {item['name']}: MINDEN katalógus-sor korábban "
                                 f"megbukott ezen az eszközön - nincs mit felajánlani.")
                    return None
        # A legjobb pontszámúak közül a LEGFRISSEBB DÁTUMÚ sor nyer, és csak azonos
        # dátumnál dönt a verziószám. (A katalógus sor-sorrendje nem newest-first.)
        #
        # Miért a dátum az elsődleges: ugyanaz a gyártó ugyanarra az eszközre több,
        # egymással összehasonlíthatatlan verziósémát is használ. Mérve a gépen, a
        # Realtek NIC-re: "Realtek - Net - 1168.19.704.2024" (2024-07-03) és
        # "Realtek Net Driver Update (10.79.50.1003)" (2025-10-02). Verzió szerint az
        # 1168.19.704.2024 "nyerne" - pedig több mint egy évvel régebbi csomag. A
        # kiadási dátum viszont mindkét sémán át értelmes, és a két terepi esetben
        # (Realtek audio + Realtek LAN) is a helyes csomagot választja.
        ordered = sorted(cands, key=lambda c: ((c[3] or ''), _parse_driver_version(c[2]) or ()),
                         reverse=True)
        best = ordered[0]
        best_ver = _parse_driver_version(best[2])
        _bs, best_id, best_title, best_date = best
        # EGY összegző sor a teljes választásról (a soronkénti pontozás szándékosan nem
        # logol - lásd common._CALL_LOG_EXCLUDE). Ebből visszafejthető, MIÉRT ez a csomag
        # nyert: hány sorból, hány HWID-ről, milyen pontszámmal, és mik voltak a közeli
        # versenytársak (cím + dátum). Egy rossz választásnál pontosan ez a sor kell.
        rivals = ', '.join(f"{t[:40]}|{d or '?'}" for _s, _g, t, d in
                           sorted(cands, key=lambda c: (c[3] or ''), reverse=True)[1:4])
        logging.info(f"[CATALOG] Döntés: {item['name']} - {len(rows_by_guid)} sor / {len(hwids[:4])} HWID, "
                     f"legjobb pont={best_score}, {len(cands)} holtverseny -> NYERTES: '{best_title}' "
                     f"[{best_date or '?'}] v={best_ver}" + (f" | közeli: {rivals}" if rivals else ""))

        if replace_inbox:
            # SZÁNDÉKOSAN NINCS verzió-összehasonlítás: a beépített driver verziója a
            # Windows buildje (10.0.26100.8457), a gyárié meg saját sémájú (6.0.9992.1),
            # tehát a generikus MINDIG "újabbnak" látszana, és pont a jobb drivert
            # dobnánk el. Ugyanez a csapda a WU-ágon már kezelve van
            # (wu_core._filter_wu_downgrades / _is_inbox_driver).
            # Nem pörög körbe: telepítés után az eszköz oemNN.inf-en, gyári providerrel
            # fut, így a következő szkennen már nem jelölt (is_generic_replace_candidate).
            logging.info(f"[CATALOG] Generikus -> gyári csere jelölt: {item['name']} "
                         f"(most: {inst.get('provider') or '?'} {inst_ver_str} / {inst.get('inf') or '?'}) -> '{best_title}'")
        elif _is_inbox_driver(inst):
            # A telepített driver a Windows BEÉPÍTETT generikusa, de az eszköz nem jelölt a
            # gyári cserére (különben a fenti `replace_inbox` ág vitte volna). Ilyenkor a
            # dátum-szabályt NEM alkalmazzuk: az inbox driver dátuma a Windowsé (a
            # `input.inf` pl. 2006-06-21-et visel), így minden gyári csomag "újabbnak"
            # látszana, és a mély szken csendben átvenné a generikus->gyári csere
            # szerepét - épp azokon az osztályokon (HID, billentyűzet, egér), amiket a
            # mark_generic_replace_candidates SZÁNDÉKOSAN kihagy, és rollback-ellenőrzés
            # nélkül. Ezt a döntést ott kell meghozni, nem itt.
            if best_ver is not None and inst_ver is not None and best_ver <= inst_ver:
                logging.debug(f"[CATALOG] Kihagyva (Windows-alapdriveren fut, nem gyári-csere jelölt; "
                              f"telepített {inst_ver_str} >= katalógus '{best_title}'): {item['name']}")
                return None
        else:
            # ÚJABB-E EGYÁLTALÁN? DÁTUM DÖNT, a verzió csak azonos dátumnál (közös mag:
            # wu_core.is_newer_release). A régi, tisztán verzió-alapú kapu a gyártói
            # verziósémaváltásoknál bizonyítottan a rossz csomagot tartotta meg: terepen
            # (2026-07-27) az AMD SMBus 5.12.0.38 / 2017-08-30 "nagyobb" volt, mint a
            # katalógus 2.0.0.26 / 2025-12-03 csomagja, így a gép egy 2017-es driveren
            # maradt. A WU-ág (wu_core._filter_wu_downgrades) és a katalógus SOR-választása
            # már régóta dátum-alapú - ez a kapu volt az utolsó verzió-alapú döntés a
            # telepítési úton.
            newer = is_newer_release(best_date, best_title, inst.get('date'), inst_ver_str)
            if newer is False and own_rows and len(own_rows) != len(scored):
                # NEM ADJUK FEL, AMÍG VAN MÉG KIPRÓBÁLATLAN SOR (2026-09-03, explicit user
                # decision: *"nem akarom h feladja a program sehol semmilyen esetben se…
                # persze akkor lehet csak ezt kiírni ha TÉNYLEG MINDENT megprobalt"*).
                #
                # A fenti SUBSYS-elsőbbség önmagában itt „feladássá" válna: ha a gyártó a
                # gép saját kulcsán nem adott ki újabbat, a kapu `None`-t adna, és a
                # program SOHA nem próbálná meg az általános kulcs sorait - pedig azok
                # között lehet olyan univerzális csomag, ami mégis erre a gépre való.
                # Pontosan az az eset, amit a felhasználó az ASRock-alaplapról idéz:
                # *"az se volt igaz h nincs hozzá driver, aztán mégis lett"*.
                #
                # Ezért itt kinyitjuk a kört a TELJES `scored`-ra és újraválasztunk. Ez nem
                # visz végtelen körbe: amit a program letölt és az INF-vizsgálat elvet, azt
                # a tartós no-bind tár megjegyzi (`bad_guids`), tehát a következő futáson
                # az a sor már ki van szűrve - így a kör magától fogy el, és a végén
                # ŐSZINTÉN mondható, hogy mindent megpróbáltunk.
                # MÉRET-ALAPÚ DÖNTÉS (2026-09-03, explicit user decision): egy KICSI
                # csomagot érdemes kipróbálni akkor is, ha valószínűleg más gépgyártóé -
                # csak így derül ki az igazság, és 11 MB nem tétel. Egy NAGY csomagnál
                # (videokártya, 1,2 GB) viszont a próba fél órát vinne el egy olyan
                # csomagra, amiről a gyártó saját kulcsa már megmondta, hogy nem ide való.
                # A méret a találati sorból INGYEN megvan (Size oszlop), tehát a döntés
                # letöltés nélkül meghozható.
                alt_pool = [c for c in scored if c[1] not in bad_guids]
                nagyok = [c for c in alt_pool
                          if (self._catalog_row_size_mb(rows_by_guid.get(c[1], ('', '', ''))[1])
                              or 0) > CATALOG_FOREIGN_TRY_MAX_MB]
                if nagyok:
                    logging.info(f"[CATALOG] {item['name']}: {len(nagyok)} idegen-gyártós jelölt "
                                 f"kihagyva méret miatt (> {CATALOG_FOREIGN_TRY_MAX_MB} MB) - a gép "
                                 f"saját kulcsán már a legfrissebb csomag van, egy ekkora letöltés "
                                 f"nem éri meg a próbát.")
                    alt_pool = [c for c in alt_pool if c not in nagyok]
                if alt_pool:
                    alt_best = max(s for s, _g, _t, _d in alt_pool)
                    alt_cands = [c for c in alt_pool if c[0] == alt_best]
                    alt_cands.sort(key=lambda c: ((c[3] or ''), _parse_driver_version(c[2]) or ()),
                                   reverse=True)
                    a_bs, a_id, a_title, a_date = alt_cands[0]
                    if is_newer_release(a_date, a_title, inst.get('date'), inst_ver_str) is not False:
                        logging.info(
                            f"[CATALOG] {item['name']}: a gép saját kulcsán nincs újabb "
                            f"('{best_title}' [{best_date or '?'}] <= telepített {inst_ver_str}), "
                            f"de az általános kulcson MÉG VAN kipróbálatlan sor - nem adjuk fel, "
                            f"azt ajánljuk: '{a_title}' [{a_date or '?'}]. Ha az INF-vizsgálat "
                            f"elveti, a no-bind tár megjegyzi, és a kör magától fogy el.")
                        cands = alt_cands
                        best = alt_cands[0]
                        _bs, best_id, best_title, best_date = best
                        best_ver = _parse_driver_version(best_title)
                        newer = True
            if newer is False:
                logging.debug(f"[CATALOG] Kihagyva (nem újabb kiadás - telepített {inst_ver_str} "
                              f"[{inst.get('date') or '?'}] vs katalógus '{best_title}' [{best_date or '?'}]): {item['name']}")
                return None
            if newer is None:
                logging.info(f"[CATALOG] Nem eldönthető, melyik újabb (telepített {inst_ver_str} "
                             f"[{inst.get('date') or '?'}] vs '{best_title}' [{best_date or '?'}]) - felajánljuk: {item['name']}")
            elif _parse_driver_version(best_title) is not None and inst_ver is not None \
                    and _parse_driver_version(best_title) <= inst_ver:
                # Pont az a helyzet, amiért a szabály átállt: dátum szerint újabb, verzió
                # szerint nem. Ez INFO-szintű, mert egy "miért települt rá kisebb verzió?"
                # kérdésre ez az egyetlen válasz a terepi logból.
                logging.info(f"[CATALOG] Dátum szerint ÚJABB, verzió szerint nem - a dátum dönt: "
                             f"{item['name']} - telepített {inst_ver_str} [{inst.get('date') or '?'}] "
                             f"-> '{best_title}' [{best_date or '?'}]")

        # HOLTVERSENY ELDÖNTÉSE A RÉSZLETLAPRÓL, LETÖLTÉS ELŐTT.
        # Idáig csak akkor jutunk el, ha tényleg fel is akarjuk ajánlani a csomagot (a
        # kiadás-kapun túl vagyunk), tehát ez körönként néhány eszközt érint, nem az
        # összeset. Csak az AZONOS DÁTUMÚ jelöltek versenyeznek: a kiadás-kapu rájuk
        # ugyanazt mondja, tehát a köztük való választás nem ronthat a döntésen - viszont
        # pont ez a 10-es NVIDIA holtverseny, ahol eddig vaktában választottunk.
        tie = [c for c in ordered if (c[3] or '') == (best_date or '')]
        if len(tie) > 1:
            # ITT IS EGY CSOMAG = EGY LEKÉRDEZÉS (2026-09-08). A holtverseny definíció
            # szerint azonos dátumú, tehát a csomagot a CÍME azonosítja - és a katalógus
            # ugyanazt a csomagot több tucat GUID alatt publikálja (élőben mérve: az
            # 'NVIDIA Display Driver Update (32.0.15.9595)' **32 GUID** alatt). A régi kód
            # a `tie` első 10 elemét kérdezte le, ami így akár 10 AZONOS tartalmú
            # részletlap-letöltés volt - ugyanaz a hiba, mint a támogatott-azonosító
            # ellenőrzésnél, csak a holtverseny-döntő ágon. A `Driver Model` mező a CSOMAG
            # tulajdonsága, tehát a testvérek ugyanazt a rangot kapják.
            cim_szerint = {}
            for c in tie:
                cim_szerint.setdefault((c[2] or '').strip().lower(), []).append(c)
            if len(cim_szerint) != len(tie):
                logging.debug(f"[CATALOG] {item['name']}: {len(tie)} holtverseny-sor "
                              f"{len(cim_szerint)} tényleges csomag - csomagonként egy "
                              f"'Driver Model' lekérdezés.")
            ranked = []
            for tagok in list(cim_szerint.values())[:CATALOG_MODEL_PROBE_MAX]:
                cand = tagok[0]
                rank = driver_model_rank(item['name'], self._catalog_driver_models(cand[1], ssl_ctx))
                ranked.extend((rank, t) for t in tagok)
                if rank >= 2:
                    # Pontos névegyezés: nincs értelme több részletlapot lekérdezni. E nélkül
                    # a korai kilépés nélkül egy 45 tételes holtverseny (mérve: Realtek NIC)
                    # 45 kérést jelentene, a korlát pedig kizárhatná a jó jelöltet - az
                    # offline teszt pont ezt kapta el, amikor a 8. sor volt a helyes.
                    break
            probed = {c[1] for _r, c in ranked}
            ranked += [(0, c) for c in tie if c[1] not in probed]
            ranked.sort(key=lambda r: (r[0], _parse_driver_version(r[1][2]) or ()), reverse=True)
            if ranked[0][0] > 0 and ranked[0][1][1] != best_id:
                logging.info(f"[CATALOG] Holtverseny eldöntve a részletlap alapján: {item['name']} - "
                             f"'{ranked[0][1][2]}' (a támogatott eszközök közt szerepel), "
                             f"a korábbi vak választás helyett '{best_title}'.")
                best = ranked[0][1]
                _bs, best_id, best_title, best_date = best
                best_ver = _parse_driver_version(best_title)
            elif ranked[0][0] == 0:
                logging.debug(f"[CATALOG] {item['name']}: {len(tie)} holtverseny-jelölt, de a "
                              f"részletlapok 'Driver Model' mezője egyiknél sem mond semmit - "
                              f"marad a dátum/verzió szerinti sorrend.")
            # A tartalék: a maradék azonos dátumú jelölt. Ha a nyertes INF-jéről kiderül,
            # hogy nem ehhez az eszközhöz való, a telepítő ezekkel próbálkozik tovább
            # ahelyett, hogy feladná (lásd _install_catalog_sync). Azonos dátum miatt
            # tartalékként sem kerülhet fel régebbi kiadás.
            # A TARTALÉK-LISTA CSOMAGONKÉNT EGY TÉTEL. Ugyanaz az ok, mint fent: a katalógus
            # egy csomagot több tucat GUID alatt publikál, tehát a szűretlen lista simán
            # kitöltődhetne UGYANANNAK a csomagnak a testvéreivel - a telepítő ezt a letöltési
            # URL alapján úgyis felismerné ("ugyanarra a csomagra mutat"), de addigra elvette
            # a helyet a következő VALÓDI jelölt elől. Így a 3 (inbox eszköznél 6) tartalék
            # 3 (illetve 6) tényleges csomagot jelent.
            alts, alt_cimek = [], set()
            for _r, c in ranked:
                cim = (c[2] or '').strip().lower()
                if c[1] == best_id or cim in alt_cimek or cim == (best_title or '').strip().lower():
                    continue
                alt_cimek.add(cim)
                alts.append((c[1], c[2], c[3]))
                if len(alts) >= CATALOG_MAX_CANDIDATES - 1:
                    break
        else:
            alts = []

        def _tartalek_bovit(alts, extra, room):
            """A tartalék-lista bővítése, CSOMAGONKÉNT EGY tétellel (2026-09-08).

            A `room` a VALÓDI CSOMAGOK száma, nem a katalógus-bejegyzéseké: a katalógus egy
            csomagot több tucat GUID alatt publikál (élőben mérve: 32 GUID egy NVIDIA
            kiadásra), így a szűretlen lista mind a 3 (inbox eszköznél 6) tartalék-helyet
            UGYANANNAK a csomagnak a testvéreivel töltötte volna ki. A telepítő ezt a
            letöltési URL alapján felismeri és nem tölti le kétszer - de addigra a testvér
            már elvette a helyet a következő VALÓDI jelölt elől, vagyis pont a tartalék-
            mechanizmus lényege veszett el."""
            hasznalt = {(t or '').strip().lower() for _g, t, _d in alts}
            hasznalt.add((best_title or '').strip().lower())
            uj = []
            for c in extra:
                cim = (c[2] or '').strip().lower()
                if cim in hasznalt:
                    continue
                hasznalt.add(cim)
                uj.append((c[1], c[2], c[3]))
                if len(uj) >= room:
                    break
            return uj

        # RÉGEBBI KIADÁSOK IS TARTALÉKKÉNT - de CSAK Windows-alapdriveres eszköznél.
        #
        # Explicit user decision (2026-08-26): "a semminél, az inbox drivernél jobb a régi
        # driver milliószor... ha semmit se tud feltelepíteni, akkor azt ami jó hozzá, de
        # verzióban régebbi, azt nyugodtan felrakhatja, sőt rakja fel - és ez ne csak a
        # hangra legyen igaz, MINDENBŐL mindennél legyen ez a szabály".
        #
        # A terepi eset (ASRock ALC897): az azonos dátumú tartalékok MIND Acer-változatok
        # voltak, a helyes csomag (6.0.9360.1) pedig RÉGEBBI kiadás - a régi szabály szerint
        # tartalékként sem jöhetett szóba, így a hangkártya a generikus `hdaudio.inf`-en
        # maradt. Egy régebbi GYÁRI driver viszont minden szempontból jobb a Windows
        # generikusánál.
        #
        # MIÉRT CSAK INBOX-ESZKÖZNÉL: ha az eszköz már GYÁRI driveren fut, egy régebbi
        # kiadás felrakása visszalépés lenne - azt a `is_newer_release` kapu tiltja, és ez
        # a szabály nem írja felül. Ott marad a régi, azonos dátumú tartalék-viselkedés.
        #
        # SORREND: elsőként a legSPECIFIKUSABB kulcsról származó sorok (spec_by_guid), azon
        # belül a legfrissebb dátum. Így a helyes csomag jellemzően az ELSŐ tartalék, tehát
        # a bővítés a gyakorlatban nem jelent plusz letöltést - a telepítő ráadásul URL
        # szerint deduplikál, és a korábban megbukott csomagokat le sem tölti.
        if _is_inbox_driver(inst):
            have = {best_id} | {a[0] for a in alts}
            # A TARTALÉK-KÉSZLET A TELJES `scored` HALMAZ, NEM CSAK A `cands`.
            #
            # `cands` csak a LEGJOBB OS-pontszámú sorokat tartja meg - és pont ez vágta le
            # a keresett csomagokat. A pontozó (`_catalog_row_score`) `None`-t ad a valóban
            # KIZÁRT sorokra (arm64 x64-en, Win11-only Win10-en), a 0-3 viszont csak
            # PREFERENCIA: Windows 11-es gépen a "windows 11" sor 3 pont, a "windows 10"
            # csak 1. Mérve ezen a gépen (ASRock B450M + ALC892): a saját SUBSYS-kulcs
            # 41 sora mind 2019-2021-es, "Windows 10"-es Realtek csomag, tehát 3 helyett
            # 1 pontot kap - így a `cands`-ból kiesett, és a tartalékok mind a 2026-os,
            # MÁS gyártóknak szóló sorok lettek (élőben ellenőrizve: 5 tartalék, egyik
            # sem ASRock). Egy Windows 10-es gyári driver viszont Windows 11-en is
            # felmegy, és minden szempontból jobb a generikusnál.
            extra = [c for c in scored if c[1] not in have
                     and c[1] not in probe_kizart_guids]
            # Három menetben, a Python STABIL rendezésére építve (a legutolsó a
            # legerősebb): dátum csökkenő -> pontszám csökkenő -> specifikusság növekvő.
            # Így a LEGSPECIFIKUSABB kulcs sorai jönnek elöl (ott minden sor ehhez a
            # géphez való), azon belül a jobb OS-illeszkedés, azon belül a frissebb.
            extra.sort(key=lambda c: (c[3] or ''), reverse=True)
            extra.sort(key=lambda c: c[0], reverse=True)
            extra.sort(key=lambda c: spec_by_guid.get(c[1], 99))
            room = max(0, CATALOG_INBOX_FALLBACK_CANDIDATES - 1 - len(alts))
            uj = _tartalek_bovit(alts, extra, room) if room else []
            if uj:
                alts += uj
                logging.info(f"[CATALOG] {item['name']}: Windows-alapdriveren fut, ezért RÉGEBBI kiadások "
                             f"is tartalékba kerülnek ({len(uj)} db, a legspecifikusabb kulcs "
                             f"felől) - egy régi gyári driver jobb a generikusnál. Első tartalék: "
                             f"'{uj[0][1][:60]}' [{uj[0][2] or '?'}]")
        else:
            # GYÁRI DRIVEREN FUTÓ ESZKÖZ IS KAP TARTALÉKOT - A SAJÁT KULCSÁRÓL (2026-09-03).
            #
            # A hiányzó ág, ami miatt a technikus azt látta, hogy *"felajánlott egy realtek
            # drivert, feltelepítettem, működik, utána kerestem, újra felajánlotta"*:
            #
            #   1. kör - az eszköz még a Windows `hdaudio.inf`-jén ült, tehát lefutott a
            #      fenti (inbox) ág, ami a LEGSPECIFIKUSABB kulcs felől sorolta a
            #      tartalékokat -> megtalálta a géphez való HP-csomagot -> felment, működik.
            #   2. kör - az eszköz MOST MÁR gyári driveren fut, tehát `_is_inbox_driver`
            #      hamis, és a tartalék-lista fel sem épült (`alts` csak az AZONOS DÁTUMÚ
            #      holtverseny-jelöltekből állt). Maradt a dátum-elsőségű nyertes, ami az
            #      ÁLTALÁNOS kulcsról jött és MÁS gépgyártó változata -> INF-vétó ->
            #      "nincs való csomag" -> feljegyzés a no-bind tárba.
            #   Közben a gép SAJÁT `&SUBSYS_`-kulcsán ott volt 14 sor, amiket a program
            #   soha nem nézett meg. (Mérve: HP EliteDesk 800 G2, ALC0221, 2026-09-03.)
            #
            # A KÉT DOLGOT SZÉT KELL VÁLASZTANI, és eddig egybe volt gyúrva:
            #   - a SPECIFIKUSSÁG SZERINTI SORREND univerzálisan helyes: egy sor, ami a
            #     gép saját SUBSYS-kulcsáról jött, definíció szerint ehhez a géphez való;
            #   - a RÉGEBBI kiadás elfogadása viszont TÉNYLEG csak inbox-eszköznél helyes,
            #     különben visszalépés lenne.
            # Ezért itt a `scored`-ból csak azokat vesszük tartaléknak, amik a kiadás-kapun
            # is átmennek (`is_newer_release`), a sorrend viszont ugyanaz: legspecifikusabb
            # kulcs -> jobb OS-pontszám -> frissebb dátum. Downgrade így sem történhet.
            have = {best_id} | {a[0] for a in alts}
            extra = [c for c in scored if c[1] not in have
                     and c[1] not in probe_kizart_guids
                     and is_newer_release(c[3], c[2], inst.get('date'), inst.get('version')) is not False]
            extra.sort(key=lambda c: (c[3] or ''), reverse=True)
            extra.sort(key=lambda c: c[0], reverse=True)
            extra.sort(key=lambda c: spec_by_guid.get(c[1], 99))
            room = max(0, CATALOG_MAX_CANDIDATES - 1 - len(alts))
            uj = _tartalek_bovit(alts, extra, room) if room else []
            if uj:
                alts += uj
                logging.info(f"[CATALOG] {item['name']}: gyári driveren fut, tartalékok a "
                             f"LEGSPECIFIKUSABB kulcs felől ({len(uj)} db, csak "
                             f"újabb kiadások) - e nélkül egy már felrakott gyári driver "
                             f"mellett örökre a rossz gyártójú csomagot ajánlanánk. "
                             f"Első tartalék: '{uj[0][1][:60]}' [{uj[0][2] or '?'}]")

        # ===== CSAK OSZTÁLYKÓD-KULCSRÓL JÖTT-E A NYERTES? (2026-09-04, terepen mérve) =====
        #
        # A `&CC_xxxx` (PCI osztálykód) kulcs azt jelenti: "bármely gyártó ilyen FAJTA
        # eszközéhez való csomag". Ez a katalógusban az idegen gyártók OEM-bundle-jeinek
        # mágnese - és ebből lett a fejlesztői gép egyik legcsúnyább esete:
        #
        #   eszköz: PCI\VEN_8086&DEV_A123&SUBSYS_8054103C  (HP EliteDesk SMBus vezérlő)
        #   nyertes: 'AlpsAlpine - System - 10.4200.1616.141' [2019-03-04]
        #   a kulcs, ami behozta: ...&CC_0C0500      <- osztálykód, NEM a gép SUBSYS-e
        #
        # MINDEN ellenőrzésünk igazat mondott rá: az INF tényleg deklarálja a
        # `pci\ven_8086&dev_a123&cc_0c05`-öt (inf_package_applies -> True), a Windows
        # rangsora is ezt választotta (hardver-azonosítós egyezés, míg az Intel saját
        # csomagja csak kompatibilis azonosítón illeszkedik), és tényleg rá is kötött
        # (kötés-ellenőrzés -> True). A csomag mégsem ide való: a driver felrakása után
        # LEGYÁRTOTT egy nem létező ThinkPad UltraNav tapipadot (`HID\VID_044E&PID_1212`)
        # négy szellem-gyerekkel, és minden rendszerindításkor hibaüzenetet dobott
        # ("Set user settings to driver failed"). Mérve a Windows setupapi.dev.log-jából:
        # a telepítés 13:19:11, a szellemeszköz születése 13:19:13.7 - a driver csinálta.
        #
        # MIÉRT NEM FOGTA MEG SEMMI: a tíz döntési pontunk mind azt kérdezi, hogy
        # "ALKALMAZHATÓ-e ez a csomag erre az eszközre" - és a válasz becsületesen igen
        # volt. Azt egyik sem kérdezi, hogy "ennek a GÉPNEK szánta-e a gyártó". Ezt az
        # információt pontosan két hely hordozza: a WU szerver-oldali célzása (amit a
        # katalógus használatával definíció szerint megkerülünk), és a katalógus
        # részletlapjának SUBSYS-szintű azonosító-listája (`_catalog_supported_hwids`) -
        # csakhogy az utóbbi üres is lehet, és olyankor szándékosan nem szűrünk.
        #
        # EZ A JELÖLÉS A MÁSODIK VÉDŐVONAL, ÉS SZÁNDÉKOSAN NEM TILTÁS. A tétel bekerül a
        # listába, a technikus bejelölheti és feltelepítheti - csak nem lesz ELŐRE
        # kipipálva, és az AutoFix (ahol senki nem ül a gép előtt) kihagyja. Ez nem
        # eszköz-kizárás: az eszköz minden körben, minden forrásból keresésre kerül,
        # lásd CLAUDE.md "MINDEN ESZKÖZ KAPJON DRIVERT".
        #
        # NEM VESZÍTÜNK VELE VALÓDI CSOMAGOT: ha a csomag tényleg ehhez a géphez való, a
        # gép SAJÁT `&SUBSYS_`-kulcsa is behozza, és akkor a `spec_by_guid` ott adja a
        # kisebb (specifikusabb) indexet - vagyis a jelölés fel sem kerül.
        #
        # KIVÉTEL: HIBAKÓDOS eszköz. Ott nincs működő driver, tehát bármi jobb a semminél -
        # ugyanaz az elv, mint a downgrade-védelemnél és a SUBSYS-szűkítésnél fentebb.
        best_spec = spec_by_guid.get(best_id, 99)
        src_key = hwids[best_spec] if best_spec < len(hwids) else ''
        class_code_only = ('&CC_' in (src_key or '').upper()) and not item.get('err_code')
        if class_code_only:
            logging.warning(
                f"[CATALOG] {item['name']}: a nyertes csomag ('{best_title[:60]}') KIZÁRÓLAG "
                f"osztálykód-kulcsról jött ({src_key}) - vagyis 'bármely gyártó ilyen fajta "
                f"eszközéhez' szól, nem ehhez a géphez. A gép saját SUBSYS-kulcsa nem hozta be. "
                f"Felajánljuk, de NEM jelöljük be előre, és az AutoFix kihagyja.")

        cab_url = self._catalog_download_url(best_id, ssl_ctx, item['name'])
        if not cab_url:
            return None
        logging.debug(f"[CATALOG] Találat: {item['name']} ('{best_title}') - {cab_url[:50]}...")
        return {
            "name": item['name'], "cat": item['cat'], "hwid": item['id'],
            "url": cab_url, "pnp_id": item.get('pnp_id', ''),
            # A tétel katalógus-GUID-ja + a tartalék jelöltek [(guid, cím, dátum)]: a
            # telepítő ezekből tud továbblépni, a no-bind tár pedig GUID szerint jegyzi
            # meg, melyik konkrét csomag bukott meg ezen az eszközön.
            "cat_guid": best_id,
            "alt_candidates": alts,
            # WINDOWS-ALAPDRIVEREN FUT-E MOST? A telepítő ebből tudja, hogy a hosszabb
            # (CATALOG_INBOX_FALLBACK_CANDIDATES) jelölt-listát kell végigjárnia.
            #
            # ENÉLKÜL A 2026-08-26-I TARTALÉK-SZABÁLY HATÁSTALAN VOLT: a kereső itt
            # gondosan 6 jelöltet állított sorba (a legspecifikusabb kulcs felől,
            # RÉGEBBI kiadásokat is beengedve), a telepítő viszont fixen a rövid,
            # 3-as korlátot használta - vagyis pont azokat a tartalékokat vágta le,
            # amikért az egész szabály született. Terepen mérve (ASRock B450M + ALC892):
            # az eszköz SAJÁT SUBSYS-kulcsa 25 sort ad (2019-2021, ASRock-specifikus
            # Realtek csomagok), az általános kulcs viszont a 2026-os, MÁS gyártóknak
            # szóló változatokat - a napló pedig "a katalógus 3 jelöltjéből egyik sem"
            # üzenettel zárult, holott a 4-6. jelölt lett volna a jó.
            "inbox_now": bool(_is_inbox_driver(inst)),
            "installed_version": inst_ver_str,
            "installed_date": inst.get('date', ''),
            # A telepítés UTÁNI kötés-ellenőrzéshez: az eszköz ÖSSZES valódi hardver-
            # azonosítója (a csomag alkalmazhatóságához) és a MOSTANI INF-je (ha telepítés
            # után sem változik, a csomag felment ugyan, de az eszköz nem vette át).
            "all_hwids": list(item.get('all_hwids') or []),
            "installed_inf": (inst.get('inf') or '').strip().lower(),
            "wu_title": f"MS Katalógus: {best_title}",
            "wu_date": best_date,
            # A felület ezt jelöli meg külön ("most Microsoft alapdriver"), és a
            # telepítő ezeknél futtat utóellenőrzést + szükség esetén visszaállítást.
            "generic_replace": replace_inbox,
            "installed_provider": inst.get('provider', ''),
            # KOCKÁZATOS (tárolóvezérlő/lemez/firmware) találat: a felület pirossal jelöli
            # és NEM jelöli be előre. Alapesetben csak a manuális szkenben fordulhat elő -
            # az AutoFix ilyen eszközt csak akkor kérdez meg, ha a felhasználó a fix indító
            # dialógusán engedélyezte (wu_core.filter_autofix_risky_devices + a
            # deep_catalog_candidates include_risky/include_firmware kapcsolói).
            # A risk_label a listába való RÖVID felirat: a felület korábban minden `risky`
            # találatra a tárolóvezérlős szöveget írta ki, firmware-re is.
            "risky": bool(item.get('risky')),
            "risk_label": item.get('risk_label') or '',
            "risk_reason": item.get('risk_reason') or '',
            # CSAK OSZTÁLYKÓD-KULCSRÓL JÖTT (lásd a fenti blokkot): a felület nem jelöli
            # be előre és kiírja az okot, az AutoFix pedig kihagyja. A kulcsot is
            # visszaadjuk, mert a "miért nincs bepipálva?" kérdésre csak az válaszol.
            "class_code_only": class_code_only,
            "class_code_key": src_key if class_code_only else '',
        }

    def _catalog_search_collect(self, devices_to_check, installed_info=None):
        """Microsoft Update Catalog keresés a megadott eszközökre 10 szálon. Az eredményt
        LISTAKÉNT adja vissza (nem nyúl a hw_updates_pool-hoz), így az AutoFix záró
        katalógus-köre is használhatja; a manuális szken a _catalog_search wrapperen át
        appendeli a poolhoz."""
        logging.info(f"[CATALOG] _catalog_search_collect() - {len(devices_to_check)} eszköz ellenőrzése...")
        import ssl
        ssl_ctx = ssl.create_default_context()
        if installed_info is None:
            installed_info = self._get_installed_driver_info()
        # A tartós no-bind tár EGYSZER olvasva (nem eszközönként/szálanként): eszközönként
        # azok a katalógus-GUID-ok, amiket egy korábbi futás már letöltött és az INF-vizsgálat
        # elvetett. Ezeket a jelöltválasztás átugorja - így nem tölthetjük le másodszor
        # ugyanazt az 1,2 GB-ot ugyanarra a kártyára.
        known_records = self._no_bind_load()
        bad_by_pnp = {}
        for rec in known_records:
            bad_by_pnp.setdefault(_device_stem(rec.get('pnp')), []).append(rec)
        found = []
        lock = threading.Lock()
        q = queue.Queue()
        for dev in devices_to_check:
            q.put(dev)

        # ÉLŐ VISSZAJELZÉS (2026-08-29, explicit user decision). Ez a kör a szken leghosszabb
        # szakasza: 90+ eszköz, eszközönként max 4 HTTP-lekérdezés, 10 szálon - percekig tart.
        # Eddig EGYETLEN státuszsort küldött az elején, utána semmit, így a technikus nem
        # tudta megkülönböztetni a dolgozó programot a beragadttól ("azt se latom h most keres
        # e valamit vagy beragadt"). Eszközönként jelezünk vissza: hányadiknál tartunk, mennyi
        # a találat, és épp melyik eszközt kérdezzük.
        total_cat = len(devices_to_check)
        progress = {'done': 0}

        def _report(dev_name):
            """Egy eszköz feldolgozása után jelez. A számlálót a `lock` védi: 10 szál írja."""
            with lock:
                progress['done'] += 1
                done, hits = progress['done'], len(found)
            self.emit('hw_scan_progress', {
                'status': f'🌐 Katalógus-keresés: {done}/{total_cat} eszköz'
                          + (f' · {hits} találat' if hits else ''),
                'detail': dev_name,
                'determinate': True, 'current': done, 'total': total_cat,
            })

        def cat_worker():
            while not q.empty():
                try:
                    dev = q.get_nowait()
                except Exception:
                    break
                try:
                    hit = self._catalog_find_driver(dev, installed_info, ssl_ctx,
                                                    known_no_bind=bad_by_pnp)
                    if hit:
                        with lock:
                            found.append(hit)
                except Exception as e:
                    # TELJES VEREMKÉPPEL ÉS WARNING-GAL (2026-09-07). Két ok:
                    #  1. Ez SZÁLBAN futó munka, és a projekt legdrágább hibája pont ez
                    #     volt (2026-09-01, `NameError: title`): a kivétel elnyelődött, az
                    #     eszköz se a sikeres, se a sikertelen listára nem került, csak
                    #     ELTŰNT. Egy egysoros DEBUG üzenet ehhez kevés volt.
                    #  2. A `_catalog_find_driver` kikerült a [CALL]-rétegből (log-zaj,
                    #     lásd common._CALL_LOG_EXCLUDE), és eddig CSAK az adott veremképet
                    #     - vagyis enélkül a csere információt VESZTENE, nem csak zajt.
                    # Nem hamis riasztás: a hálózati hibákat a _catalog_fetch_rows már
                    # elkapja, ide csak valódi rendellenesség jut el.
                    logging.warning(f"[CATALOG] Kivétel az eszköz keresése közben, "
                                    f"ez az eszköz kimaradt: {dev.get('name')} - {e}", exc_info=True)
                try:
                    _report(dev.get('name') or '')
                except Exception as e:
                    # A visszajelzés SOHA nem akaszthatja meg a keresést.
                    logging.debug(f"[CATALOG] Folyamatjelzés hiba: {e}")
                q.task_done()

        threads = [threading.Thread(target=cat_worker, daemon=True, name=f"catalog-{i}") for i in range(10)]
        for t in threads:
            t.start()
        # A join-plafon az ESZKÖZSZÁMHOZ igazodik. A fix 120 mp a szűk kiegészítéshez
        # (1-5 eszköz) készült; a mély szken 25-30 eszközt ad, eszközönként max 4 HTTP
        # lekérdezéssel - ott a régi plafon lejárt volna, MIELŐTT a szálak végeznek, és a
        # `found` lista hiányosan (ráadásul még írás közben) került volna vissza.
        # ~4 mp/eszköz 10 szálon bőven tartalékos, a 900 mp abszolút ceiling.
        join_timeout = min(900, max(120, len(devices_to_check) * 4))
        for t in threads:
            t.join(timeout=join_timeout)
        alive = [t for t in threads if t.is_alive()]
        if alive:
            logging.warning(f"[CATALOG] {len(alive)} szál még fut a {join_timeout}s plafon után - "
                            f"a találati lista hiányos lehet ({len(found)} db).")
        # A kör alatt összegyűlt katalógus-válaszok kiírása, EGY írással. Ez az, ami a
        # következő láb zárókörének megspórolja ugyanezt a néhány száz HTTP-kérést -
        # a lábak külön folyamatok, memóriában semmi nem élné túl az újraindítást.
        self._catalog_rows_flush()
        # Ugyanez a részletlapokból kiolvasott, támogatott-azonosító listákra: enélkül
        # minden láb újraszedi ugyanazt az 50-70 lapot (mérve: a zárókörök ideje szinte
        # teljes egészében ez volt).
        self._catalog_hwids_flush()
        # UGYANAZ A CSOMAG TÖBB ESZKÖZRE: egy chipset-csomag jellemzően több PnP-eszközt
        # szolgál ki (élő mérés: az "AMD PCI" kétszer szerepelt, azonos letöltési URL-lel),
        # és enélkül ugyanazt a cab-ot kétszer töltenénk le és telepítenénk. A második
        # telepítés amúgy is "already exists" no-op lenne, csak sávszélességbe kerül.
        by_url, deduped = {}, []
        for hit in found:
            u = hit.get('url') or ''
            if u and u in by_url:
                by_url[u].append(hit.get('name'))
                continue
            by_url[u] = []
            deduped.append(hit)
        for u, extra in by_url.items():
            if extra:
                logging.info(f"[CATALOG] Azonos csomag több eszközre, egyszer telepítjük - "
                             f"kihagyott duplikátumok: {extra}")
        # TARTÓS NO-BIND JELÖLÉS: amit egy korábbi futás már feltett/kipróbált, de az
        # eszköz nem vette át (catalog_no_bind.json), azt megjelöljük - a felület így
        # nem ajánlja fel ELŐRE BEJELÖLVE ugyanazt a más gépre szabott csomagot minden
        # AutoFix után (terepi visszajelzés, 2026-07-28). Csak jelölés: a felhasználó
        # bejelölheti, az AutoFix saját (láncon belüli) tiltólistáját nem érinti.
        # A jelölés HÁROM úton illeszkedhet, és mindegyikre szükség van:
        #  (a) ugyanaz a katalógus-GUID (a legpontosabb - egy címhez 10 sor is tartozhat);
        #  (b) ugyanaz a cím (a régi kulcs, a korábbi bejegyzésekhez);
        #  (c) UGYANAZ A CSOMAG RÉGEBBI/AZONOS KIADÁSA. Ez utóbbi a 2026-08-05-i eset:
        #      a záró kör a Realtek audiót 6.0.9992.1 [2026-05-18] néven ajánlotta, míg a
        #      feljegyzett bukás a 6.0.10007.1 [2026-06-22] volt - más cím, tehát a régi
        #      kulcs nem fogta, és a program letöltötte ugyanazt a Clevo-csomagot még
        #      egyszer. Egy ÚJABB kiadás viszont továbbra sem maszkolható (szándékos:
        #      lehet, hogy a gyártó épp kijavította) - ezért a release_rank-összevetés.
        #
        # AZ ESZKÖZ AZONOSÍTÁSA A HARDVER-AZONOSÍTÓ TÖRZSE, NEM A PÉLDÁNY-AZONOSÍTÓ
        # (2026-08-31, mérve a fejlesztői gépen). A tár a TELJES példány-azonosítót
        # tárolta, annak a farkában viszont ott a PnP példányszám, amit a Windows LÉPTET,
        # ha egy eszköz-csomópontot eltávolítanak és újra felderítenek - vagyis pontosan
        # akkor, amikor a program `pnputil /remove-device`-t futtat (újrakötés-kör,
        # regresszió-javítás). A gép saját no-bind emlékezetét tehát a saját maga által
        # kiváltott újrafelderítés törölte el. A 4 feljegyzésből 2 így vált halottá:
        #     USB\VID_041E&PID_3274&MI_03\7&887C350&0&0003 -> ...&1&0003
        #     HDAUDIO\...&REV_1003\5&337FEF56&0&0001       -> ...&1&0001
        # Következmény: ugyanazt a bizonyítottan nem illeszkedő csomagot a felület újra
        # ELŐRE BEJELÖLVE ajánlotta, az AutoFix pedig újra letöltötte.
        #
        # A törzs (az utolsó `\` előtti rész) maga a hardver-azonosító, ami újrafelderítés
        # után is ugyanaz. Két azonos eszköznél közös a törzs - ez helyes: ami az egyikre
        # nem alkalmazható, az az ikertestvérére sem. Régi bejegyzésekhez is működik,
        # nem kell séma-váltás.
        if known_records:
            marked = []
            for hit in deduped:
                pnp = _device_stem(hit.get('pnp_id'))
                title = hit.get('wu_title') or ''
                hit_rank = release_rank(hit.get('wu_date'), title)
                for rec in known_records:
                    if _device_stem(rec.get('pnp')) != pnp:
                        continue
                    same = (rec.get('guid') and rec['guid'] == hit.get('cat_guid')) or \
                           _nb_title(rec.get('title')) == _nb_title(title)
                    older_variant = (
                        not same and rec.get('title')
                        and catalog_title_family(rec['title']) == catalog_title_family(title)
                        and hit_rank <= release_rank(rec.get('date'), rec.get('title')))
                    if same or older_variant:
                        hit['prev_no_bind'] = True
                        hit['prev_no_bind_reason'] = rec.get('reason') or ''
                        marked.append(hit.get('name'))
                        break
            if marked:
                logging.info(f"[CATALOG] {len(marked)} találat megjelölve (korábbi futásban az eszköz "
                             f"nem vette át, nem lesz előre bejelölve): {marked}")
        logging.info(f"[CATALOG] Kész - {len(deduped)} eszközre van katalógus-találat"
                     + (f" ({len(found) - len(deduped)} duplikált csomag összevonva)" if len(found) != len(deduped) else ""))
        return deduped

    def _catalog_search(self, devices_to_check, installed_info=None):
        """Katalógus-keresés a manuális szkenhez: a találatok a self.hw_updates_pool-ba
        KERÜLNEK HOZZÁ (nem törli a meglévőt, így a hibrid kiegészítő mód is ezt hívja).
        A telepített/naprakész listát a hívó számolja a teljes pool alapján."""
        found = self._catalog_search_collect(devices_to_check, installed_info)
        self.hw_updates_pool.extend(found)

    # ================================================================
    # WU DRIVER INSTALL
    # ================================================================
    def install_selected_wu(self, selected_indices):
        logging.info(f"[API] install_selected_wu() - {len(selected_indices)} index kiválasztva")
        logging.debug(f"[WU_INSTALL] Indexek: {selected_indices}")
        selected_pool = [self.hw_updates_pool[i] for i in selected_indices if 0 <= i < len(self.hw_updates_pool)]
        if not selected_pool:
            logging.warning("[WU_INSTALL] Nincs érvényes driver kiválasztva!")
            self.emit('toast', {'message': '⚠️ Nincs érvényes driver kiválasztva!', 'type': 'warning'})
            return

        # DISZPÉCSER: a pool a hibrid keresés óta vegyes lehet (WU-s elemek update_id-vel,
        # katalógusosak url-lel), ezért a telepítési módot ELEMENKÉNT döntjük el, nem
        # globálisan - a régi, globális wu_api_mode-alapú elágazás vegyes poolnál a
        # katalógusos elemeket a WU-s útra küldte volna (vagy fordítva).
        if self.target_os_path:
            # A WU API (Microsoft.Update.Session COM) mindig az élő rendszert célozza meg,
            # offline cél-OS esetén ez csendben a host gépre telepítene drivert a kiválasztott
            # offline image helyett - ezért ilyenkor minden elem a dism-alapú katalógus úton megy.
            logging.warning("[WU_INSTALL] Offline cél-OS: minden elem katalógus (DISM) módban települ.")
            self.emit('toast', {'message': '⚠️ Offline célrendszer esetén a WU API mód nem elérhető - katalógus (DISM) módban folytatjuk.', 'type': 'warning'})
            wu_items, cat_items = [], selected_pool
        else:
            wu_items = [d for d in selected_pool if d.get('update_id')]
            cat_items = [d for d in selected_pool if not d.get('update_id')]
        logging.info(f"[WU_INSTALL] {len(selected_pool)} driver telepítése (WU API: {len(wu_items)}, Katalógus: {len(cat_items)})")

        def worker():
            total = len(wu_items) + len(cat_items)
            self.emit('task_start', {'task': 'wu_install', 'title': f'Driver Telepítés ({total} db)'})
            # Az OKRB (újraindítás szükséges) jelzést a _install_wu_api_sync állítja be.
            self._wu_reboot_required = False
            success = fail = 0
            cancelled = False
            # Biztonsági háló a manuális telepítés elé is (az AutoFix eddig is csinálta):
            # gyors visszaállítási pont, mielőtt driverhez nyúlunk. Élő rendszeren fut
            # csak - offline cél-OS-nél a Checkpoint-Computer a HOST gépet mentené.
            if not self.target_os_path:
                self._create_restore_point_sync(task_id='wu_install')
            if wu_items:
                s, f, cancelled = self._install_wu_api_sync(wu_items)
                success += s
                fail += f
            if cat_items and not cancelled:
                if wu_items:
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'\n--- Katalógusos elemek telepítése ({len(cat_items)} db) ---'})
                s, f, cancelled = self._install_catalog_sync(cat_items)
                success += s
                fail += f
            if cancelled:
                # A MEGSZAKÍTOTT FUTÁS NAPLÓJA IS FELMEGY: pont egy félbehagyott
                # telepítésnél a legérdekesebb, meddig jutott a program.
                self.upload_run_log('wu_install',
                                    f'KÉZI telepítés MEGSZAKÍTVA - {success} sikeres, {fail} sikertelen')
                self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                return
            # ZÁRÓ DriverStore-TAKARÍTÁS: egy frissen telepített driver régi verziója
            # ottmarad a DriverStore-ban - itt azonnal el is takarítjuk (közös mag:
            # dupdrivers_core.auto_cleanup_duplicates, ugyanazokkal a biztonsági
            # szabályokkal, mint a kézi takarító panel). Csak élő rendszeren - offline
            # cél-OS-nél a dup-takarítás nem értelmezett (a hívók mind elutasítják).
            if success > 0 and not self.target_os_path:
                self.emit('task_progress', {'task': 'wu_install', 'log': '\n🧹 DriverStore-takarítás: a lecserélt driverek régi verzióinak törlése...'})
                dupdrivers_core.auto_cleanup_duplicates(
                    self._run,
                    lambda m: self.emit('task_progress', {'task': 'wu_install', 'log': m}),
                    self._get_third_party_drivers,
                    check_cancel=self._check_cancel)
            reboot_needed = getattr(self, '_wu_reboot_required', False)
            msg = f'Kész! Sikeres: {success}, Sikertelen: {fail}'
            if reboot_needed:
                msg += ' — ⚠️ Újraindítás szükséges!'
                self.emit('task_progress', {'task': 'wu_install', 'log': '\n⚠️ Legalább egy driver csak ÚJRAINDÍTÁS után lép életbe!'})
                self.emit('toast', {'message': '⚠️ A telepített driverek egy része csak újraindítás után él!', 'type': 'warning'})
            # NAPLÓ-FELTÖLTÉS a kézi telepítés végén is (2026-09-03, explicit user
            # decision) - eddig CSAK az 1 kattintásos fix töltött fel, tehát egy kézi
            # telepítés után a napló a gépen maradt. SZÁNDÉKOSAN a `task_complete` ELŐTT,
            # mint a láncnál: így a technikus a folyamat-ablakban látja a feltöltés
            # sorait, nem egy már lezárt művelet után futna némán. A közös segédfüggvény
            # mindent elnyel, tehát a telepítés eredményét nem befolyásolhatja.
            self.upload_run_log('wu_install',
                                f'KÉZI telepítés - {success} sikeres, {fail} sikertelen'
                                + (' (újraindítás szükséges)' if reboot_needed else ''))
            self.emit('task_complete', {'task': 'wu_install', 'success': success, 'fail': fail, 'status': msg,
                                        'counter': msg, 'reboot_required': reboot_needed})
            # Chipset/USB-vezérlő driver után új eszközök bukkanhatnak elő (az AutoFix
            # ezért megy több körben) - siker esetén a felület felajánlja az új szkennelést.
            #
            # ÚJRAKÖTÉS FELAJÁNLÁSA (2026-09-03): ha a telepítés során volt olyan csomag,
            # ami FELMENT, de az eszköz nem vette át, akkor az új szkennelés önmagában
            # semmit nem old meg - a driver ott van, a KÖTÉS hiányzik, és arra az
            # újrakötés-kör való. Az 1 kattintásos fix ezt a lánc végén magától lefuttatja
            # (`_autofix_closing_rebind`), a kézi úton viszont eddig a technikusnak kellett
            # kitalálnia, hogy ez a dolga - pedig a program pontosan tudja, hogy kellene.
            #
            # CSAK AKKOR AJÁNLJUK FEL, HA TÉNYLEG FELMENT VALAMI (2026-09-18, explicit
            # user decision): *"most pl nem sikerült telepiteni egyet se, ha 0 sikeres
            # akkor feleslegesen dobja fel h kössem ujra a drivereket... csak akkor
            # fusson le ha sikeresen felrakott bármi drivert, ha nem akkor feleslegesen
            # akarja újrakötni"*.
            #
            # EZ A 2026-09-03-I SZABÁLY VISSZAVONÁSA, és a régi szöveget azért hagyom itt,
            # mert a "miért NEM úgy csináljuk" ugyanolyan értékes: akkor az ablak azért
            # kezdett MINDEN befejezett telepítés végén feljönni, mert egy terepi futásban
            # mind a három találatot az INF-vétó fogta meg (`success=0`, `nobind=[]`), az
            # esemény el sem sült, és a technikus azt látta, hogy "még mindig nem dobja
            # fel". A mostani döntés ugyanarra a helyzetre az ELLENKEZŐ választ adja, és
            # jó okkal: ha egyetlen csomag sem került a gépre, akkor a Windowsnak nincs
            # MIBŐL jobb drivert választania az újrafelderítéskor - garantáltan ugyanazt
            # kötné vissza, az ára pedig egy teljes ÚJRAINDÍTÁS. Vagyis a régi viselkedés
            # egy fölösleges reboot felajánlása volt egy olyan műveletért, ami bizonyítottan
            # nem tud változtatni semmin. (Ugyanez az érv szűkítette 2026-09-01-én a lánc
            # végi újrakötés-kört a `REBIND_ONLY_WITH_PACKAGE`-dzsel.)
            #
            # A `nobind` ág MARAD a feltételben, és ez nem következetlenség: ott a csomag
            # FELKERÜLT a gépre, csak az eszköz nem vette át - pontosan az az eset, amit az
            # újrakötés + újraindítás megold. Az `installed == 0 and nobind` kombináció
            # tehát valódi teendő, nem üresjárat.
            #
            # Megszakításnál NEM ajánljuk fel: ott a technikus épp leállította a műveletet,
            # egy azonnali "újraindítsam?" kérdés a szándéka ellen menne.
            nobind = list(getattr(self, '_catalog_staged_nobind', None) or [])
            if not self.target_os_path and (success > 0 or nobind):
                self.emit('offer_rescan', {'installed': success, 'rebind_devices': nobind})
            elif not self.target_os_path:
                logging.info("[WU_INSTALL] Újrakötés-felajánlás kihagyva: 0 sikeres "
                             "telepítés és nincs kötés nélkül maradt csomag - nincs mit "
                             "újrakötni, egy újraindítás itt tiszta veszteség lenne.")

        self._safe_thread('wu_install', worker)

    def _install_wu_api_sync(self, selected_pool):
        """A kijelölt WU-s (update_id-s) elemek telepítése a KÖZÖS _build_wu_install_ps
        scripttel. A diszpécser (install_selected_wu) worker-szálán fut, task_start/
        task_complete NÉLKÜL. Visszatérés: (sikeres, sikertelen, megszakítva)."""
        logging.info(f"[WU_API] WU API telepítés indítása: {len(selected_pool)} driver")
        self.emit('task_progress', {'task': 'wu_install', 'log': 'Windows Update szervereiről történő telepítés indítása...', 'indeterminate': True})

        # A kiválasztott driverek azonosítói: elsődlegesen a pontos WU UpdateID
        # (a hardver-szkennelés eredményéből), HWID-prefix egyezés csak azokra a
        # bejegyzésekre, amelyeknek nincs UpdateID-ja - a kettő NEM vagylagos egy
        # elemen belül, mert azonos HWID-n több különböző csomag is lóghat.
        pool_uids = []
        pool_hwids = []
        for drv in selected_pool:
            if drv.get('update_id'):
                pool_uids.append(str(drv['update_id']))
            elif drv.get('hwid'):
                pool_hwids.append(str(drv['hwid']).upper())

        if not pool_uids and not pool_hwids:
            logging.warning("[WU_INSTALL] A kiválasztott elemekhez nincs UpdateID/HWID - telepítés megszakítva.")
            self.emit('toast', {'message': '⚠️ A kiválasztott driverekhez nincs azonosító, futtass új hardver-szkennelést!', 'type': 'warning'})
            self.emit('task_progress', {'task': 'wu_install', 'log': '⚠️ Hiányzó azonosítók - futtass új szkennelést!'})
            return 0, 0, False

        # A telepítő script a KÖZÖS _build_wu_install_ps-ből jön - az AutoFix (GUI és CLI)
        # is ugyanazt használja, itt csak a szűrők (kijelölt UpdateID-k) különböznek.
        ps_script = _build_wu_install_ps(target_uids=pool_uids, target_hwids=pool_hwids)
        # Ez volt a projekt EGYETLEN olyan Popen-je, ami nem írta ki a futtatott parancsot
        # (a többi mind logol egy "[CMD] Popen futtatása:" sort) - ráadásul pont a manuális
        # telepítési úton, ami történetileg a "AutoFix megy, a manuális némán törött"
        # hibaosztály helyszíne (Build ~192). A kért UpdateID-k/HWID-ek nélkül egy
        # "semmit nem telepített" bejelentést nem lehet kivizsgálni.
        logging.info(f"[WU_INSTALL] Kért UpdateID-k ({len(pool_uids)}): {pool_uids}")
        logging.info(f"[WU_INSTALL] Kért HWID-ek ({len(pool_hwids)}): {pool_hwids}")
        logging.debug(f"[CMD] Popen futtatása: {ps_script[:300]}...")
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            startupinfo=self._si, creationflags=self._nw)

        success = 0
        fail = 0
        install_total = 0
        had_error = False
        # A kijelölt elemek nevei + a script FOUND: sorai: a végén ebből derül ki, ha egy
        # kiválasztott driver nem került a telepítési listába (lásd unoffered_requested_titles).
        requested_titles = [str(d.get('title') or d.get('name') or '') for d in selected_pool]
        requested_titles = [t for t in requested_titles if t]
        found_titles = []

        # A sorokat a KÖZÖS _iter_process_lines olvassa (wu_core): cancel-ellenőrzés
        # 0,5 mp-enként (nem csak új sor érkezésekor - régen a Mégse halott volt, ha a
        # scripten belüli WU-keresés beragadt), plusz watchdog: 30 perc néma folyamatot leöl.
        try:
            for line in _iter_process_lines(
                    process, self._run, cancel_check=self._check_cancel,
                    # Lásd az AutoFix párját: a watchdog-hosszabbítás látszódjon a felületen.
                    on_notice=lambda msg: self.emit('task_progress', {'task': 'wu_install', 'log': msg})):
                if line.startswith("INIT:") or line.startswith("SEARCH:"):
                    self.emit('task_progress', {'task': 'wu_install', 'status': line.split(":", 1)[1].strip(), 'log': line})
                elif line.startswith("FOUND:"):
                    found_titles.append(line[6:].strip())
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  📦 {line[6:].strip()}'})
                elif line.startswith("SKIP:"):
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  ⏭ {line[5:].strip()}'})
                elif line.startswith("TOTAL:"):
                    m = re.search(r'(\d+)', line)
                    if m:
                        install_total = int(m.group(1))
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'Összesen {install_total} driver telepítése...',
                                                'total': install_total, 'current': 0, 'counter': f'0 / {install_total}'})
                elif line.startswith("DLONE:"):
                    self.emit('task_progress', {'task': 'wu_install', 'status': f'⬇ Letöltés: {line[6:].strip()}', 'log': f'  ⬇ {line[6:].strip()}'})
                elif line.startswith("INSTONE:"):
                    self.emit('task_progress', {'task': 'wu_install', 'status': f'⚙ Telepítés: {line[8:].strip()}', 'log': f'  ⚙ {line[8:].strip()}'})
                elif line.startswith("OKRB:"):
                    # Sikeres, de a WUA jelezte: a driver csak újraindítás után él.
                    success += 1
                    self._wu_reboot_required = True
                    done = success + fail
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  ✅ {line[5:].strip()} (⚠️ újraindítás szükséges)',
                                                'current': done, 'total': install_total, 'counter': f'{done}/{install_total}', 'ok': success, 'fail': fail})
                elif line.startswith("OK:"):
                    success += 1
                    done = success + fail
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  ✅ {line[3:].strip()}',
                                                'current': done, 'total': install_total, 'counter': f'{done}/{install_total}', 'ok': success, 'fail': fail})
                elif line.startswith("FAIL:"):
                    fail += 1
                    done = success + fail
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  ❌ {line[5:].strip()}',
                                                'current': done, 'total': install_total, 'counter': f'{done}/{install_total}', 'ok': success, 'fail': fail})
                elif line.startswith("DONE:"):
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'\n--- {line[5:].strip()} ---'})
                elif line.startswith("EMPTY:"):
                    self.emit('task_progress', {'task': 'wu_install', 'log': line[6:].strip()})
                elif line.startswith("ERROR:"):
                    had_error = True
                    logging.error(f"[WU_INSTALL] PowerShell hiba: {line[6:].strip()}")
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'❌ HIBA: {line[6:].strip()}'})
                else:
                    self.emit('task_progress', {'task': 'wu_install', 'log': line})
        except WuProcessAborted as ab:
            if ab.reason == 'cancel':
                self.emit('task_progress', {'task': 'wu_install', 'log': '\n❗ Megszakítva!'})
                return success, fail, True
            had_error = True
            self.emit('task_progress', {'task': 'wu_install',
                                        'log': '\n❌ A Windows Update telepítő 30 percen át nem adott életjelet - a watchdog leállította! '
                                               '(Ez tipikusan beragadt WU szolgáltatásra utal - próbáld újra, vagy a katalógus-találatokat telepítsd.)'})

        # Kijelölt, de a telepítési listába be sem került csomagok - e nélkül némán tűnnének
        # el (a felhasználó 3 drivert jelöl ki, és csak 2-ről lát visszajelzést).
        for t in unoffered_requested_titles(requested_titles, found_titles):
            self.emit('task_progress', {'task': 'wu_install',
                                        'log': f'  ⏭ {t} - a Windows Update már telepítettként látja, nincs mit telepíteni.'})

        if success > 0:
            self.emit('task_progress', {'task': 'wu_install', 'log': 'Eszközök újraszkennelése...', 'status': 'Aktiválás...'})
            self._run(['pnputil', '/scan-devices'])
            self.emit('task_progress', {'task': 'wu_install', 'log': '✅ Eszközök frissítve!'})

        if had_error and success == 0 and fail == 0:
            self.emit('task_progress', {'task': 'wu_install', 'log': '❌ A WU telepítés hibával leállt! (részletek fent a naplóban)'})
        return success, fail, False

    # ------------------------------------------------------------------
    # FUTÁSOKON ÁTÍVELŐ "nem kötött rá" emlékezet (catalog_no_bind.json).
    # Miért kell az autofix_stats.json-beli lista MELLÉ: az a lánccal együtt törlődik,
    # így a kézi szken az AutoFix után újra felajánlotta (előre bejelölve!) azokat a
    # csomagokat, amikről a lánc már bizonyította, hogy más gépre szabottak vagy az
    # eszköz nem veszi át őket. A fájl CSAK jelölésre való (a felület nem jelöli be
    # előre + jelvényt tesz rá) - telepítést nem tilt, az AutoFix köreit nem szűri:
    # egy friss lánc a törlés/újratelepítés után szándékosan újra próbálkozhat, és egy
    # esetleg tévesen feljegyzett csomagot a felhasználó kézzel bármikor feltehet.
    # Kulcs: (pnp_id, csomagcím) - egy ÚJABB katalógus-kiadás címe eltér, azt tehát
    # semmi nem jelöli meg. Sikeres, IGAZOLTAN átvett telepítés törli a bejegyzést.
    # ------------------------------------------------------------------
    def _no_bind_store_path(self):
        return os.path.join(_app_data_dir(), 'catalog_no_bind.json')

    def _no_bind_load(self):
        """A tartós no-bind lista beolvasása; hibánál üres lista (a jelölés elmaradása
        nem hiba, csak a kényelmi funkció esik ki)."""
        try:
            with open(self._no_bind_store_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:
            logging.debug(f"[CATALOG] catalog_no_bind.json nem olvasható: {e}")
            return []

    def _no_bind_record(self, no_bind_items, bound_ok_items=None):
        """Nem-kötő csomagok feljegyzése + igazoltan átvett telepítések kivezetése."""
        try:
            existing = self._no_bind_load()
            bound_keys = {(_device_stem(d.get('pnp_id')), _nb_title(d.get('wu_title')))
                          for d in (bound_ok_items or [])}
            removed = [t.get('name') or t.get('title') for t in existing
                       if (_device_stem(t.get('pnp')), _nb_title(t.get('title'))) in bound_keys]
            if removed:
                existing = [t for t in existing
                            if (_device_stem(t.get('pnp')), _nb_title(t.get('title'))) not in bound_keys]
                logging.info(f"[CATALOG] {len(removed)} bejegyzés törölve a tartós no-bind listáról "
                             f"(az eszköz most már átvette a csomagot): {removed}")
            keys = {(_device_stem(t.get('pnp')), _nb_title(t.get('title'))) for t in existing}
            added = []
            for d in (no_bind_items or []):
                k = (_device_stem(d.get('pnp_id')), _nb_title(d.get('wu_title')))
                if not k[1] or k in keys:
                    continue
                keys.add(k)
                # A GUID és a KIADÁS DÁTUMA is elmegy a bejegyzésbe: a GUID-ról ismerhető
                # fel újra pontosan ugyanaz a katalógus-sor (egy címhez tíz is tartozhat),
                # a dátum pedig ahhoz kell, hogy a csomag RÉGEBBI kiadását se töltsük le
                # újra - miközben egy ÚJABB kiadás továbbra sem maszkolódik le.
                existing.append({'pnp': k[0], 'title': k[1], 'name': d.get('name') or '',
                                 'guid': d.get('cat_guid') or '',
                                 # A LETÖLTÉSI URL a legerősebb kulcs: mérve (2026-08-06) a
                                 # katalógus UGYANAZT a cab-ot 25 külön bejegyzésként (25 GUID)
                                 # listázza, tehát a GUID-tiltás önmagában nem akadályozza meg,
                                 # hogy a következő futás egy másik bejegyzésen keresztül
                                 # ugyanazt az 1,2 GB-ot letöltse. Az URL tartalom-hasht
                                 # tartalmaz, így egy ÚJABB kiadás automatikusan más URL-t kap.
                                 'url': d.get('url') or '',
                                 'date': d.get('wu_date') or '',
                                 'reason': d.get('no_bind_reason') or '',
                                 'recorded': time.strftime('%Y-%m-%d')})
                added.append(d.get('name') or k[1])
            if not added and not removed:
                return
            existing = existing[-100:]   # ne nőhessen korlátlanul
            with open(self._no_bind_store_path(), 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=1)
            if added:
                logging.info(f"[CATALOG] {len(added)} nem-kötő csomag feljegyezve a tartós emlékezetbe "
                             f"({self._no_bind_store_path()}): {added}")
        except Exception as e:
            logging.warning(f"[CATALOG] A tartós no-bind lista frissítése nem sikerült: {e}")

    # ------------------------------------------------------------------
    # ELHALASZTOTT INF-KIVEZETÉS (pending_inf_cleanup.json).
    # Miért kell: a reboot-igényes (3010) telepítésnél a kivezetés jogosan marad ki -
    # a kötés csak a következő bootnál dől el, most nem ítélkezhetünk. Csakhogy
    # korábban EZZEL VÉGE IS VOLT: senki nem tért vissza rájuk, így a fel nem használt
    # INF-ek örökre a DriverStore-ban maradtak. Terepen (2026-08-05, Dell Latitude 7400):
    # az `Intel(R) PCI Express Root Port #13 - 9DB4` katalógus-cab a TELJES Intel
    # chipset-INF gyűjteményt hordozza (ApolloLake, Avoton, Baytrail, Braswell,
    # Broadwell, ColetoCreek, CougarPoint, Crystalwell, Denverton, FPGA, Haswell,
    # IceLake, IvyBridge, IvyTown, JakeTown, KabyLake...), a pnputil 3010-nel tért
    # vissza, a kivezetés kimaradt - és a gép 23 third-party csomagja 163-ra hízott
    # (a záró duplikátum-takarítás után is 143 maradt, mert ezek nem duplikátumok:
    # mindnek külön eredeti INF-neve van). Mellékhatás: a `dism /Get-Drivers` 0,6 mp-ről
    # 77 mp-re lassult. Ezért a kihagyott csomag INF-jeit ide jegyezzük fel, és a
    # KÖVETKEZŐ LÁB elején (tehát egy valódi újraindítás után) fejezzük be a munkát.
    # A fájl szándékosan NEM az autofix_stats.json (az a lánccal együtt törlődik):
    # ha a halasztás az utolsó lábban történt, a bejegyzés túléli a láncot, és a
    # program következő indulásakor takarítunk.
    # ------------------------------------------------------------------
    def _deferred_inf_cleanup_path(self):
        return os.path.join(_app_data_dir(), 'pending_inf_cleanup.json')

    def _deferred_inf_cleanup_load(self):
        """Az elhalasztott kivezetés-lista; hibánál üres (a takarítás elmaradása nem
        végzetes, csak a DriverStore marad szemetesebb)."""
        try:
            with open(self._deferred_inf_cleanup_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:
            logging.debug(f"[CATALOG_INSTALL] pending_inf_cleanup.json nem olvasható: {e}")
            return []

    def _deferred_inf_cleanup_save(self, items):
        """Üres listánál a fájl TÖRLŐDIK - így a "van-e elhalasztott munka?" kérdés a
        következő induláskor egy olcsó fájl-létezés-vizsgálat."""
        try:
            p = self._deferred_inf_cleanup_path()
            if not items:
                if os.path.exists(p):
                    os.remove(p)
                return
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(items[-500:], f, ensure_ascii=False, indent=1)
        except Exception as e:
            logging.warning(f"[CATALOG_INSTALL] pending_inf_cleanup.json írása nem sikerült: {e}")

    def _defer_inf_cleanup(self, entries, name):
        """A reboot-igényes csomag MINDEN publikált INF-jét feljegyzi későbbi elbírálásra.

        Szándékosan az összeset (nem csak a most használatlanokat): a kötés a bootnál
        dől el, tehát a mostani "used" jelzés még nem ítélet. A következő láb friss
        rendszerállapotból dönt, nem ebből a listából."""
        try:
            items = self._deferred_inf_cleanup_load()
            have = {(i.get('published') or '').lower() for i in items}
            added = []
            for inf, pub, _used in entries:
                if pub in have:
                    continue
                have.add(pub)
                items.append({'published': pub,
                              'original': inf.split('\\')[-1].split('/')[-1].lower(),
                              'package': name,
                              'recorded': time.strftime('%Y-%m-%d')})
                added.append(pub)
            if not added:
                return
            self._deferred_inf_cleanup_save(items)
            logging.info(f"[CATALOG_INSTALL] {name}: újraindítást igénylő telepítés - a csomag "
                         f"{len(added)} INF-jének elbírálása a következő lábra halasztva "
                         f"({self._deferred_inf_cleanup_path()}): {added}")
        except Exception as e:
            logging.warning(f"[CATALOG_INSTALL] Az elhalasztott kivezetés feljegyzése nem sikerült ({name}): {e}")

    def _finish_deferred_inf_cleanup(self, task_id='autofix'):
        """A korábban elhalasztott INF-kivezetés befejezése FRISS BOOT után.

        Három biztonsági szabály, mindegyik ugyanazt a hibát zárja ki (nehogy egy
        használatban lévő vagy időközben újraszámozott csomagot lőjünk ki):
          - az aktív publikált INF-ek listája (Win32_PnPSignedDriver) a döntő; ha a
            lekérdezés hibázik (None), NEM törlünk semmit és a lista is megmarad;
          - csak akkor törlünk, ha a publikált oemNN.inf MÉG MINDIG ugyanahhoz az
            eredeti INF-névhez tartozik, mint a feljegyzéskor (újraszámozás ellen);
          - a törlés sima `pnputil /delete-driver` (se /uninstall, se /force): ha bármi
            mégis használja, a pnputil elutasítja és a csomag marad.
        Visszatérés: hány csomag lett kivezetve."""
        items = self._deferred_inf_cleanup_load()
        if not items:
            return 0
        logging.info(f"[CATALOG_INSTALL] {len(items)} elhalasztott INF-bejegyzés elbírálása a friss boot után...")
        active = dupdrivers_core.get_active_published_infs(self._run)
        if active is None:
            logging.warning("[CATALOG_INSTALL] Az aktív INF-lista nem kérdezhető le - az elhalasztott "
                            "kivezetés kimarad, a lista megmarad a következő alkalomra.")
            return 0
        current = {(d.get('published') or '').lower(): (d.get('original') or '').lower()
                   for d in self._get_third_party_drivers()}
        todo, in_use, gone, renumbered = [], [], 0, []
        for it in items:
            pub = (it.get('published') or '').lower()
            if pub not in current:
                gone += 1                      # már nincs a gépen - a bejegyzés elévült
            elif current[pub] != (it.get('original') or '').lower():
                renumbered.append(pub)         # időközben más csomag kapta ezt a nevet
            elif pub in active:
                in_use.append(pub)             # a boot után mégis rákötött egy eszköz
            else:
                todo.append(it)
        if renumbered:
            logging.warning(f"[CATALOG_INSTALL] {len(renumbered)} bejegyzés kihagyva, mert a publikált név "
                            f"időközben másik csomagé lett (újraszámozás): {renumbered}")
        logging.info(f"[CATALOG_INSTALL] Elhalasztott kivezetés mérlege: {len(todo)} törlendő, "
                     f"{len(in_use)} a boot után mégis használatba került ({in_use}), "
                     f"{gone} már nincs a gépen, {len(renumbered)} újraszámozott.")
        if not todo:
            self._deferred_inf_cleanup_save([])
            return 0
        self.emit('task_progress', {'task': task_id, 'log': f'🧹 {len(todo)} fel nem használt INF kivezetése a DriverStore-ból (az előző kör újraindítás-igényes csomagjaiból)...'})
        done, refused = 0, 0
        for i, it in enumerate(todo):
            if getattr(self, '_cancel_flag', False):
                self._deferred_inf_cleanup_save(todo[i:])
                raise Exception("Magyar_Megszakit_Flag")
            pub = it['published']
            dres = self._run(['pnputil', '/delete-driver', pub], ok_codes=(0, 3010),
                             timeout=DELETE_DRIVER_TIMEOUT)
            if dres and dres.returncode in (0, 3010):
                done += 1
            else:
                refused += 1
                logging.info(f"[CATALOG_INSTALL] Kivezetés elutasítva (marad): {pub} "
                             f"({it.get('original')}, {it.get('package')}), rc={getattr(dres, 'returncode', '?')}")
        self._deferred_inf_cleanup_save([])
        logging.info(f"[CATALOG_INSTALL] Elhalasztott kivezetés kész: {done} törölve, {refused} elutasítva.")
        self.emit('task_progress', {'task': task_id, 'log': f'✅ DriverStore-takarítás: {done} fel nem használt INF kivezetve.\n'})
        return done

    def _cleanup_unused_staged_infs(self, pnp_out, name, task_id, defer=False, selected_infs=None):
        """Több-INF-es katalógus-csomag FEL NEM HASZNÁLT INF-jeinek kivezetése a DriverStore-ból.

        Miért: a `pnputil /add-driver <mappa>\\*.inf /subdirs /install` a csomag MINDEN
        INF-jét stage-eli, akkor is, ha az eszközhöz csak egy (vagy nulla) illik. Terepen
        (2026-07-28, dev gép): a Razer katalógus-cab a teljes Razer termékpaletta INF-jeit
        hordozza (rz0001dev...rz0f2cdev, egerek/billentyűzetek/headsetek külön INF-fel),
        és egyetlen AutoFix-futás után a gép 19 third-party csomagja 219-re hízott
        (mérve: 201 rz*-INF a DriverStore-ban, egyik sincs eszközhöz kötve). Funkcionálisan
        ártalmatlan, de a DriverStore-t szemeteli és a driverlistát használhatatlanná teszi.

        Döntési szabály: a pnputil kimenete blokkonként elárulja az egyes INF-ek sorsát -
        amelyik "installed on device" / "up-to-date on device" sort kapott, azt jelen lévő
        eszköz használja, MARAD; a csak stage-elt többi megy. Csak több-INF-es csomagnál
        fut (az egy-INF-es nem-kötő esetet a bind-check kezeli no-bindként). A törlés sima
        `pnputil /delete-driver` (se /uninstall, se /force): ha bármi mégis használja,
        a pnputil elutasítja és a csomag marad - ezt a kimenetel-logika elviseli.

        `defer=True` (reboot-igényes telepítés): MOST nem ítélkezünk - a kötés a következő
        bootnál dől el -, de a csomagot NEM ejtjük: feljegyezzük, és a következő láb
        (`_finish_deferred_inf_cleanup`) friss rendszerállapotból dönt róla. Korábban itt
        egyszerűen véget ért a történet, és a fel nem használt INF-ek örökre bent maradtak.

        `selected_infs`: AMIT A `select_applicable_infs` KIVÁLASZTOTT - EZEKET SOSEM
        VEZETJÜK KI (2026-09-03, terepen mérve, HP EliteDesk 800 G2 + Intel HD 530).
        A program SAJÁT MAGA rontotta el vele a videokártyát, és ez a "kiírja hogy hibás
        a videokartya driverem code 18... most telepitettem fel es mukodik" jelentés oka:

          13:20:01  4/9 INF illeszkedik erre a gépre - csak azokat telepítjük:
                    ['cui_dch.inf', 'igcc_dch.inf', 'iigd_dch.inf', 'IntcDAud.inf']
          13:21:04  ✅ Intel(R) HD Graphics 530 telepítve!
          13:21:04  a csomag 4 INF-jéből 2 egyetlen jelen lévő eszközön sem használt
                    - kivezetés a DriverStore-ból: ['cui_dch.inf', 'igcc_dch.inf']
          13:21:05  pnputil /delete-driver oem21.inf   -> deleted
          13:21:05  pnputil /delete-driver oem31.inf   -> deleted
          13:21:05  pnputil /scan-devices              <- CSAK EZUTÁN!

        A SORREND A HIBA. A `cui_dch.inf` / `igcc_dch.inf` a DCH-driver KÍSÉRŐ
        komponensei (Intel Graphics Control Panel / Command Center): a hozzájuk tartozó
        SoftwareComponent eszköz-csomópontokat a Windows csak a PnP újra-felderítéskor
        hozza létre. A takarítás viszont AZELŐTT futott, tehát "egyetlen jelen lévő
        eszköz sem használja" - és kivezette őket. Két másodperccel később a
        `/scan-devices` létrehozta a csomópontokat, amiknek addigra MÁR NEM VOLT
        csomagjuk -> `Code 18: A drivert újra kell telepíteni`. A felület ezt hibás
        eszközként jelentette, a technikus újratelepítette, a takarítás megint törölte:
        végtelen kör, és ebből jött a "sose tudom elérni hogy minden naprakész legyen".

        MIÉRT EZ A HELYES SZABÁLY, és miért nem elég a `/scan-devices` előrehozása: a
        `select_applicable_infs` a JELEN LÉVŐ eszközök hardver-azonosítói alapján
        választott, tehát a kiválasztott INF-ekről a program MÁR KIMONDTA, hogy ehhez a
        géphez valók. Ha utána a takarítás mégis kiveszi őket, a program két helyen
        mond ellent önmagának. A takarítás eredeti feladata (Razer-eset: 212 INF-ből 1
        kell) ettől nem sérül: ott a szűkítés 1 INF-et választ, tehát nincs is mit
        kivezetni; a `sel is None` ág (nem eldönthető -> csillagos telepítés) pedig
        változatlanul takarít, és pont az a Razer-eset."""
        try:
            blocks = re.split(r'(?=Adding driver package)', pnp_out or '')
            entries = []
            for b in blocks:
                m = re.search(r'Adding driver package\s*:?\s*(\S+)', b)
                p = re.search(r'Published Name\s*:\s*(oem\d+\.inf)', b, re.IGNORECASE)
                if not (m and p):
                    continue
                used = bool(re.search(r'installed on device|up-to-date on device', b, re.IGNORECASE))
                entries.append((m.group(1), p.group(1).lower(), used))
            if len(entries) <= 1:
                return
            if defer:
                self._defer_inf_cleanup(entries, name)
                return
            unused = [(inf, pub) for inf, pub, used in entries if not used]
            # A KIVÁLASZTOTT INF-EK VÉDETTEK (lásd a docstring Intel/Code 18 esetét).
            # Fájlnév szerint hasonlítunk (a `sel` teljes útvonalakat tartalmaz, a
            # pnputil kimenete pedig hol útvonalat, hol csak nevet ad), kisbetűsen.
            keep = {os.path.basename(x).lower() for x in (selected_infs or [])}
            if keep:
                protected = [(inf, pub) for inf, pub in unused
                             if os.path.basename(inf).lower() in keep]
                if protected:
                    logging.info(f"[CATALOG_INSTALL] {name}: {len(protected)} INF-et NEM vezetünk ki, "
                                 f"mert a szűkítés szerint ehhez a géphez valók (a kísérő "
                                 f"komponensek eszközei csak a következő PnP-felderítéskor "
                                 f"jönnek létre): {[os.path.basename(i) for i, _ in protected]}")
                unused = [(inf, pub) for inf, pub in unused
                          if os.path.basename(inf).lower() not in keep]
            if not unused:
                return
            logging.info(f"[CATALOG_INSTALL] {name}: a csomag {len(entries)} INF-jéből {len(unused)} egyetlen "
                         f"jelen lévő eszközön sem használt - kivezetés a DriverStore-ból: "
                         f"{[inf for inf, _pub in unused]}")
            self.emit('task_progress', {'task': task_id, 'log': f'  🧹 {name}: a csomag {len(unused)} fel nem használt INF-jének kivezetése a DriverStore-ból...'})
            refused = 0
            for inf, pub in unused:
                dres = self._run(['pnputil', '/delete-driver', pub], ok_codes=(0, 3010),
                             timeout=DELETE_DRIVER_TIMEOUT)
                if not dres or dres.returncode not in (0, 3010):
                    refused += 1
                    logging.info(f"[CATALOG_INSTALL] Kivezetés elutasítva (marad): {pub} ({inf}), rc={getattr(dres, 'returncode', '?')}")
            logging.info(f"[CATALOG_INSTALL] {name}: kivezetve {len(unused) - refused}/{len(unused)} fel nem használt INF.")
        except Exception as e:
            logging.warning(f"[CATALOG_INSTALL] A fel nem használt INF-ek kivezetése nem sikerült ({name}): {e}")

    def _build_add_driver_cmd(self, ext_path, selected_infs=None):
        """A `pnputil /add-driver` parancs(ok): szűkített INF-listával vagy a teljes csomaggal.

        Több INF-et a pnputil egy hívásban nem fogad, ezért a szűkített ág INF-enként külön
        parancsot ad - a hívó ezért listát is kaphat vissza. Egy-két hívás nagyságrendekkel
        olcsóbb, mint 212 INF felstage-elése."""
        if not selected_infs:
            return [['pnputil', '/add-driver', f"{ext_path}\\*.inf", '/subdirs', '/install']]
        return [['pnputil', '/add-driver', inf, '/install'] for inf in selected_infs]

    def _run_add_driver(self, cmds):
        """Egy vagy több `pnputil /add-driver` hívás lefuttatása, EGY összevont eredménnyel.

        A szűkített telepítés INF-enként külön parancsot ad, a hívó logikája viszont egyetlen
        eredményt vár (az `Added driver packages: N` és a blokkonkénti kimenet elemzését).
        Ezért a kimeneteket összefűzzük, a visszatérési kód pedig a "legrosszabb" lesz: így
        egy részleges hiba nem tűnhet el egy sikeres testvér-hívás mögött.

        IDŐKORLÁT: terepen (2026-08-31, ThinkPad T14 Gen 1) egy `/add-driver` **31 percig**
        futott. A határ bőkezű (INSTALL_DRIVER_TIMEOUT): nem a lassú, hanem a VÉGTELEN
        telepítést kell megfogni."""
        out, err, rc = [], [], 0
        for c in cmds:
            r = self._run(c, ok_codes=(0, 259, 3010), timeout=INSTALL_DRIVER_TIMEOUT)
            out.append(r.stdout or '')
            err.append(r.stderr or '')
            if r.returncode == CMD_TIMEOUT_RETURNCODE:
                rc = CMD_TIMEOUT_RETURNCODE
                break
            if r.returncode not in (0, 259) and rc in (0, 259):
                rc = r.returncode          # 3010 vagy valódi hiba felülírja a semlegeset
        return CommandResult(rc, '\n'.join(out), '\n'.join(err))

    def _present_hwid_sets(self, drv):
        """A cél-eszköz ÉS a gép többi jelenlévő eszközének hardver-azonosítói.

        MIÉRT KELL A TÖBBI IS: egy csomag jogosan tartalmazhat INF-et a gép MÁSIK
        eszközéhez is (pl. a hangchip mellé a hozzá tartozó effekt-komponens). Ha csak a
        cél-eszközre szűkítenénk, azokat kihagynánk - miközben a telepítés utáni takarítás
        (`_cleanup_unused_staged_infs`) is a "bármelyik jelenlévő eszköz használja"
        kritériumot alkalmazza. A két helynek ugyanazt kell mondania, különben a szűkítés
        olyat dobna el, amit a takarítás megtartana.

        A lekérdezés egyszer fut és a példányon marad: egy telepítési kör több csomagot
        dolgoz fel, és a gép eszközlistája közben nem változik érdemben."""
        sets = [drv.get('all_hwids') or []]
        cached = getattr(self, '_present_hwids_cache', None)
        if cached is None:
            cached = []
            try:
                res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS],
                                encoding='utf-8', timeout=180)
                data = json.loads(res.stdout) if (res.stdout or '').strip() else []
                for d in _filter_wu_scan_devices(data):
                    if d.get('all_hwids'):
                        cached.append(d['all_hwids'])
                logging.info(f"[INF-SELECT] Eszközlista az INF-válogatáshoz: {len(cached)} eszköz.")
            except Exception as e:
                # Nem kritikus: enélkül csak a cél-eszközre szűkítünk (kevesebb INF
                # minősül illeszkedőnek), a visszaesési ág pedig mindent helyrerak.
                logging.warning(f"[INF-SELECT] Az eszközlista beolvasása sikertelen: {e}")
            self._present_hwids_cache = cached
        return sets + cached

    def _install_catalog_sync(self, selected_pool, task_id='wu_install'):
        """A kijelölt katalógusos (url-es) elemek telepítése: cab letöltés -> expand ->
        pnputil /add-driver /install (offline cél-OS-nél dism /Add-Driver); .msu csomagnál
        wusa /quiet (offline: dism /Add-Package); .exe letöltési linket kihagyunk (ismeretlen
        telepítő csendes futtatása kockázatos). A diszpécser worker-szálán fut, task_start/
        task_complete NÉLKÜL; a task_id-vel az AutoFix záró katalógus-köre is használhatja
        ('autofix' progress-csatornán). Visszatérés: (sikeres, sikertelen, megszakítva).
        Megjegyzés: a korábbi változat minden cab-ot KÉTSZER töltött le (egy elavult
        szekvenciális kör + a szálas feldolgozó) - a szekvenciális kör törölve."""
        logging.info(f"[CATALOG_INSTALL] _install_catalog_sync() - {len(selected_pool)} driver (task={task_id})")
        import urllib.request, ssl
        ssl_ctx = ssl.create_default_context()
        total = len(selected_pool)

        temp_dir = os.path.join(os.environ.get('SystemDrive', 'C:') + '\\DV_Temp', 'driverdoktor_wu')
        os.makedirs(temp_dir, exist_ok=True)
        logging.debug(f"[CATALOG_INSTALL] Temp dir: {temp_dir}")
        success = 0
        fail = 0
        skipped = 0
        cancelled = False
        # TÉTELES MÉRLEG: melyik eszközzel MI TÖRTÉNT (2026-09-03, terepi visszajelzés:
        # *"7 db-ot talált, telepítés 7 db, utána kiírja h 3db sikeres?? mi lett a maradek
        # 4-el? ... azert az ugyfelnek is irja mar ki h mi tortenik"*). A záró sor eddig
        # három SZÁMOT adott (sikeres/sikertelen/kihagyott), a magyarázatok pedig
        # szétszórva, a görgethető naplóban tűntek el - a technikus jogosan érezte úgy,
        # hogy négy driver "a levegőben maradt". Innentől minden tétel bekerül ide a
        # kimenetelével, és a kör végén csoportosítva, NÉVVEL jelenik meg.
        outcome_detail = []          # [(kimenetel, eszköznév, részlet)]
        outcome_lock = threading.Lock()

        def _mark(kind, dev_name, extra=''):
            with outcome_lock:
                outcome_detail.append((kind, dev_name, extra))
        # Azok az eszközök, amikre a katalógus MINDEN jelöltje alkalmatlan volt. A záró
        # összegzés nevesíti őket: egy görgethető naplóban eltűnő "kihagyva" sor nem
        # visszakereshető információ, márpedig ez konkrét teendő (gyártói driver-oldal).
        no_source = []
        # Generikus -> gyári cserék: (pool-elem, [publikált oemNN.inf]) párok. A telepítés
        # UTÁN ellenőrizzük őket, és ha az eszköz hibakódos lett, visszaállunk (lásd
        # _verify_generic_replacements). Csak itt gyűjtjük, a kiértékelés a szálak után fut.
        generic_installs = []
        # Sikeresnek látszó telepítések, amiknél MEG KELL NÉZNI, hogy az eszköz tényleg
        # átvette-e a drivert (bind). Elemei: (pool-elem, reboot_pending).
        bind_checks = []
        # Azok az elemek, amikre a csomag NEM alkalmazható / nem kötött rá. A hívó (AutoFix)
        # ezt átviszi a következő lábra, hogy ne töltse le újra ugyanazt.
        no_bind = []
        self._catalog_no_bind = no_bind
        # AMI CSAK A LETÖLTÉSEN BUKOTT EL (hálózat, csonka cab, sérült kicsomagolás).
        # EZ NEM UGYANAZ, MINT A no_bind, ÉS A KÜLÖNBSÉG LÉTFONTOSSÁGÚ (2026-09-03):
        # a no_bind azt jelenti, hogy a csomagról BIZONYÍTOTTUK, hogy nem ide való; ez
        # viszont annyit tesz, hogy a csomag EL SEM JUTOTT a gépre. Az AutoFix a
        # `catalog_done` listát MÉG A TELEPÍTÉS ELŐTT írja ki ("ebben a láncban már
        # próbáltuk"), hogy egy crash után ne kezdjen elölről egy több száz MB-os
        # letöltést - csakhogy egy pillanatnyi hálózat-kiesés így ugyanúgy "megpróbáltuk"
        # bejegyzést kap, és a lánc TÖBBI LÁBA kihagyja az eszközt, ráadásul a képernyőn
        # a valótlan *"már felment, de az eszköz nem vette át"* szöveggel. Terepen ez egy
        # 8 másodperces DNS-kiesésnél hat eszközt vitt el egyszerre. A hívó ezért ezeket
        # VISSZAVONJA a `catalog_done`-ból - egy le sem töltött csomagot nem szabad
        # megpróbáltnak tekinteni.
        dl_failed = []
        self._catalog_dl_failed = dl_failed
        # AMI FELMENT, DE AZ ESZKÖZ NEM VETTE ÁT (a csomag a DriverStore-ban van, az
        # eszköz mégis a Windows alapdriverén fut). Erre KONKRÉT teendő van - az
        # újrakötés-kör -, és a hívó ezt fel is ajánlja a telepítés végén. Az 1 kattintásos
        # fix ezt a kört magától lefuttatja a lánc végén (`_autofix_closing_rebind`); a
        # kézi úton eddig a technikusnak kellett rájönnie, hogy ez a dolga.
        staged_nobind = []
        self._catalog_staged_nobind = staged_nobind
        # Azok az elemek, amiknél a kötés-ellenőrzés IGAZOLTA, hogy az eszköz átvette a
        # drivert - ezek kulcsát a tartós no-bind emlékezetből törölni kell (ha egy
        # korábban nem-kötő csomag most mégis felment, a jelölése elavult).
        bound_ok = []
        # KORÁBBAN MÁR BIZONYÍTOTTAN NEM IDE VALÓ CSOMAGOK, eszközönként, LETÖLTÉSI URL
        # szerint. Ez az egyetlen kulcs, ami tényleg megfogja az ismétlést: a katalógus
        # ugyanazt a cab-ot több tucat külön bejegyzésként (külön GUID-dal, külön címmel)
        # listázza, tehát a következő futás simán "másik" jelöltet választana ugyanarra a
        # fájlra. Az URL feloldása pár KB-os kérés, a letöltés viszont akár 1,2 GB.
        prev_bad = {}
        if not self.target_os_path:
            for rec in self._no_bind_load():
                prev_bad.setdefault(_device_stem(rec.get('pnp')), []).append(rec)

        def _proven_wrong(pnp, guid, title, date, url):
            """Bizonyítottan NEM ehhez az eszközhöz való-e már ez a csomag? Három kulcs:
            az azonos letöltési URL, az azonos katalógus-GUID, illetve UGYANANNAK a
            csomagcsaládnak egy régebbi/azonos kiadása - de az utóbbi csak akkor, ha a
            korábbi bukás oka az INF-vizsgálat volt (az "ez az INF nem ismeri ezt az
            eszközt" tény; a "felment, de nem vette át" viszont változhat).
            A találat ettől még LISTÁRA KERÜL a kézi szkenben (megjelölve, be nem
            jelölve) - itt csak a fölösleges LETÖLTÉST spóroljuk meg."""
            for rec in prev_bad.get((pnp or '').upper()) or []:
                if url and rec.get('url') == url:
                    return True
                if guid and rec.get('guid') == guid:
                    return True
                if 'nem alkalmazható' in (rec.get('reason') or '') and rec.get('title') \
                        and catalog_title_family(rec['title']) == catalog_title_family(title or '') \
                        and release_rank(date, title) <= release_rank(rec.get('date'), rec.get('title')):
                    return True
            return False

        try:
            import concurrent.futures

            counter_lock = threading.Lock()

            def process_catalog_driver(idx, drv):
                nonlocal success, fail, skipped
                if self._check_cancel():
                    return
                name = drv['name']
                url = drv.get('url', '')
                if not url:
                    logging.warning(f"[CATALOG_INSTALL] Kihagyás - nincs URL: {name}")
                    self.emit('task_progress', {'task': task_id, 'log': f'  [KIHAGYÁS] {name} - nincs letöltési link'})
                    _mark('nolink', name)
                    with counter_lock:
                        skipped += 1
                    return

                # A katalógus letöltési linkje nem mindig .cab: .msu és .exe is előfordul.
                # A régi kód ezekre is expand-ot futtatott, ami csendben nem csinált semmit,
                # és a telepítés értelmetlen hibával bukott.
                url_file = url.split('?')[0].rsplit('/', 1)[-1].lower()
                file_ext = os.path.splitext(url_file)[1]
                if file_ext == '.exe':
                    logging.warning(f"[CATALOG_INSTALL] Kihagyás - .exe telepítő ({name}): {url[:80]}")
                    self.emit('task_progress', {'task': task_id, 'log': f'  [KIHAGYÁS] {name} - a katalógus .exe telepítőt adott, ezt biztonsági okból nem futtatjuk automatikusan'})
                    _mark('exe', name)
                    with counter_lock:
                        skipped += 1
                    return

                cab_path = os.path.join(temp_dir, f"drv_{idx}{file_ext or '.cab'}")
                ext_path = os.path.join(temp_dir, f"drv_ext_{idx}")

                # HOLTVERSENY-TARTALÉK: a nyertes mellett a vele AZONOS DÁTUMÚ jelöltek is
                # itt vannak (lásd _catalog_find_driver). Ha a nyertes INF-jéről kiderül,
                # hogy nem ehhez az eszközhöz való, továbblépünk a következőre ahelyett,
                # hogy feladnánk. Terep (2026-08-05): a videokártyára 10 azonos című sor
                # közül vaktában vittünk el egyet, 1,2 GB letöltés után derült ki, hogy nem
                # ismeri ezt a kártyát - és a maradék 9-et meg se néztük, a gép pedig úgy
                # zárta a láncot, hogy "a katalógusban nincs jobb driver".
                candidates = [(drv.get('cat_guid') or '', drv.get('wu_title') or '',
                               drv.get('wu_date') or '', url)]
                # HÁNY JELÖLTET JÁRUNK VÉGIG: Windows-alapdriveren futó eszköznél
                # többet, mert ott a tét más. Egy gyári driveren futó eszköznél a
                # tartalék csak "egy másik, ugyanolyan friss csomag" - ott a 3-as korlát
                # bőven elég, és minden további jelölt egy fölösleges, akár 1 GB-os
                # letöltés. Inbox driveren viszont az alternatíva a GENERIKUS driver,
                # tehát a lista végén álló RÉGEBBI gyári csomag is nyereség
                # (explicit user decision: "a gyári driver mindig jobb mint az alap
                # inbox windowsos generic driver"). A kereső ilyenkor eleve a
                # legspecifikusabb HWID felől rendezi a tartalékokat, tehát a jó csomag
                # jellemzően az első néhány között van.
                max_cand = (CATALOG_INBOX_FALLBACK_CANDIDATES if drv.get('inbox_now')
                            else CATALOG_MAX_CANDIDATES)
                for (g, t, d) in (drv.get('alt_candidates') or [])[:max_cand - 1]:
                    candidates.append((g, t, d, None))

                self.emit('task_progress', {'task': task_id, 'log': f'-> {name} letöltése...'})
                # ÚJRAPRÓBÁLKOZÁS: a katalógus cab-jai százmegásak (egy videokártya-csomag
                # 1,1 GB), és egy ekkora letöltés alatt egy megszakadt kapcsolat teljesen
                # hétköznapi. Terepen (2026-07-27) az NVIDIA-csomag [WinError 10054]
                # ("a távoli gép bontotta a kapcsolatot") hibával elhasalt 18 másodperc
                # után, egyetlen próbálkozás után véglegesen sikertelenként könyvelve -
                # pedig a következő lábon ugyanaz az URL simán lejött. A félbemaradt fájlt
                # minden kör elején töröljük (ugyanaz a szabály, mint a stresstools.zip-nél:
                # a maradék épp azt a helyet enné el, ami az újrapróbáláshoz kell).
                #
                # A kör a KICSOMAGOLÁST is magában foglalja (2026-07-28, terepi log): a
                # szerver a kapcsolat bontását nem mindig jelzi hibával - a http.client a
                # darabolt olvasásnál kivétel NÉLKÜL ad vissza rövid fájlt, így az NVIDIA
                # 1,22 GB-os cab-jából 139 MB jött le "sikeresen", majd az expand kód=1-gyel
                # elhasalt (amit senki nem nézett), és a hiba "nincs INF a csomagban"-ként,
                # VÉGLEGES bukásként jelent meg - a pont erre épített retry egyszer sem
                # indult el. Ezért: (a) a letöltött méretet a Content-Length-hez mérjük,
                # (b) az expand hibája és a hiányzó INF is újrapróbálást vált ki (sérült
                # cab), nem végleges hibát.
                CATALOG_DL_ATTEMPTS = 3
                # HÁLÓZAT-KIESÉS: mennyit várunk a visszatérésére, és hányszor írhatunk
                # jóvá emiatt egy próbálkozást. A 180 mp a `_wait_for_internet` szokásos
                # kerete (ugyanaz a nagyságrend, mint az AutoFix vezetékes várakozása);
                # a 2 jóváírás azért kell, hogy egy ingadozó kapcsolat se vihessen
                # végtelen körbe - lásd a ciklusban a részletes indoklást.
                CATALOG_DL_NET_WAIT = 180
                CATALOG_DL_NET_REFUNDS = 2
                # INSTABIL (de nem halott) kapcsolatnál ennyit várunk két kísérlet közt.
                # A régi 3 mp arra volt méretezve, hogy "hátha most sikerül"; egy több száz
                # MB-os letöltés viszont épp egy driver-csere utáni, még rendeződő hálózaton
                # szakad meg, és ott 3 másodperc semmit nem old meg (mérve: 5 kísérlet 52 mp
                # alatt bukott el egy 236 MB-os csomagnál).
                CATALOG_DL_UNSTABLE_WAIT = 20
                chosen = None        # (guid, cím, dátum, url) - amit végül telepítünk
                # UGYANAZT A CSOMAGOT NEM TÖLTJÜK LE KÉTSZER (lásd lent).
                tried_urls = set()
                spent_bytes = 0          # amit ERRE az eszközre már letöltöttünk
                for cand_i, (cand_guid, cand_title, cand_date, cand_url) in enumerate(candidates):
                    if self._check_cancel():
                        return
                    # LETÖLTÉSI KORLÁT a tartalékok végigpróbálására (az ELSŐ jelöltet
                    # mindig végigvisszük). Lásd CATALOG_FALLBACK_MAX_BYTES: kis
                    # csomagoknál sosem lép be, egy 1,2 GB-os videokártya-csomagnál
                    # viszont a második után megállít - különben a hosszabb jelölt-lista
                    # a lánc idejét vinné el.
                    if cand_i and spent_bytes >= CATALOG_FALLBACK_MAX_BYTES:
                        logging.warning(f"[CATALOG_INSTALL] {name}: a tartalékok próbálgatása "
                                        f"leállt, mert erre az eszközre már {spent_bytes / 1048576:.0f} MB "
                                        f"letöltés ment el ({cand_i}/{len(candidates)} jelölt után).")
                        self.emit('task_progress', {'task': task_id, 'log':
                                  f'  ⏹ {name}: {spent_bytes / 1048576:.0f} MB letöltés után megálltunk a '
                                  f'tartalékok próbálgatásával (a maradék {len(candidates) - cand_i} jelölt '
                                  f'kimarad) - a gyártó saját oldala a következő lépés.'})
                        break
                    # KORÁBBI FUTÁSBAN MÁR MEGBUKOTT? Még az URL feloldása előtt eldönthető,
                    # ha a GUID vagy a csomagcsalád egyezik - ilyenkor egy kérés sem megy ki.
                    if _proven_wrong(drv.get('pnp_id'), cand_guid, cand_title, cand_date, cand_url):
                        logging.info(f"[CATALOG_INSTALL] {name}: a(z) {cand_i + 1}. jelölt "
                                     f"('{cand_title}') egy KORÁBBI futásban már bizonyítottan "
                                     f"nem ehhez az eszközhöz való - nem töltjük le újra.")
                        continue
                    if cand_url is None:
                        # A tartalék URL-jét csak akkor oldjuk fel, ha tényleg kell.
                        cand_url = self._catalog_download_url(cand_guid, ssl_ctx, name)
                        if not cand_url:
                            logging.warning(f"[CATALOG_INSTALL] {name}: a(z) {cand_i + 1}. jelölt "
                                            f"('{cand_title}') letöltési linkje nem oldható fel - kihagyva.")
                            continue
                    # UGYANAZ A CSOMAG TÖBB KATALÓGUS-BEJEGYZÉSKÉNT. Mérve (2026-08-06,
                    # élő katalógus): a `PCI\VEN_10DE&DEV_2504` legfrissebb dátumú 25 sora
                    # KÖZÜL AZ ELSŐ ÖT MIND UGYANARRA a cab-ra mutat (azonos fájlnév, azonos
                    # 1165,1 MB méret, azonos INF-lista) - a katalógus OS-ágakként külön
                    # bejegyzésként listázza ugyanazt a csomagot. A tartalék-logika enélkül
                    # háromszor töltené le ugyanazt az 1,2 GB-ot, ami rosszabb a hibánál,
                    # amit javítani akar. A DownloadDialog-kérés pár KB, tehát az URL
                    # feloldása után derül ki - és onnan már ingyen ugorjuk át.
                    if cand_url in tried_urls or _proven_wrong(drv.get('pnp_id'), cand_guid,
                                                              cand_title, cand_date, cand_url):
                        logging.info(f"[CATALOG_INSTALL] {name}: a(z) {cand_i + 1}. jelölt "
                                     f"('{cand_title}') ugyanarra a csomagra mutat, mint egy már "
                                     f"kipróbált (vagy korábban megbukott) jelölt - "
                                     f"nem töltjük le újra.")
                        continue
                    tried_urls.add(cand_url)
                    if cand_i:
                        self.emit('task_progress', {'task': task_id, 'log': f'  ↻ {name}: következő katalógus-jelölt próbája ({cand_title})...'})
                    url = cand_url
                    pkg_ok, last_err = False, None
                    # `while` és nem `for`, mert egy HÁLÓZAT-KIESÉS miatti kör NEM számít
                    # bele a keretbe (lásd lentebb): olyankor `attempt_budget` nő eggyel,
                    # tehát ugyanannyi VALÓDI próbálkozás marad. A keret így is korlátos -
                    # `CATALOG_DL_NET_REFUNDS` a maximum, amit a hálózat "visszaadhat",
                    # különben egy örökké ingadozó kapcsolat végtelen körbe vinne.
                    attempt, attempt_budget, refunds = 0, CATALOG_DL_ATTEMPTS, 0
                    while attempt < attempt_budget:
                        attempt += 1
                        if self._check_cancel():
                            return
                        try:
                            logging.debug(f"[CATALOG_INSTALL] Letöltés ({attempt}/{attempt_budget}): {url[:80]}...")
                            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                            with urllib.request.urlopen(req, context=ssl_ctx, timeout=120) as resp, open(cab_path, 'wb') as f:
                                expected_len = resp.headers.get('Content-Length')
                                shutil.copyfileobj(resp, f)
                            got_len = os.path.getsize(cab_path)
                            if expected_len and expected_len.isdigit() and got_len != int(expected_len):
                                raise IOError(f"csonka letöltés: {got_len}/{expected_len} byte jött le")
                            logging.debug(f"[CATALOG_INSTALL] Letöltve: {cab_path} ({got_len} byte, "
                                          f"{attempt}. próbálkozásra)")
                            # A ténylegesen lejött bájtok - ebből dönt a tartalék-korlát
                            # (CATALOG_FALLBACK_MAX_BYTES). Az újrapróbálkozások is
                            # beleszámítanak: az adatforgalom akkor is elment.
                            spent_bytes += got_len
                            if file_ext == '.msu':
                                pkg_ok = True   # az .msu-t a wusa/dism ellenőrzi, expand-kör nincs
                                break
                            # Kicsomagolás + INF-jelenlét még a próbálkozás-körön BELÜL: egy
                            # sérült cab tünete pont ez a kettő, és mindkettőre a friss
                            # újraletöltés a gyógyszer, nem a végleges hiba.
                            if os.path.isdir(ext_path):
                                shutil.rmtree(ext_path, ignore_errors=True)   # előző kör maradéka
                            os.makedirs(ext_path, exist_ok=True)
                            exp_res = self._run(['expand', cab_path, '-F:*', ext_path])
                            if not exp_res or exp_res.returncode != 0:
                                rc = exp_res.returncode if exp_res else '?'
                                raise IOError(f"az expand nem tudta kicsomagolni (kód={rc}) - valószínűleg sérült cab")
                            for inner_cab in glob.glob(os.path.join(ext_path, '*.cab')):
                                inner_ext = inner_cab + '_ext'
                                os.makedirs(inner_ext, exist_ok=True)
                                self._run(['expand', inner_cab, '-F:*', inner_ext])
                            has_inf = False
                            for _r, _d, files in os.walk(ext_path):
                                if any(fn.lower().endswith('.inf') for fn in files):
                                    has_inf = True
                                    break
                            if not has_inf:
                                raise IOError("a kicsomagolt csomagban nincs .inf - sérült vagy nem driver-csomag")
                            pkg_ok = True
                            break
                        except Exception as e:
                            last_err = e
                            logging.warning(f"[CATALOG_INSTALL] Letöltési/kicsomagolási hiba ({name}, {attempt}/{attempt_budget}): {e}")
                            try:
                                if os.path.exists(cab_path):
                                    os.remove(cab_path)
                            except Exception as ce:
                                logging.debug(f"[CATALOG_INSTALL] A félbemaradt fájl törlése sikertelen ({cab_path}): {ce}")
                            shutil.rmtree(ext_path, ignore_errors=True)
                            # HÁLÓZAT-KIESÉSNÉL MEGVÁRJUK A HÁLÓZATOT, NEM ÉGETJÜK EL A
                            # PRÓBÁLKOZÁSOKAT (2026-09-03, terepen mérve, HP EliteDesk).
                            # A DNS 8 másodpercre elment, és a 3 fix, 3 másodperces szünetű
                            # próbálkozás pont abba az ablakba esett:
                            #   13:15:56  1/3 csonka letöltés (45 MB / 339 MB)
                            #   13:16:00  2/3 <urlopen error [Errno 11001] getaddrinfo failed>
                            #   13:16:03  3/3 ugyanaz  ->  VÉGLEG sikertelen
                            # Hét másodperc alatt elfogyott minden esély, és ugyanez a
                            # kiesés sorban megölte a köteg TÖBBI elemét is (HD Graphics,
                            # Management Engine, SMBus, AMT SOL, Alaplap erőforrásai) -
                            # egy 30 másodperces DNS-hiba tehát egy egész telepítési kört
                            # vitt el. Ez a "hiába telepítgetem, sose lesz minden naprakész"
                            # érzés harmadik forrása, és a naplóból nézve "sikertelen
                            # csomag"-nak látszik, pedig a csomaggal semmi baj nem volt.
                            #
                            # A hálózati hibát ezért megkülönböztetjük: ilyenkor a szünet
                            # helyett a MEGLÉVŐ `_wait_for_internet`-tel várunk (DNS-t is
                            # ellenőriz - lásd a 2026-09-01-i javítást), és a várakozás
                            # NEM számít bele a próbálkozásokba. Így egy pillanatnyi kiesés
                            # már nem dönt el semmit; egy tartós viszont továbbra is
                            # korlátos, mert a `_wait_for_internet` maga is időkorlátos, és
                            # a hálózat nélküli kísérletek ugyanúgy elfogynak.
                            # A CSONKA LETÖLTÉS IS HÁLÓZATI HIBA - és pont ez hiányzott
                            # (2026-09-07, a 2026-09-04-i Lenovo naplóból). A jóváírás
                            # mechanizmusa működött, csak a legyakoribb tünetet nem ismerte
                            # fel: a mintalistában szereplő 'getaddrinfo'/'connection'
                            # egyike sem szerepel a saját "csonka letöltés: X/Y byte jött le"
                            # üzenetünkben, ezért a fél letöltés minden alkalommal ELÉGETETT
                            # egy próbálkozást. A napló ezt mutatta egy 236 MB-os csomagnál:
                            #   10:06:05  1/3 csonka (180 MB)   <- nincs jóváírás
                            #   10:06:09  2/3 getaddrinfo       <- jóváírás, budget 4
                            #   10:06:28  3/4 csonka (44 MB)    <- nincs jóváírás
                            #   10:06:31  4/4 getaddrinfo       <- jóváírás, budget 5
                            #   10:06:57  5/5 csonka (132 MB)   -> VÉGLEG sikertelen
                            # Mind az öt kísérlet 52 MÁSODPERC alatt égett el, miközben a
                            # kapcsolat épp a driver-csere után állt helyre. Egy félbeszakadt
                            # nagy fájl ugyanúgy "a hálózat esett szét" tünet, mint a DNS-hiba.
                            net_err = any(s in str(e).lower() for s in (
                                'getaddrinfo', '11001', 'urlopen error', 'timed out',
                                'connection', 'unreachable', 'ssl', 'csonka letöltés',
                                'incompleteread', 'remote end closed'))
                            if net_err and refunds < CATALOG_DL_NET_REFUNDS:
                                # KÉT KÜLÖN ESET, ÉS MINDKETTŐ JÓVÁÍRÁST ÉRDEMEL:
                                # (a) a hálózat TELJESEN halott (DNS sem megy) - megvárjuk;
                                # (b) a kapcsolat "él", de a nagy fájl mégis megszakadt
                                #     (instabil vonal, félbontott TCP, szerveroldali reset).
                                # A régi kód csak az (a) ágat ismerte, mert a jóváírás
                                # feltétele `not self._check_internet(...)` volt - egy csonka
                                # letöltésnél viszont az ellenőrzés jellemzően SIKERÜL, tehát
                                # a leggyakoribb eset épp kimaradt belőle.
                                if not self._check_internet(require_dns=True):
                                    self.emit('task_progress', {'task': task_id, 'log':
                                              f'  🌐 {name}: megszakadt a hálózat - várunk, amíg visszajön '
                                              f'(ez a próbálkozás nem vész el)...'})
                                    if self._wait_for_internet(CATALOG_DL_NET_WAIT, task_id=task_id,
                                                               reason='a driver letöltéséhez'):
                                        refunds += 1
                                        attempt_budget += 1
                                        logging.info(f"[CATALOG_INSTALL] {name}: a hálózat visszajött, a "
                                                     f"{attempt}. próbálkozás nem számít bele "
                                                     f"({refunds}/{CATALOG_DL_NET_REFUNDS} jóváírás).")
                                        continue
                                else:
                                    refunds += 1
                                    attempt_budget += 1
                                    logging.info(f"[CATALOG_INSTALL] {name}: a kapcsolat él, de a letöltés "
                                                 f"megszakadt (instabil vonal) - {CATALOG_DL_UNSTABLE_WAIT} mp "
                                                 f"szünet, a {attempt}. próbálkozás nem számít bele "
                                                 f"({refunds}/{CATALOG_DL_NET_REFUNDS} jóváírás).")
                                    self.emit('task_progress', {'task': task_id, 'log':
                                              f'  🌐 {name}: a letöltés megszakadt, pedig van kapcsolat - '
                                              f'{CATALOG_DL_UNSTABLE_WAIT} mp szünet, majd újra '
                                              f'(ez a próbálkozás nem vész el)...'})
                                    time.sleep(CATALOG_DL_UNSTABLE_WAIT)
                                    continue
                            if attempt < attempt_budget:
                                self.emit('task_progress', {'task': task_id, 'log': f'  ↻ {name} letöltése megszakadt ({e}) - újrapróbálás ({attempt + 1}/{attempt_budget})...'})
                                time.sleep(3)
                    if not pkg_ok:
                        # A LETÖLTÉS bukása nem "rossz csomag" - itt nincs értelme a következő
                        # jelöltnek (a hálózat a hibás), ezért az eredeti viselkedés marad.
                        logging.error(f"[CATALOG_INSTALL] Letöltés/kicsomagolás VÉGLEG sikertelen {attempt} próbálkozás után ({name}): {last_err}")
                        self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name} letöltési/kicsomagolási hiba {attempt} próbálkozás után: {last_err}'})
                        self.emit('task_progress', {'task': task_id, 'log':
                                  f'     ℹ️ Ez NEM azt jelenti, hogy a csomag rossz - le sem jött. '
                                  f'A következő szkennelés újra felajánlja.'})
                        with counter_lock:
                            fail += 1
                            # A hívónak (AutoFix) tudnia kell, hogy ez CSAK letöltési bukás
                            # volt: a "ebben a láncban már próbáltuk" jelölést vissza kell
                            # vonnia, különben egy hálózat-kiesés a lánc végéig kizárja az
                            # eszközt. Lásd a `dl_failed` deklarációjánál a magyarázatot.
                            dl_failed.append(dict(drv))
                        _mark('netfail', name, str(last_err)[:80])
                        return

                    # ALKALMAZHATÓSÁG-ELLENŐRZÉS a telepítés ELŐTT (lásd wu_core.inf_package_applies):
                    # a katalógus a törzs-HWID-re más gépgyártóra szabott változatot is adhat,
                    # ami feltelepül, de sosem köt rá az eszközre. Ilyet meg se próbálunk -
                    # helyette a következő azonos dátumú jelölttel folytatjuk.
                    if file_ext == '.msu' or self.target_os_path or not drv.get('all_hwids'):
                        chosen = (cand_guid, cand_title, cand_date, cand_url)
                        break
                    if inf_package_applies(ext_path, drv.get('all_hwids')) is False:
                        logging.warning(f"[CATALOG_INSTALL] Nem alkalmazható csomag ({cand_i + 1}/{len(candidates)}), "
                                        f"kihagyva: {name} ({cand_title})")
                        self.emit('task_progress', {'task': task_id, 'log': f'  ↷ {name}: a(z) „{cand_title}” csomag más gépre/alaplapra készült (az INF nem ismeri ezt az eszközt) - kihagyva.'})
                        # MINDEN megbukott jelölt bekerül a no-bind emlékezetbe (GUID-dal),
                        # így a következő futás nem tölti le újra ugyanezt a csomagot.
                        with counter_lock:
                            no_bind.append(dict(drv, wu_title=cand_title, wu_date=cand_date,
                                                cat_guid=cand_guid,
                                                no_bind_reason='nem alkalmazható (más gépre szabott INF)'))
                        continue
                    chosen = (cand_guid, cand_title, cand_date, cand_url)
                    break

                if chosen is None:
                    # NEM ELÉG ANNYIT MONDANI, HOGY "KIHAGYVA" (2026-08-31, terepi
                    # visszajelzés: *"itt nem próbál és ennyi kihagyva és kész, ez így nem
                    # jó"*). A program ilyenkor VÉGIGPRÓBÁLTA az összes jelöltet - a
                    # naplóban ott is van soronként -, de a záró sor ezt nem mondta ki, és
                    # teendőt sem adott.
                    #
                    # DE A KIMENETEL KÉT, GYÖKERESEN KÜLÖNBÖZŐ DOLGOT JELENTHET, ÉS A KETTŐT
                    # SZÉT KELL VÁLASZTANI (2026-09-08, terepen mérve, ASRock B450M Pro4):
                    #
                    #   (a) az eszköz a WINDOWS ALAPDRIVERÉN fut -> tényleg VAN teendő,
                    #       a gyári drivert a gyártó oldaláról kell pótolni;
                    #   (b) az eszköz MÁR GYÁRI DRIVEREN fut (jellemzően amit ez a lánc rakott
                    #       fel rá pár perce), csak ÚJABB alkalmazható csomag nincs -> NINCS
                    #       teendő, az eszköz kész van.
                    #
                    # A régi szöveg mindkét esetre a gyártói oldalra küldött. Mérve ugyanabban
                    # a láncban: 23:05:38 "✅ High Definition Audio Device telepítve!" (Realtek
                    # 6.0.9136.1), 23:06:08 "gyári driver működik", majd a KÖVETKEZŐ lábon
                    # ugyanarra az eszközre "⚠️ NINCS megfelelő csomag ... a gyártó saját
                    # driver-oldaláról kell". A kereső naplósora ki is mondja, miért:
                    # "a gép saját kulcsán nincs újabb (6.0.9136.1 <= telepített 6.0.9136.1)".
                    # Vagyis a program a saját, sikeres munkáját jelentette hiányosságként -
                    # és ezzel ellentmondott a záró egészségjelentésnek, ami ezt az eszközt
                    # (helyesen) nem is sorolta fel.
                    #
                    # AMI ITT KORÁBBAN ÁLLT, ÉS TÉVES VOLT: "az ASRock egyáltalán nem publikál
                    # a Windows Update-re". Ezt a CLAUDE.md 2026-09-02-én újramérve visszavonta,
                    # és ugyanez a napló is cáfolja - a gép saját `&SUBSYS_18496893` kulcsára
                    # a katalógus 25 (más méréssel 41) Realtek-csomagot ad, és a lánc fel is
                    # rakta a helyeset. A "nincs való csomag" itt tehát azt jelenti, hogy a
                    # MEGVIZSGÁLT jelöltek INF-je nem ismeri ezt az eszközt - nem azt, hogy a
                    # gyártó nem publikál.
                    on_vendor = not drv.get('inbox_now')
                    inst_txt = ' '.join(x for x in ((drv.get('installed_provider') or '').strip(),
                                                    (drv.get('installed_version') or '').strip()) if x)
                    logging.warning(f"[CATALOG_INSTALL] {name}: mind a(z) {len(candidates)} katalógus-jelölt "
                                    f"INF-je más eszközre való - nincs telepíthető csomag. "
                                    f"{'MÁR GYÁRI DRIVEREN FUT' if on_vendor else 'WINDOWS-ALAPDRIVEREN MARAD'}"
                                    f"{' (' + inst_txt + ')' if inst_txt else ''}.")
                    self.emit('task_progress', {'task': task_id, 'log':
                              f'  ↷ {name}: mind a(z) {len(candidates)} katalógus-jelöltet végigpróbáltuk, '
                              f'egyik INF-je sem ismeri ezt az eszközt - nincs telepíthető csomag.'})
                    if on_vendor:
                        self.emit('task_progress', {'task': task_id, 'log':
                                  f'     ✅ Ez NEM hiányosság: az eszközön MÁR GYÁRI DRIVER FUT'
                                  f'{" (" + inst_txt + ")" if inst_txt else ""}, csak újabb, ehhez a '
                                  f'géphez való csomag nincs a katalógusban. Nincs teendő.'})
                    else:
                        self.emit('task_progress', {'task': task_id, 'log':
                                  f'     → Ehhez az eszközhöz a Microsoft katalógusában nincs való csomag, '
                                  f'és a Windows alapdriverén fut. A gyári drivert a gyártó '
                                  f'(alaplap/laptop) saját driver-oldaláról kell pótolni.'})
                    with counter_lock:
                        skipped += 1
                        # A záró összegzés nevesíti őket: egy eltűnő "kihagyva" sor a
                        # görgethető naplóban nem visszakereshető információ. A pnp_id és az
                        # "alapdriveren fut-e" azért utazik együtt a névvel, mert a záró blokk
                        # CSAK a valódi teendőt jelentheti be (lásd ott).
                        no_source.append({'name': name, 'pnp_id': drv.get('pnp_id') or '',
                                          'hwid': drv.get('hwid') or '',
                                          'inbox_now': not on_vendor,
                                          'installed_inf': drv.get('installed_inf') or '',
                                          'installed': inst_txt})
                        _mark('nosource', name,
                              '' if not on_vendor else 'már gyári driveren fut, nincs teendő')
                    return
                if chosen[1] != (drv.get('wu_title') or ''):
                    # Ez a sor a bizonyíték, hogy a tartalék-logika dolgozott: enélkül a
                    # terepi logból nem derülne ki, miért MÁS csomag ment fel, mint amit a
                    # keresés nyertesként kiírt.
                    logging.info(f"[CATALOG_INSTALL] {name}: a nyertes csomag nem volt alkalmazható, "
                                 f"a tartalék jelölt megy fel: '{chosen[1]}' [{chosen[2] or '?'}]")
                    self.emit('task_progress', {'task': task_id, 'log': f'  ✔ {name}: a tartalék katalógus-csomag illik az eszközre ({chosen[1]}).'})
                drv['cat_guid'], drv['wu_title'], drv['wu_date'], drv['url'] = \
                    chosen[0], chosen[1], chosen[2], chosen[3]
                url = chosen[3]

                if file_ext == '.msu':
                    # .msu: wusa csendes telepítés (offline cél-OS-nél dism /Add-Package).
                    self.emit('task_progress', {'task': task_id, 'log': f'  Telepítés (.msu): {name}...'})
                    if self.target_os_path:
                        res = self._run(['dism', f'/Image:{self.target_os_path}', '/Add-Package', f'/PackagePath:{cab_path}'], timeout=1800, ok_codes=(0, 3010))
                        ok = bool(res) and res.returncode in (0, 3010)
                    else:
                        res = self._run(['wusa', cab_path, '/quiet', '/norestart'], timeout=1800, ok_codes=(0, 3010))
                        ok = bool(res) and res.returncode in (0, 3010)
                    with counter_lock:
                        if ok:
                            success += 1
                        else:
                            fail += 1
                    rc = res.returncode if res else '?'
                    self.emit('task_progress', {'task': task_id, 'log': f'  {"✅" if ok else "❌"} {name} (.msu, kód={rc})'})
                    return

                self.emit('task_progress', {'task': task_id, 'log': f'  Telepítés: {name}...'})
                is_offline = bool(self.target_os_path)
                if is_offline:
                    cmd = ['dism', f'/Image:{self.target_os_path}', '/Add-Driver', f'/Driver:{ext_path}', '/Recurse']
                    res = self._run(cmd)
                else:
                    # CSAK AZ ILLESZKEDŐ INF-EKET TELEPÍTJÜK, ha el tudjuk dönteni, melyek
                    # azok (wu_core.select_applicable_infs). Terepen egy Realtek hangcsomag
                    # 212 INF-fel jött, amiből EGY kellett: a teljes telepítés 31 percig
                    # futott, majd a takarítás 211 csomagot törölt egyenként - és az egyik
                    # törlés beragadt 42 percre. A szűkítés ugyanoda érkezik, csak a
                    # felesleges kerülő nélkül. `None` = nem eldönthető -> marad a régi,
                    # csillagos telepítés.
                    sel, total_inf, why = select_applicable_infs(ext_path, self._present_hwid_sets(drv))
                    if sel:
                        logging.info(f"[CATALOG_INSTALL] {name}: {why} - csak azokat telepítjük: "
                                     f"{[os.path.basename(x) for x in sel]}")
                        self.emit('task_progress', {'task': task_id, 'log':
                                  f'  ⓘ {name}: a csomag {total_inf} INF-jéből {len(sel)} való erre a gépre - csak azt telepítjük.'})
                    elif total_inf > 1:
                        logging.info(f"[CATALOG_INSTALL] {name}: teljes telepítés ({total_inf} INF) - {why}.")
                    cmd = self._build_add_driver_cmd(ext_path, sel)
                    # 259 = a csomag már fent van / nincs rá kötő eszköz (lentebb no-op),
                    # 3010 = siker, de reboot kell - mindkettő VÁRT kimenet, WARNING nélkül
                    # (terepi log, 2026-07-28: 4 hamis WARNING egy hibátlan futásban).
                    #
                    # IDŐKORLÁT: terepen (2026-08-31, ThinkPad T14 Gen 1, Win11 26200) egy
                    # ilyen hívás **31 PERCIG** futott, egy másik 15,5 percig - korlát nélkül
                    # egyetlen beragadt telepítés órákra megfogja a láncot, és a technikus
                    # csak annyit lát, hogy "nem történik semmi". A határ SZÁNDÉKOSAN bőkezű
                    # (INSTALL_DRIVER_TIMEOUT): egy nagy chipset-csomag telepítése valóban
                    # lehet több perc, tehát nem szabad egy lassú, de HALADÓ telepítést
                    # elvágni - csak a végtelen lógást kell megfogni.
                    res = self._run_add_driver(cmd)
                    if res.returncode == CMD_TIMEOUT_RETURNCODE:
                        # Nem hallgatjuk el: a csomag állapota ilyenkor bizonytalan, és a
                        # technikusnak tudnia kell, melyik telepítés akadt el.
                        logging.error(f"[CATALOG_INSTALL] IDŐTÚLLÉPÉS ({INSTALL_DRIVER_TIMEOUT}s) "
                                      f"a telepítéskor: {name}")
                        self.emit('task_progress', {'task': task_id, 'log':
                                  f'⏱️ A(z) "{name}" telepítése {INSTALL_DRIVER_TIMEOUT // 60} perc után '
                                  f'sem fejeződött be - továbblépünk. A csomag a következő '
                                  f'újraindítás után befejeződhet.'})
                    elif sel and res.returncode != 3010:
                        # VISSZAESÉS: ha a szűkített telepítés után a pnputil NEM jelenti,
                        # hogy az eszköz megkapta a drivert, feltesszük a TELJES csomagot.
                        # Így a szűkítés a legrosszabb esetben sem ronthat: vagy gyorsabb
                        # ugyanazzal az eredménnyel, vagy visszaáll a korábbi viselkedésre.
                        # (3010 = újraindítás kell -> a kötés a következő bootnál dől el,
                        # ott most nem ítélkezünk.)
                        if not package_bound_to_device_family(res.stdout or '', drv):
                            logging.warning(f"[CATALOG_INSTALL] {name}: a szűkített telepítés után az "
                                            f"eszköz nem kapta meg a drivert - teljes csomag telepítése.")
                            self.emit('task_progress', {'task': task_id, 'log':
                                      f'  ↻ {name}: a szűkített telepítés nem volt elég - a teljes csomag megy fel.'})
                            full = self._run_add_driver(self._build_add_driver_cmd(ext_path))
                            res = CommandResult(full.returncode,
                                                (res.stdout or '') + '\n' + (full.stdout or ''),
                                                (res.stderr or '') + '\n' + (full.stderr or ''))
                # pnputil kimenet: "Added driver packages:  N". Ha N==0, semmi nem települt
                # (a csomag már a store-ban van / up-to-date, kód 259) - ezt TILOS sikernek
                # számolni: az AutoFix katalógus-záróköre soha be nem bind-elő eszközön
                # (pl. kód-28 Ismeretlen Eszköz) minden körben "1 települt"-et jelentene, és a
                # lánc végtelen reboot-loopba kerülne (field-seen: AMDIF031 amdgpio3.inf).
                added_m = re.search(r'Added driver packages?\s*:\s*(\d+)', res.stdout or '', re.IGNORECASE)
                added_zero = added_m is not None and int(added_m.group(1)) == 0
                # "(Already exists in the system)": a csomag MÁR a DriverStore-ban van egy
                # korábbi körből, a pnputil mégis "Added driver packages: N"-t ír (N>0), az
                # added_zero-guard tehát NEM fog rá. Ha MINDEN "added successfully" sor
                # már-létező, akkor SEMMI új nem került fel - a generikus->gyári jelölt
                # (pl. Realtek UAD audio, ami a hdaudio.inf-en ragad és nem bind-el át)
                # különben minden körben "1 települt"-et jelentene, végtelen reboot-loopot
                # okozva (terepen, Build 228: az audio 3 körön át újratelepült). Ezt is
                # no-opként kell kezelni, pontosan mint az added_zero-t.
                add_ok_lines = len(re.findall(r'added successfully', res.stdout or '', re.IGNORECASE))
                already_exists = len(re.findall(r'already exists in the system', res.stdout or '', re.IGNORECASE))
                all_already = add_ok_lines > 0 and already_exists >= add_ok_lines
                no_op = added_zero or all_already
                installed_ok = (res.returncode == 0 or any(k in res.stdout for k in ["Added", "sikeres", "successfully"])) and not no_op
                if installed_ok:
                    with counter_lock:
                        success += 1
                        # "reboot kell" esetén az eszköz csak a következő indulásnál veszi
                        # át a drivert - ilyenkor a kötés-ellenőrzés MOST még hamis negatív
                        # lenne (terepen: AMD PSP 3010-nel jött fel, és a reboot után rendben
                        # átkötött). Ezért a reboot-jelzést külön visszük.
                        reboot_pending = (res.returncode == 3010
                                          or 'reboot is needed' in (res.stdout or '').lower())
                        if drv.get('pnp_id') and drv.get('installed_inf') and not is_offline:
                            # A pnputil kimenete is elmegy: abból derül ki, ha a csomag egy
                            # GYEREK-INTERFÉSZRE kötött rá (composite USB), miközben maga a
                            # lekérdezett szülő - helyesen - usb.inf-en maradt.
                            bind_checks.append((drv, reboot_pending, res.stdout or ''))
                        if drv.get('generic_replace'):
                            # A pnputil kiírja, milyen néven publikálta a csomagot
                            # ("Published Name: oem42.inf") - visszaálláskor pontosan ezt
                            # kell törölni, semmi mást.
                            generic_installs.append(
                                (drv, re.findall(r'Published Name\s*:\s*(oem\d+\.inf)', res.stdout or '', re.IGNORECASE)))
                    self.emit('task_progress', {'task': task_id, 'log': f'  ✅ {name} telepítve!'})
                    _mark('ok', name, drv.get('wu_title') or '')
                elif no_op:
                    with counter_lock:
                        skipped += 1
                    # KÉT, GYÖKERESEN KÜLÖNBÖZŐ ESET VOLT EGY SZÖVEG ALATT (2026-09-03,
                    # terepi képernyőkép, HID billentyűzet):
                    #
                    #   ⓘ a csomag 3 INF-jéből 2 való erre a gépre - csak azt telepítjük
                    #   ↻ a szűkített telepítés nem volt elég - a teljes csomag megy fel
                    #   ↷ már naprakész (már a rendszerben van) - kihagyva      <- HAZUGSÁG
                    #
                    # A csomag ILLETT és FEL IS MENT (a pnputil szerint már ott van a
                    # DriverStore-ban) - csak az ESZKÖZ nem vette át, tehát továbbra is a
                    # Windows alapdriverén fut. Ez nem "naprakész": a driver a gépen van, a
                    # kötés hiányzik, és arra KONKRÉT teendő van (újrakötés / újraindítás).
                    # A "nincs új csomag" ág viszont valóban naprakészt jelent.
                    if all_already and drv.get('generic_replace'):
                        _mark('staged_nobind', name)
                        with counter_lock:
                            staged_nobind.append(name)
                        self.emit('task_progress', {'task': task_id, 'log':
                                  f'  ⚠️ {name}: a gyári csomag MÁR FENT VAN a gépen, de az eszköz '
                                  f'még a Windows alapdriverén fut - a csomag telepítése tehát nem '
                                  f'hiányzik, a KÖTÉS hiányzik.'})
                        self.emit('task_progress', {'task': task_id, 'log':
                                  f'     👉 Teendő: „🔄 Eszközök újrakötése a gyári driverre” gomb '
                                  f'(a keresési mód alatt), vagy egy újraindítás - ettől veszi át '
                                  f'a Windows a már fent lévő gyári drivert.'})
                    else:
                        reason = 'már a rendszerben van' if all_already else 'nincs új csomag'
                        _mark('uptodate', name, reason)
                        self.emit('task_progress', {'task': task_id, 'log': f'  ↷ {name} már naprakész ({reason}) - kihagyva.'})
                else:
                    with counter_lock:
                        fail += 1
                    self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name} hiba: {res.stdout[:100]}'})
                    _mark('fail', name, (res.stdout or '')[:80])

                # Több-INF-es csomag fel nem használt INF-jeinek kivezetése (Razer-eset,
                # lásd _cleanup_unused_staged_infs). Reboot-igényes telepítésnél MOST nem
                # ítélkezünk (a kötés a következő bootnál dől el), de a csomagot nem
                # ejtjük: a defer=True feljegyzi a következő lábnak. Korábban itt egy
                # `if not pkg_reboot` állt, és a kihagyott csomag INF-jei örökre bent
                # maradtak - így hízott egy gép 23 csomagról 143-ra (2026-08-05, Latitude).
                if not is_offline:
                    pkg_reboot = (res.returncode == 3010
                                  or 'reboot is needed' in (res.stdout or '').lower())
                    # `sel` = amit a select_applicable_infs BIZONYÍTOTTAN ehhez a géphez
                    # valónak talált. Ezeket a takarítás nem veheti ki - lásd az ottani
                    # magyarázatot (Intel DCH kísérő-INF-ek / Code 18 eset).
                    self._cleanup_unused_staged_infs(res.stdout or '', name, task_id,
                                                     defer=pkg_reboot, selected_infs=sel)

            # HALADÁS-JELZÉS CSOMAGONKÉNT (2026-08-31, terepi visszajelzés: *"nincs
            # progress bar nincs szazalek nincs ido nincs semmi csak plain szöveg"*).
            # Ez a kör eddig EGYETLEN haladás-eseményt sem küldött: a felület csak a
            # naplósorokat kapta, tehát a sáv végig üresen állt, miközben a program
            # több száz MB-ot töltött le. A `process_catalog_driver` több ágon is
            # visszatér (nincs URL, már naprakész, INF-veto, hiba...), ezért a jelzés
            # egy BURKOLÓBAN van, `finally`-vel: így minden kimenetel után pontosan
            # egyszer lép a számláló, akkor is, ha az elem kivétellel végződött.
            done_count = [0]

            def _process_and_report(idx, drv):
                nonlocal fail
                try:
                    process_catalog_driver(idx, drv)
                except Exception as e:
                    # EGY ELNYELT KIVÉTEL A LEGROSSZABB KIMENETEL - ezt terepen mérve
                    # bizonyítottuk (2026-09-01). A `concurrent.futures.wait()` NEM dobja
                    # tovább a szálban keletkezett kivételt, csak eltárolja a Future-ben,
                    # és mivel `process_catalog_driver` egy beágyazott függvény, az
                    # `install_call_logging` sem fogja meg. Következmény: a hangkártya
                    # telepítése egy `NameError`-rel elszállt, a kör pedig derűsen
                    # "Sikeres: 0, Sikertelen: 0" összegzéssel zárult - az eszköz se a
                    # sikeres, se a hibás, se a kihagyott listán nem szerepelt, egyszerűen
                    # ELTŰNT. Néma hamis siker, pontosan az a hibaosztály, amit ez a
                    # projekt mindenhol üldöz. Innentől: naplóba teljes veremmel, a
                    # képernyőre érthető sorral, és HIBÁNAK számít.
                    logging.error(f"[CATALOG_INSTALL] KIVÉTEL a(z) '{drv.get('name')}' "
                                  f"feldolgozásakor: {e}", exc_info=True)
                    self.emit('task_progress', {'task': task_id, 'log':
                              f'  ❌ {drv.get("name")}: váratlan hiba a telepítés közben ({e}) - '
                              f'a részletek a naplóban.'})
                    with counter_lock:
                        fail += 1
                finally:
                    with counter_lock:
                        done_count[0] += 1
                        d, s, f, sk = done_count[0], success, fail, skipped
                    self.emit('task_progress', {
                        'task': task_id, 'current': d, 'total': total,
                        'counter': f'{d} / {total}',
                        # KÜLÖN MEZŐBEN, nem a szövegből visszafejtve: a felület fejléce
                        # ezekből rajzolja a "kész / hiba" jelzőket, és egy szövegből
                        # kitalált szám előbb-utóbb hazudna (pl. a visszaállítási pont
                        # "✅ kész" sorát is sikeres telepítésnek számolná).
                        'ok': s, 'fail': f, 'skipped': sk})

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(_process_and_report, i, drv) for i, drv in enumerate(selected_pool)]
                concurrent.futures.wait(futures)

            if self._check_cancel():
                self.emit('task_progress', {'task': task_id, 'log': '\n❗ Megszakítva!'})
                cancelled = True
                return success, fail, cancelled

            if success > 0 and not self.target_os_path:
                self.emit('task_progress', {'task': task_id, 'log': 'Eszközök újraszkennelése és Code 14 újraindítások elvégzése...'})
                self._run(['pnputil', '/scan-devices'])

                # Automatikus Eszközkezelő restart Code 14 (Restart Required) esetén
                code14_ps = r"""
                $devs = Get-PnpDevice | Where-Object { $_.ConfigManagerErrorCode -eq 14 }
                foreach ($d in $devs) {
                    Write-Output "Restarting $($d.Name)..."
                    Disable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
                    Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
                }
                """
                self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code14_ps])

                # KÖTÉS-ELLENŐRZÉS: a pnputil "Added driver packages: N" (N>0) csak annyit
                # jelent, hogy a csomag bekerült a DriverStore-ba - NEM azt, hogy az eszköz
                # át is vette. Terepen mérve (2026-07-25) két ilyen is volt egy futásban:
                # a Clevo-változatú Realtek hangdriver és egy NVIDIA katalógus-csomag
                # (32.0.15.9595), ami a nála SPECIFIKUSABB HWID-en álló 32.0.15.9186 mögött
                # maradt. Mindkettő "✅ telepítve"-ként jelent meg, a lánc pedig minden
                # lábon újra letöltötte őket. Sikernek csak a ténylegesen ÁTVETT driver
                # számít; a többi "kihagyva", és a hívó a következő lábra átviszi.
                if bind_checks:
                    now_info = self._get_installed_driver_info()
                    stuck = []
                    for drv, reboot_pending, pnp_out in bind_checks:
                        if reboot_pending:
                            continue   # csak a következő bootnál dől el - most nem ítélkezünk
                        cur = (now_info.get((drv.get('pnp_id') or '').upper()) or {})
                        cur_inf = (cur.get('inf') or '').strip().lower()
                        if cur_inf and cur_inf == drv.get('installed_inf'):
                            # A SZÜLŐ INF-je nem változott - de ez composite USB-nél NEM
                            # bukás: ott a gyári driver a &MI_xx gyerek-interfészre megy, a
                            # szülő pedig marad usbccgp-n, mert az a helyes driver rajta. A
                            # pnputil ilyenkor a gyereket nevezi meg ("installed/up-to-date
                            # on device: USB\VID_041E&PID_3274&MI_00\..."), és 2026-08-05-ig
                            # pont ezt az esetet könyveltük el "az eszköz nem vette át"-ként.
                            if package_bound_to_device_family(pnp_out, drv):
                                logging.info(f"[CATALOG_INSTALL] {drv.get('name')}: a csomag az eszköz "
                                             f"gyerek-interfészére kötött rá (a szülő marad "
                                             f"{drv.get('installed_inf')}, ez így helyes).")
                                bound_ok.append(drv)
                            else:
                                stuck.append(drv)
                        else:
                            bound_ok.append(drv)
                    for drv in stuck:
                        logging.warning(f"[CATALOG_INSTALL] A csomag felment, de az eszköz NEM vette át: "
                                        f"{drv.get('name')} - marad {drv.get('installed_inf')} "
                                        f"({drv.get('wu_title')})")
                        self.emit('task_progress', {'task': task_id, 'log': f'  ⚠️ {drv.get("name")}: a csomag feltelepült, de az eszköz TOVÁBBRA IS a régi driverén fut ({drv.get("installed_inf")}) - a Windows nem ezt választotta.'})
                        drv['no_bind_reason'] = 'felment, de az eszköz nem vette át'
                        no_bind.append(drv)
                    if stuck:
                        success -= len(stuck)
                        skipped += len(stuck)

                # Generikus -> gyári cserék utóellenőrzése (és szükség esetén visszaállítás).
                if generic_installs:
                    rolled_back = self._verify_generic_replacements(generic_installs, task_id)
                    if rolled_back:
                        success -= rolled_back
                        fail += rolled_back

            # TARTÓS NO-BIND EMLÉKEZET (catalog_no_bind.json): az autofix_stats.json-beli
            # lánc-szintű lista a lánc végén törlődik, ezért a kézi szken minden AutoFix
            # után újra ELŐRE BEJELÖLVE ajánlotta fel ugyanazokat a bizonyítottan nem-kötő
            # csomagokat (terepi log 2026-07-28: 5 katalógus-találatból 4 ilyen volt, és a
            # felhasználó jogosan hitte, hogy az AutoFix hagyott ki drivereket). Élő
            # rendszeren frissítjük; offline képnél kötés-ellenőrzés sincs, nincs adat.
            if not self.target_os_path and (no_bind or bound_ok):
                self._no_bind_record(no_bind, bound_ok)

        finally:
            logging.debug(f"[CATALOG_INSTALL] Temp dir törlése: {temp_dir}")
            for _ in range(3):
                try:
                    shutil.rmtree(temp_dir, ignore_errors=False)
                    break
                except Exception:
                    time.sleep(2)
            shutil.rmtree(temp_dir, ignore_errors=True)

        logging.info(f"[CATALOG_INSTALL] Kész - Sikeres: {success}/{total}, Sikertelen: {fail}, Kihagyott: {skipped}")
        self.emit('task_progress', {'task': task_id, 'current': total, 'total': total,
                                    'log': f'\n--- Katalógus: Sikeres: {success}, Sikertelen: {fail}' + (f', Kihagyott: {skipped}' if skipped else '') + ' ---'})
        # A "KIHAGYOTT" SZÁM ÖNMAGÁBAN MEGVÁLASZOLATLAN KÉRDÉS (2026-09-03, terepi
        # visszajelzés: *"7 telepitendo van 5 sikeres 0 sikertelen a maradek 2 vel mi
        # tortenik ilyenkor?"*). A technikus joggal olvassa úgy, hogy "2 driver a
        # levegőben maradt". Nem ez a helyzet: a kihagyott tétel vagy már naprakész volt,
        # vagy a program bizonyította, hogy a csomag nem ehhez a géphez való - egyik sem
        # elvarratlan szál. Ezt ki kell mondani, különben a szám maga kelt hiányérzetet.
        # TÉTELES ZÁRÓ MÉRLEG: MINDEN eszköz meg van nevezve a kimenetelével.
        #
        # Terepi visszajelzés (2026-09-03): *"7 db-ot talált, telepítés 7 db, utána kiírja
        # h 3db sikeres?? mi lett a maradek 4-el?"*. A három szám (sikeres/sikertelen/
        # kihagyott) önmagában megválaszolatlan kérdés, a magyarázatok pedig szétszórva,
        # a görgethető naplóban tűntek el - a technikus jogosan érezte úgy, hogy négy
        # driver "a levegőben maradt". Itt egy helyen, csoportosítva, NÉVVEL áll.
        #
        # A sorrend szándékos: elöl, ami történt (siker), utána ami rendben van
        # (naprakész), és a végén, amivel TEENDŐ lehet - az utolsó sor marad meg a
        # technikus szeme előtt.
        _CIMKEK = [
            ('ok',       '✅ Feltelepítve',        ''),
            ('uptodate', '↷ Már naprakész volt',  'nem kellett telepíteni - ez jó hír, nem kimaradás'),
            ('staged_nobind', '⚠️ A gyári csomag fent van, de az eszköz nem vette át',
             'nem a telepítés hiányzik, hanem a KÖTÉS → „Eszközök újrakötése” gomb vagy újraindítás'),
            ('netfail',  '↻ Le sem jött (hálózat)', 'a csomaggal nincs baj; a következő szkennelés újra felajánlja'),
            ('nolink',   '⏭️ Nincs letöltési link', 'a katalógus nem adott letölthető fájlt'),
            ('exe',      '⏭️ .exe telepítő',       'biztonsági okból nem futtatunk ismeretlen telepítőt automatikusan'),
            ('nosource', '🚫 Nincs hozzá való csomag', 'minden katalógus-jelöltet végigpróbáltunk, mind más gépgyártó változata'),
            ('fail',     '❌ Telepítési hiba',      ''),
        ]
        if outcome_detail:
            self.emit('task_progress', {'task': task_id, 'log': '\n📋 MI TÖRTÉNT TÉTELESEN:'})
            for kind, cimke, magyarazat in _CIMKEK:
                tetelek = [(n, x) for k, n, x in outcome_detail if k == kind]
                if not tetelek:
                    continue
                self.emit('task_progress', {'task': task_id,
                                            'log': f'  {cimke} ({len(tetelek)} db)'
                                                   + (f' — {magyarazat}' if magyarazat else '')})
                for nev, extra in tetelek:
                    self.emit('task_progress', {'task': task_id,
                                                'log': f'     • {nev}' + (f'  [{extra}]' if extra else '')})
            logging.info(f"[CATALOG_INSTALL] Tételes mérleg: "
                         f"{[(k, n) for k, n, _x in outcome_detail]}")
        if skipped:
            self.emit('task_progress', {'task': task_id, 'log':
                      f'ℹ️ A {skipped} kihagyott tétel NEM sikertelen telepítés — a fenti '
                      f'bontásban látod, melyik miért maradt ki. Amit a program egyszer már '
                      f'bizonyítottan nem tudott felrakni, azt a következő szkennelés a '
                      f'„Nem telepíthető" fülön mutatja, nem előre bejelölve.'})
        if no_source:
            # A ZÁRÓ FIGYELMEZTETÉS CSAK VALÓDI TEENDŐT JELENTHET BE (2026-09-08, terepen
            # mérve). Két szűrés kell hozzá, és mindkettőt egy-egy konkrét ellentmondás
            # kényszerítette ki ugyanabból a láncból:
            #
            #  1) MÁR GYÁRI DRIVEREN FUT (`inbox_now` hamis) - lásd a `chosen is None` ág
            #     indoklását. Ezt a program maga rakta fel pár perce; a gyártói oldalra
            #     küldeni érte félrevezető.
            #  2) A záró EGÉSZSÉGJELENTÉS ugyanezt az eszközt SZÁNDÉKOSAN nem sorolja fel.
            #     Mérve: a `PCI standard ISA bridge` a `machine.inf`-en fut, ami rajta van a
            #     HEALTH_REPORT_SKIP_INFS listán -> a katalógus-kör "menj a gyártó oldalára"
            #     teendőt írt ki rá, a lánc végi jelentés viszont (helyesen) meg sem
            #     említette. Ugyanaz a "két lista ellentmond egymásnak ugyanarról a gépről"
            #     hiba, amit az AOC-monitor esete után a CLAUDE.md már rögzít.
            #
            # A ki nem írt tételek NEM tűnnek el: a naplóba nevesítve, okkal mennek ki, és a
            # tételes mérleg ("🚫 Nincs hozzá való csomag") is felsorolja őket a képernyőn.
            # A döntés tiszta függvény (wu_core.no_source_is_actionable), hogy offline
            # tesztelhető legyen, és hogy a záró egészségjelentéssel egy helyen maradjon.
            todo = [e for e in no_source if no_source_is_actionable(e)]
            skipped_todo = [e for e in no_source if e not in todo]
            logging.warning(
                f"[CATALOG_INSTALL] Nincs való csomag a katalógusban: "
                f"{[e.get('name') for e in no_source]} - ebből VALÓDI TEENDŐ: "
                f"{[e.get('name') for e in todo]}")
            for e in skipped_todo:
                logging.info(
                    f"[CATALOG_INSTALL] NEM jelentjük teendőként ({e.get('name')}): "
                    + ('már gyári driveren fut' if not e.get('inbox_now')
                       else f"a Windows saját busz-/beviteli INF-jén fut ({e.get('installed_inf') or '?'}), "
                            f"vagy típuskódos azonosítója van - ehhez gyári csomag nem is létezik.")
                    + " A záró egészségjelentés sem sorolja fel, tehát nincs mit tenni vele.")
            if todo:
                self.emit('task_progress', {'task': task_id, 'log':
                          f'\n⚠️ {len(todo)} eszköz a Windows alapdriverén maradt, és a Microsoft '
                          f'katalógusában NINCS hozzá való csomag:'})
                for e in todo:
                    self.emit('task_progress', {'task': task_id, 'log': f'   • {e.get("name")}'})
                self.emit('task_progress', {'task': task_id, 'log':
                          '   Ezekhez a gyártó saját driver-oldaláról kell a driver (a szken végén ott a '
                          'gép/alaplap gyártójának hivatkozása). Nem hiba: a katalógus egyszerűen nem '
                          'tartalmaz ilyen csomagot ehhez a konkrét alaplap-változathoz.'})
        return success, fail, cancelled

    def _verify_generic_replacements(self, generic_installs, task_id='wu_install'):
        """A generikus -> gyári drivercserék utóellenőrzése, szükség esetén VISSZAÁLLÁS.

        Miért vállalható egyáltalán a csere: a Windows beépített drivere sosem tűnik el,
        csak háttérbe kerül - ha a frissen telepített gyári csomagot töröljük és
        újraszkennelünk, a PnP AUTOMATIKUSAN visszaköti a generikusat. Vagyis a művelet
        visszafordítható... DE csak olyan eszközön, ami futás közben újraköthető.

        EZ A HÁLÓ NEM FEDI A TÁROLÓVEZÉRLŐT. Ott a hiba a KÖVETKEZŐ bootnál jelentkezik
        (INACCESSIBLE_BOOT_DEVICE), amikor ez az ellenőrzés már rég lefutott - visszaállni
        csak helyreállító médiáról lehet. A tároló ezért 2026-07-28-ig fixen tiltva volt
        ezen a körön; 2026-07-28 és 2026-09-02 között egy alapból kikapcsolt
        jelölőnégyzet engedhette be - 2026-09-02 ÓTA VISZONT ÚJRA FIXEN TILTVA, a
        kapcsoló mindkét felületről eltűnt (a `allow_storage` paraméter megmaradt, de a
        hívók fixen False-t adnak). Vagyis ide tároló-eszköz ma NEM érkezhet; ha egy
        jövőbeli hívó mégis True-t adna, a "sikeres" verdikt csak annyit jelentene, hogy
        FUTÁS KÖZBEN nem lett hibás - a bootot ez a háló nem tudja ellenőrizni.

        Döntési szabály eszközönként:
          - hibakód 0            -> siker, marad a gyári driver;
          - hibakód != 0         -> a most telepített csomag törlése + rescan, majd
                                    egyetlen utóellenőrzés, és jelentés a felhasználónak;
          - az eszköz nincs a listában -> NEM állunk vissza (jellemzően kihúzott USB-s
                                    eszköz), csak jelezzük - egy lekérdezési hiba miatt
                                    kár lenne eldobni egy jó gyári drivert.
        Visszatérés: a visszaállított (tehát végül sikertelen) cserék száma."""
        logging.info(f"[GENERIC] {len(generic_installs)} generikus->gyári csere ellenőrzése...")
        self.emit('task_progress', {'task': task_id, 'log': '\n🔎 Gyári driverek ellenőrzése (visszaállítás, ha bármelyik hibás lett)...'})
        # A PnP-nek kell pár másodperc, amíg az új driverre átköti az eszközt.
        time.sleep(8)

        def device_error_codes():
            """{PNPDeviceID(nagybetűs): hibakód} a JELENLÉVŐ eszközökről."""
            try:
                ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                      "Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true } | "
                      "Select-Object PNPDeviceID, ConfigManagerErrorCode | ConvertTo-Json -Compress")
                res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=120)
                data = json.loads(res.stdout) if (res.stdout or '').strip() else []
                if isinstance(data, dict):
                    data = [data]
                out = {}
                for d in data:
                    pid = (d.get('PNPDeviceID') or '').upper()
                    if pid:
                        try:
                            out[pid] = int(d.get('ConfigManagerErrorCode') or 0)
                        except (TypeError, ValueError):
                            out[pid] = 0
                return out
            except Exception as e:
                logging.warning(f"[GENERIC] Eszköz-állapot lekérdezése sikertelen: {e}")
                return None

        # A hibakód mellé a TÉNYLEGESEN betöltött INF is kell: a hibakód 0 csak annyit
        # jelent, hogy az eszköz működik - azt nem, hogy a gyári drivert vette át. Terepen
        # (2026-07-25) ez pontosan félrevezetett: a Realtek hangcsomag felment, a hibakód 0
        # maradt, a felület "✅ gyári driver működik"-et írt, miközben az eszköz végig a
        # Microsoft hdaudio.inf-jén futott (ugyanabban a futásban a záró egészségjelentés
        # már helyesen jelezte). Sikernek csak az számít, ha az INF is kicserélődött.
        inf_now = {}
        try:
            inf_now = self._get_installed_driver_info() or {}
        except Exception as e:
            logging.warning(f"[GENERIC] Telepített driver-infó lekérdezése sikertelen: {e}")

        codes = device_error_codes()
        if codes is None:
            # Nem tudjuk eldönteni - inkább hagyjuk állni a gyári drivert, de mondjuk meg.
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ Az eszközök állapotát nem sikerült ellenőrizni - a gyári driverek fent maradtak. Nézd meg az Eszközkezelőt!'})
            return 0

        rolled_back = 0
        for drv, published_infs in generic_installs:
            name = drv.get('name') or '?'
            pnp_id = (drv.get('pnp_id') or '').upper()
            code = codes.get(pnp_id)
            if code is None:
                logging.warning(f"[GENERIC] {name}: az eszköz nincs a jelenlévők között, visszaállítás nélkül jelezzük.")
                self.emit('task_progress', {'task': task_id, 'log': f'  ⚠️ {name}: az eszköz eltűnt a listából (kihúzva?) - a gyári driver fent maradt.'})
                continue
            if code == 0:
                cur = (inf_now.get(pnp_id) or {})
                if cur and _is_inbox_driver(cur):
                    # Feltelepült, de az eszköz maradt a Windows driverén - NEM siker.
                    # Visszaállítani nincs mit (a generikus eleve rajta van), de kimondjuk.
                    logging.warning(f"[GENERIC] {name}: a gyári csomag felment, de az eszköz "
                                    f"a Windows driverén maradt ({cur.get('inf')}) - nem valódi csere.")
                    self.emit('task_progress', {'task': task_id, 'log': f'  ⚠️ {name}: a gyári csomag feltelepült, de az eszköz TOVÁBBRA IS a Windows driverén fut ({cur.get("inf")}) - valószínűleg más gépre szabott változat.'})
                    continue
                logging.info(f"[GENERIC] {name}: gyári driver OK (hibakód 0, INF: {cur.get('inf') or '?'}).")
                self.emit('task_progress', {'task': task_id, 'log': f'  ✅ {name}: gyári driver működik (eddig Microsoft alapdriver volt).'})
                continue

            desc = PNP_ERROR_CODE_DESCRIPTIONS.get(code, f'hibakód {code}')
            logging.warning(f"[GENERIC] {name}: a gyári driver után hibakód {code} - VISSZAÁLLÁS a Windows driverére.")
            self.emit('task_progress', {'task': task_id, 'log': f'  ⚠️ {name}: a gyári driver nem működik ({desc}) - visszaállás a Windows driverére...'})
            if not published_infs:
                # Nem tudjuk, mit publikált a pnputil - törölni sem tudunk pontosan.
                self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name}: nem sikerült azonosítani a telepített csomagot, kézi visszaállítás kellhet (Eszközkezelő -> Driver visszaállítása).'})
                rolled_back += 1
                continue
            for inf in published_infs:
                self._run(['pnputil', '/delete-driver', inf, '/uninstall', '/force'], ok_codes=(0, 3010), timeout=180)
            self._run(['pnputil', '/scan-devices'], timeout=180)
            time.sleep(5)
            after = device_error_codes() or {}
            new_code = after.get(pnp_id)
            rolled_back += 1
            if new_code == 0:
                self.emit('task_progress', {'task': task_id, 'log': f'  ↩️ {name}: visszaállítva a Windows alapdriverére, az eszköz újra működik.'})
            else:
                self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name}: a visszaállítás után is hibás (kód {new_code}) - indítsd újra a gépet, majd nézd meg az Eszközkezelőben!'})
        if rolled_back:
            self.emit('task_progress', {'task': task_id, 'log': f'ℹ️ {rolled_back} db gyári driver nem vált be, azoknál maradt a Windows alapdrivere.'})
        return rolled_back

    # ================================================================
    # PROBLÉMÁS ESZKÖZÖK - EGYKATTINTÁSOS GYORSJAVÍTÁS
    # ================================================================
    def fix_problem_device(self, pnp_id, code):
        """A "Problémás eszközök" szekció gyorsjavító gombja. Kód-függő akció:
        22 (letiltva) -> Enable-PnpDevice; minden más javítható kódnál (10/14/31/43...)
        disable+enable ciklus (az Eszközkezelő "eszköz újraindítása" megfelelője).
        Szinkron fut (pár másodperc), a _task_busy-t szándékosan nem foglalja - gyors,
        izolált művelet, nem nyúl a hw_updates_pool-hoz. Visszatérés:
        {'ok': bool, 'new_code': int|None, 'error': str} - a toast/megjelenítés a JS dolga."""
        logging.info(f"[API] fix_problem_device({pnp_id!r}, code={code})")
        if self.target_os_path:
            return {'ok': False, 'new_code': None, 'error': 'Offline módban nem elérhető'}
        if not pnp_id:
            return {'ok': False, 'new_code': None, 'error': 'Hiányzó eszköz-azonosító'}
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 0
        action = 'enable' if code == 22 else 'cycle'
        ps = (f"$id = '{_ps_quote(str(pnp_id))}'\n"
              f"$act = '{action}'\n"
              r"""
try {
    if ($act -eq 'enable') {
        Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
    } else {
        Disable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
        Start-Sleep -Seconds 2
        Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
    }
    Write-Output "ACTED"
} catch { Write-Output "ERR: $($_.Exception.Message)" }
Start-Sleep -Seconds 3
try {
    $p = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction Stop).Data
    Write-Output "CODE: $p"
} catch { Write-Output "CODE: ?" }
""")
        try:
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                            encoding='utf-8', timeout=90)
            out = (res.stdout or '')
            acted = 'ACTED' in out
            err_m = re.search(r'ERR:\s*(.+)', out)
            code_m = re.search(r'CODE:\s*(\d+)', out)
            new_code = int(code_m.group(1)) if code_m else None
            error = (err_m.group(1).strip() if err_m else '')
            ok = acted and (new_code == 0 or new_code is None)
            logging.info(f"[FIX-DEVICE] {pnp_id}: acted={acted}, new_code={new_code}, err={error!r}")
            return {'ok': ok, 'new_code': new_code, 'error': error}
        except Exception as e:
            logging.error(f"[FIX-DEVICE] Hiba: {e}")
            return {'ok': False, 'new_code': None, 'error': str(e)}
