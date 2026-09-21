"""DriverVarázsló GUI - ESZKÖZ-ÚJRAKÖTÉS ("miért nem a friss drivert használja?").

A DriverToolApi része (összerakás: app/gui/api.py).

MIÉRT LÉTEZIK - terepen kétszer bizonyítva, ThinkPad T580, 2026-08-24 és 08-25:
egy driver FEL TUD MENNI ÚGY, hogy az eszköz mégsem indul el rajta. A gép saját
`setupapi.dev.log`-ja szó szerint ezt írta:

    Installing best driver (oem19.inf) on device 'ACPI\\LEN009B'
    Strong Name=oem19.inf:...:LENOVO_GROUP53_InterTouch_Win8_Inst:19.3.4.228
    Start: ACPI\\LEN009B\\4&27A0EA9D&0
    Device 'ACPI\\LEN009B' not started: Device has problem: 0x0a (CM_PROB_FAILED_START)

A Windows ilyenkor VISSZAESIK a saját általános driverére (itt: `msmouse.inf`, PS/2
egér-emuláció), ami hibátlanul elindul - tehát a ConfigManagerErrorCode 0 lesz, és minden
hibakód-alapú ellenőrzés (a miénk is) tisztának látja a gépet. Közben a tapipad szaggat,
dobálja a kurzort, elveszti a trackinget, a fizikai gombjai nem működnek - mert a
precíziós út helyett PS/2-emulációban fut.

MI GYÓGYÍTJA MEG: az eszköz ÚJRAENUMERÁLÁSA. A technikus terepen véletlenül találta meg -
kivette az SSD-t a gépből, betette máshova, majd visszarakta, és utána hibátlan lett a
tapipad. Nem új driver kellett: a friss eszköz-felderítéskor a Windows NULLÁRÓL választ
drivert, és akkor a gyári csomag már el tud indulni. Ennek pontos szoftveres megfelelője
a `pnputil /remove-device` + `/scan-devices` páros, és ezt csinálja ez a modul - hogy
soha többé ne kelljen hozzányúlni a vashoz.

EZ NEM A TILTOTT "mentsük el a régi drivert és rakjuk vissza" MINTA (lásd CLAUDE.md):
itt a lánc által MOST telepített, a DriverStore-ban ott lévő csomag telepítését fejezzük
be. Semmit nem konzerválunk a gép fix előtti állapotából.

A KÖR TERJEDELME (explicit user decision, 2026-08-25): MINDEN alapdriveres eszköz, kivéve
a tárolót/firmware-t és a rendszerlemez eszközláncát - lásd REBIND_BLOCKED_CLASSES. A
korábbi osztály-FEHÉRLISTA ugyanabba a hibába esett, amit a CLAUDE.md a generic-replace
osztálylistáinál már egyszer megírt: egy eszköz azért nem kapta meg a gépen MÁR OTT LÉVŐ
gyári drivert, mert az osztálya nem volt felsorolva (kimaradt pl. a videokártya és a
kártyaolvasó is). Az ár, amit ezért cserébe kezelni kell: a kör az USB-vezérlőt és a
HID-eszközöket is érinti, tehát a billentyűzet/egér a kör után halott lehet - ezért kéri a
felület ELŐRE a hozzájárulást, és ezért indul újra a gép magától a végén
(_rebind_finish_reboot).
"""
import os
import time
import json
import logging

from app.common import _ps_quote
from app.wu_core import WU_PNP_QUERY_PS
from app.wu_core import _filter_wu_scan_devices
from app.wu_core import _is_inbox_driver
from app.wu_core import _hwid_tokens
from app.wu_core import _hwid_token_seq
from app.wu_core import is_specific_hwid
from app.wu_core import extract_inf_hardware_ids
from app.wu_core import _read_text_best_effort
from app.wu_core import driverstore_package_inf
from app.wu_core import STORAGE_RISK_CLASSES
from app.wu_core import FIRMWARE_RISK_CLASSES
from app.wu_core import _collect_boot_path_protection

# A PnP-nek kell pár másodperc, mire az újraenumerálás után rákötötte a drivert.
REBIND_SETTLE_SECONDS = 5

# CSAK AZOKAT AZ ESZKÖZÖKET RÚGJUK MEG, AMIKHEZ VAN GYÁRI CSOMAG A GÉPEN
# (explicit user decision, 2026-09-01 - a 2026-08-25-i "mindent meg lehet rúgva"
# terjedelem SZŰKÍTÉSE, nem a visszavonása).
#
# MIÉRT: a kör két, minőségileg különböző esetet kezelt egyformán. Ahol van stage-elt
# gyári csomag, ott az újraenumerálás VALÓDI munkát végez: a Windows nulláról választ, és
# a csomag el tud indulni - pontosan ez a T580 tapipadjának esete, amiért a modul született.
# Ahol viszont NINCS csomag a gépen, ott a Windowsnak nincs miből jobbat választania, tehát
# garantáltan ugyanazt az inbox drivert köti vissza - ezt a kör saját üzenete is kimondja
# ("Ha nincs jobb, ugyanazt kapja vissza"). Az a menet tehát idő és kockázat, nulla
# nyereséggel.
#
# MÉRVE (2026-09-01, Dell Latitude 5580, Build 288): 30 megrúgott eszközből **4**-hez volt
# csomag, 26-hoz nem; a kör ~3,5 percig tartott, és a záró jelentés szerint utána is 9
# eszköz maradt alapdriveren. Ugyanez a T580-on 8 eszközből 0 azonnali visszakötés.
#
# A KOCKÁZAT-CSÖKKENÉS ÖNMAGÁBAN IS INDOK: a kör az USB-vezérlőt, a HID-eszközöket és a
# billentyűzetet is érinti, ezek az újraindításig halottak. Minél kevesebb csomópontot
# bántunk feleslegesen, annál kisebb az esély, hogy a technikus egy használhatatlan géppel
# marad, ha az újraindítás valamiért elmarad.
#
# A kihagyott eszközök NEM tűnnek el a szem elől: a záró egészségjelentés
# (`_emit_driver_health`) továbbra is felsorolja, ami alapdriveren maradt.
REBIND_ONLY_WITH_PACKAGE = True

# Visszaszámlálás a kör végi automatikus újraindításig (lásd _rebind_finish_reboot).
REBIND_REBOOT_GRACE_SECONDS = 10

# MINDEN ÚJRAENUMERÁLHATÓ, KIVÉVE A TÁROLÓT ÉS A FIRMWARE-T (explicit user decision,
# 2026-08-25): "mindent rúgjon meg ami nem boot kritikus... ha van gyári driver akkor
# inkább az legyen fent mint az inbox, kivéve a vezérlő és a firmware".
#
# Ez a projekt általános szabályának a folytatása (CLAUDE.md): a kizárás csak az lehet,
# amit a felhasználó tilt le, illetve ami nem hardver. A korábbi FEHÉRLISTA
# (egér/HID/billentyű/hang/háló/kamera/monitor) ugyanabba a hibába esett, mint a
# generic-replace osztálylistái: egy kártyaolvasó, egy chipset-eszköz vagy egy alapdriveren
# ragadt VIDEOKÁRTYA azért nem kapta meg a gépen MÁR OTT LÉVŐ gyári drivert, mert az
# osztálya nem volt felsorolva.
#
# MI MARAD KI, ÉS MIÉRT:
#   1) tároló + firmware osztályok (lent): a csomópont pár másodperces eltűnése a FUTÓ
#      rendszert viheti el, a firmware-nél pedig eleve nem driver-cseréről van szó;
#   2) a rendszerlemezt hordozó TELJES eszközlánc, osztálytól függetlenül - lásd
#      _rebind_candidates. Az osztály-tiltás ezt NEM fedi le: a dev gépen a lánc
#      `WD Blue SN580 [DiskDrive] -> NVMe vezérlő [SCSIAdapter] -> PCI Express Root Port
#      [System] -> Root Complex [System] -> ACPI [System]`, tehát a boot NVMe alatti PCIe
#      port System osztályú, és a puszta osztálylista alapján kirúgható lenne;
#   3) típuskódos azonosítójú eszköz (rendszeridőzítő, PCI-híd): ehhez gyári csomag nem
#      létezhet, tehát az újraenumerálás garantáltan ugyanazt az inbox drivert hozná
#      vissza - csak időbe és fölösleges kockázatba kerülne.
#
# AMI TUDATOSAN BENNE MARADT, pedig a Build 274-es futásnál gyanús volt: TPM, USB-vezérlő
# és -gyökérhub, PCIe portok, processzorösszesítő. Ezek a Windows saját busz-driverén
# futnak, ott is maradnak (nincs jobb csomag), a csomópont-eltávolítás pedig nem törli a
# TPM tartalmát (az a `tpm.msc` Clear, nem ez), csak újrafelderítteti az eszközt. A
# gyakorlati ár: az USB-vezérlő kirúgásakor a billentyűzet/egér az újraindításig nem
# működik - ezért mondja ki a felület, hogy ÚJRAINDÍTÁS KELL, és ezért ajánlja fel egyből.
REBIND_BLOCKED_CLASSES = set(STORAGE_RISK_CLASSES) | set(FIRMWARE_RISK_CLASSES)

# FAIL-SAFE: ha a rendszerlemez eszközláncát NEM sikerült felderíteni, a busz-/
# infrastruktúra-osztályokat is kihagyjuk, mert pont ezek visznek a boot-eszközhöz
# (PCIe port, root complex, ACPI csomópont). Ugyanaz az elv, mint a törlési fázis
# BOOT_FALLBACK_PROTECT_CLASSES ága: bizonytalanságban az óvatos ág.
REBIND_BOOT_FALLBACK_CLASSES = {'SYSTEM', 'COMPUTER'}

# A GYÁRTÓT ÉS AZ ESZKÖZT azonosító tokenek. Ezeknek KELL egyezniük ahhoz, hogy két
# hardver-azonosítót ugyanarra az eszközre vonatkozónak tekintsünk. A SUBSYS/REV/COL/MI
# eltérhet (ugyanaz a chip más gépgyártói változatban, illetve a kompozit eszköz
# interfészei), a VEN/DEV (PCI, ACPI) és a VID/PID (USB, HID) viszont nem.
_IDENTITY_PREFIXES = ('VEN_', 'DEV_', 'VID_', 'PID_')


def _identity_tokens(tokens):
    return frozenset(t for t in tokens if t.startswith(_IDENTITY_PREFIXES))


def _strict_hwid_match(inf_id, dev_hwid):
    """SZIGORÚ azonosító-egyezés (a laza `_hwid_matches` helyett - lásd
    _staged_vendor_inf_for indoklását: a laza változat terepen Intel GPS-drivert húzott
    egy USB kompozit eszközre, és a gomb ezután kiszedte a tapipadot)."""
    a, b = _hwid_tokens(inf_id), _hwid_tokens(dev_hwid)
    if not a or not b or a[0] != b[0]:          # busz-előtag nélkül / eltérő buszon: nem
        return False
    ia, ib = _identity_tokens(a[1]), _identity_tokens(b[1])
    if not ia or not ib:
        # Nincs VEN/DEV-szerű tagja valamelyiknek (pl. `ACPI\LEN009B`): csak a teljes
        # tokenkészlet AZONOSSÁGÁT fogadjuk el - részhalmaz itt vaktalálat lenne.
        return a[1] == b[1]
    if ia != ib:
        return False
    # A HID-KOLLEKCIÓ LÁNCA (&COL..) HALMAZKÉNT LÁTHATATLAN - UGYANAZ A SZABÁLY, MINT A
    # `wu_core._hwid_matches`-BEN (2026-09-21, terepen mérve, HP EliteDesk + Alps).
    #
    # A COL-szabály 2026-09-03-án BEKERÜLT a laza `_hwid_matches`-be, de EBBŐL, a
    # SZIGORÚBBNAK szánt párjából KIMARADT - a projekt legrégebbi visszatérő hibája
    # (duplikált logika, aminek az egyik példánya lemarad). A mérés:
    #
    #   INF    : HID\VID_044E&PID_1212&COL02            {VID_044E, PID_1212, COL02}
    #   eszköz : HID\VID_044E&PID_1212&COL02&COL04      {VID_044E, PID_1212, COL02, COL04}
    #
    # A `<=` részhalmaz-szabály szerint ez EGYEZÉS, pedig két KÜLÖN csomópont: az INF a
    # touchpad-kollekciót deklarálja, az eszköz annak egy GYEREK-kollekciója. Emiatt az
    # újrakötés-kör négy csomópontot (`&COL01..COL04`) fölöslegesen eltávolított, majd
    # kikényszerített egy újraindítást - a naplóban `[REBIND] Egyezés: ... <- apvhid.inf`
    # négyszer, `2. lépés - a csomópont eltávolítása` négyszer.
    #
    # A COL-lánc SORREND és DARABSZÁM szerint azonosít; rá csak a PONTOS egyezés jó.
    if ([t for t in _hwid_token_seq(inf_id) if t.startswith('COL')]
            != [t for t in _hwid_token_seq(dev_hwid) if t.startswith('COL')]):
        return False
    # A gyártó+eszköz egyezik; a többi tag (SUBSYS/REV/MI) egyik irányban bővebb lehet.
    return a[1] <= b[1] or b[1] <= a[1]


class GuiRebindMixin:
    """Eszköz-újrakötés: automatikusan (AutoFix záró köre) és kézzel (gomb)."""

    # ------------------------------------------------------------------
    # KÖZÖS MAG - ezt hívja az AutoFix regresszió-javítója ÉS a kézi gomb is
    # ------------------------------------------------------------------
    def _rebind_device(self, pnp, inf_path, dev_name, dev_class, task_id, verify=True):
        """Egy eszköz visszakötése a stage-elt gyári driverre.

        Visszatérés: 'fixed' | 'needs_reboot' | 'failed'.

        A LÉPCSŐK ÉS AZ INDOKLÁSUK (terepen mérve, ThinkPad T580):
          1) a csomag újratelepítése (`pnputil /add-driver ... /install`) - ez önmagában
             sokszor NEM vált kötést, mert a Windows a meglévő drivert elég jónak látja;
          2) a csomópont ELTÁVOLÍTÁSA (`/remove-device`) + `/scan-devices` - néha elég;
          3) ha nem: ÚJRAINDÍTÁS. **Ez a lényeg.** A technikus terepen az SSD ki-be
             pakolásával javította meg a tapipadot, és annak a hatása nem a csomópont
             eltávolítása volt, hanem hogy UTÁNA A GÉP ÚJRAINDULT és a Windows a teljes
             eszközfát nulláról derítette fel. A Build 273-as futás ezt be is bizonyította:
             a `/remove-device` + `/scan-devices` páros 8 eszközből NULLÁT kötött vissza -
             mert újraindítás nélkül a PnP nem építi újra a köteléket.
             Ezért az eltávolítás után `needs_reboot`-ot adunk vissza: a csomópont ilyenkor
             már NINCS meg, tehát a következő induláskor a Windows frissen felderíti - ez a
             lemez visszadugásának pontos megfelelője.

        A tárolóvezérlőket/lemezeket SOSEM távolítjuk el: ott a csomópont eltűnése a futó
        rendszert viheti el. Beviteli/egyéb eszköznél ez ártalmatlan."""
        if inf_path:
            logging.info(f"[REBIND] 1. lépés - újratelepítés: {dev_name} <- {inf_path}")
            self._run(['pnputil', '/add-driver', inf_path, '/install'],
                      timeout=600, ok_codes=(0, 259, 3010))
            self._run(['pnputil', '/scan-devices'], timeout=300)
            if self._device_on_vendor_driver(pnp):
                logging.warning(f"[REBIND] SIKER (újratelepítéssel): {dev_name}")
                return 'fixed'

        cls = (dev_class or '').strip().upper()
        if cls in STORAGE_RISK_CLASSES or cls in FIRMWARE_RISK_CLASSES:
            logging.info(f"[REBIND] {dev_name} [{cls}]: tároló/firmware - eltávolítás KIHAGYVA "
                         f"(a csomópont eltűnése a futó rendszert vinné).")
            return 'failed'

        logging.warning(f"[REBIND] 2. lépés - a csomópont eltávolítása: {dev_name} [{pnp}]")
        self.emit('task_progress', {'task': task_id, 'log': f'   🔄 {dev_name}: eszköz eltávolítása, hogy a Windows újra felderítse...'})
        rem = self._run(['pnputil', '/remove-device', pnp], timeout=300, ok_codes=(0, 1, 3010))
        removed = getattr(rem, 'returncode', 1) in (0, 3010)
        if not removed:
            # A /remove-device csak Win10 1903-tól létezik; régebbin a letiltás/
            # engedélyezés páros a megfelelője.
            logging.info(f"[REBIND] A /remove-device nem elérhető (rc={getattr(rem, 'returncode', '?')}) "
                         f"- letiltás/engedélyezés jön.")
            ps = (f"$id='{_ps_quote(pnp)}'; "
                  "Disable-PnpDevice -InstanceId $id -Confirm:$false -EA SilentlyContinue; "
                  "Start-Sleep -Seconds 3; "
                  "Enable-PnpDevice -InstanceId $id -Confirm:$false -EA SilentlyContinue")
            r2 = self._run(["powershell", "-NoProfile", "-Command", ps], timeout=300)
            removed = getattr(r2, 'returncode', 1) == 0
        if not verify:
            # KÖTEGELT MÓD: a felderítés + ellenőrzés a KÖR VÉGÉN, egyszer fut le mindenkire
            # (lásd `_rebind_sweep`). Itt csak annyit mondunk, hogy a csomópont eltűnt-e.
            return 'removed' if removed else 'failed'
        self._run(['pnputil', '/scan-devices'], timeout=300)
        time.sleep(REBIND_SETTLE_SECONDS)
        if self._device_on_vendor_driver(pnp):
            logging.warning(f"[REBIND] SIKER (újrafelderítéssel): {dev_name}")
            return 'fixed'
        if removed:
            # A csomópont eltűnt, de a futó rendszerben nem épült újra a kötés. Az
            # újraindítás ezt oldja meg - ekkor derül fel az eszköz nulláról.
            logging.warning(f"[REBIND] {dev_name}: eltávolítva, a kötés ÚJRAINDÍTÁS után épül fel.")
            return 'needs_reboot'
        logging.error(f"[REBIND] NEM SIKERÜLT és eltávolítani sem lehetett: {dev_name}")
        return 'failed'

    def _device_on_vendor_driver(self, pnp):
        """Gyári (nem Windows-alap) driveren fut-e most az eszköz?"""
        info = (self._get_installed_driver_info() or {}).get(pnp) or {}
        return bool(info) and not _is_inbox_driver(info)

    def _rebind_boot_chain_ids(self):
        """A rendszerlemezt hordozó eszközlánc PnP-azonosítói (nagybetűsen).

        Ugyanaz a felderítés, amit a törlési fázis is használ (`_collect_boot_path_protection`),
        csak itt nem az INF-ek, hanem maguk az ESZKÖZÖK kellenek: ezeknek a csomópontját
        nem szedjük ki. Az osztály-tiltás erre nem elég - a boot-lánc felső fele System
        osztályú (PCIe port, root complex, ACPI).

        Ha a felderítés nem sikerül, ÜRES helyett None-t adunk vissza, és a hívó ilyenkor
        fail-safe módon a tároló-osztályok mellett a busz-osztályokat is békén hagyja.
        Ugyanaz a logika, mint a törlésnél: egy nem bootoló gép visszafordíthatatlan."""
        try:
            _infs, chain, detected = _collect_boot_path_protection(self._run)
        except Exception as e:
            logging.warning(f"[REBIND] A boot-lánc felderítése hibára futott: {e}")
            return None
        if not detected:
            return None
        ids = {str(c.get('Id') or '').strip().upper() for c in (chain or []) if c.get('Id')}
        logging.info(f"[REBIND] A rendszerlemez eszközlánca ({len(ids)} eszköz) ki van zárva "
                     f"az újraenumerálásból: {sorted(ids)}")
        return ids

    def _rebind_candidates(self, devices, inst, boot_ids=None):
        """Mely eszközöket érdemes újra felderíttetni a Windowszal?

        MIÉRT NEM AZ INF-EKBŐL DOLGOZUNK (élő méréssel bizonyítva a T580 lemezén):
        az `extract_inf_hardware_ids` csak az ALÁHÚZÁST tartalmazó azonosítókat tartja meg,
        ezért a `synpd.inf`-ből kiolvasott 16 azonosító közt NINCS ott az `ACPI\\LEN009B` -
        vagyis a csomag-INF alapján épp azt a tapipadot NEM találtuk volna meg, ami miatt
        az egész funkció készült. Ugyanez a laza illesztés viszont a `hdbusext.inf`-et
        ráhúzta a HD Audio vezérlőre. Az INF-alapú párosítás tehát rossz alapon állt:
        egyszerre volt vak és túl bőkezű.

        AMIT HELYETTE CSINÁLUNK - pontosan az, ami a lemez ki-be pakolásakor történik:
        nem mi választunk csomagot, hanem ELTÁVOLÍTJUK a csomópontot, és az újraindítás
        után a WINDOWS választ drivert nulláról, a saját rangsorolásával. Ez nem tud
        rosszabb állapotot előidézni: ha nincs jobb driver, ugyanazt az alapdrivert köti
        vissza, mint eddig.

        Kiket veszünk be: Windows-ALAPDRIVEREN futó, GYÁRTÓ-KÓDOS azonosítójú eszközök -
        osztálytól FÜGGETLENÜL (explicit user decision, 2026-08-25).
        Kiket nem: tároló/firmware osztály, a rendszerlemez eszközlánca, és a típuskódos
        azonosítójú eszközök. Az indoklás a REBIND_BLOCKED_CLASSES-nél.

        `boot_ids`: a boot-lánc eszköz-azonosítói; None = a felderítés nem sikerült,
        ilyenkor fail-safe módon a busz-osztályokat is kihagyjuk (egy nem bootoló gép
        visszafordíthatatlan, egy inbox driveren maradt PCIe port nem az)."""
        out = []
        dropped = {}
        for d in devices or []:
            info = inst.get(d.get('pnp_id') or '')
            if not info or not _is_inbox_driver(info):
                continue                      # gyári driveren fut - nincs dolgunk vele
            cls = (d.get('pclass') or '').strip().upper()
            if cls in REBIND_BLOCKED_CLASSES:
                dropped['tároló/firmware'] = dropped.get('tároló/firmware', 0) + 1
                continue
            if boot_ids is None and cls in REBIND_BOOT_FALLBACK_CLASSES:
                dropped['boot-lánc ismeretlen (fail-safe)'] = dropped.get('boot-lánc ismeretlen (fail-safe)', 0) + 1
                continue
            if boot_ids and (d.get('pnp_id') or '').strip().upper() in boot_ids:
                dropped['a rendszerlemez láncán van'] = dropped.get('a rendszerlemez láncán van', 0) + 1
                logging.info(f"[REBIND] Kihagyva (rendszerlemez lánca): {d.get('name')} [{cls}]")
                continue
            hwids = [h for h in (d.get('all_hwids') or []) if h and is_specific_hwid(h)]
            if not hwids:
                dropped['csak típuskódos azonosító'] = dropped.get('csak típuskódos azonosító', 0) + 1
                continue
            out.append((d, info))
        # Egy összegző sor - a terepi kérdés ("miért nem rúgta meg X-et?") csak ebből
        # válaszolható meg, viszont eszközönként naplózni már zajos lenne (lásd Rule 0).
        if dropped:
            logging.info("[REBIND] Alapdriveres eszközök kizárva az újraenumerálásból: "
                         + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
        return out

    def _rebind_pkg_ids(self, orig_inf):
        """(INF útvonala, a benne szereplő hardver-azonosítók) egy stage-elt csomaghoz.

        Futásonként cache-el (`_rebind_pkg_cache`), mert ugyanazt a néhány INF-et minden
        egyes eszköz-jelölt újra beolvastatná. A cache-t a kör indulása üríti, hogy egy
        közben települt csomag ne maradjon ki."""
        cache = getattr(self, '_rebind_pkg_cache', None)
        if cache is None:
            cache = self._rebind_pkg_cache = {}
        if orig_inf in cache:
            return cache[orig_inf]
        path = driverstore_package_inf(orig_inf)
        ids = []
        if path:
            try:
                ids = extract_inf_hardware_ids(_read_text_best_effort(path))
            except Exception as e:
                logging.debug(f"[REBIND] A(z) {orig_inf} INF nem olvasható: {e}")
                path = None
        cache[orig_inf] = (path, ids)
        return path, ids

    def _staged_vendor_inf_for(self, dev, third_party):
        """Van-e a DriverStore-ban olyan STAGE-ELT gyári csomag, ami EZT az eszközt állítja?

        SZIGORÚ EGYEZÉS, és ez nem óvatoskodás - terepen (T580, Build 273) a laza illesztés
        aktívan KÁRT OKOZOTT. A `_hwid_matches` token-részhalmaz szabálya általános
        eszközökre teljesen oda nem való csomagokat húzott rá:

            USB kompozit eszköz          -> intelgnssdriver.inf   (Intel GPS-driver)
            Intel USB 3.0 vezérlő        -> appleusbvhci.inf      (Apple USB)
            USB kompozit eszköz          -> rtlejf.inf            (Realtek kártyaolvasó)

        A gomb ezután `remove-device`-szal kiszedte ezeket az eszközöket (köztük a tapipad
        HID-gyerekeit), és nem tudta visszakötni őket - vagyis pont azt rontotta el, aminek
        a javítására való. Két szabály zárja ki ezt:

          1) MINDKÉT oldalnak KONKRÉT, gyártó-kódos azonosítónak kell lennie
             (`is_specific_hwid`). Egy `USB\\COMPOSITE`, `USB\\CLASS_03` vagy `*PNP0F13`
             típuskódra bármely gyártó csomagja "illeszkedne" - ezekre soha nem lépünk.
          2) PONTOS token-egyezés kell, nem részhalmaz: az INF-ben szereplő azonosító
             tokenkészletének AZONOSNAK kell lennie az eszközével, vagy az eszközének kell
             bővebbnek lennie ugyanazon a buszon úgy, hogy a gyártó+eszköz (VEN/DEV, VID/PID)
             tokenek mind egyeznek. Így a `SUBSYS`/`REV` eltérés még belefér, egy másik
             gyártó csomagja viszont nem.

        Visszatérés: (INF útvonala, eredeti INF-név) vagy (None, '').

        A csomagok INF-jeit CSOMAGONKÉNT EGYSZER olvassuk be (`_rebind_pkg_ids`): mióta a
        kör minden alapdriveres eszközre kiterjed, ez a metódus eszközönként fut le, és
        cache nélkül a beolvasás a jelöltek számával szorzódna (a dev gépen 62 jelölt x 19
        csomag = ~1200 fájlbeolvasás ugyanabból a néhány INF-ből)."""
        hwids = [h for h in (dev.get('all_hwids') or []) if h and is_specific_hwid(h)]
        if not hwids:
            return None, ''
        for pkg in third_party or []:
            orig = (pkg.get('original') or '').strip()
            if not orig:
                continue
            path, ids = self._rebind_pkg_ids(orig)
            if not path:
                continue
            for inf_id in ids:
                if not is_specific_hwid(inf_id):
                    continue
                for hw in hwids:
                    if _strict_hwid_match(inf_id, hw):
                        logging.info(f"[REBIND] Egyezés: {dev.get('name')} [{hw}] <- {orig} [{inf_id}]")
                        return path, orig
        return None, ''

    # ------------------------------------------------------------------
    # A KÖR MAGJA - ezt hívja a KÉZI GOMB és az AUTOFIX ZÁRÓ KÖRE is
    # ------------------------------------------------------------------
    def _rebind_sweep(self, task_id):
        """Minden alapdriveres eszköz újra felderíttetése. Visszatérés: (fixed, failed, pending).

        Csak `task_progress`-t emittál - a `task_start`/`task_complete`/újraindítás a hívóé,
        mert a gomb és a lánc ezekben különbözik (a gomb saját taskként fut és a végén
        magától újraindít, a lánc az 'autofix' csatornán jelent és a saját ütemezett
        újraindítását használja). A DÖNTÉSI LOGIKA viszont közös - ha két példányban élne,
        a gomb és a lánc előbb-utóbb más eszközöket kötne újra, és a terepi jelentésből
        nem lehetne megmondani, melyik futott."""
        fixed, failed, pending = 0, [], []
        # Amiknek a csomópontját eltávolítottuk: a verdiktjük a kör VÉGÉN, egyetlen
        # kötegelt felderítéssel + lekérdezéssel dől el (lásd a ciklus utáni blokkot).
        removed_devs = []
        self._rebind_pkg_cache = {}           # friss kör = friss INF-olvasás
        self.emit('task_progress', {'task': task_id, 'log': 'Eszközök és telepített driverek felmérése...', 'indeterminate': True})
        res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS], encoding='utf-8')
        devices = _filter_wu_scan_devices(json.loads(res.stdout or '[]'))
        inst = self._get_installed_driver_info() or {}
        third_party = [d for d in (self._get_third_party_drivers() or []) if d.get('original')]
        self.emit('task_progress', {'task': task_id, 'log': f'{len(devices)} eszköz, {len(third_party)} gyári driver-csomag a gépen.'})

        # A rendszerlemez eszközlánca osztálytól függetlenül kimarad (a lánc felső fele
        # System osztályú, oda az osztály-tiltás nem ér el).
        boot_ids = self._rebind_boot_chain_ids()
        candidates = self._rebind_candidates(devices, inst, boot_ids)
        logging.info(f"[REBIND] {len(candidates)} jelölt: Windows-alapdriveren fut, "
                     f"gyártó-kódos azonosítóval, nem tároló/firmware, nem a boot-láncon.")
        for d, info in candidates:
            logging.info(f"[REBIND]   jelölt: {d.get('name')} [{d.get('pclass')}] "
                         f"most: {info.get('inf')} ({info.get('provider')})")

        # A csomagot NEM mi választjuk ki (lásd _rebind_candidates indoklását): eltávolítjuk
        # a csomópontot, és a Windows dönt az újraindítás után. Ha van szigorúan az eszközhöz
        # köthető stage-elt gyári csomag, azt előbb megpróbáljuk telepíteni - hátha reboot
        # nélkül is megoldódik. De ez csak gyorsítás, nem feltétel.
        todo, with_pkg = [], 0
        for d, info in candidates:
            path, orig = self._staged_vendor_inf_for(d, third_party)
            if path:
                with_pkg += 1
            todo.append((d, info, path, orig or '(a Windows választ)'))
        # SZŰKÍTÉS: csak az az eszköz kerül sorra, amihez VAN gyári csomag a gépen.
        # A többinél a Windowsnak nincs miből jobbat választania, tehát garantáltan
        # ugyanazt az inbox drivert kötné vissza - idő és kockázat, nulla nyereséggel
        # (lásd REBIND_ONLY_WITH_PACKAGE indoklását a fájl tetején).
        if REBIND_ONLY_WITH_PACKAGE:
            skipped = [t for t in todo if not t[2]]
            todo = [t for t in todo if t[2]]
            if skipped:
                logging.info(f"[REBIND] {len(skipped)} eszköz kihagyva (nincs hozzá gyári csomag a "
                             f"gépen, az újraenumerálás ugyanazt az inbox drivert adná vissza): "
                             f"{[ (d.get('name') or d.get('pnp_id')) for d, _i, _p, _o in skipped ]}")
        if not todo:
            if REBIND_ONLY_WITH_PACKAGE and with_pkg == 0 and candidates:
                # FONTOS KÜLÖNBSÉG: nem az van, hogy minden rendben - hanem az, hogy
                # amihez csomag kellene, ahhoz nincs csomag a gépen. Ezt ki kell mondani,
                # különben a technikus "✅ nincs teendő"-nek olvasná.
                self.emit('task_progress', {'task': task_id, 'log': f'\nℹ️ {len(candidates)} eszköz fut Windows-alapdriveren, de egyikhez sincs gyári csomag a gépen - az újra-felderítés ugyanazt adná vissza, ezért kihagyjuk.'})
                self.emit('task_progress', {'task': task_id, 'log': '   Ezek a záró jelentésben tételesen szerepelnek; gyári driver a gép/alaplap gyártójának oldaláról pótolható.'})
                return 0, [], []
            self.emit('task_progress', {'task': task_id, 'log': '\n✅ Nincs olyan eszköz, ami Windows-alapdriveren futna és amit érdemes lenne újra felderíttetni.'})
            return 0, [], []

        # PONTOS SZÖVEG: a lista két külön dolgot tartalmaz, és a régi egymondatos
        # "N eszközhöz VAN gyári driver a gépen" MINDKETTŐRE ezt állította - akkor is, ha
        # egyetlen eszközhöz sem volt csomag. A technikus ebből azt olvasta ki, hogy N
        # drivert fog visszakapni, holott a többségnél a Windows ugyanazt az inbox drivert
        # köti majd vissza (ami nem hiba, csak nem javulás).
        self.emit('task_progress', {'task': task_id, 'log': f'\n🔧 {len(todo)} eszközhöz VAN gyári csomag a gépen, de a Windows alapdriverén futnak - ezeket felderíttetjük újra, hogy a gyári csomag rájuk kössön:'})
        if REBIND_ONLY_WITH_PACKAGE and len(candidates) > len(todo):
            self.emit('task_progress', {'task': task_id, 'log': f'   • további {len(candidates) - len(todo)} db alapdriveres eszközhöz nincs csomag a gépen - azokat NEM bántjuk, mert a Windows úgyis ugyanazt adná vissza (a záró jelentés felsorolja őket).'})
        for i, (d, info, path, orig) in enumerate(todo, 1):
            if self._cancel_flag:
                break
            name = d.get('name') or d.get('pnp_id')
            self.emit('task_progress', {'task': task_id, 'log': f'\n({i}/{len(todo)}) {name}\n   most: {info.get("inf")} ({info.get("provider")}) → gyári: {orig}',
                                        'current': i, 'total': len(todo)})
            # KÖTEGELT ELLENŐRZÉS (verify=False). Az eltávolítás UTÁNI, eszközönkénti
            # `/scan-devices` + 5 mp várakozás + TELJES WMI-kiíratás mérhetően soha nem
            # talál semmit: terepen 26 eszközből 0 (2026-08-31, T14), korábban 8-ból 0
            # (Build 273, T580) - a kötés a futó rendszerben nem épül újra, csak az
            # újraindításnál. Eszközönként ~10 mp-be került, összesen 4-5 percbe.
            # A verdiktet ezért a kör VÉGÉN, egyetlen felderítéssel + egyetlen
            # lekérdezéssel hozzuk meg - a kimenet ugyanaz, a kerülő marad el.
            state = self._rebind_device(d.get('pnp_id'), path, name, d.get('pclass'),
                                        task_id, verify=False)
            if state == 'fixed':
                # Az ÚJRATELEPÍTÉSI ág sikere (ott a kötés menet közben is felépülhet, ezt
                # az ellenőrzést szándékosan meghagytuk) - itt már nem kell újra kérdezni.
                fixed += 1
                self.emit('task_progress', {'task': task_id, 'log': '   ✅ Sikerült - a gyári driver újratelepítéssel felkötött.'})
            elif state == 'removed':
                removed_devs.append((d, name))
                self.emit('task_progress', {'task': task_id, 'log': '   🔄 Eltávolítva - a Windows az ÚJRAINDÍTÁS után deríti fel újra (ez a lemez visszadugásának megfelelője).'})
            else:
                failed.append(name)
                self.emit('task_progress', {'task': task_id, 'log': '   ❌ Nem sikerült - az eszköz a Windows alapdriverén marad.'})

        # EGYETLEN záró felderítés + EGYETLEN állapot-lekérdezés az összes eltávolítottra.
        # Ritka, de nem lehetetlen, hogy a Windows menet közben mégis felköt egyet; ha igen,
        # itt kiderül, és nem a "vár az újraindításra" listán marad.
        if removed_devs:
            self._run(['pnputil', '/scan-devices'], timeout=300)
            time.sleep(REBIND_SETTLE_SECONDS)
            after_all = {}
            try:
                after_all = self._get_installed_driver_info() or {}
            except Exception as e:
                # Nem baj: ilyenkor mindenkit "újraindításra vár"-ként kezelünk, ami a
                # mért valóság (0/26, 0/8) - és az újraindítás úgyis jön.
                logging.warning(f"[REBIND] A záró állapot-lekérdezés nem sikerült: {e}")
            for d, name in removed_devs:
                info = after_all.get(d.get('pnp_id')) or {}
                if info and not _is_inbox_driver(info):
                    fixed += 1
                    logging.warning(f"[REBIND] SIKER (újrafelderítéssel): {name} -> {info.get('inf')}")
                    self.emit('task_progress', {'task': task_id, 'log': f'   ✅ {name}: mégis felkötött a gyári driverre ({info.get("inf")}).'})
                else:
                    pending.append(name)
        logging.info(f"[REBIND] Kör vége: {fixed} azonnal visszakötve, {len(pending)} újraindításra vár, "
                     f"{len(failed)} sikertelen.")
        return fixed, failed, pending

    # ------------------------------------------------------------------
    # KÉZI GOMB
    # ------------------------------------------------------------------
    def rescan_and_rebind_drivers(self, auto_reboot=True):
        """KÉZI ÚJRASCANNELÉS: MINDEN Windows-alapdriveren futó eszközt újra felderíttetünk
        a Windowszal - kivéve a tárolót, a firmware-t és a rendszerlemez eszközláncát.

        Ez a gomb arra való, amit a technikus eddig az SSD ki-be pakolásával oldott meg.
        Ha van a gépen az eszközhöz illő gyári csomag, azzal jön vissza; ha nincs, ugyanazt
        az inbox drivert kapja (nem lesz rosszabb).

        `auto_reboot`: a kör végén magától újraindul-e a gép. Alapból IGEN, mert a kör a
        beviteli eszközöket (USB-vezérlő, hubok, HID) is érintheti, tehát utána kattintani
        már nem biztos, hogy lehet - a hozzájárulást a felület előre kéri."""
        logging.info(f"[API] rescan_and_rebind_drivers(auto_reboot={auto_reboot}) - kézi eszköz-újrakötés")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Offline módban nem elérhető!', 'type': 'error'})
            return

        def worker():
            task = 'rebind'
            self.emit('task_start', {'task': task, 'title': 'Eszközök újrakötése a gyári driverekre'})
            try:
                fixed, failed, pending = self._rebind_sweep(task)
                if not fixed and not failed and not pending:
                    self.emit('task_progress', {'task': task, 'log': 'Ha valamelyik eszköz mégis rosszul működik, ahhoz a gépen NINCS gyári driver - a "Driver Keresés és Telepítés" menüben kerestethetsz hozzá.'})
                    self.emit('task_complete', {'task': task, 'status': 'Nincs javítanivaló'})
                    return

                self.emit('task_progress', {'task': task, 'log': f'\n📊 Kész: {fixed} eszköz azonnal visszakötve.'})
                if failed:
                    self.emit('task_progress', {'task': task, 'log': f'⚠️ {len(failed)} eszközt nem sikerült: {", ".join(failed[:6])}'})
                    self.emit('task_progress', {'task': task, 'log': '👉 Ezekhez a gyártó letöltőoldaláról telepíts drivert kézzel.'})
                if pending:
                    # EZT KI KELL MONDANI ÉS FEL KELL AJÁNLANI: a csomópontok már NINCSENEK
                    # meg, tehát ezek az eszközök AZ ÚJRAINDÍTÁSIG nem működnek. Enélkül a
                    # technikus egy elrontott gépet venne át - pontosan az a hiba, ami a
                    # Build 273-as futásban megtörtént.
                    self.emit('task_progress', {'task': task, 'log': f'\n🔄 {len(pending)} eszköz ÚJRAINDÍTÁST igényel: {", ".join(pending[:6])}'})
                    self.emit('task_progress', {'task': task, 'log': '❗ FONTOS: ezek az eszközök az újraindításig NEM működnek - a Windows ekkor deríti fel őket újra, és ekkor kapják meg a gyári drivert. Pontosan ez történik akkor is, amikor kihúzod és visszadugod a lemezt.'})
                self.emit('task_complete', {'task': task,
                                            'status': f'✅ {fixed} azonnal javítva' + (f', {len(pending)} újraindítás után' if pending else ''),
                                            'need_reboot': bool(pending)})
                if pending:
                    time.sleep(1)
                    self._rebind_finish_reboot(task, auto_reboot)
            except Exception as e:
                logging.error(f"[REBIND] Hiba a kézi újrakötésben: {e}", exc_info=True)
                self.emit('task_error', {'task': task, 'error': str(e)})

        self._safe_thread('rebind', worker)

    def _rebind_finish_reboot(self, task, auto_reboot):
        """A sweep utáni újraindítás.

        MIÉRT NEM ELÉG ITT A "Szeretnéd újraindítani?" KÉRDÉS (2026-08-25): amióta a kör
        minden alapdriveres eszközre kiterjed, a jelöltek közt ott van az USB-állomásvezérlő
        és a gyökérhubok is - azok csomópontja nélkül pedig A BILLENTYŰZET ÉS AZ EGÉR IS
        HALOTT az újraindításig. A régi folyamat pont ekkor kérdezett rá egy gombos ablakkal,
        amire a technikus már nem tudott volna kattintani. A hozzájárulást ezért a felület
        ELŐRE kéri (lásd ui.html: rebindDrivers), itt már csak visszaszámolunk és megyünk.

        Ha a hívó mégis interaktív módot kért (auto_reboot=False), marad a régi kérdés."""
        if not auto_reboot:
            self.emit('ask_reboot', None)
            return
        logging.warning("[REBIND] Automatikus újraindítás: a kör beviteli eszközöket is "
                        "érinthetett, kézi megerősítésre nem lehet számítani.")
        self.emit('task_progress', {'task': task, 'log': f'\n🔄 A gép {REBIND_REBOOT_GRACE_SECONDS} másodperc múlva ÚJRAINDUL, hogy a Windows felderítse az eszközöket.'})
        for left in range(REBIND_REBOOT_GRACE_SECONDS, 0, -1):
            if self._cancel_flag:
                self.emit('task_progress', {'task': task, 'log': '⏹️ Megszakítva - az újraindítás elmarad. FONTOS: az érintett eszközök addig NEM működnek, amíg kézzel újra nem indítod a gépet!'})
                logging.warning("[REBIND] Az automatikus újraindítást a felhasználó megszakította.")
                return
            if left % 5 == 0 or left <= 3:
                self.emit('task_progress', {'task': task, 'log': f'   {left}...'})
            time.sleep(1)
        logging.warning("[REBIND] Újraindítás: shutdown /r /t 0 /f")
        self._run(['shutdown', '/r', '/t', '0', '/f'])
