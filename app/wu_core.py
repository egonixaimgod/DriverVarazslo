"""WU DRIVER KERESÉS / TELEPÍTÉS - KÖZÖS MAG. Az eszköz-szűrés, a WU-találat<->eszköz
párosítás és a telepítő PowerShell script EGYETLEN példánya - a GUI manuális telepítés,
a GUI AutoFix és a CLI AutoFix is EZT hívja. NE másold vissza osztályba (lásd CLAUDE.md)!"""

# === AUTO-IMPORTS ===
import os
import re
import json
import time
import queue
import shutil
import logging
import threading
from app.common import _ps_quote
from app.common import _app_data_dir
# === /AUTO-IMPORTS ===




# AutoFix-nál opcionálisan kihagyható driver-osztályok (nyomtató + szkenner/multifunkciós) -
# ezek gyakran csak gyári driverrel működnek jól, a WU nem mindig telepíti vissza automatikusan.
AUTOFIX_PRINTER_SKIP_CLASSES = {'Printer', 'PrintQueue', 'Image'}


# Ennyi EGYMÁST KÖVETŐ telepítési hiba után megszakítjuk a kört (lásd a
# _install_abort_reason docstringjét: a "mérgezett session" tünete).
WU_MAX_CONSECUTIVE_FAILURES = 3


class WuProcessAborted(Exception):
    """A WU telepítő PowerShell folyamat idő előtt leállítva. reason='cancel' (felhasználói
    megszakítás), 'hang' (a watchdog ölte meg, mert túl sokáig nem jött kimenet),
    'reboot' (pending-reboot miatt értelmetlen tovább telepíteni) vagy 'failstreak'
    (sorozatos telepítési hiba - a session mérgezett)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _iter_process_lines(process, run_fn, cancel_check=None, inactivity_timeout=1800, abort_check=None):
    """A telepítő PowerShell stdout-jának CANCEL-KÉPES, WATCHDOG-OS olvasása - mindhárom
    fogyasztó (GUI manuális, GUI AutoFix, CLI AutoFix) ezen keresztül olvassa a sorokat.

    A régi, közvetlen `for line in process.stdout` minta két terepi hibát hordozott:
    (1) a megszakítás-ellenőrzés csak új sor érkezésekor futott le, így ha a scripten
    belüli $Searcher.Search() végleg beragadt (arra ott nincs timeout, csak a külön
    _search_wu_api-nak van), a Mégse gomb halott volt; (2) a beragadt folyamatot semmi
    nem ölte meg, a feladat örökre "futott". Itt a tényleges olvasás egy háttérszálon
    történik queue-ba, a fogyasztó 0,5 mp-enként ellenőrzi a cancel-t, és ha
    inactivity_timeout másodpercig egyetlen sor sem érkezik, taskkill-lel leállítja a
    folyamatot. A timeout szándékosan hosszú (alapból 30 perc): egyetlen nagy driver
    letöltése lassú neten percekig ad nulla kimenetet - inkább későn ölünk, mint egy
    élő letöltést.

    abort_check: opcionális callback, amely MINDEN feldolgozott sor UTÁN lefut, és egy
    okot (string) ad vissza, ha a hívó le akarja állítani a telepítőt - a folyamatot
    itt öljük le, és WuProcessAborted(ok) száll. Ezen keresztül lép közbe a
    pending-reboot felismerés ('reboot') és a sorozatos-hiba megszakító ('failstreak'):
    a hívónak nem kell PID-et kezelnie, és a leállítás mindhárom fogyasztónál azonos.

    Kivétel: WuProcessAborted('cancel' | 'hang' | abort_check oka) - a folyamat ilyenkor
    már le van ölve."""
    q = queue.Queue()

    def _reader():
        try:
            for raw in process.stdout:
                q.put(raw)
        except Exception as e:
            logging.debug(f"[WU-READER] stdout olvasási hiba: {e}")
        finally:
            q.put(None)

    threading.Thread(target=_reader, daemon=True, name="wu-reader").start()

    def _kill(why):
        logging.warning(f"[WU-WATCHDOG] Telepítő folyamat leállítása (PID={process.pid}, ok={why})")
        try:
            run_fn(['taskkill', '/F', '/T', '/PID', str(process.pid)])
        except Exception as e:
            logging.error(f"[WU-WATCHDOG] taskkill hiba: {e}")
        try:
            process.wait(timeout=10)
        except Exception as e:
            logging.debug(f"[WU-WATCHDOG] process.wait a taskkill után sem tért vissza: {e}")

    last_output = time.time()
    while True:
        if cancel_check and cancel_check():
            _kill('cancel')
            raise WuProcessAborted('cancel')
        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            if time.time() - last_output > inactivity_timeout:
                logging.error(f"[WU-WATCHDOG] {inactivity_timeout}s óta nincs kimenet - a WU folyamat beragadt.")
                _kill('hang')
                raise WuProcessAborted('hang')
            continue
        if item is None:
            break
        last_output = time.time()
        line = item.strip()
        if line:
            yield line
            # A hívó MÁR feldolgozta a sort (a yield visszatért) - itt kérdezzük meg,
            # akar-e leállni. Így a hívó számlálói/állapota naprakészek a döntéskor.
            if abort_check:
                reason = abort_check()
                if reason:
                    logging.warning(f"[WU-WATCHDOG] A hívó megszakítást kért: {reason}")
                    _kill(reason)
                    raise WuProcessAborted(reason)
    process.wait()



# ============================================================================
# WU DRIVER KERESÉS / TELEPÍTÉS - KÖZÖS MAG
# A temp-cleanup mintájára: az eszköz-szűrés, a WU-találat<->eszköz párosítás és
# a telepítő PowerShell script EGYETLEN példányban itt él, és a manuális
# telepítés (DriverToolApi._install_wu_api + start_hw_scan), a GUI AutoFix
# (_scan_and_install_wu_sync) és a CLI AutoFix (CliApi) is EZEKET hívja.
# Ha itt javítasz valamit, mindhárom út egyszerre javul - NE másold vissza a
# logikát egyik osztályba se, mert pont az szülte a korábbi "az autofix
# működik, a manuális eltört" hibát!
# ============================================================================

# WU driver-kereséskor figyelmen kívül hagyott PnP eszközosztályok (mindhárom út közös szűrője).
# A DRIVER-KERESÉSBŐL TELJESEN KIHAGYOTT eszközosztályok. FIGYELEM: ez a szűrő a
# `_filter_wu_scan_devices`-ben fut, tehát MINDKÉT forrásra hat - a WU Agent SEM lát
# ilyen eszközt, nem csak a katalógus. Ami ide kerül, arra a program soha, semmilyen
# úton nem ad drivert; ezért csak olyan osztály lehet itt, amihez gyári driver NEM
# LÉTEZIK (tisztán szoftveres/absztrakt objektumok) vagy amit külön kapcsoló véd.
#
# 2026-07-27 (explicit user decision, "driverezzük fel normálisan a gépet"): a Monitor,
# a Battery és a Processor KIKERÜLT innen. Egyik sem absztrakt objektum és egyikhez sem
# tartozik boot-kockázat:
#  - Monitor: a monitor-INF nem "csak metaadat" - EDID-felülbírálást ÉS a gyári ICC
#    színprofilt hozza magával, tehát pont az a csomag, ami a színkezelést helyre teszi
#    (lásd app/colorprofile_core.py: a fix a kalibrációt gyári alapra állítja, a gyári
#    alap viszont a monitor INF-jéből jön). Rossz monitor-driver a képen kívül semmit
#    nem tud elrontani, és a Windows alap-monitordrivere sosem tűnik el mögüle;
#  - Processor: az AMD/Intel chipset-csomagok processzor-energiakezelő drivert is
#    szállítanak (a WU is ad rá csomagot);
#  - Battery: a gyártói akku-/töltésvezérlő driverek ide tartoznak (laptopoknál valós).
# 2026-07-27, MÁSODIK KÖR (explicit user decision, "MINDEN kapjon drivert, semmi ne
# legyen kizárva, kivéve amit checkboxszal nem engedek"): innen kikerült a Printer, a
# CDROM, a WPD és a DiskDrive is.
#  - Printer: a FIZIKAI nyomtató mostantól kap drivert. Eddig itt volt, és ez egy csúnya
#    aszimmetriát okozott: a nyomtató-checkbox csak a TÖRLÉSTŐL védte, a keresésből
#    viszont eleve ki volt zárva - vagyis kipipálatlan checkboxnál az AutoFix letörölte a
#    nyomtató driverét, és soha nem is kereshetett helyette újat. A checkbox jelentése
#    változatlan (= ne töröljük a meglévőt), de ha mégis törlődik, a WU most már tud
#    helyette telepíteni;
#  - CDROM / WPD: optikai meghajtó és hordozható eszköz (telefon). Ritkán van rájuk gyári
#    csomag, de ha van, semmi nem indokolja a kizárást - a keresés úgyis csak akkor talál,
#    ha tényleg létezik csomag;
#  - DiskDrive: NEM kikerült, hanem ÁTKERÜLT a checkbox-vezérelt körbe: benne van a
#    STORAGE_RISK_CLASSES-ben, tehát a tároló-kapcsoló dönt róla (mint a vezérlőkről).
#    Így az SSD-firmware / gyártói lemez-csomagok is elérhetők, de csak tudatosan.
#
# AMI MARAD, és miért - kizárólag olyan osztályok, amikhez driver FOGALMILAG nem tartozik
# (mind `SWD\`/`ROOT\` szoftver-objektum, nem hardver), plusz a HAL:
#  - Volume, VolumeSnapshot: a kötet (C:, D:) és az árnyékmásolat mint PnP-objektum. Nem
#    hardver; a mögöttes LEMEZT és VEZÉRLŐT külön driverezzük (DiskDrive/SCSIAdapter/HDC);
#  - Endpoint, AudioEndpoint: a `SWD\MMDEVAPI\...` hang-VÉGPONTOK ("Hangszórók (Realtek)",
#    "Mikrofon"). Ezek a hangkártya KIMENETEI, nem a hangkártya - azt a MEDIA osztályban
#    driverezzük;
#  - PrintQueue: a `SWD\PRINTENUM\...` nyomtatási SOR, nem a nyomtató (az már Printer);
#  - LegacyDriver: nem-PnP, régi driver-bejegyzés; nincs mögötte eszköz, amire telepíteni
#    lehetne;
#  - Computer: a HAL ("ACPI x64-based PC"). Ezt kicserélni azt jelenti, hogy a gép nem
#    indul el - és a WU/katalógus amúgy sem szállít hozzá csomagot.
# (AutoFix: soha; manuális szken: igen, piros figyelmeztetéssel).
WU_SCAN_IGNORED_CLASSES = ['Volume', 'VolumeSnapshot', 'Computer',
                           'LegacyDriver', 'Endpoint', 'AudioEndpoint', 'PrintQueue']

# VIRTUÁLIS (hypervisor-vendég) eszközök neve. Régen ez egy nyers `"virtual" in név`
# részszöveg-vizsgálat volt, ami VALÓDI hardvert is kizárt: az "Intel(R) Virtual Buttons"
# (a hangerő/bekapcsoló gombok sok üzleti laptopon és 2-in-1-en) csak azért esett ki, mert
# a nevében szerepel a "Virtual" szó - és erről semmilyen napló nem szólt. A minta ezért
# konkrét hypervisor-nevekre szűkült, szóhatárral; a valóban virtuális Microsoft-eszközöket
# (Basic Render, RDPBUS, NDIS Virtual Bus) amúgy is a `ROOT\`/osztály-szabály fogja.
_VIRTUAL_DEVICE_NAME_RE = re.compile(
    r'\b(vmware|virtualbox|vbox|qemu|parallels|hyper-?v|xen|virtio)\b', re.IGNORECASE)

# A jelenlévő PnP eszközök lekérdezése (a kimenetet a _filter_wu_scan_devices dolgozza fel).
# A ConfigManagerErrorCode is jön: a hibakódos eszközök (28 = nincs driver, 10 = nem indul,
# stb.) a manuális szken "Problémás eszközök" szekciójához és a hibrid katalógus-
# kiegészítéshez kellenek.
WU_PNP_QUERY_PS = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                   "Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true -and $_.ConfigManagerErrorCode -ne 45 } | "
                   "Select-Object Name, PNPClass, PNPDeviceID, HardwareID, ConfigManagerErrorCode | ConvertTo-Json -Compress")


def _filter_wu_scan_devices(pnp_data):
    """A WU_PNP_QUERY_PS JSON kimenetéből kiszűri a driver-kereséshez érdemi eszközöket
    (virtuális/ROOT/ignorált osztályok nélkül, HWID szerint deduplikálva) és kategorizálja őket."""
    if not isinstance(pnp_data, list):
        pnp_data = [pnp_data] if pnp_data else []
    # HWID -> a már felvett eszköz dict-je. A deduplikálás azért kell, hogy két AZONOS
    # eszközre (2 db ugyanolyan NIC, 4 db azonos USB-vezérlő) ne fusson le kétszer
    # ugyanaz a WU-/katalógus-lekérdezés. A duplikátum viszont NEM veszhet el nyomtalanul:
    # ha a második példány hibakódos és az elsőnek nincs hibája, a hibakód (és a
    # példányszám) felkerül a megtartott eszközre - különben egy döglött második
    # hangkártya/hálókártya sosem jelent meg a "Problémás eszközök" listán.
    seen_hwids = {}
    devices = []
    # MIT DOBTUNK EL, ÉS MIÉRT - okonként. A CLAUDE.md szabálya szerint minden
    # listaszűkítő döntésnek meg kell neveznie, mit ejtett; ez a függvény viszont sokáig
    # NÉGY okból dobott el eszközöket úgy, hogy egyikről sem szólt egy sort sem. Emiatt a
    # "miért nem kapott az X eszköz drivert?" kérdésre a terepi logból nem lehetett
    # válaszolni - csak a forráskódból, és ott is csak ha az ember tudta, hol keresse.
    dropped = {'nincs PNPDeviceID': [], 'virtuális gép eszköze': [], 'osztály': []}
    for d in pnp_data:
        n = d.get("Name") or "Ismeretlen Eszköz"
        pid = d.get("PNPDeviceID") or ""
        pclass = d.get("PNPClass") or ""
        hwids_list = d.get("HardwareID") or []
        if isinstance(hwids_list, str):
            hwids_list = [hwids_list]

        if not pid:
            dropped['nincs PNPDeviceID'].append(n)
            continue
        if _VIRTUAL_DEVICE_NAME_RE.search(n):
            dropped['virtuális gép eszköze'].append(n)
            continue
        # A `ROOT\` szűrő 2026-07-27-én MEGSZŰNT (explicit user decision): a ROOT-ból
        # enumerált eszközök többsége Microsoft szoftver-eszköz (Basic Render, RDPBUS,
        # volmgr) - azokra úgysincs csomag, tehát a felvételük csak pár fölösleges
        # összehasonlítás -, DE ide kerülnek az alkalmazások által telepített driverek is
        # (ViGEmBus, VPN TAP-adapterek). Azokat eddig NÉMÁN kizártuk a keresésből ÉS a
        # "Problémás eszközök" listáról is, vagyis egy hibás VPN-adapter sosem tűnt fel.
        # A katalógus-oldal amúgy is kiszűri őket (a `ROOT\...` nem gyártó-kódos HWID).
        if pclass in WU_SCAN_IGNORED_CLASSES:
            dropped['osztály'].append(f"{n} [{pclass}]")
            continue

        hwid_clean = hwids_list[0] if hwids_list else pid
        if not hwid_clean:
            continue
        try:
            err_code = int(d.get("ConfigManagerErrorCode") or 0)
        except (TypeError, ValueError):
            err_code = 0
        prev = seen_hwids.get(hwid_clean)
        if prev is not None:
            # Azonos hardver másodpéldánya: nem külön eszközként visszük tovább (fölösleges
            # duplikált keresés lenne), de a hibakódját és a példányszámot megőrizzük.
            prev['dup_count'] = prev.get('dup_count', 1) + 1
            prev.setdefault('dup_pnp_ids', []).append(pid)
            if err_code and not prev.get('err_code'):
                prev['err_code'] = err_code
                prev['err_from_dup'] = True
                logging.info(f"[PNP] Azonos HWID másodpéldánya hibakódos ({err_code}): {n} - "
                             f"a hibakód átvéve a megtartott példányra ({hwid_clean}).")
            continue

        # A Win32_PnPEntity.PNPClass írásmódja eszközönként eltér (a hangkártyáknál pl.
        # "MEDIA" csupa nagybetűvel), ezért a besorolás KISBETŰSÍTVE hasonlít - a régi,
        # kis-nagybetű érzékeny összehasonlítás miatt minden hangeszköz a
        # "🔧 Egyéb (MEDIA)" gyűjtőbe esett a "🎵 Hangkártya (Audio)" helyett.
        pclass_l = pclass.strip().lower()
        if pclass_l == "display": cat = "🎮 Videókártya (VGA)"
        elif pclass_l == "media": cat = "🎵 Hangkártya (Audio)"
        elif pclass_l == "net": cat = "🌐 Hálózat (LAN/Wi-Fi)"
        elif pclass_l == "bluetooth": cat = "🔵 Bluetooth"
        elif pclass_l == "system": cat = "⚙️ Rendszereszköz"
        elif pclass_l == "usb": cat = "🔌 USB Vezérlő"
        elif pclass_l in ("camera", "image"): cat = "📷 Webkamera"
        elif pclass_l in ("mouse", "keyboard", "hidclass"): cat = "🖱️ Periféria"
        elif pclass_l == "biometric": cat = "🔒 Ujjlenyomat / Biometria"
        else: cat = f"🔧 Egyéb ({pclass})"

        # pclass: a nyers PNPClass megőrzése - erre épül a "gyári driver a generikus
        # helyett" osztály-szűrése (is_generic_replace_candidate); a 'cat' emberi
        # felirat, azzal szűrni törékeny lenne.
        dev = {"cat": cat, "name": n, "id": hwid_clean, "pnp_id": pid,
               "all_hwids": hwids_list, "err_code": err_code, "pclass": pclass}
        seen_hwids[hwid_clean] = dev
        devices.append(dev)
    # A deduplikálás LISTÁT SZŰKÍT, tehát meg kell mondania, mit vont össze - különben egy
    # "eltűnt az egyik hálókártyám" bejelentés visszakövethetetlen (lásd CLAUDE.md).
    merged = [(d['name'], d['dup_count']) for d in devices if d.get('dup_count')]
    if merged:
        logging.info(f"[PNP] {len(devices)} eszköz a szűrés után; azonos HWID-ű példányok "
                     f"összevonva: {merged}")
    else:
        logging.info(f"[PNP] {len(devices)} eszköz a szűrés után (nincs többpéldányos eszköz).")
    # A KIZÁRÁSOK okonként: összesítő INFO-n, a nevek DEBUG-on. Ez a sor a válasz arra,
    # hogy "mit NEM néztünk meg egyáltalán" - eddig ez a terepi logból hiányzott.
    total_dropped = sum(len(v) for v in dropped.values())
    if total_dropped:
        logging.info(f"[PNP] {total_dropped} eszköz kizárva a driver-keresésből "
                     f"({', '.join(f'{k}={len(v)}' for k, v in dropped.items() if v)}).")
        for reason, names in dropped.items():
            if names:
                logging.debug(f"[PNP] Kizárva ({reason}): {names}")
    return devices


def _hwid_tokens(hwid):
    """('PCI', {'VEN_1002','DEV_6811','REV_00'}) alakra bontás, vagy None, ha az azonosítónak
    nincs busz-előtagja (pl. a csupasz 'usbmmidd')."""
    s = str(hwid or '').strip().upper()
    bus, sep, rest = s.partition('\\')
    if not sep or not bus:
        return None
    toks = frozenset(t for t in rest.split('&') if t)
    return (bus, toks) if toks else None


def _hwid_matches(wu_hwid, dev_hwid):
    """Ugyanarra az eszközre vonatkozik-e a WU-csomag hardver-azonosítója és az eszközé?

    Két szabály, ÖSSZEVONVA (a token-alapú csak bővíti a találatokat, a régit nem rontja):

    1. prefix-egyezés bármelyik irányban - a `usb\\vid_046d&pid_c52b` (kompozit eszköz) és a
       `usb\\vid_046d&pid_c52b&mi_00` (interfész) egymásra illesztésére;
    2. TOKEN-RÉSZHALMAZ azonos buszon: a WU-azonosító minden tagja szerepel az eszközében.

    A 2. szabály nélkül a Windows által generált azonosítók tagsorrendje kizár valódi
    egyezéseket. Terepen bizonyított (2026-07, Win10 + Radeon R9 200): a WU
    `pci\\ven_1002&dev_6811&rev_00`-t ad, az eszköz azonosítói viszont
    `PCI\\VEN_1002&DEV_6811&SUBSYS_30001682&REV_00` / `...&CC_030000` alakúak - a SUBSYS a REV
    ELÉ ékelődik, így egyik sem prefixe a másiknak. A videokártya-driver ezért mindhárom
    körben "nem párosítható" maradt: az AutoFix TÖRÖLTE a gyári AMD drivert, majd sosem
    telepítette vissza, és a gép a Microsoft alap videokártya-driverén maradt - hibakód
    nélkül, tehát az összefoglaló is tisztának látta. Ugyanez érinti a `&cc_` osztály-szintű
    csomagokat is (előző terepi gép: `pci\\ven_1002&cc_040300`, amdafd.inf).

    Busz-előtag NÉLKÜLI azonosítónál (pl. 'usbmmidd') CSAK a pontos egyezés számít: ezek
    olyan rövidek, hogy bármilyen lazább szabály hamis találatot adna."""
    w = str(wu_hwid or '').strip().upper()
    d = str(dev_hwid or '').strip().upper()
    if not w or not d:
        return False
    if w == d:
        return True
    wt = _hwid_tokens(w)
    dt = _hwid_tokens(d)
    # Busz-előtag NÉLKÜLI azonosító (pl. 'usbmmidd'): csak a fenti pontos egyezés számít.
    # A korábbi kód itt még egy nyers string-prefixet is elfogadott, ami pont az ellen
    # hatott, amit a docstring ígér: az 'USB' így illeszkedett az 'USBMMIDD'-re.
    if not wt or not dt or wt[0] != dt[0]:
        return False
    # ALSÓ KORLÁT: a szűkebb azonosítónak legalább 2 tokenesnek kell lennie. Egyetlen
    # tokenes azonosító (pl. 'PCI\VEN_8086') különben a gép ÖSSZES Intel PCI-eszközére
    # illeszkedne - részhalmazként ÉS prefixként is -, és a csomag egy tetszőleges
    # eszközhöz rendelődne. Rossz telepítést ez nem okoz (a pnputil úgyis ellenőrzi az
    # alkalmazhatóságot), viszont a downgrade-védelem és a "telepítve: X" kijelzés a
    # ROSSZ eszköz adatait nézné. A terepen bizonyított esetek mind 2+ tokenesek:
    # ven+dev+rev (R9 200), ven+cc (amdafd), vid+pid (USB kompozit).
    if min(len(wt[1]), len(dt[1])) < 2:
        return False
    # Részhalmaz MINDKÉT irányban: a WU-azonosító lehet általánosabb (ven+dev vs.
    # ven+dev+subsys+rev), de lehet specifikusabb is (a kompozit USB-eszköz szülője
    # rövidebb, mint a csomag &MI_00-s interfész-azonosítója). Ez a két irány váltja ki
    # a régi string-prefix szabályt, annak hamis találatai nélkül.
    return wt[1] <= dt[1] or dt[1] <= wt[1]


# INF-ből kiolvasható hardver-azonosító: BUSZ\TOKEN&TOKEN... alak. A `_` megkövetelése
# zárja ki a registry-utakat (SYSTEM\CurrentControlSet\...), amikben nincs VEN_/DEV_-szerű tag.
_INF_HWID_RE = re.compile(r'\b([A-Z0-9]{2,12}\\[A-Z0-9_&\.\-]{4,})', re.IGNORECASE)


def extract_inf_hardware_ids(text):
    """Egy INF fájl szövegéből a benne felsorolt hardver-azonosítók halmaza."""
    out = set()
    for m in _INF_HWID_RE.finditer(text or ''):
        cand = m.group(1).strip().upper().rstrip('.,;')
        if '_' in cand:
            out.add(cand)
    return out


def _read_text_best_effort(path):
    """INF beolvasása: a fájlok hol UTF-16 LE (BOM-mal), hol BOM NÉLKÜLI UTF-16 LE/BE-ben,
    hol ANSI, hol UTF-8 kódolásúak.

    A BOM NÉLKÜLI UTF-16 ág nem elméleti: a `latin-1` fallback SOHA nem dob kivételt,
    tehát egy ilyen fájlt is "sikeresen" beolvasunk - csak épp `P\\x00C\\x00I\\x00...`
    alakban, amiben a hardver-azonosító minta nem illeszkedik, így a fájl NULLA
    azonosítót ad. Terepen (2026-07-27, RTX 3060) pont ez történt: az NVIDIA
    katalógus-csomag display-INF-je nem adott egyetlen azonosítót sem, csak a
    (sima ANSI) nvhda.inf 158 HDAUDIO-s ID-je jött át - a csomagot ezért
    "nem erre a gépre való"-ként vetettük el, és 1,1 GB letöltés ment a kukába
    (kétszer, két külön lábon). A BOM nélküli UTF-16-ot ezért felismerjük."""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except Exception as e:
        logging.debug(f"[INF] Nem olvasható ({path}): {e}")
        return ''
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try:
            return raw.decode('utf-16')
        except Exception:
            pass
    # BOM nélküli UTF-16 felismerése: az INF-ek ASCII-tartalmúak, ezért UTF-16-ban minden
    # második bájt 0x00. A minta-vizsgálat az első ~4 KB páros/páratlan pozícióin fut
    # (a teljes fájl végigszámolása nagy display-INF-eknél felesleges munka lenne).
    probe = raw[:4096]
    if len(probe) >= 16:
        even_nul = probe[0::2].count(0)
        odd_nul = probe[1::2].count(0)
        half = len(probe) // 2
        for enc, nul in (('utf-16-le', odd_nul), ('utf-16-be', even_nul)):
            if half and nul >= half * 0.8:
                try:
                    return raw.decode(enc, errors='replace')
                except Exception:
                    break
    for enc in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return ''


def inf_package_applies(ext_dir, dev_hwids):
    """Vonatkozik-e a kicsomagolt driver-csomag EGYÁLTALÁN erre az eszközre?

    Miért kell: a katalógus a gyártó+eszköz TÖRZS-azonosítójára (SUBSYS nélkül) az adott
    chip ÖSSZES gépgyártó-specifikus változatát visszaadja, és a legfrissebb dátumú nyer -
    ami könnyen egy másik gyártó gépére szabott csomag. Terepen mérve (2026-07-25, ASRock
    B450M + Realtek ALC892): a katalógus a `HDXClevo.inf` / `HDXWHITE.inf` (Clevo-laptop)
    változatot adta 6.0.9992.1 verzióval. A pnputil ezt szó nélkül a DriverStore-ba tette
    ("Added driver packages: 4"), de a SUBSYS_18496893-as ASRock hangchipre SOHA nem
    kötötte rá - se telepítéskor, se újraindítás után. Eredmény: hamis "✅ telepítve",
    az eszköz marad a hdaudio.inf-en, és a következő láb újra letölti ugyanazt.

    Ez a függvény azt csinálja, amit a PnP is: megnézi, hogy a csomag INF-jeiben szerepel-e
    az eszköz valamelyik VALÓDI hardver-azonosítója. (Szándékosan a nyers `all_hwids`
    listával hasonlítunk, nem a keresésnél szintetizált törzs-azonosítóval - a Windows is
    az igazi ID-kre illeszt.)

    Visszatérés: True (illeszkedik), False (biztosan nem), None (nem eldönthető - nem
    találtunk értelmezhető azonosítót az INF-ekben, ilyenkor NEM blokkolunk)."""
    dev_hwids = [h for h in (dev_hwids or []) if h]
    if not dev_hwids:
        return None
    inf_ids = set()
    per_file = []          # (fájlnév, hány azonosítót adott) - a döntés bizonyítéka a logban
    for root, _dirs, files in os.walk(ext_dir):
        for fn in files:
            if fn.lower().endswith('.inf'):
                ids = extract_inf_hardware_ids(_read_text_best_effort(os.path.join(root, fn)))
                per_file.append((fn, len(ids)))
                inf_ids |= ids
    if not inf_ids:
        logging.info(f"[INF] A csomag {len(per_file)} INF-jéből egyetlen hardver-azonosítót sem "
                     f"sikerült kiolvasni - nem döntünk, a pnputil-ra bízzuk. Fájlok: {per_file[:8]}")
        return None
    for inf_id in inf_ids:
        for dh in dev_hwids:
            if _hwid_matches(inf_id, dh):
                return True

    # VÉTÓ ELŐTTI JÓZANSÁGI PRÓBA: csak akkor mondjuk ki, hogy "ez a csomag nem ennek az
    # eszköznek szól", ha egyáltalán a MEGFELELŐ INF-et olvastuk. Ha a kicsomagolt fából
    # egyetlen azonosító sem esik az eszköz saját BUSZÁRA (pl. az eszköz PCI\..., a
    # kiolvasott ID-k viszont mind HDAUDIO\...), akkor bizonyíthatóan nem a készülékhez
    # tartozó INF-et néztük - jellemzően mert a fő INF-et nem sikerült dekódolni.
    # Ilyenkor NEM vétózunk (None = eldönthetetlen): a pnputil úgyis ellenőrzi az
    # alkalmazhatóságot, a telepítés utáni kötés-ellenőrzés pedig elkapja, ha mégsem
    # kötött rá. Terepen (2026-07-27) a régi, feltétel nélküli False dobta el a
    # jogosan újabb NVIDIA display-csomagot (32.0.15.9595 > telepített 32.0.15.9186).
    # A valódi terepi rossz-csomag esetet (Clevo-változatú Realtek audio) ez NEM
    # gyengíti: ott a device is HDAUDIO\, az INF-ek is HDAUDIO\-sak - azonos busz,
    # tehát a vétó ott továbbra is életbe lép.
    dev_buses = {b for b in ((_hwid_tokens(d) or (None,))[0] for d in dev_hwids) if b}
    inf_buses = {b for b in ((_hwid_tokens(i) or (None,))[0] for i in inf_ids) if b}
    if dev_buses and not (dev_buses & inf_buses):
        logging.warning(f"[INF] A csomag INF-jei NEM az eszköz buszáról valók "
                        f"(eszköz: {sorted(dev_buses)}, INF-ekben: {sorted(inf_buses)[:6]}) - "
                        f"valószínűleg a fő INF-et nem sikerült beolvasni, ezért NEM vétózunk. "
                        f"Fájlok: {per_file[:8]}")
        return None

    logging.info(f"[INF] A csomag egyik INF-je sem illeszkedik az eszközre. "
                 f"Eszköz-ID-k: {dev_hwids[:3]} | INF-ben {len(inf_ids)} azonosító, minta: {sorted(inf_ids)[:4]} "
                 f"| INF-enként: {per_file[:8]}")
    return False


def _match_wu_updates_to_devices(wu_results, devices, exclude_uids=None):
    """WU-találatok párosítása a jelenlévő eszközökhöz. A "legjobb mindkettőből" logika:
    - elsődlegesen HWID-egyezés (`_hwid_matches`: prefix VAGY azonos buszon token-részhalmaz;
      a substring-egyezés rövid HWID-knél - pl. "usbmmidd" - hamis találatot adhat),
    - tartalékként cím<->eszköznév egyezés (az AutoFix módszere - e nélkül a SoftwareComponent
      típusú csomagok, pl. Realtek szolgáltatások, sosem párosulnak, mert nincs a jelenlévő
      eszközökhöz köthető HWID-jük).
    Egy WU-csomag legfeljebb egyszer szerepel (UpdateID szerint deduplikálva), de egy eszközhöz
    több csomag is tartozhat. A párosítatlan (ghost) találatok kimaradnak.
    Visszatérés: [{'uid', 'title', 'device'}] lista."""
    exclude_uids = exclude_uids or set()
    matches = []
    seen_uids = set()
    for wu in wu_results:
        uid = wu.get('UpdateID')
        if not uid or uid in exclude_uids or uid in seen_uids:
            continue
        hwids = wu.get('HardwareID') or []
        if isinstance(hwids, str):
            hwids = [hwids]
        hwids_upper = [str(h).upper() for h in hwids]
        title = wu.get('Title', '') or ''

        matched_dev = None
        for dev in devices:
            dev_hwids_upper = [str(dh).upper() for dh in dev.get('all_hwids', [])]
            dev_pnp_upper = (dev.get('pnp_id') or '').upper()
            for wu_h in hwids_upper:
                if any(_hwid_matches(wu_h, dh) for dh in dev_hwids_upper) or \
                   (dev_pnp_upper and (dev_pnp_upper.startswith(wu_h) or wu_h.startswith(dev_pnp_upper))):
                    matched_dev = dev
                    break
            if matched_dev:
                break

        if matched_dev is None:
            w_title = title.lower()
            for dev in devices:
                n_lower = (dev.get('name') or '').lower()
                if n_lower and n_lower != "ismeretlen eszköz" and len(n_lower) > 3 and \
                   (n_lower in w_title or w_title in n_lower):
                    matched_dev = dev
                    break

        if matched_dev is not None:
            seen_uids.add(uid)
            matches.append({'uid': uid, 'title': title, 'device': matched_dev})
    return matches


_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _iso_date_or_none(s):
    """'yyyy-MM-dd' formátumú dátum-string vagy None. Az ilyen stringek sima
    string-összehasonlítással helyesen rendeződnek, nem kell datetime."""
    s = (s or '').strip()[:10]
    return s if _ISO_DATE_RE.match(s) else None


def _is_inbox_driver(inst):
    """Windows-beépített (inbox) generikus driver-e a MOST telepített driver? A
    _get_installed_driver_info egy elemét kapja ({'version','date','provider','inf'}).

    Két, egymást erősítő jel alapján döntünk:
    - a published INF NEM oemXX.inf: a third-party (DriverStore-ba publikált) csomagok
      mindig oemNN.inf néven futnak, a beépítettek a saját nevükön (hdaudio.inf, pci.inf);
    - a szolgáltató a Microsoft.

    Ha egyik adat sincs meg (régi/hibás lekérdezés), False-t adunk: inkább maradjon a
    korábbi, dátum-alapú viselkedés, mint hogy egy valódi downgrade átcsússzon."""
    inf = (inst.get('inf') or '').strip().lower()
    provider = (inst.get('provider') or '').strip().lower()
    if inf:
        return not re.match(r'^oem\d+\.inf$', inf)
    if provider:
        return provider.startswith('microsoft')
    return False


# ============================================================================
# "GYÁRI DRIVER A GENERIKUS HELYETT" - eszköz-kiválasztás
#
# Kiindulás (terepen mért, 2026-07-24): egy hibátlanul lefutott AutoFix után az
# alaplapi hang a Microsoft generikus `hdaudio.inf`-jén, a LAN pedig az inbox
# `rtcx21x64.inf`-en (provider: Microsoft) futott - miközben a Microsoft Update
# Catalogban ott volt hozzájuk a chipgyártó saját csomagja (Realtek MEDIA 6.0.9992.1
# és Realtek Net 10.79.50.1003). A WU Agent ezeket nem ajánlja fel, mert szerinte a
# jelenlegi driver megfelelő - a katalógusban viszont elérhetők.
#
# Ezt a kiválasztást a MANUÁLIS SZKEN és az AUTOFIX is ezen az egy függvényen
# keresztül végzi (mark_generic_replace_candidates), hogy a két út garantáltan
# ugyanazokat az eszközöket találja meg - a különbség csak az, hogy a manuális
# szken listázza és a szerelő dönt, az AutoFix pedig magától telepíti.
# ============================================================================

# Ahol a chipgyártó drivere jellemzően TÖBBET tud a Windows generikusánál (hang-DSP,
# energiakezelés, offload-funkciók), ÉS a csere visszafordítható (az inbox driver a
# csomag törlése után automatikusan visszaköt).
GENERIC_REPLACE_CLASSES = {
    'MEDIA', 'NET', 'BLUETOOTH', 'SYSTEM', 'IMAGE', 'CAMERA',
    'BIOMETRIC', 'PORTS', 'MODEM', 'SMARTCARDREADER', 'INFRARED',
    # 2026-07 bővítés:
    #  - SDHOST/MEMORY: kártyaolvasók (Realtek RTS5227/5229 gyakorlatilag minden üzleti
    #    laptopban). A gyári driver érdemben többet tud (kártyafelismerés, energiakezelés),
    #    a csere pedig visszafordítható - a rollback-háló fedi.
    #  - SENSOR: laptop-szenzorok (Intel ISH, gyorsulásmérő, fényérzékelő).
    #  - DISPLAY: SZÁNDÉKOS kivétel a "videokártyához a gyártói kártya a jobb forrás"
    #    szabály alól. Ez az ág CSAK akkor fut, ha az eszköz JELENLEG inbox driveren van
    #    (_is_inbox_driver) - vagyis a Microsoft Basic Display Adapteren, gyorsítás nélkül.
    #    Ott bármilyen valódi driver jobb a semminél, és a gyártói kártyák közül az
    #    AMD/Intel csak LINK-OUT (nem telepít), az NVIDIA-é meg csak a manuális szkenben
    #    fut - tehát AutoFix után a gép addig a basic display-en maradt volna.
    'SDHOST', 'MEMORY', 'SENSOR', 'DISPLAY',
    #  - MONITOR (2026-07-27, explicit user decision): a "Generic PnP Monitor" a Windows
    #    beépített monitordrivere; a gyártói monitor-INF EDID-felülbírálást ÉS a gyári
    #    ICC színprofilt hozza. Ez az a csomag, ami a színkezelést ténylegesen gyári
    #    alapra teszi (párja: app/colorprofile_core.py, ami az egyedi kalibrációt szedi
    #    le). Kockázata gyakorlatilag nincs: boot-kritikus nem lehet, és a rollback-háló
    #    (_verify_generic_replacements) itt is fut.
    'MONITOR',
}

# SOHA nem cserélünk generikus drivert ezekben az osztályokban:
#  - tárolóvezérlő (SCSIAdapter/HDC/DiskDrive): a rendszerlemez vezérlőjének
#    drivercseréje INACCESSIBLE_BOOT_DEVICE-t okozhat, és a hiba csak a KÖVETKEZŐ
#    bootnál derül ki, amikor már nincs mód visszaállni - ez az egyetlen eset, amit
#    a "telepítsd, ellenőrizd, szükség esetén állítsd vissza" háló NEM fed le;
#  - Display: a videokártyához az NVIDIA/AMD/Intel kártya adja a gyári legfrissebbet
#    (app/gui/nvidia.py, vendorgpu.py), a katalógus ennél mindig régebbi;
#  - beviteli eszközök / nyomtatósor / kötetek: itt a Microsoft drivere a helyes, gyári
#    csere se nem elérhető, se nem kívánatos.
# A MONITOR 2026-07-27-én ÁTKERÜLT az engedélyezett osztályok közé - lásd ott az indoklást.
GENERIC_REPLACE_BLOCKED_CLASSES = {
    'SCSIADAPTER', 'HDC', 'DISKDRIVE', 'VOLUME', 'VOLUMESNAPSHOT', 'FLOPPYDISK',
    'HIDCLASS', 'KEYBOARD', 'MOUSE', 'PRINTER', 'PRINTQUEUE',
    'USB', 'COMPUTER', 'PROCESSOR', 'FIRMWARE', 'SOFTWAREDEVICE', 'SOFTWARECOMPONENT',
}

# Csak konkrét chipet/eszközt azonosító, gyártó-kódos HWID jöhet szóba
# (PCI\VEN_xxxx&DEV_yyyy, HDAUDIO\FUNC_01&VEN_xxxx, USB\VID_xxxx&PID_yyyy). Ez zárja ki
# az ACPI\PNP0C02-féle általános rendszer-csomópontokat, amikre se katalógus-találat
# nincs, se értelme - egyben drasztikusan csökkenti a felesleges lekérdezések számát.
#
# A MONITOR\ ág külön eset (2026-07-27): a monitorok azonosítója `MONITOR\ACI27FA` alakú,
# vagyis nincs benne VEN_/VID_ tag, mégis PONTOSAN egy gyártót+típust azonosít (a 3 betűs
# PnP-gyártókód + a modellkód), és a katalógusban valódi monitor-csomagok ülnek rá
# (EDID-felülbírálás + gyári ICC színprofil). Enélkül a monitor hiába kerül be az
# eszközlistába, a katalógust sosem kérdeznénk meg rá.
_VENDOR_HWID_RE = re.compile(r'^(?:(?:PCI|HDAUDIO|USB)\\.*(?:VEN_|VID_)|MONITOR\\[A-Z0-9]{5,})',
                             re.IGNORECASE)

# Windows-BUSZ/ENUMERÁTOR INF-ek: ezekhez nem létezik "gyári megfelelő", a Microsoft
# drivere maga a helyes megoldás. Enélkül a szabály elszaladt: a gépen mért 19 jelöltből
# 17 PCI-híd / host bridge / root port volt (pci.inf, machine.inf) - értelmetlen
# katalógus-lekérdezések tucatjai, és a végén egy oda nem való csomag telepítése.
# A hdaudbus.inf szándékosan itt van: az a HD Audio BUSZ enumerátora (a pci.inf párja),
# nem a hangchip drivere - azt a hdaudio.inf adja, és azt cseréljük.
GENERIC_REPLACE_BLOCKED_INFS = {
    'pci.inf', 'machine.inf', 'acpi.inf', 'hal.inf', 'msisadrv.inf', 'isapnp.inf',
    'swenum.inf', 'umbus.inf', 'compositebus.inf', 'vdrvroot.inf', 'msports.inf',
    'hdaudbus.inf', 'ksfilter.inf', 'audioendpoint.inf', 'usbhub3.inf', 'usbxhci.inf',
    'usb.inf', 'kdnic.inf', 'ndisvirtualbus.inf', 'spaceport.inf', 'volmgr.inf',
    'mssmbios.inf', 'wmiacpi.inf', 'uefi.inf', 'c_firmware.inf', 'cpu.inf',
}


_HWID_SUFFIX_RE = re.compile(r'&(SUBSYS|REV|CC)_[0-9A-F]+', re.IGNORECASE)


def base_vendor_hwid(hwid):
    """A HWID gyártó+eszköz törzse, az alrendszer-/revízió-/osztálykód-utótagok nélkül:
        HDAUDIO\\FUNC_01&VEN_10EC&DEV_0892&SUBSYS_18496893&REV_1003
            -> HDAUDIO\\FUNC_01&VEN_10EC&DEV_0892
        PCI\\VEN_10EC&DEV_8168&SUBSYS_81681849&REV_15  ->  PCI\\VEN_10EC&DEV_8168

    Miért kell: az eszköz HWID-listája (Win32_PnPEntity.HardwareID) sokszor CSAK
    alrendszer-kötött azonosítókat tartalmaz - a gépen mérve (Realtek ALC892) pontosan
    kettőt, mindkettő &SUBSYS_18496893-mal. A Microsoft Update Catalogban viszont az
    alrendszeres kulcson egy 2021-es csomag ül (6.0.9136.1), a gyártó FRISS csomagja
    (6.0.9992.1, 2026-05-18) pedig csak az alrendszer nélküli törzsön található meg.
    E nélkül a kiegészítés nélkül tehát pont az 5 évvel újabb drivert nem találnánk meg.
    Üres sztringet ad, ha nincs mit levágni (a HWID már törzsalakú)."""
    if not hwid or '\\' not in hwid:
        return ''
    bus, _sep, rest = hwid.partition('\\')
    stripped = _HWID_SUFFIX_RE.sub('', rest).strip('&')
    if not stripped or stripped.lower() == rest.strip('&').lower():
        return ''
    return f'{bus}\\{stripped}'


def is_generic_replace_candidate(dev, inst):
    """Cserélhető-e ezen az eszközön a Windows generikus drivere gyárira?

    dev: a _filter_wu_scan_devices egy eleme, inst: a _get_installed_driver_info
    hozzá tartozó rekordja ({'version','date','provider','inf'}).

    Feltételek: (1) a jelenlegi driver beépített (inbox) - erre a _is_inbox_driver
    válaszol; (2) az eszközosztály engedélyezett; (3) a HWID konkrét chipet azonosít.
    Hibakódos eszközre False-t adunk: azokat a hívó a saját (régebbi, teljes körű)
    hibás-eszköz ágán kezeli, különben kétszer kerülnének a keresésbe."""
    if not dev or dev.get('err_code'):
        return False
    if not inst or not _is_inbox_driver(inst):
        return False
    pclass = (dev.get('pclass') or '').strip().upper()
    if pclass in GENERIC_REPLACE_BLOCKED_CLASSES or pclass not in GENERIC_REPLACE_CLASSES:
        return False
    if (inst.get('inf') or '').strip().lower() in GENERIC_REPLACE_BLOCKED_INFS:
        return False
    return bool(_VENDOR_HWID_RE.match(dev.get('id') or ''))


# A MÉLY KATALÓGUS-SZKENBŐL kizárt osztályok. SZÁNDÉKOSAN SZŰKEBB, mint a
# GENERIC_REPLACE_BLOCKED_CLASSES, mert más a kockázat: ott a Microsoft generikus driverét
# cserélnénk gyárira (ismeretlen kimenetel), itt egy MEGLÉVŐ gyári drivert frissítenénk a
# saját újabb verziójára (ugyanaz, amit a WU is tenne). Ezért a HID/Display/Ports itt
# engedélyezett - a verzió-kapu miatt csak szigorúan újabb csomag jöhet szóba.
#
# Ami viszont NEM engedhető, mert a hiba VISSZAFORDÍTHATATLAN:
#  - tárolóvezérlő + lemez: egy rossz csere INACCESSIBLE_BOOT_DEVICE-szal jelentkezik a
#    KÖVETKEZŐ bootnál, amikor már semmilyen visszaállításunk nem fut le. Ez a projekt
#    egyik legerősebb szabálya (lásd CLAUDE.md), és a mély szken nem kerülheti meg:
#    élő mérésen a kör be is hozta a "Standard NVM Express Controller"-t és a "Standard
#    SATA AHCI Controller"-t, mielőtt ez a lista elkészült.
# 2026-07-27 (explicit user decision, "ott is csak a tárolóvezérlőnek kéne kint lennie"):
# a lista LESZŰKÜLT a tároló-osztályokra. Ami korábban itt állt - USB, FIRMWARE, COMPUTER,
# PROCESSOR, VOLUME, VOLUMESNAPSHOT, FLOPPYDISK -, azt MÉRÉSSEL ellenőrizve mind kidobja
# már a másik két szűrő is, tehát a felsorolásuk itt semmit nem védett, csak azt a
# látszatot keltette, hogy védjük őket:
#   System Firmware   UEFI\RES_{...}        -> elhasal a gyártói-HWID teszten
#   AMD Ryzen 5 5600  ACPI\AUTHENTICAMD_... -> elhasal a gyártói-HWID teszten
#   Computer          COMPUTER\{GUID}       -> elhasal a gyártói-HWID teszten
#   USB xHCI / hub / composite              -> busz-INF (usbxhci.inf/usbhub3.inf/usb.inf)
# Az EGYETLEN eset, amit az USB-tiltás ténylegesen fogott, egy VALÓDI gyári USB-vezérlő-
# driveren futó vezérlő volt - vagyis pont az, amit frissíteni KELL (ugyanaz, amit a WU is
# tenne). A MONITOR ugyanezen a napon került ki, más okból (lásd a GENERIC_REPLACE_CLASSES
# melletti indoklást: EDID + gyári ICC profil).
#
# A tároló marad az egyetlen valódi határvonal, mert ott a hiba VISSZAFORDÍTHATATLAN
# (INACCESSIBLE_BOOT_DEVICE a következő bootnál) - és azt is a felhasználó oldhatja fel a
# fix indító dialógusán (include_risky, lásd deep_catalog_candidates).
# A TÁROLÓ-osztályok: az egyetlen eszközcsoport, ahol egy rossz driver a KÖVETKEZŐ
# bootnál INACCESSIBLE_BOOT_DEVICE-t okoz, vagyis olyan állapotot, amiből már semmilyen
# visszaállításunk (a visszaállítási pontot is beleértve) nem tud kikapaszkodni - csak
# helyreállító média. Ezért ez a halmaz több helyen is határvonal:
#  - katalógus mély szken: alapból kizárva; `include_risky=True`-val bekérhető, `risky`
#    jelzővel. A MANUÁLIS szken mindig így hívja (pirosan, előre be nem jelölve, ember
#    dönt), az AutoFix pedig akkor, ha a felhasználó a fix indító dialógusán engedte;
#  - AutoFix WU-út: `filter_autofix_risky_devices` - ugyanaz a kapcsoló vezérli (a firmware külön kapcsolón).
# Vagyis a fix indító dialógusának tároló-checkboxa MINDKÉT AutoFix-forrásra hat.
STORAGE_RISK_CLASSES = {'SCSIADAPTER', 'HDC', 'DISKDRIVE'}

# FIRMWARE: a másik, külön kapcsolóval védett csoport (explicit user decision, 2026-07-27).
# Miért nem elég ide a "tároló" kategória: a firmware-frissítés NEM drivert cserél, hanem
# NEM FELEJTŐ MEMÓRIÁT ÍR ÚJRA (UEFI/BIOS, SSD-vezérlő, TPM, dokkoló). Következmények,
# amik semmilyen más művelettel nem hasonlíthatók össze:
#  - VISSZAFORDÍTHATATLAN: nincs az a visszaállítási pont, driver-rollback vagy
#    Windows-újratelepítés, ami egy felírt firmware-t visszahozna;
#  - egy megszakadt írás (áramszünet a flash közben) HARDVERESEN teszi tönkre az eszközt -
#    a gép nem "nem indul el", hanem nincs többé;
#  - a TPM firmware-frissítése bizonyos esetekben ÉRVÉNYTELENÍTI a BitLocker-kulcsokat.
# Ezért a firmware NEM megy fel magától: a felhasználó a fix indító dialógusán, külön
# jelölőnégyzettel engedélyezheti (alapértelmezés KI).
FIRMWARE_RISK_CLASSES = {'FIRMWARE'}

# A `risky` jelölt találatokhoz tartozó, FELÜLETRE kiírandó figyelmeztetések (a manuális
# szken pirosan mutatja őket, előre be nem jelölve).
RISKY_CLASS_WARNING = ('Tárolóvezérlő/lemez: egy rossz driver esetén a Windows a KÖVETKEZŐ '
                       'indításnál nem biztos, hogy elindul (INACCESSIBLE_BOOT_DEVICE). '
                       'A telepítés előtt a program készít visszaállítási pontot, DE ha a gép '
                       'nem bootol, azt már csak helyreállító környezetből (WinRE / telepítő '
                       'USB) lehet visszatölteni - a program onnan nem tud segíteni. '
                       'Csak akkor telepítsd, ha van kéznél helyreállító média!')

FIRMWARE_CLASS_WARNING = ('FIRMWARE-frissítés: ez nem drivert cserél, hanem újraírja az eszköz '
                          'nem felejtő memóriáját (UEFI/BIOS, SSD, TPM). VISSZAFORDÍTHATATLAN - '
                          'sem visszaállítási pont, sem Windows-újratelepítés nem hozza vissza a '
                          'régit. Írás közbeni áramszünet HARDVERESEN teheti tönkre az eszközt, '
                          'TPM-nél pedig érvénytelenítheti a BitLocker-kulcsokat. Csak stabil '
                          'tápellátás mellett és mentett BitLocker-kulccsal telepítsd!')

# WU-csomag firmware-nek minősítése. Elsődlegesen a DriverClass dönt (ez a WUA saját
# kategóriája), a cím-minta pedig azokra a csomagokra való, amiket a szerver más
# kategóriába sorol, de a nevük szerint mégis flashelnek valamit. SZÁNDÉKOSAN
# megengedő: ha egy csomag firmware-nek nevezi magát, akkor firmware - és minden
# kiszűrt csomag NÉV SZERINT a logba kerül, hogy a túlszűrés is látható legyen.
_FIRMWARE_TITLE_RE = re.compile(r'\bfirmware\b', re.IGNORECASE)


def is_firmware_update(wu):
    """Igaz, ha a WU-találat firmware-csomag (nem sima driver)."""
    if (wu.get('DriverClass') or '').strip().upper() == 'FIRMWARE':
        return True
    return bool(_FIRMWARE_TITLE_RE.search((wu.get('Title') or '') + ' ' + (wu.get('DriverModel') or '')))


def filter_firmware_updates(matches, wu_by_uid, allow_firmware):
    """A párosított WU-találatokból kiszűri a firmware-csomagokat, ha nincs engedélyezve.

    Miért kell az ESZKÖZ-szintű szűrés MELLETT is: egy firmware-csomag nem feltétlenül a
    `Firmware` osztályú eszközhöz párosul (egy SSD-firmware a tárolóvezérlőhöz, egy
    dokkoló-firmware egy USB-eszközhöz is köthető), ezért a csomag oldalán is meg kell
    fogni. Visszatérés: (megtartott matches, kiszűrt [{'title','reason'}] lista)."""
    if allow_firmware:
        return list(matches or []), []
    kept, skipped = [], []
    for m in matches or []:
        wu = (wu_by_uid or {}).get(m.get('uid')) or {}
        if is_firmware_update(wu):
            skipped.append({'title': m.get('title', ''),
                            'reason': f"firmware-csomag (osztály: {wu.get('DriverClass') or '?'}) - "
                                      f"a fix indításakor nem volt engedélyezve"})
        else:
            kept.append(m)
    if skipped:
        logging.warning("[AUTOFIX-WU] Firmware-csomagok kihagyva (nem volt engedélyezve): "
                        f"{[s['title'] for s in skipped]}")
    return kept, skipped


# A mély katalógus-szkenből ALAPBÓL kizárt osztályok: a tároló és a firmware. Mindkettőt
# a felhasználó oldhatja fel a fix indító dialógusán (külön-külön). Semmi más nincs itt -
# lásd fent, miért lett volna no-op.
DEEP_CATALOG_BLOCKED_CLASSES = STORAGE_RISK_CLASSES | FIRMWARE_RISK_CLASSES


def filter_autofix_risky_devices(devices, allow_storage=False, allow_firmware=False):
    """Az AutoFix WU-útjáról kiszűri a KOCKÁZATOS osztályokat, amiket a felhasználó nem
    engedélyezett a fix indításakor. Két, egymástól FÜGGETLEN kapcsoló:
      - tároló (STORAGE_RISK_CLASSES): rossz driver -> a gép nem bootol;
      - firmware (FIRMWARE_RISK_CLASSES): visszafordíthatatlan, akár hardveres kár.
    Visszatérés: (megtartott, {'tároló': [...], 'firmware': [...]})."""
    kept = []
    dropped = {'tároló': [], 'firmware': []}
    for d in devices or []:
        pclass = (d.get('pclass') or '').strip().upper()
        if pclass in STORAGE_RISK_CLASSES and not allow_storage:
            dropped['tároló'].append(d)
        elif pclass in FIRMWARE_RISK_CLASSES and not allow_firmware:
            dropped['firmware'].append(d)
        else:
            kept.append(d)
    for label, items in dropped.items():
        if items:
            # Nevesítve: a "miért nem kapott a gépem X drivert?" kérdésre ez a válasz.
            names = ['{0} [{1}]'.format(d.get('name') or '?', d.get('pclass') or '?') for d in items]
            logging.info(f"[AUTOFIX-WU] {len(items)} {label}-eszköz kihagyva a WU-egyeztetésből "
                         f"(a fix indításakor nem volt engedélyezve): {names}")
    if allow_storage or allow_firmware:
        enabled = [n for n, on in (('tároló', allow_storage), ('firmware', allow_firmware)) if on]
        risky_now = [d.get('name') for d in kept
                     if (d.get('pclass') or '').strip().upper() in (STORAGE_RISK_CLASSES | FIRMWARE_RISK_CLASSES)]
        if risky_now:
            logging.warning(f"[AUTOFIX-WU] A felhasználó ENGEDÉLYEZTE ({', '.join(enabled)}): {risky_now}")
    return kept, dropped


def deep_catalog_candidates(devices, installed_info, include_risky=False, include_firmware=False):
    """MÉLY KATALÓGUS-SZKEN eszközhalmaza: azok az eszközök, amikre egyáltalán ÉRTELMES
    megkérdezni a Microsoft Update Catalogot.

    A mély szken célja, hogy egy RÉGI, de hibátlanul működő gyári driver is frissülhessen -
    a WU Agent ilyet nem ajánl fel (szerinte az eszköz rendben van), a szűk katalógus-
    kiegészítés pedig csak a hibakódos és az inbox-driveres eszközöket nézte. Ezért itt
    NINCS osztály-whitelist és nincs inbox-feltétel.

    Három szűrő viszont marad, és mind MÉRÉSEN alapul (élő gép, 2026-07, 99 eszköz):
    - a VISSZAFORDÍTHATATLAN kockázatú osztályok kizárása (DEEP_CATALOG_BLOCKED_CLASSES) -
      lásd ott, ez a legfontosabb a háromból;
    - gyártó-kódos HWID kell (_VENDOR_HWID_RE): egy 'ACPI\\PNP0C02' rendszer-csomópontra a
      katalógus sosem ad találatot, csak hálózati kört és időt visz;
    - busz-enumerátor INF-en futó eszköz kimarad (GENERIC_REPLACE_BLOCKED_INFS): PCI-hidak,
      root portok, ACPI-csomópontok - ezekhez gyári driver nem is létezik.
    A szűrők a 99 eszközt 16-ra vágták, a teljes katalógus-kör 22 mp lett (10 szálon).

    A csomag-szintű döntés (mi számít újabbnak) VÁLTOZATLANUL a _catalog_find_driver
    kiadás-kapuja - ez a függvény csak azt mondja meg, kit érdemes megkérdezni.

    Két KÜLÖN feloldó kapcsoló van, mert két külön kockázat:
      include_risky=True   -> a TÁROLÓ-osztályok (STORAGE_RISK_CLASSES) is bekerülnek,
      include_firmware=True-> a FIRMWARE-osztály (FIRMWARE_RISK_CLASSES) is bekerül.
    Az így bevont eszközök `risky=True` + `risk_reason` jelzőt kapnak. A MANUÁLIS szken
    mindkettőt True-val hívja (a találat pirosan, ELŐRE BE NEM JELÖLVE jelenik meg, és
    ember indítja a telepítést), az AutoFix pedig annyit enged, amennyit a felhasználó a
    fix indító dialógusán bejelölt - alapértelmezésben egyiket sem."""
    out = []
    dropped = {'osztály': [], 'nincs gyártói HWID': [], 'busz-INF': []}
    risky_added = []
    for dev in devices or []:
        name = dev.get('name') or '?'
        pclass = (dev.get('pclass') or '').strip().upper()
        if pclass in DEEP_CATALOG_BLOCKED_CLASSES:
            # Tároló és firmware: KÜLÖN kapcsolóval kérhetők be, de akkor is megjelölve.
            allowed = ((pclass in STORAGE_RISK_CLASSES and include_risky) or
                       (pclass in FIRMWARE_RISK_CLASSES and include_firmware))
            if allowed:
                dev = dict(dev)
                dev['risky'] = True
                dev['risk_reason'] = (FIRMWARE_CLASS_WARNING if pclass in FIRMWARE_RISK_CLASSES
                                      else RISKY_CLASS_WARNING)
                risky_added.append(f"{name} [{dev.get('pclass')}]")
            else:
                dropped['osztály'].append(f"{name} [{dev.get('pclass')}]")
                continue
        if not _VENDOR_HWID_RE.match(dev.get('id') or ''):
            dropped['nincs gyártói HWID'].append(name)
            continue
        inst = (installed_info or {}).get((dev.get('pnp_id') or '').upper()) or {}
        if (inst.get('inf') or '').strip().lower() in GENERIC_REPLACE_BLOCKED_INFS:
            dropped['busz-INF'].append(f"{name} [{inst.get('inf')}]")
            continue
        out.append(dev)
    # A terepi kérdés MINDIG az, hogy "miért nem kapott az X eszköz drivert?" - és az első
    # lépés annak eldöntése, hogy egyáltalán MEGKÉRDEZTÜK-e rá a katalógust. E nélkül a
    # szűrő némán ejtene eszközöket (lásd CLAUDE.md: a listaszűkítő döntéseknek meg kell
    # nevezniük, mit dobtak el). Egy összegző sor + kategóriánként a nevek, DEBUG-on -
    # nem hot loop (szkennenként egyszer fut), de a nevek sokan lehetnek.
    logging.info(f"[CATALOG] Mély szken köre: {len(out)}/{len(devices or [])} eszköz "
                 f"(kizárva: {', '.join(f'{k}={len(v)}' for k, v in dropped.items() if v) or 'semmi'})")
    for reason, names in dropped.items():
        if names:
            logging.debug(f"[CATALOG] Mély szkenből kizárva ({reason}): {names}")
    if risky_added:
        # Destruktív-kockázatú eszköz bevonása: NEVESÍTVE a logba (CLAUDE.md Rule 0) -
        # ha egy gép a szken után nem indul, ez a sor mondja meg, mit ajánlottunk fel rá.
        logging.warning(f"[CATALOG] KOCKÁZATOS (tároló) eszközök bevonva a manuális szkenbe, "
                        f"piros figyelmeztetéssel és előre BE NEM jelölve: {risky_added}")
    return out


def mark_generic_replace_candidates(devices, installed_info):
    """A jelöltekre ráteszi a 'generic_ok' jelzőt, és visszaadja őket listaként.

    A jelző azért kell, mert a katalógus-kereső (_catalog_find_driver) CSAK a
    megjelölt eszközöknél hagyja ki a verzió-összehasonlítást: az inbox driver
    verziószáma a Windows buildje (10.0.26100.8457), tehát számszerűen mindig
    magasabb, mint a gyári csomagé (Realtek 6.0.9992.1) - a verzió itt értelmetlen
    mérce. Máshol (pl. a teljes katalógus-fallbacknél) marad a régi, verzió-alapú
    szűrés, hogy ez a szabály ne szivárogjon ki minden eszközre."""
    out = []
    for dev in devices or []:
        inst = (installed_info or {}).get((dev.get('pnp_id') or '').upper()) or {}
        if is_generic_replace_candidate(dev, inst):
            dev['generic_ok'] = True
            out.append(dev)
    return out


def _filter_wu_downgrades(matches, wu_by_uid, installed_info):
    """DOWNGRADE-VÉDELEM (AutoFix): kiszűri azokat a párosított WU-találatokat, amelyek
    bizonyíthatóan RÉGEBBIEK az eszköz éppen telepített driverénél. Terepi kockázat:
    gyári (pl. NVIDIA) driver telepítése után a WU IsInstalled=0-val felajánl egy
    hónapokkal korábbi csomagot, és az AutoFix gondolkodás nélkül visszabutítaná.

    Szabályok (szándékosan konzervatív, csak BIZONYÍTOTT downgrade esik ki):
    - hibakódos eszközt SOSEM szűrünk - egy driver nélküli/hibás eszköznek egy régebbi
      driver is jobb, mint a semmi;
    - INBOX (Windows-beépített generikus) drivert SOSEM védünk downgrade-ként: az AutoFix
      törli a gyári csomagot, a Windows azonnal ráteszi a saját generikusát, aminek a
      dátuma mindig frissebb (a rendszerrel együtt szállítják), és ettől a gyári driver
      SOHA többé nem tudna visszakerülni. Terepen bizonyított (2026-07, X670 gép): az
      atihdwt6.inf (AMD HDMI hang, 2021-07-13) törlés után véglegesen kiesett, mert a
      helyére került inbox hdaudio.inf 2026-05-16-os dátumot visel, és a védelem mindhárom
      körben kiszűrte a gyári csomagot. Lásd _is_inbox_driver;
    - csak akkor szűrünk, ha a WU DriverVerDate ÉS a telepített driver dátuma is
      értelmezhető, és a WU-é szigorúan korábbi;
    - egyenlő vagy újabb dátum, hiányzó adat -> marad a találat.

    matches: a _match_wu_updates_to_devices kimenete; wu_by_uid: UpdateID -> nyers
    WU-találat dict (DriverVerDate mezővel); installed_info: UPPER(pnp instance id) ->
    {'version','date'} map (GUI: _get_installed_driver_info). Visszatérés:
    (megtartott matches, kiszűrt [{'title','reason'}] lista - a hívó logolja)."""
    kept = []
    skipped = []
    for m in matches:
        dev = m.get('device') or {}
        if dev.get('err_code'):
            kept.append(m)
            continue
        wu = wu_by_uid.get(m.get('uid')) or {}
        wu_date = _iso_date_or_none(wu.get('DriverVerDate'))
        inst = installed_info.get((dev.get('pnp_id') or '').upper()) or {}
        inst_date = _iso_date_or_none(inst.get('date'))
        if _is_inbox_driver(inst):
            kept.append(m)
            continue
        if wu_date and inst_date and wu_date < inst_date:
            skipped.append({'title': m.get('title', ''),
                            'reason': f"WU driver dátuma ({wu_date}) régebbi a telepítettnél ({inst_date})"})
            continue
        kept.append(m)
    return kept, skipped


def _parse_driver_version(text):
    """Verzió-sorozat kinyerése egy katalógus-/WU-címből ("Realtek - Net - 1153.21.1009.2025")
    vagy egy telepített driver-verzióból ("10.50.511.2021"), összehasonlítható int-tuple-ként.
    Csak a legalább 3 tagú szám-sorozat számít verziónak - a "2.5GbE"-féle terméknevekben lévő
    "2.5" különben hamis verzióként viselkedne. Több jelölt esetén a legtöbb tagút választjuk.
    Nincs találat -> None. (Közös mag: a hwscan katalógus-logikája és az AutoFix
    duplikátum-/utóellenőrző szűrői is ezt használják.)"""
    best = None
    for m in re.findall(r'\d+(?:\.\d+){2,}', text or ''):
        parts = tuple(int(p) for p in m.split('.'))
        if best is None or len(parts) > len(best):
            best = parts
    return best


def release_rank(date_str, version_text):
    """Egy driver-kiadás rendezési kulcsa: (ISO dátum, verzió-tuple).

    DÁTUM AZ ELSŐDLEGES, a verzió csak azonos dátumnál dönt (explicit user decision,
    2026-07-27: "kit érdekel milyen verziót adnak neki, ha dátum szerint újabb, az mehet
    fel"). Ugyanaz a gyártó ugyanarra az eszközre egymással ÖSSZEHASONLÍTHATATLAN
    verziósémákat használ, és a szám-alapú összevetés ilyenkor bizonyítottan a rossz
    csomagot választja:
      - AMD SMBus (terep, 2026-07-27): telepítve 5.12.0.38 / 2017-08-30, a katalógusban
        'System Driver Update (2.0.0.26)' / 2025-12-03. Verzió szerint az 5.12 "nyer",
        így a gép egy 2017-es driveren maradt egy 2025-ös helyett;
      - Realtek NIC: 'Realtek - Net - 1168.19.704.2024' (2024-07-03) vs
        'Realtek Net Driver Update (10.79.50.1003)' (2025-10-02) - verzió szerint az
        előbbi három nagyságrenddel "nagyobb", holott több mint egy évvel régebbi.
    A katalógus SOR-választása (`_catalog_find_driver`) már régóta így dönt; ez a
    függvény ugyanezt a szabályt teszi elérhetővé a többi döntési pontnak is, hogy a
    projektben egyetlen "melyik az újabb kiadás?" definíció legyen."""
    return (_iso_date_or_none(date_str) or '', _parse_driver_version(version_text) or ())


def is_newer_release(cand_date, cand_version, cur_date, cur_version):
    """Újabb-e a jelölt kiadás a jelenleginél? DÁTUM dönt, verzió csak holtversenynél.

    Visszatérés:
      True  - a jelölt bizonyítottan újabb (telepíthető),
      False - a jelölt bizonyítottan NEM újabb (kihagyandó),
      None  - nem eldönthető (nincs használható dátum SEM verzió mindkét oldalon).
              A hívók a None-t SOSEM kezelik kizárásként: inkább felajánlunk egy
              esetleg fölösleges csomagot, mint hogy némán kihagyjunk egy szükségeset
              (a pnputil úgyis ellenőrzi az alkalmazhatóságot, a telepítés utáni
              kötés-ellenőrzés pedig elkapja, ha az eszköz mégsem vette át).

    Ha csak az egyik oldalon van dátum, a dátumokat nem lehet összevetni - ilyenkor
    esünk vissza a verzió-összehasonlításra (ez a régi viselkedés)."""
    cd, cur_d = _iso_date_or_none(cand_date), _iso_date_or_none(cur_date)
    cv, cur_v = _parse_driver_version(cand_version), _parse_driver_version(cur_version)
    if cd and cur_d:
        if cd != cur_d:
            return cd > cur_d
        # Azonos dátum: a verzió a holtverseny-döntő (pl. ugyanaznap kiadott javítás).
        if cv is not None and cur_v is not None:
            return cv > cur_v
        return False
    if cv is not None and cur_v is not None:
        return cv > cur_v
    return None


# ============================================================================
# PENDING-REBOOT FELISMERÉS (AutoFix: mikor értelmetlen tovább telepíteni)
# ============================================================================

# A négy klasszikus "újraindítás függőben" jelző. Bármelyik elég.
PENDING_REBOOT_PS = r"""
$p = $false
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $p = $true }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $p = $true }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootInProgress') { $p = $true }
try {
    $v = Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction Stop
    if ($v.PendingFileRenameOperations) { $p = $true }
} catch {}
if ($p) { Write-Output 'PENDING' } else { Write-Output 'CLEAN' }
"""


def is_reboot_pending(run):
    """Igaz, ha a rendszer "újraindítás függőben" állapotban van.

    MIÉRT KELL: terepen bizonyított (Build 214 és 218, Dell OptiPlex 7060, kétszer
    egyformán) - amint egy tárolóvezérlő-driver (Intel RST, iaAHCIC/iastorhsa) települ,
    a gép pending-reboot állapotba kerül, és onnantól a WUA a session ÖSSZES további
    telepítésére orcFailed(4)-et ad, DARABONKÉNT ~143 MP VÁRAKOZÁS UTÁN. A 8 maradék
    csomag így ~20 percet evett meg feleslegesen, ráadásul a driverek a DriverStore-ba
    valójában felkerültek (a "hiba" hamis negatív volt). Ilyenkor az egyetlen értelmes
    lépés: kör vége, reboot, és a maradék a következő lábon települ tisztán.

    Hiba esetén False (óvatos alapértelmezés: inkább menjen tovább, mint hogy egy
    registry-olvasási hiba miatt fölöslegesen újraindítsuk a gépet)."""
    try:
        res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PENDING_REBOOT_PS],
                  timeout=60)
        return 'PENDING' in (res.stdout or '')
    except Exception as e:
        logging.warning(f"[WU] Pending-reboot ellenőrzés sikertelen (folytatjuk): {e}")
        return False


def _install_abort_reason(consecutive_failures, reboot_pending):
    """A telepítő kör megszakításának oka (vagy None) - az _iter_process_lines
    abort_check callbackjének közös döntési logikája, mindkét AutoFix ág ezt hívja.

    - 'reboot': pending-reboot állapot; a maradék csomag ebben a session-ben úgysem
      tud rendesen települni (lásd is_reboot_pending).
    - 'failstreak': WU_MAX_CONSECUTIVE_FAILURES egymást követő telepítési hiba. Ez a
      védőháló arra az esetre, ha a session más okból mérgeződik meg, és a
      pending-reboot jelzők mégsem állnak - darabonként 2,5 perc a tovább-őrlés ára."""
    if reboot_pending:
        return 'reboot'
    if consecutive_failures >= WU_MAX_CONSECUTIVE_FAILURES:
        return 'failstreak'
    return None


def _filter_wu_older_duplicates(matches, wu_by_uid):
    """UGYANANNAK AZ ILLESZTŐPROGRAM-CSALÁDNAK csak a LEGÚJABB verzióját tartja meg.

    A WU ugyanarra az eszközre a csomag teljes történetét felajánlja: terepen egyetlen
    Intel UHD 630-ra 10 db iigd_ext Extension csomag jött 2018-tól (24.20.100.6287,
    26.20.100.6952, 26.20.100.7262 kétszer, 27.20.100.8190, ...), és az AutoFix mindet
    feltelepítette egymás után. Feleslegesen: csak a legújabb marad érvényben, a többi
    holt súlyként ül a DriverStore-ban (és a következő futás mindet törli-telepíti újra).

    Csoportosítás: (HardwareID, DriverClass, DriverProvider, DriverModel) - ez azonosít
    egy csomag-családot. A kulcs SZÁNDÉKOSAN szűk (a DriverModel is benne van): ha két
    valóban különböző csomagot vonnánk össze, az egyik SOHA nem települne fel - egy
    kimaradó dedup viszont csak annyit jelent, hogy a régi viselkedés marad. A győztes a
    legnagyobb verzió (a címből parse-olva); verzió híján a DriverVerDate dönt; ha egyik
    sincs, az első találat marad. Visszatérés:
    (megtartott matches, kiszűrt [{'title','reason'}] lista - a hívó logolja)."""
    groups = {}
    order = []
    for m in matches:
        wu = wu_by_uid.get(m.get('uid')) or {}
        key = ((wu.get('HardwareID') or '').lower(),
               (wu.get('DriverClass') or '').lower(),
               (wu.get('DriverProvider') or '').lower(),
               (wu.get('DriverModel') or '').lower())
        if not any(key):
            # Nincs mire csoportosítani - a találat érintetlenül marad.
            key = ('__egyedi__', m.get('uid'), '', '')
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((m, wu))

    kept = []
    skipped = []
    for key in order:
        items = groups[key]
        if len(items) == 1:
            kept.append(items[0][0])
            continue

        def _rank(item):
            # DÁTUM az elsődleges, a verzió csak azonos dátumnál dönt (közös szabály:
            # release_rank). Korábban fordítva volt, és egy gyártói verziósémaváltás
            # ilyenkor a RÉGEBBI csomagot tartotta meg a családból - pont az a hiba,
            # ami miatt a katalógus sor-választása is dátum-elsődlegű.
            m, wu = item
            return release_rank(wu.get('DriverVerDate'),
                                m.get('title', '') or wu.get('Title', ''))

        best = max(items, key=_rank)
        for m, _wu in items:
            if m is best[0]:
                kept.append(m)
            else:
                skipped.append({'title': m.get('title', ''),
                                'reason': f"ugyanazon driver újabb kiadása is elérhető: {best[0].get('title', '')}"})
    return kept, skipped


def unoffered_requested_titles(requested_titles, found_titles):
    """Azok a KÉRT csomagok, amelyeket a telepítő script már nem talált meg.

    MIÉRT KELL: a script a saját WUA-keresésének MINDEN találatán végigmegy, és amelyik
    nem szerepel a mi szűrőnkben, arra `SKIP:` sort ír - vagyis a SKIP-ek TÖBBSÉGE teljesen
    normális, számolni értelmetlen. A valódi jel az, ha egy általunk KÉRT címre nem érkezik
    `FOUND:` sor: az a csomag nem került a telepítési listába, és eddig NÉMÁN eltűnt.
    Terepen (Build 224, 2. kör): 3 csomagot választottunk ki, a script TOTAL-ja 2 lett, a
    harmadikról egy szó sem esett - jellemzően azért, mert az előző kör telepítése után a
    szerver már telepítettként látja (IsInstalled), tehát nem hiba, de meg kell mondani.

    Visszatérés: a hiányzó címek rendezett listája."""
    found = set(found_titles or ())
    return sorted(t for t in (requested_titles or ()) if t not in found)


def verify_failed_installs(failed_titles, pkgs_before, pkgs_after):
    """A "sikertelen" telepítések UTÓELLENŐRZÉSE a DriverStore alapján.

    MIÉRT: a WUA orcFailed(4)-et ad vissza olyan csomagokra is, amelyeket a PnP közben
    rendben letett a DriverStore-ba (terepen bizonyított: a 8 "bukott" driver mindegyike
    - iastorhsa_ext 17.11.3.1010, e1d 12.19.2.57, heci 2433.6.3.0, Dell firmware
    0.1.32.0, unifying_receiver 2.0.998.0 stb. - ott volt a következő DISM listában).
    Ezek hamis negatívok: nem szabad se hibaként jelenteni, se a további körökből
    véglegesen kizárni őket.

    Módszer: a kör ELŐTTI és UTÁNI third-party csomaglista különbsége adja az újonnan
    felkerült csomagokat; ha egy bukott cím verziója (a cím tartalmazza, pl.
    "Intel - Net - 12.19.2.57") szerepel az újak verziói közt, a telepítés valójában
    sikerült. Visszatérés: azon címek halmaza, amelyek igazoltan felkerültek."""
    before_pub = {(p.get('published') or '').lower() for p in (pkgs_before or [])}
    new_versions = set()
    for p in (pkgs_after or []):
        if (p.get('published') or '').lower() in before_pub:
            continue
        ver = _parse_driver_version(p.get('version'))
        if ver:
            new_versions.add(ver)
    verified = set()
    if not new_versions:
        return verified
    for title in failed_titles:
        ver = _parse_driver_version(title)
        if ver and ver in new_versions:
            verified.add(title)
    return verified


def _build_wu_install_ps(target_uids=(), target_hwids=(), match_system_devices=False):
    """A WUA (Microsoft.Update.Session) telepítő PowerShell script EGYETLEN forrása.
    Szűrési módok (vagylagosak egy csomagra, de kombinálhatók egy híváson belül):
    - target_uids: pontos UpdateID egyezés (manuális telepítés + GUI AutoFix),
    - target_hwids: HWID prefix-egyezés, tartalék UpdateID nélküli pool-elemekhez,
    - match_system_devices: a gép ÖSSZES jelenlévő eszközéhez párosítás a scripten belül
      (CLI AutoFix - ott nincs Python-oldali előszűrés).
    Ha egyik szűrő sincs megadva, SEMMIT nem telepít (EMPTY) - nincs "mindent telepít" mód!
    A letöltés SZINKRON $DL.Download() - SOHA ne cseréld BeginDownload($null,...)-ra, az
    null callbackekkel azonnal NullReferenceException-nel elhal (Build ~192 regresszió).
    Kimeneti protokoll (a hívók ezt parse-olják): INIT/SEARCH/FOUND/SKIP/TOTAL/DLONE/
    INSTONE/OK/OKRB/FAIL/EMPTY/DONE/ERROR prefixű sorok. Az OKRB ugyanaz mint az OK,
    de a WUA jelezte, hogy a driver csak ÚJRAINDÍTÁS után él ($IR.RebootRequired) -
    a sikeres számlálóba beleszámít, a hívó dönt a reboot-jelzés megjelenítéséről."""
    uid_list_ps = ','.join(f"'{_ps_quote(u)}'" for u in target_uids)
    hwid_list_ps = ','.join(f"'{_ps_quote(str(h).upper())}'" for h in target_hwids)
    match_sys_ps = '$true' if match_system_devices else '$false'
    return ('$TargetUIDs = @(' + uid_list_ps + ')\n'
            '$TargetHWIDs = @(' + hwid_list_ps + ')\n'
            '$MatchSystemDevices = ' + match_sys_ps + '\n') + r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    Write-Output "INIT: Windows Update Session létrehozása..."
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try { $SM = New-Object -ComObject Microsoft.Update.ServiceManager; $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null } catch {}
    $Searcher.ServerSelection = 3
    $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    Write-Output "SEARCH: Driver frissítések keresése..."
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    if ($Result.Updates.Count -eq 0) { Write-Output "EMPTY: Nem található elérhető driver frissítés."; return }

    $systemHWIDs = @()
    if ($MatchSystemDevices) {
        $pnpDevs = Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true -and $_.ConfigManagerErrorCode -ne 45 }
        foreach ($dev in $pnpDevs) {
            if ($dev.HardwareID) {
                foreach ($hid in $dev.HardwareID) { $systemHWIDs += "$hid".ToUpper() }
            }
            if ($dev.PNPDeviceID) { $systemHWIDs += "$($dev.PNPDeviceID)".ToUpper() }
        }
    }

    $ToInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($U in $Result.Updates) {
        $matchFound = $false
        if ($TargetUIDs.Count -gt 0 -and $TargetUIDs -contains $U.Identity.UpdateID) { $matchFound = $true }
        if (-not $matchFound -and $TargetHWIDs.Count -gt 0) {
            foreach ($hwid in $U.DriverHardwareID) {
                if (-not $hwid) { continue }
                $hUpper = "$hwid".ToUpper()
                foreach ($tgt in $TargetHWIDs) {
                    if ($tgt.StartsWith($hUpper) -or $hUpper.StartsWith($tgt)) {
                        $matchFound = $true; break
                    }
                }
                if ($matchFound) { break }
            }
        }
        if (-not $matchFound -and $MatchSystemDevices) {
            foreach ($hwid in $U.DriverHardwareID) {
                if (-not $hwid) { continue }
                $hUpper = "$hwid".ToUpper()
                foreach ($sys_hid in $systemHWIDs) {
                    if ($sys_hid.StartsWith($hUpper) -or $hUpper.StartsWith($sys_hid)) {
                        $matchFound = $true; break
                    }
                }
                if ($matchFound) { break }
            }
        }
        if (-not $matchFound) { Write-Output "SKIP: $($U.Title)"; continue }
        if (-not $U.EulaAccepted) { $U.AcceptEula() }
        $ToInstall.Add($U) | Out-Null
        Write-Output "FOUND: $($U.Title)"
    }
    if ($ToInstall.Count -eq 0) { Write-Output "EMPTY: Nem található egyező driver. (Lehet, hogy időközben települt vagy lekerült a szerverről - futtass új szkennelést!)"; return }
    $total = $ToInstall.Count; Write-Output "TOTAL: $total"
    $s = 0; $f = 0
    for ($i = 0; $i -lt $total; $i++) {
        $U = $ToInstall.Item($i); $t = $U.Title; $idx = $i + 1
        Write-Output "DLONE: $idx/$total $t"
        $SC = New-Object -ComObject Microsoft.Update.UpdateColl; $SC.Add($U) | Out-Null
        $DL = $Session.CreateUpdateDownloader(); $DL.Updates = $SC
        try { $DR = $DL.Download() } catch { Write-Output "FAIL: [LETÖLTÉS HIBA] $t - $($_.Exception.Message)"; $f++; continue }
        if (-not $DR -or ($DR.ResultCode -ne 2 -and $DR.ResultCode -ne 3)) { Write-Output "FAIL: [LETÖLTÉS HIBA kód=$($DR.ResultCode)] $t"; $f++; continue }
        Write-Output "INSTONE: $idx/$total $t"
        $Inst = $Session.CreateUpdateInstaller(); $Inst.Updates = $SC
        try { $IR = $Inst.Install() } catch { Write-Output "FAIL: [TELEPÍTÉS HIBA] $t"; $f++; continue }
        $rc = $IR.GetUpdateResult(0).ResultCode
        $rb = $false; try { $rb = [bool]$IR.RebootRequired } catch {}
        if ($rc -eq 2 -or $rc -eq 3) {
            if ($rb) { Write-Output "OKRB: $t" } else { Write-Output "OK: $t" }
            $s++
        } else { Write-Output "FAIL: [kód=$rc] $t"; $f++ }
    }
    Write-Output "DONE: Sikeres=$s, Sikertelen=$f"
} catch { Write-Output "ERROR: $($_.Exception.Message)" }
"""


# ============================================================================
# NYOMTATÓ-VÉDELEM 2.0 - KÖZÖS MAG (GUI AutoFix + CLI AutoFix)
# Terepi igény: az ügyfélgépeken a nyomtatónak a driver-fix UTÁN is működnie
# kell. A puszta osztály-alapú kihagyás (Printer/PrintQueue/Image) NEM elég:
# egy multifunkciós HP/Canon csomag segéd-driverei USB/Ports/SYSTEM osztályba
# esnek (pl. mvusbews.inf, hppscnd.inf, hpbuio70l.inf - valós gépről), amiket a
# régi szűrő törölt, és a WU nem feltétlenül rakja vissza a gyári csomagot.
# ============================================================================

# Nyomtató-gyártó kulcsszavak: ha egy jelenlévő nyomtatási/szkennelési komponens
# szolgáltatója (provider) ezek egyikére illik, akkor a gépen lévő ÖSSZES ilyen
# szolgáltatójú third-party csomag védetté válik. Szándékosan túl-védő: pl. HP
# laptopon HP nyomtatóval a HP rendszer-driverek is megmaradnak - ezeket a WU
# úgyis visszarakná, a nyomtató működése viszont pótolhatatlan.
PRINTER_VENDOR_KEYWORDS = [
    'hewlett', 'hp inc', 'canon', 'epson', 'seiko', 'brother', 'samsung',
    'lexmark', 'kyocera', 'ricoh', 'xerox', 'oki ', 'okidata', 'zebra',
    'pantum', 'konica', 'minolta', 'dymo', 'star micronics', 'citizen',
    'bixolon', 'godex', 'tsc ', 'sagem', 'olivetti', 'toshiba tec', 'sharp',
]

_PRINTER_PROTECT_PS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$out = @{ Infs = @(); Providers = @() }
try {
    Get-PrinterDriver -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.InfPath) { $out.Infs += [System.IO.Path]::GetFileName("$($_.InfPath)") }
        if ($_.Manufacturer) { $out.Providers += "$($_.Manufacturer)" }
    }
} catch {}
try {
    $devs = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.Class -in @('Printer','PrintQueue','Image') }
    foreach ($d in $devs) {
        try {
            $inf = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverInfPath' -ErrorAction SilentlyContinue).Data
            if ($inf) { $out.Infs += "$inf" }
            $prov = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverProvider' -ErrorAction SilentlyContinue).Data
            if ($prov) { $out.Providers += "$prov" }
        } catch {}
    }
} catch {}
$out | ConvertTo-Json -Compress
"""


def _collect_printer_protection(run_fn):
    """Összegyűjti, hogy a gépen JELENLÉVŐ nyomtatási/szkennelési komponensek ténylegesen
    melyik driver-csomagokat használják. Visszatérés: (védett INF-nevek halmaza kisbetűvel,
    pl. {'oem113.inf'}, érintett nyomtató-gyártó kulcsszavak halmaza). Forrás: minden felvett
    nyomtató drivere (Get-PrinterDriver InfPath) + minden jelenlévő Printer/PrintQueue/Image
    eszköz aktív INF-je és szolgáltatója. Hiba esetén üres halmazok - olyankor csak a
    hagyományos osztály-alapú védelem él."""
    protected_infs = set()
    printing_vendors = set()
    try:
        res = run_fn(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _PRINTER_PROTECT_PS],
                     encoding='utf-8', timeout=120)
        data = json.loads(res.stdout) if res and (res.stdout or '').strip() else {}
        infs = data.get('Infs') or []
        provs = data.get('Providers') or []
        if isinstance(infs, str):
            infs = [infs]
        if isinstance(provs, str):
            provs = [provs]
        for inf in infs:
            base = os.path.basename(str(inf)).strip().lower()
            if base.endswith('.inf'):
                protected_infs.add(base)
        for p in provs:
            pl = str(p).lower()
            for kw in PRINTER_VENDOR_KEYWORDS:
                if kw in pl:
                    printing_vendors.add(kw)
        logging.info(f"[PRINTER-PROTECT] Védett INF-ek: {sorted(protected_infs)}, nyomtató-gyártók: {sorted(printing_vendors)}")
    except Exception as e:
        logging.warning(f"[PRINTER-PROTECT] Védett lista gyűjtése sikertelen (marad az osztály-alapú védelem): {e}")
    return protected_infs, printing_vendors


def _is_printer_protected(drv, protected_infs, printing_vendors, skip_classes):
    """Egy dism-listás third-party driver-bejegyzésről eldönti, hogy nyomtató-védelem alá
    esik-e: (1) osztály szerint (a régi viselkedés), (2) a jelenlévő nyomtatási komponensek
    által TÉNYLEGESEN használt INF-ek szerint, (3) a gépen nyomtatóval jelen lévő gyártó
    minden csomagja szerint. Az INF-egyeztetés a publikált (oemXX.inf) ÉS az eredeti
    (pl. hpc1320u.inf) névvel is fut: a Get-PrinterDriver InfPath-ja az EREDETI nevet
    adja, a PnP-eszközök DriverInfPath-ja viszont a publikáltat - élesben mindkét forma
    előfordul a védett halmazban."""
    if drv.get('class', '') in (skip_classes or set()):
        return True
    if (drv.get('published', '') or '').lower() in protected_infs:
        return True
    if (drv.get('original', '') or '').lower() in protected_infs:
        return True
    prov = (drv.get('provider', '') or '').lower()
    return any(kw in prov for kw in printing_vendors)


# ============================================================================
# BOOT-PATH (RENDSZERLEMEZ-ÚTVONAL) VÉDELEM - KÖZÖS MAG (GUI + CLI AutoFix)
#
# MIÉRT: az AutoFix törlési fázisának SZÁNDÉKOSAN nincs osztályszűrése (a "mindent
# letörlünk, a WU tegyen fel tisztát" a termék lényege, explicit user decision), és a
# pnputil hívás /force-szal megy - vagyis a HASZNÁLATBAN LÉVŐ tárolóvezérlő-drivert is
# leszedi. Amíg a rendszerlemez sima AHCI/NVMe módban van, ez ártalmatlan: a Microsoft
# inbox storahci/stornvme átveszi. DE ha a lemez Intel VMD vagy RST RAID vezérlő mögött
# ül (11. gen+ Intel gépeken gyári alapbeállítás, és sok üzleti desktopon is), akkor az
# inbox verem nem feltétlenül fedi le a vezérlőt -> a KÖVETKEZŐ bootnál
# INACCESSIBLE_BOOT_DEVICE, és onnantól SEMMILYEN visszaállításunk nem fut le. A
# visszaállítási pont sem ér semmit, mert nem lehet elindulni hozzá.
#
# A megoldás NEM osztály-alapú tiltás (az túl széles lenne, és pont a fix lényegét venné
# el), hanem célzott: megkeressük, milyen eszközlánc hordozza a RENDSZERLEMEZT, és ha
# ezen a láncon third-party (oemNN.inf) driver van, AZT AZ EGY-KÉT CSOMAGOT védjük -
# pontosan úgy, ahogy a nyomtatóknál már bevált.
#
# FAIL-SAFE: ha a lánc felderítése bármiért nem sikerül, NEM a régi (védtelen)
# viselkedésre esünk vissza, hanem a tárolóvezérlő-osztályokat védjük le összesen. Egy
# feleslegesen bent maradt tárolódriver a legrosszabb esetben elavult marad; egy tévesen
# letörölt boot-driver viszont nem bootoló gép.
# ============================================================================

# A tárolóvezérlő-osztályok - CSAK a fail-safe ágon (ha a boot-lánc felderítése elbukott).
BOOT_FALLBACK_PROTECT_CLASSES = {'scsiadapter', 'hdc', 'diskdrive'}

_BOOT_PATH_PS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$out = @{ Infs = @(); Chain = @(); Found = $false }
$diskIds = @()
$sysDrive = "$env:SystemDrive"
# 1) Elsődleges út: WMI asszociációk C: -> partíció -> fizikai lemez PNPDeviceID.
try {
    $l2p = @(Get-CimInstance Win32_LogicalDiskToPartition -ErrorAction SilentlyContinue)
    $d2p = @(Get-CimInstance Win32_DiskDriveToDiskPartition -ErrorAction SilentlyContinue)
    $disks = @(Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue)
    $partIds = @($l2p | Where-Object { $_.Dependent.DeviceID -eq $sysDrive } | ForEach-Object { $_.Antecedent.DeviceID })
    foreach ($rel in $d2p) {
        if ($partIds -contains $rel.Dependent.DeviceID) {
            foreach ($dk in $disks) {
                if ($dk.DeviceID -eq $rel.Antecedent.DeviceID -and $dk.PNPDeviceID) { $diskIds += "$($dk.PNPDeviceID)" }
            }
        }
    }
} catch {}
# 2) Tartalék út: Storage modul (Get-Partition/Get-Disk). A Path alakja
#    \\?\scsi#disk&ven...#4&abc&0&000000#{guid} -> ebből instance ID-t képezünk.
if ($diskIds.Count -eq 0) {
    try {
        $p = Get-Partition -DriveLetter ($sysDrive.TrimEnd(':')) -ErrorAction Stop
        $d = Get-Disk -Number $p.DiskNumber -ErrorAction Stop
        if ($d.Path) {
            $raw = "$($d.Path)" -replace '^\\\\\?\\', ''
            $raw = $raw -replace '#\{[0-9a-fA-F\-]+\}$', ''
            $diskIds += ($raw -replace '#', '\')
        }
    } catch {}
}
$seen = @{}
foreach ($id in ($diskIds | Select-Object -Unique)) {
    $cur = "$id"
    for ($i = 0; $i -lt 12 -and $cur; $i++) {
        if ($seen.ContainsKey($cur)) { break }
        $seen[$cur] = $true
        $dev = $null
        try { $dev = Get-PnpDevice -InstanceId $cur -ErrorAction Stop } catch {}
        $inf = ''; $svc = ''
        try { $inf = "$((Get-PnpDeviceProperty -InstanceId $cur -KeyName 'DEVPKEY_Device_DriverInfPath' -ErrorAction SilentlyContinue).Data)" } catch {}
        try { $svc = "$((Get-PnpDeviceProperty -InstanceId $cur -KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue).Data)" } catch {}
        $out.Found = $true
        $out.Chain += [PSCustomObject]@{
            Id = $cur
            Name = $(if ($dev) { "$($dev.FriendlyName)" } else { '' })
            Class = $(if ($dev) { "$($dev.Class)" } else { '' })
            Inf = $inf
            Service = $svc
        }
        if ($inf) { $out.Infs += $inf }
        $parent = ''
        try { $parent = "$((Get-PnpDeviceProperty -InstanceId $cur -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue).Data)" } catch {}
        if (-not $parent -or $parent -eq $cur -or $parent -like 'HTREE*') { break }
        $cur = $parent
    }
}
$out | ConvertTo-Json -Compress -Depth 4
"""


def _collect_boot_path_protection(run_fn):
    """Felderíti, milyen eszközlánc hordozza a RENDSZERLEMEZT, és mely driver-csomagok
    élnek ezen a láncon.

    Visszatérés: (protected_infs, chain, detected)
      - protected_infs: a láncon lévő INF-nevek halmaza kisbetűvel (pl. {'oem7.inf'});
      - chain: a lánc eszközei (név/osztály/INF/szolgáltatás) - CSAK naplózásra, de a
        terepi kérdésre ("miért maradt bent ez a driver?") ez az egyetlen válasz;
      - detected: sikerült-e egyáltalán felderíteni a láncot. HAMIS esetén a hívónak a
        fail-safe ágra kell mennie (BOOT_FALLBACK_PROTECT_CLASSES) - lásd a fenti blokk
        magyarázatát: egy nem bootoló gép visszafordíthatatlan, egy bent maradt elavult
        tárolódriver nem."""
    protected_infs = set()
    chain = []
    detected = False
    try:
        res = run_fn(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _BOOT_PATH_PS],
                     encoding='utf-8', timeout=180)
        data = json.loads(res.stdout) if res and (res.stdout or '').strip() else {}
        detected = bool(data.get('Found'))
        infs = data.get('Infs') or []
        if isinstance(infs, str):
            infs = [infs]
        for inf in infs:
            base = os.path.basename(str(inf)).strip().lower()
            if base.endswith('.inf'):
                protected_infs.add(base)
        raw_chain = data.get('Chain') or []
        if isinstance(raw_chain, dict):
            raw_chain = [raw_chain]
        chain = raw_chain
    except Exception as e:
        logging.warning(f"[BOOT-PROTECT] A rendszerlemez eszközláncának felderítése sikertelen: {e}")
        detected = False
    if detected:
        for c in chain:
            logging.info(f"[BOOT-PROTECT] Boot-lánc: {c.get('Name') or '?'} [{c.get('Class') or '?'}] "
                         f"INF={c.get('Inf') or '-'} szolgáltatás={c.get('Service') or '-'}")
        logging.info(f"[BOOT-PROTECT] A rendszerlemez útvonalán lévő védett INF-ek: {sorted(protected_infs)}")
    else:
        logging.warning("[BOOT-PROTECT] A rendszerlemez eszközlánca NEM azonosítható - "
                        "fail-safe: a tárolóvezérlő-osztályok csomagjait védjük.")
    return protected_infs, chain, detected


def _is_boot_path_protected(drv, protected_infs, detected):
    """Egy third-party driver-bejegyzésről eldönti, hogy a rendszerlemez útvonalához
    tartozik-e, tehát tilos törölni.

    Sikeres felderítés esetén az INF-név dönt (a publikált oemNN.inf ÉS az eredeti név
    is - ugyanaz a kettősség, mint a nyomtatóvédelemnél). Sikertelen felderítés esetén
    az osztály dönt (fail-safe)."""
    if not detected:
        return (drv.get('class', '') or '').strip().lower() in BOOT_FALLBACK_PROTECT_CLASSES
    if (drv.get('published', '') or '').lower() in protected_infs:
        return True
    return (drv.get('original', '') or '').lower() in protected_infs


# ============================================================================
# HÁLÓZATI DRIVER MENTŐÖV - KÖZÖS MAG (GUI AutoFix + CLI AutoFix)
# Terepen látott kockázat: az AutoFix a LAN/Wi-Fi drivert is törli, és ha sem a
# beépített, sem a WU-s driver nem fedi le az adott kártyát (valós eset: friss
# AM5-ös gép Realtek 2.5GbE-vel), a gép internet nélkül ragad - miközben a lánc
# folytatása pont internetből dolgozna. Ezért törlés ELŐTT a Net-osztályú
# drivereket pnputil /export-driver-rel elmentjük, és ha a folytatásnál nincs
# net, visszatöltjük őket.
# ============================================================================

def _net_backup_dir():
    return os.path.join(_app_data_dir(), 'netdrv_backup')


def _export_net_driver_backup(run_fn, drivers):
    """A törlésre váró listából a Net-osztályú driver-csomagokat exportálja a
    _net_backup_dir()-be (előtte üríti, hogy ne keveredjen régi mentéssel).
    Visszaadja a sikeresen exportált csomagok számát."""
    net_drivers = [d for d in drivers if (d.get('class', '') or '').lower() == 'net' and d.get('published')]
    if not net_drivers:
        return 0
    dest = _net_backup_dir()
    try:
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
    except Exception as e:
        logging.warning(f"[NET-BACKUP] Mentési mappa előkészítése sikertelen: {e}")
        return 0
    exported = 0
    for d in net_drivers:
        res = run_fn(['pnputil', '/export-driver', d['published'], dest], timeout=300)
        if res and res.returncode == 0:
            exported += 1
        else:
            logging.warning(f"[NET-BACKUP] Export sikertelen: {d.get('published')} ({d.get('original')})")
    logging.info(f"[NET-BACKUP] {exported}/{len(net_drivers)} hálózati driver elmentve ide: {dest}")
    return exported


def _restore_net_driver_backup(run_fn):
    """A korábban elmentett Net-driverek visszatelepítése (pnputil /add-driver /install).
    Visszaadja, hogy volt-e egyáltalán mit visszatölteni (bool)."""
    src = _net_backup_dir()
    if not os.path.isdir(src):
        logging.info("[NET-BACKUP] Nincs mentett hálózati driver, visszaállítás kihagyva.")
        return False
    has_inf = False
    for _root, _dirs, files in os.walk(src):
        if any(f.lower().endswith('.inf') for f in files):
            has_inf = True
            break
    if not has_inf:
        logging.info("[NET-BACKUP] A mentési mappa üres, visszaállítás kihagyva.")
        return False
    res = run_fn(['pnputil', '/add-driver', os.path.join(src, '*.inf'), '/subdirs', '/install'], timeout=600)
    ok = bool(res) and ('successfully' in (res.stdout or '').lower() or res.returncode in (0, 259, 3010))
    logging.info(f"[NET-BACKUP] Visszaállítás {'sikeres' if ok else 'részben/nem sikerült'} innen: {src}")
    return True
