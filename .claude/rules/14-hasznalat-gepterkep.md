---
paths:
  - "app/driverusage_core.py"
  - "app/machinemap_core.py"
  - "app/dupdrivers_core.py"
  - "app/gui/dupdrivers.py"
  - "app/gui/drivers.py"
  - "app/wu_core.py"
  - "tests/test_cleanup_usage.py"
  - "tests/test_usage_labels_mapclass.py"
---

*A CLAUDE.md 14. fejezete, 2026-11-13 óta külön fájlban, szó szerint. A Claude Code magától betölti, ha a fenti útvonalak egyikéhez nyúlsz. A CLAUDE.md (elvek, 1–11. és 18. fejezet) itt is érvényes; a bizonyíték: `docs/terep/torles-hasznalat-gepterkep.md`.*

## 14. Használat, nyomtató-felismerés, gép-térkép, duplikátumok

### Mit használ a gép (`app/driverusage_core.py`, EGY mag a Driverek nézetnek és a fix előnézetének)

**Jelek:** jelenlévő eszköz kötése (eszközfa, `win32.enumerate_device_nodes`) / **futó kernel-szolgáltatás** (a `.sys` + a DriverStore-mappa HASH-es neve dönt, nem a szolgáltatásnév: két csomag ugyanazt a `.sys`-t hordhatja) / nem jelenlévő (ghost) eszköz / filter-regisztráció / **a spoolerben regisztrált nyomtató-driver** (az eredeti INF-név alapján; nyomtatóval → `active`, csak regisztrálva → `standby`) / **Extension INF** (HWID-párosítás az eszközfára; terepen még nem mérve). **Állapotok:** `active` / `standby` („Nincs bedugva”) / `idle` („Nem fut”) / `unused` / **`unknown`** (a felderítés nem futott le vagy offline mód van: erre nem szabad törlést alapozni; ha a szolgáltatás-lekérdezés bukik → `None`). **2026-10-30 (explicit user decision: a „Készenlétben” nem mondta meg az okot):** `standby` = kihúzott eszköz, nyomtató nélküli nyomtató-driver, kihúzott eszköz Extension INF-je, VAGY hardvert megnevező INF leállított szolgáltatással/szűrővel (a szolgáltatás bedugáskor indul); `idle` = HWID nélküli (szoftveres) INF leállított szolgáltatással/szűrővel. A belső `standby` kulcs maradt. A ninja-eset (távoli asztal drivere eszköz nélkül) és az ESET 4 drivere miatt kell. **„Mikor használta utoljára" NEM létezik** (`LastArrivalDate` = boot, a registryben nincs, a fájlrendszer last-access ki van kapcsolva): ne próbáld újra. **Ez besorolás, nem szűrő**: semmit nem rejt el, semmit nem tilt. **Olvashatatlan publikált INF + nincs pozitív jel = `unknown`, nem `unused`** (2026-10-12: az INF nélkül a szolgáltatás-jel nem ellenőrizhető, egy csak szolgáltatással dolgozó driver „nem használt”-nak látszott, és a „Csak a nem használtak” gomb kijelölte volna). A felület a használat- és nyomtató-választ csak arra a listára teszi rá, amire a kérés indult (`forList`).

### A nyomtató-felismerés a DRIVERBŐL dönt, nem abból, hogy be van-e dugva (2026-09-17)

> **Saját hiba:** egyszer átírtam úgy, hogy a „jelenlévő nyomtató használja" döntsön. *„elhozza a gépet az ember szervizre, de mivel nincs bedugva … letörli a nyomtató driverét"*. **A szervizben lévő laptop mellett soha nincs ott az ügyfél nyomtatója.** Egy szűrő feltételét soha nem szabad olyan jelre alapozni, ami a tipikus helyzetben hiányzik. Ha az a kérdés, hogy „jól működik-e?", előbb VÁLASZOLJ, ne javíts.

`wu_core.identify_printer_packages`, az INF-ből: (1) osztály (`Printer`, `PrintQueue`, `Image`, `Dot4`, `Dot4Print`, `PrinterUpgrade`); (2) nyomtató-kulcsszó a szövegben, **CSAK `USB`/`System`/`Ports` osztályban** (az ESET `Class=AntiVirus` csomagjában ott a „scanner"; a `fax` szándékosan nincs a listán, mert a modemekben is szerepel); (3) **azonos fizikai eszköz** (közös `VID&PID` egy felismert nyomtatóval, `&MI_xx` nélkül). **A gyártónév nem dönt** (egyszer 70 Samsung telefon-drivert rejtett el). **Egy függvény, minden fogyasztó** (`collect_printer_packages`: lista-szűrő, fix-törlés, előnézet, CLI). Egy szűrő agresszivitását **a teljes populáción** mérd, ne 6 kiválasztott eseten.

### A gép felépítése (`app/machinemap_core.py`)

A **Class oszlop félrevezető** (a `Camera` egy telefon kameramódja volt, a `Display` egy virtuális monitor). Erősorrend: **kötés (eszközfa) → container ID** (`{…FFFF-FFFFFFFFFFFF}` = a gép része) → hardver-osztálykód → **PCI-topológia** a GPU-hoz (dedikált = olyan híd alatt van, ahol minden funkció az ő gyártójáé; a név csak holtversenynél számít) → Windows-osztály → név (leírásonként, sorrendben). A nem jelenlévő belső eszköz sosem kerül egy létező alkatrész alá (`absent:`); a jelen lévő szülőnél csak az öröklő szoftver-komponens marad (2026-11-03: egy kiszerelt GPU szelleme a PCIe-port alatt „Egyéb – board:gpu” lett). Az eszköz nélküli csomagok rendeltetése az INF-ből jön (`package_purpose`, telefon: `PHONE_USB_VIDS` **Lenovo nélkül**). Ahol az eszközfa kötést mutat, nincs felülbírálás. **Kivétel: az elavult kötés** (`driverusage_core.stale_binding`, 2026-10-12): ha az eszköz Windows-osztálya ≠ a csomag osztálya, az `oemNN` számot azóta egy másik csomag kapta (a vándorló teszt-SSD egy ASRock-os „AMD SMBus” szellemeszköze a Lenovón a „Print to PDF” `oem2.inf`-jére mutatott) → a kötést kihagyjuk a térképben, a használat-besorolásban és a `find_superseded_packages` bizonyítékai közül; üres vagy `%…%` osztálynévre nem dönt. Géptípus: `PowerDeterminePlatformRoleEx`, ennek híján `ACPI\PNP0C0A`. A térkép hibája nem viheti el a táblázatot (tartalék: használat szerinti csoportosítás).

### Duplikátumok és kiváltott csomagok (`dupdrivers_core`)

- Csoport = eredeti INF-név, **a legújabb kiadás marad** (dátum, utána verzió). A jelenlévő eszköz által használt INF zárolt (`None` → minden zárolt). Csak `oemNN.inf` törölhető; előbb sima törlés, `/force` csak hiba esetén. **Minden törlendő csomag a törlés ELŐTT név szerint naplózva van**, a megmaradó párjával (`duplicate_delete_details`). A nézet kimondja, ha minden zárolt, és van Mind-kapcsoló.
- **`retire_superseded_packages`** (2026-09-27): az ÁTNEVEZETT INF-ű régi csomagot (`smbusamd.inf` → `amdinterface.inf`) bizonyíték alapján vezeti ki: `unused` + nem Extension/nyomtató + a saját nem-osztálykódos HWID-ja illik egy jelenlévő eszközre + minden ilyen eszköz egy másik, ugyanattól a gyártótól való, legalább ugyanolyan friss csomagon fut. Sima `/delete-driver`. Ami egyetlen jelenlévő eszközre sem illik (NVIDIA laptopos kísérők), azt nem bántjuk.
