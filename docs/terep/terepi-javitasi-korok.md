# Terepi bizonyítékok — Több területet érintő terepi javítási körök

> A CLAUDE.md 2026-10-11 előtti teljes szövegéből **szó szerint áthelyezve**, eredeti sorrendben. A tömör, érvényes szabályok a CLAUDE.md-ben vannak; ha a kettő eltér, a CLAUDE.md a mérvadó, és ezt a fájlt is javítani kell. A szövegben lévő `(#…)` hivatkozások a régi egyfájlos szerkezetre mutatnak: a cél-szekciót `grep`-pel keresd a `docs/terep/` mappában.

### NYOLC JAVÍTÁS EGY 0 HIBÁS LÁNCBÓL — a DÁTUM egy éve elveszett minden magyar gépen (2026-09-21, Build 326, Dell Latitude 5480)

**A lánc kifogástalan volt**, és ezt előre ki kell mondani: 14:21 → 14:47, **8 láb, 32 driver, 0 ERROR, 0 kivétel, 0 időtúllépés**, a végén 0 hibakódos eszköz, `A FOLYAMAT SIKERESEN BEFEJEZŐDÖTT`. A 71 WARNING nagy része a `[DUPDRV]` szándékos, nevesítő naplózása. **Nyolc javítás mégis kijött belőle, és az egyik egy éve némán élt.**

> **A VIZSGÁLAT MENETE, MERT A MÁSODIK KÖR HOZTA A LEGÉRTÉKESEBB LELETET.** Az első átfutás a `WARNING`/`ERROR` sorokat és a képernyőre kiírt üzeneteket nézte — abból jött 1–6. A 7–8. pont viszont csak akkor derült ki, amikor **a lánc DÖNTÉSI sorait** is végigolvastam (`[REBIND]`, `[CATALOG] Döntés:`, `[AUTOFIX-*]`), és **összevetettem őket egymással**: a REV-hiba egyetlen `Egyezés:` sor és egy `[GENERIC] … nem valódi csere` sor EGYÜTT olvasásából látszott, egyik önmagában nem WARNING. Egy naplót tehát nem elég a hibaszintű sorok szerint átnézni — a szomszédos, INFO-szintű döntéseket is egymáshoz kell mérni.

#### 1. A DISM DÁTUMA MINDEN MAGYAR GÉPEN ELVESZETT — a `/English` csak a KULCSSZAVAKAT angolosítja

Ez a legsúlyosabb lelet, és egyetlen naplósor adta meg:

```
Published Name : oem0.inf
Date : 2006. 06. 21.          <- MAGYAR formátum, az ANGOL kulcsszó mellett
Version : 10.0.19041.1806
```

A `_dism_date_to_iso` docstringje kimondta a téves feltevést: *„A `/English` kimenet mindig M/D/YYYY alakú — **a lokalizált formátumokra nem számítunk**, mert minden hívás /English-sel megy"*. **Ez megdőlt.** A DISM a dátumot a **rendszer rövid-dátum mintájával** formázza; élőben mérve ezen a gépen `(Get-Culture)` = `hu-HU`, `ShortDatePattern` = **`yyyy. MM. dd.`**. A régi regex (`\d{1,2}[/.-]\d{1,2}[/.-]\d{4}`) erre nem illeszkedett.

**A KÖVETKEZMÉNY:** a `date` mező **minden magyar Windowson üres volt**, tehát a duplikátum-takarítás *„dátum elsődleges, verzió csak holtversenynél"* szabálya — amit a projekt 2026-07-27 óta az egységes kiadás-rendezésének nevez — **a bolt összes gépén némán verzió-alapú maradt**. Pontosan az a hibaosztály, ami miatt az a szabály született: egy gyártói verziósémaváltásnál a verzió-rendezés a **frissebb** csomagot jelöli meg törölhetőnek. A naplóban ez így látszott (a `[?]` a hiányzó dátum):

```
[DUPDRV] TÖRLENDŐ: oem47.inf (apvhid.inf) Alps v10.0.0.123 [?] - MEGMARAD helyette: oem15.inf v10.0.0.126 [?]
```

**MIÉRT ÉLT EGY ÉVIG: semmi nem jelezte.** A hiányzó dátum csendben a verzió-tagra esett vissza. Ezért a `parse_dism_driver_list` most **egy összegző WARNING-ot** ír (nem csomagonként — hot loop), és megnevezi az értelmezhetetlen nyers alakokat. Ez az a sor, ami ezt egy pillantással megadta volna.

**A PARSER KÉT ALAKOT KEZEL, és a második kétértelmű:** négyjegyű év **elöl** (`2006. 06. 21.`, ISO, `yyyy/MM/dd`) → egyértelmű; év **hátul** (`6/21/2006`, `21.06.2006`) → a hónap/nap sorrend nem olvasható ki, ezért ahol lehet, a számok döntenek (ami >12, az csak nap), a maradékra a szeparátor szokása (`/` → amerikai M/D, `.`/`-` → európai D/M). **A kétértelműség ára nem romló**: a heurisztika determinisztikus, tehát egy csoport minden csomagját ugyanúgy értelmezi, és érvénytelen értékre (`hónap>12`) üres stringet ad — egy rossz dátum rosszabb, mint a hiányzó, mert azt a verzió pótolja. Offline tesztelve **18 alakra** + végponttól végpontig a terepi DISM-blokkal + egy **regresszió-tesztre**: verziósémaváltásnál (`30.0.101.1` [2024] vs `1.0.0.9` [2026]) most a frissebb **dátumú** marad meg.

#### 2. AZ AUTO-UPDATER API-ÁGA FRISS WINDOWSON MINDIG ELBUKOTT

```
[UPDATE] A Releases API nem elérhető (<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]
         certificate verify failed: unable to get local issuer certificate>) - a raw útra váltunk.
[UPDATE] Update ellenőrzése erről a címről: https://raw.githubusercontent.com/... -> 326   <- EZ SIKERÜLT
```

A 2026-09-17-i `_latest_release()` **csupasz `urllib`** volt — miközben ez a fájl kimondja: *„Minden letöltés a közös letöltőn megy, az auto-updater is… a `fetch_text_with_cert_fallback` az egyetlen helyes út."* A raw ág már akkor is a fallbackre esett, az új API-ág kimaradt.

**Ez nem szélső eset, hanem a TIPIKUS eset:** a CLAUDE.md-ben dokumentált gyök-tanúsítvány-hiány szerint egy frissen telepített Windows tárolójából épp a `github.com`/`api.github.com` lánca (Sectigo/USERTrust) hiányzik, a raw-é (DigiCert) nem — a napló ezt ugyanabban a futásban bizonyítja. Vagyis a 09-17-i *„az API azonnal friss, a raw CDN 5 percig régi"* nyereség **pont azokon a gépeken veszett el, amiken a program dolgozik**.

**Mérve a javítás után** (élő hálózat): a közös letöltő **kiszolgálja** az API-t (89 KB, 26 kiadás, `build-326` elöl — tehát a `User-Agent` sem gond, az `Invoke-WebRequest` ad ilyet), és **szimulált** friss-Windows TLS-hibán a fallback is `build=326`-ot ad.

#### 3. A NYOMTATÓ-JEL NÉMA VOLT A LÁNCBAN — a hívó nem adta át a párosítást

```
[USAGE] A spoolerben van nyomtató-driver, de a hívó nem adott published->original
        párosítást - a nyomtató-jel ebben a körben nem tud megszólalni.        (2×)
```

A 2026-09-18-i nyomtató-jel (a spooler az **eredeti** INF-nevet ismeri, nem az `oemNN.inf`-et) az AutoFix két hívóhelyén nem kapott `packages` listát, tehát a nyomtató-csomagok **„nem használt"-nak látszottak** — épp az a hiba, amit a felhasználó 2026-09-18-án bejelentett, csak a lánc oldalán. A javítás ingyen volt: **mindkét hívónál a csomaglista már be volt töltve** két sorral feljebb.

#### 4. A KÉPERNYŐ 17%-A ISMÉTLŐDŐ „NINCS TEENDŐ" ZAJ VOLT — mérve, nem becsülve

| sor | db | a 538 képernyő-sorból |
|---|---|---|
| `🚫 <eszköz>: a katalógus egyik jelöltje sem támogatja…` | **70** | 13% |
| `🛡️ N tároló/firmware-eszköz kihagyva…` | **22** | 4% |

Mindkettő **lábanként, körönként újra** ment ki, és mindkettő azt mondja, hogy **nincs mit tenni**. A `🚫` egyetlen chipset-eszközre 8-szor, a 2–5. lábon már a `catalog_hwids.json` gyorsítótárból, tehát **új információ nélkül**. A `🛡️`-t a felhasználó a **kézi szkenből** már 2026-09-03-án kivetette (*„senkit se érdekel minek van ott"*), az AutoFix köreiből nem — ugyanaz az „egy döntés, több szöveg" hiba. **Mindkettő a naplóba került**; ami megmarad a képernyőn: a záró jelentés egyszeri, **nevesített** sorai (`🛡️ 4 eszköz … : <nevek>`, `🏭 8 eszköz Windows-alapdriveren maradt`), mert ott a technikus a lánc végén áll.

#### 5. A „gyártói kártyák" SZÖVEG HARMADIK ÉS NEGYEDIK PÉLDÁNYA

```
👉 Ezekhez sem a WU, sem a katalógus nem adott gyári csomagot… (lásd a "Driver Keresés
   és Telepítés" menü gyártói kártyáit).
```

A gyártói link-kártyák **2026-09-02-án (videokártya) és 09-18-án (gép/alaplap) TELJESEN kikerültek** a programból, tehát a szöveg egy nem létező felületi elemhez küldött. Ez ugyanannak a hibaosztálynak a harmadik és negyedik előfordulása (az első kettő a megszüntetett tároló/firmware-jelölőnégyzetre küldő szövegek voltak). **Ökölszabály, immár harmadszor bizonyítva: ha egy döntés megszüntet egy felületi elemet, `grep`-elj rá a TELJES szövegkészletben — egy elemhez több szöveg tartozik.**

#### 6. A HIBAÜZENET A pnputil FEJLÉCE VOLT, NEM A HIBA

```
❌ oem41.inf (iigd_ext.inf Intel Corporation v30.0.101.1069) törlése sikertelen:
   Microsoft PnP Utility
```

A `Microsoft PnP Utility` a pnputil **fejléce**; a valódi ok a következő bekezdésben állt (`Failed to delete driver package: One or more devices are presently installed using the specified INF`, rc=`0xE000023D`). A nyers `stdout[:120]` tehát semmit nem mondott. Új közös segéd: `drivers_core.delete_failure_text` — sorrendben (1) az in-use eset **saját, magyar** szövege, (2) a pnputil `Failed to …` sora, (3) az első nem-fejléc sor. **És ugyanitt derült ki egy önmagával ellentmondó összegzés**: az in-use kimenetel a `fail` számlálóba esett, vagyis a záró sor *„1 sikertelen"*-t írt, miközben a tétel sora azt, hogy *„ez normális"*. Az in-use mostantól a `skipped`-be megy: **ok = törölve, skipped = használatban van, fail = VALÓDI hiba**.

#### 7. A REVÍZIÓ „eltérhet" FELTEVÉS TÉVES VOLT — egy másik hardver-változat INF-jét kötöttük vissza

Ez a napló legfinomabb lelete, és **mérhető kárt okozott**. A rebind kör naplósora:

```
[REBIND] Egyezés: Realtek USB GbE Family Controller [USB\VID_0BDA&PID_8153]
                  <- rtump64arm64sta.inf [USB\VID_0BDA&PID_8153&REV_32FD]
[REBIND] - újratelepítés: ... FileRepository\rtump64arm64sta.inf_amd64_95a87af0abe9c628\...
[GENERIC] Realtek USB GbE Family Controller: a gyári csomag felment, de az eszköz
          a Windows driverén maradt (rtux64w10.inf) - nem valódi csere.
```

Az eszköz valódi azonosítója viszont **`USB\VID_0BDA&PID_8153&REV_31FD`** (a WU-sorból: `usb\vid_0bda&pid_8153&rev_31fd`), az INF-é **`REV_32FD`** — **két külön hardver-revízió**. A `_staged_vendor_inf_for` docstringje kimondta a feltevést: *„Így a `SUBSYS`/`REV` eltérés még belefér"*. A SUBSYS-ra ez **igaz** (ugyanaz a chip más gépgyártó gépében — ezen áll az R9-200 eset), a **REV-re nem**: az az eszköz hardver-változata, amihez a gyártó külön INF-szekciót ír.

**MIÉRT CSÚSZOTT ÁT A SZIGORÚ PÁROSÍTÓN:** a `_strict_hwid_match` **egy** INF-azonosítót **egy** eszköz-azonosítóval hasonlít, és az eszköznek van egy REV **nélküli**, kompatibilis azonosítója is (`USB\VID_0BDA&PID_8153`). Arra halmazként `{VID,PID} ⊆ {VID,PID,REV_32FD}`, tehát részhalmaz-egyezés. A revízió-ellentmondás csak akkor látszik, ha az eszköz **összes** azonosítóját együtt nézzük — ezért a új `rev_conflict` az `all_hwids`-ból gyűjtött REV-halmazzal dolgozik, nem a párral.

**A KÁR, tételesen:** a kör eltávolította egy működő eszköz csomópontját, újratelepítette a **rossz revíziójú** INF-et, a Windows (helyesen) nem kötötte rá, az eszköz a Windows `rtux64w10.inf`-jén maradt — és mindez **egy fölösleges újraindításba** került. A naplóban emellett egy valótlan `Egyezés:` sor áll, ami egy jövőbeli vizsgálatot is félrevezetne.

**A SZABÁLY SZÁNDÉKOSAN SZŰK:** csak akkor ellentmondás, ha **mindkét** oldal közöl revíziót, és **nincs közös**. Ha az INF általánosabb (nincs REV-je), az a legitim részhalmaz-eset — és ez a leggyakoribb alak, tehát a szűkítés nem veszít el valódi egyezést. Regresszió-tesztelve mind a négy dokumentált terepi esetre (R9 200 `ven+dev+rev`, amdafd `ven+cc`, USB kompozit `&MI_00`, ACPI pontos egyezés) + a COL-láncra: **9/9 + 4/4 állítás**, egyik sem változott.

> **⚠️ A 7. PONT JAVÍTÁSA 6 BUILDEN ÁT ELÁRASZTOTTA A NAPLÓT — SAJÁT HIBA (felfedezve 2026-09-27, az első teljes lánc az új kóddal).** A `rev_conflict` ellenőrzést a `_staged_vendor_inf_for` a párosítás ELŐTT futtatta, és **minden** olyan INF-azonosítóra DEBUG-sort írt, aminek más a revíziója, mint az eszközé — akkor is, ha a kettőnek semmi köze egymáshoz (napló: `USB Input Device ['REV_0111'] <- rt640x64.inf [PCI\VEN_10EC&DEV_8225…]`, egy USB-egér vs. egy PCI-s hálókártya INF-je). **Mérve:** a lánc záró újrakötés-köre **3 másodperc alatt 70 015 sort** írt, a 15 MB-os napló (5 MB × 3 fájl) teljesen megtelt, és a **lánc TELJES története kiesett a rotációból** — a naplóban 256 értelmes sor maradt, csak a záró jelentés. A feltöltött Drive-napló is ez az elárasztott fájl. A fejlesztői gép ~730 csomagos DriverStore-ján a régi kód egyetlen körben **945 568** ilyen sort ír, az új **3**-at. **Javítás:** a REV-ütközést csak egy EGYÉBKÉNT ILLESZKEDŐ párra nézzük (és csak akkor naplózzuk); a döntés semmit nem változott (élőben összevetve 133 eszközön: egyetlen eltérő döntés sincs, ami ebből a módosításból jönne). **Mit rontottam el:** az új ellenőrzést egy dupla ciklus BELSŐ ágába tettem, és nem mértem, hányszor fut. **Az ellenőrzés, ami megfogta volna:** egy új naplósor hurokban → egy élő futás, és a sorok MEGSZÁMLÁLÁSA (`grep -c`) — ez a fájl Rule 0-ja szó szerint ezt tiltja („hot loops must NOT log per iteration"), és a szabály már benne volt.

**EGY GYANÚ, AMIT A MÉRÉS ELVETETT:** az INF neve `rtump64**arm64**sta.inf`, ami első pillantásra rossz architektúrának látszik egy x64 gépen. A DriverStore mappaneve viszont **`_amd64_`** — vagyis a Windows amd64-ként stage-elte, tehát az INF tartalmaz amd64 szekciót. **Az INF NEVÉBŐL nem szabad architektúrára következtetni**; a Windows saját verdiktje (a mappanév) a bizonyíték. A valódi ok a revízió volt, nem az architektúra.

#### 8. A „nem létező jelölőnégyzet" HATODIK példánya — ezúttal a NAPLÓBAN

```
[AUTOFIX-CAT] 3 tároló-eszköz kihagyva a katalógus-zárókörből (a fix indításakor nem volt engedélyezve)
```

A 2026-09-07-i kör a **képernyő-szövegeket** javította, a `wu_core.filter_autofix_risky_devices` **naplósorát** nem — a docstringjével és az `allow_*` ág *„A felhasználó ENGEDÉLYEZTE"* WARNING-jával együtt. Ez a sor most annál is fontosabb, mert a 4. pont óta ez a **napló egyetlen** forrása erről a döntésről. Az `allow_*` ág szövege külön is téves volt: oda ma **csak hívási hibával** lehet eljutni (a felületen nincs kapcsoló, a hívók közös kapuja `False`-ot ad), tehát a *„felhasználó engedélyezte"* megfogalmazás épp a hívási hibát fedte volna el — most `FIGYELEM … ez hívási hiba`. Ugyanebben a körben **három elavult komment** is javítva (`wu_core` ×2, `hwscan` ×1), amik még *„a felhasználó jelölőnégyzettel engedélyezheti"*-ként írták le a 2026-09-02 óta rögzített szabályt.

#### AMIT A VIZSGÁLAT KIZÁRT — két lelet, ami IGAZAT mondott

Mindkettőnél kézenfekvő volt a „javítás", és mindkettő téves lett volna:

- **`[PREVIEW] Van Wi-Fi kártya, de EGYETLEN third-party csomag sem illeszkedik rá (védett INF-ek: ['netwtw06.inf'])`** — önmagával ellentmondónak *látszik* (a védett lista nem üres). Valójában a `netwtw06.inf` a **Win10 inbox** Wi-Fi drivere az Intel 8265-höz, tehát tényleg nincs rá third-party csomag; a lánc végén a DUPDRV egy **új, third-party** `netwtw`-t takarított, ami ezt megerősíti.
- **`Microsoft Usbccid intelligenskártya-olvasó (WUDF)` KÉTSZER a health reportban** — a Dell ControlVault (`USB\VID_0A5C&PID_5834`) kompozit eszköz **négy interfésszel** (`MI_00`…`MI_03`), amiből kettő azonos nevű kártyaolvasó. **Két külön eszköz**, a felsorolás helyes.

Ugyanez a szabály, amit a színprofil-visszavonás és a T580-tapipad óta a fájl őriz: **külön kell választani, amit a kód bizonyíthatóan tesz, attól, amit egy tünet sugall.**

### KILENC JAVÍTÁS HÁROM TEREPI NAPLÓBÓL (2026-09-07, Build 301/302 — Lenovo 20LAS25J00, Dell Latitude 5520, ASRock B450M-HDV)

Mindhárom lánc **végigment** (16 / 34 / 16 driver, `A FOLYAMAT SIKERESEN BEFEJEZŐDÖTT`), a naplókban mégis 3 ERROR és 102 WARNING volt. A felhasználó kérésére mind a kilenc lelet javítva. **A közös tanulság: a program szinte mindent JÓL csinált, de több helyen ROSSZ OKOT mondott** — és ez a projektben ugyanolyan súlyos hiba, mint a rossz működés, mert a rossz ok rossz nyomra viszi a következő vizsgálatot.

**1. A záró jelentés NEM LÉTEZŐ jelölőnégyzetre küldött** (`app/gui/autofix.py`, mind a 3 naplóban). A szöveg: *„Ha mégis kellenek, indítsd újra a fixet a megfelelő jelölőnégyzettel."* — csakhogy a tároló/firmware kapcsolók **2026-09-02 óta nem léteznek**. Ez ugyanannak a szövegnek a MÁSODIK példánya volt: a kézi szken `hw-risk-note`-jánál már javítottuk, itt kimaradt. Az új szöveg kimondja, hogy ez a program rögzített szabálya, és a valódi teendőt adja (gyártói oldal, kézzel). **Tanulság: ha egy döntés megszünteti egy felületi elem létezését, `grep`-elj rá a TELJES szövegkészletben — egy elem, több szöveg.**

**2. `⚡ Energiaséma: "KiegyensŁlyozott"` — mojibake a záró jelentésben** (`app/powerplan_core.py`; 2 naplóban, mindkettőnél magyar sémanévvel — a harmadikban angol volt, ezért nem tűnt fel korábban). A `powercfg` **natív exe**, tehát a konzol OEM kódlapján ír (magyar Windowson cp852), a `_run` viszont utf-8-cal dekódol; a `ps_force_utf8` itt szándékosan nem segít, mert az CSAK a PowerShell `-Command` hívásokra vonatkozik. Megoldás: a szöveget visszaadó `powercfg` hívások a Python **`'oem'` kodekjével** futnak (`_run_powercfg_text`), ami pontosan a konzol aktuális kódlapját használja, tehát gépfüggetlen. Élőben mérve: nyers bájt `Teljes\xa1tm\x82nycentrikus` → `'Teljesítménycentrikus'`. A GUID ASCII, azt semmi nem rontotta el — **csak a NÉV volt hibás, a működés végig helyes**. Ha a `run_fn` nem fogad `encoding`-ot (teszt-csonk), a hívás visszaesik a sima útra: a név mojibake marad, de semmi nem törik el.

**3. A DISM „foglalt" hibáját PARZOLÁSI hibaként jelentette** (`app/drivers_core.py`, Lenovo, 2×). A DISM világosan megmondta: `The specified image is currently being serviced by another DISM operation` — a program viszont ezt írta: *„/English hiányzik vagy változott a formátum?"*, és a felület **`0 driver`**-t mutatott. Három javítás: `dism_failure_reason` felismeri az ismert DISM-hibákat (foglalt / nem admin / futó Windowsra mutató `/Image` / rossz útvonal), a foglaltságot `_run_dism_list` **kivárja** (3×4 mp — egy másik DISM tipikusan másodpercek alatt végez, és gyakran a saját másik szálunk az), és ha a lista így is üres marad, a felület **kimondja az okot** (`problems` lista → `drivers_loaded.error`). **Tanulság: az üres eredmény és a sikertelen lekérdezés két különböző dolog, és a felületen sem szabad egyformán kinéznie.**

**4. A watchdog kilőtte az ÉPP FUTÓ Intel Display driver telepítését** (`app/wu_core.py`, Dell). `10:55:18 [TELEPITES] Intel Display (32.0.101.7088)` → `11:25:18 [ERROR] 1800s óta nincs kimenet` → taskkill. A kör 22. és 23. csomagja kimaradt, +30 perc veszteség — és a **következő** körben ugyanaz a csomag **68 másodperc alatt** felment. A némaság ugyanis nem hiba jele: a WU-script a csomagok KÖZÖTT ír sort, telepítés közben nem. A javítás ugyanaz a *stillness clock* elv, ami a stressz-automatizálásban már bevált: a kilövés előtt megnézzük a Windows **saját** driver-naplóját (`setupapi.dev.log` mérete+mtime, `setupapi_progress_signature`), és amíg az változik, a telepítés HALAD → várunk tovább. Védőháló: `WU_HANG_HARD_TIMEOUT` = 90 perc, azon túl akkor is ölünk. **A `progress_probe` alapértelmezése maga a jel-függvény, nem `None`** — így mind a három fogyasztó automatikusan megkapja, és nem fordulhat elő, hogy egy hívási hely lemarad róla. A hosszabbítás nem néma: `on_notice`-on át kiírjuk, hogy a program vár, nem fagyott le.

**5. Öt letöltési próbálkozás 52 MÁSODPERC alatt elégett** (`app/gui/hwscan.py`, Lenovo, 236 MB-os csomag). A hálózat-jóváírás mechanizmusa működött, csak a **leggyakoribb tünetet nem ismerte fel**: a `net_err` mintái közül (`getaddrinfo`, `connection`, …) egyik sem szerepel a saját `csonka letöltés: X/Y byte jött le` üzenetünkben, tehát minden fél letöltés **elégetett** egy próbálkozást. A napló ritmusa ezt mutatta: csonka (nincs jóváírás) → getaddrinfo (jóváírás) → csonka → getaddrinfo → csonka → vége. Két javítás: a csonka letöltés is hálózati hibának számít, ÉS a jóváírás már nem csak a teljesen halott hálózatra jár — mert csonka letöltésnél a `_check_internet` jellemzően SIKERÜL (a vonal „él", csak instabil), tehát épp a valódi eset maradt ki. Instabil vonalnál `CATALOG_DL_UNSTABLE_WAIT` = 20 mp szünet a régi 3 helyett.

**6. A nyomtató-csomagok törlése elbukott, PEDIG a technikus kifejezetten kérte** (`app/drivers_core.py` + `app/gui/autofix.py`, Lenovo 4 db, ASRock 1 db). A naplóban `09:44:45`-kor kikapcsolta a nyomtató-védelmet, tehát törölni akarta őket. A pnputil kimenete két sorban mondja meg, mi történt: `Driver package uninstalled.` + `Failed to delete driver package: Legalább egy olyan eszköz van telepítve jelenleg, amely a megadott INF fájlt használja.` (`0xE000023D`) — vagyis a **`/uninstall` LEFUTOTT**, csak a csomag maradt, mert a nyomtatósort a **Spooler** tartja életben. Megoldás a temp-takarítás bevált mintája szerint: `delete_blocked_in_use` felismeri az esetet, a csomagok egy **második körbe** kerülnek, a Spoolert **egyszer** állítjuk le az egész kötegre, és a visszaindítás **`finally`-ben** van. **Egy Spooler nélkül visszaadott gép nem tud nyomtatni — az sokkal rosszabb, mint egy megmaradt driver-csomag** —, ezért a sikertelen visszaindítást a képernyőn is kimondjuk, a kézi paranccsal együtt. Ez NEM új szűrő a törlésben (amit a CLAUDE.md tilt), hanem épp fordítva: segít törölni azt, amit eddig nem sikerült.

**7. A javíthatatlan regresszió nem került a záró TEENDŐ-listába** (`app/gui/autofix.py`, ASRock). A lánc törölte az `oem6.inf`-et (AMD 24.10.0.1) a `High Definition Audio Controller`-ről, a visszakötés nem sikerült (a csomag már nem volt a gépen), és a program ezt menet közben ki is írta — a végén olvasott `🏭 … Windows-alapdriveren maradt` lista viszont **csak a monitort** tartalmazta. Az ok szerkezeti: a `_health_report_worth_listing` épp az ilyen eszközt szűri ki (nincs hozzá stage-elt csomag), vagyis a legfontosabb sor esett ki. Az elveszett gyári driverek ezért `_note_lost_vendor_driver`-rel gyűlnek, és **MINDIG, szűrő nélkül, külön szekcióban** jelennek meg, a lista ELEJÉN — mert ez az egyetlen kategória, ahol a gép a fix miatt lett rosszabb. Duplikáció ellen a `worth` listából kiesnek. **Tanulság: egy szűrő, ami a „van-e teendő?" kérdésre válaszol, nem alkalmazható arra az esetre, ahol a teendő maga a veszteség.**

**8. A WU-keresés hibakódja elveszett a saját zajában** (`app/gui/hwscan.py` + `app/wu_core.py`, Dell). A stderr-ben ott volt a `0x80240438`, a program viszont ennyit mondott: *„időtúllépés vagy WUA hiba"*. Két külön baj: a `Write-Error` a hibás parancs **kontextusát** is kiírja, ezért a teljes szkript bekerült a naplóba (~30 sor zaj, a lényeg a végén), és a Python oldal nem is kereste a kódot. Javítás: a szkript `[Console]::Error.WriteLine("WUERROR|0x…|üzenet")`-tel ad vissza strukturált hibát (élőben mérve: **9 sor → 1 sor**), a `wu_search_error_text` pedig kiolvassa — a régi, `Write-Error`-os alakból is, hogy a korábbi naplók is elemezhetők maradjanak. **A magyarázat-tábla szándékosan rövid**: csak az szerepel benne, amit a gépen meg is mértünk (`certutil -error`: a WinHTTP-tartomány feloldható, a WU-specifikus `0x8024xxxx` kódokat sem a certutil, sem a .NET nem oldja fel) — a többire a kód + „ez a WU ügynöke, nem a hálózat" megy ki. Kitalált jelentést nem írunk be.

**9. A `_wait_for_internet` iterációnként naplózott** (`app/gui/base.py`). Egy 45 mp-es várakozás ~50 azonos DEBUG sort írt (2 hosztnév × ~24 próba), kétszer egymás után — ez a Rule 0 **saját ellen-szabályába** ütközik („hot loops must NOT log per iteration"). Most csak az ELSŐ próba naplóz soronként (abból derül ki, MI a hiba), a többi `quiet=True`, és a végeredményt a meglévő összefoglaló sorok mondják ki.

**KÉT HIBA, AMIT A SAJÁT TESZTJEIM FOGTAK MEG, mielőtt kiment volna** — mindkettő arra példa, hogy a szintaxis-ellenőrzés kevés: (a) a `wu_search_error_text` táblája SOSEM talált volna, mert a `.upper()` a `0x` előtagot is `0X`-re alakítja (az offline teszt kapta el); (b) a hibakód-formázás `[uint32]($h -band 0xFFFFFFFF)` alakja **elszáll**, mert a PowerShell a `0xFFFFFFFF` literált int32-ként **-1**-nek veszi, így a maszk nem csinál semmit — az élő próba kapta el, és épp azt a kontextus-zajt gyártotta volna vissza, ami miatt a blokk átíródott. A helyes alak a sima `('0x{0:X8}' -f $h)`: a .NET a negatív int32-t is kettes komplemensben formázza.

### HÁROM JAVÍTÁS EGY HIBÁTLAN LÁNCBÓL (2026-09-07, Build 303, ASRock B450M Pro4)

**A lánc maga kifogástalan volt**, és ezt érdemes előre kimondani: 24 perc 52 mp, 5 láb, **15 driver**, **0 ERROR**, 18 WARNING (mind ártalmatlan, egyenként ellenőrizve), a végén **0 hibakódos eszköz**, regresszió nincs, és az alaplapi **hang ÉS LAN is gyári drivert kapott** (`Realtek - MEDIA - 6.0.9136.1`, `Realtek Net 10.79.50.1003`) — a 09-04-i mérés megismételve. A `_catalog_supported_hwids` előszűrő lábanként **12 csomagot zárt ki letöltés nélkül** az RTX 3060-ra, ami önmagában több GB megspórolt forgalom. **A három javítás mégis ebből a naplóból jött: egy hibátlanul lefutó lánc is mondhat valótlant a képernyőn, és írhat használhatatlan naplót.**

**1. A NEM LÉTEZŐ JELÖLŐNÉGYZET HARMADIK ÉS NEGYEDIK PÉLDÁNYA** (`app/gui/autofix.py`, a WU-kör és a katalógus-zárókör `risky_skipped` sorai). A képernyőn: *„🛡️ 5 tároló-eszköz kihagyva **(a fix indításakor nem engedélyezted)**"* — csakhogy a kapcsoló **2026-09-02 óta nem létezik**, tehát nincs mit „nem engedélyezni", és a mondat egy meg nem hozott döntést magyaráz. A lánc **18-szor** írta ki, a 324 folyamatsor **5,5%-a**. Ugyanezt a szöveget a záró jelentésnél és a kézi szken `hw-risk-note` dobozánál 2026-09-07-én **már javítottuk** — ez a kettő kimaradt. **A tanulság szó szerint ugyanaz, amit az aznapi 1. javítás már leírt, és mégis megismétlődött: ha egy döntés megszüntet egy felületi elemet, `grep`-elj rá a TELJES szövegkészletben, mert egy elemhez több szöveg tartozik.** Ugyanebben a körben három elavult **komment** is javítva (`wu_core.py` 2×, `app/gui/hwscan.py` 1×), amik még „a felhasználó által ki nem engedett kockázati kör"-ként írták le azt, ami rögzített programszabály — a fájl eleji 3. szabály szerint az elavult, magabiztos szöveg veszélyesebb, mint a hiányzó.

**2. A NAPLÓ 73%-A HUROKBAN ÍRT `[CALL]` ZAJ VOLT — mérve, nem becsülve** (`app/common.py: _CALL_LOG_EXCLUDE`). A mai 3,4 MB megoszlása:

| függvény | KB | sor | megjegyzés |
|---|---|---|---|
| `_rebind_pkg_ids` | 797 | **4011** | mindössze **20 egyedi INF-re** |
| `_catalog_rows_cache` | 538 | 2236 | a teljes gyorsítótárat vitte visszatérésként |
| `_catalog_find_driver` | 440 | 1002 | a teljes driver-táblát vitte argumentumként (27 027 kar.) |
| `_catalog_fetch_rows` | 260 | 1792 | |
| `_catalog_detail_page` | 255 | 902 | a teljes HTML-t vitte visszatérésként |
| `_catalog_supported_hwids` | 187 | 888 | |
| `_staged_vendor_inf_for` + `_autofix_stats_path` | 71 | 300 | |

**A kár nem elméleti: a napló A LÁNC KÖZBEN fordult át** (21:17:15, a záró újrakötés-kör közepén), és a záró 2 perc **egymaga 1,34 MB**-ot írt — vagyis egy következő futás után ennek a láncnak a törlési fázisa már kiesne a rotációból, épp az a rész, amiből hibát keresünk. A kizárás után ugyanez a napló **3419 KB / 16 346 sor → 871 KB / 5215 sor (−74,5% / −68%)**, az 5 MB-os ablak tehát ~1,5 lánc helyett ~6 láncot tart el.

**A KIZÁRÁS ELŐTT MINDEGYIKNÉL ELLENŐRIZVE, HOGY A DÖNTÉS MEGMARAD** — ez a lépés nem hagyható ki, mert a `_catalog_fetch_rows` kommentje kifejezetten a `[CALL]`-rétegre hivatkozott mint a nulla találat bizonyítékára (az a mondat most javítva). A szűrt naplóban változatlanul ott van: **896** `Keresés:` sor (minden lekérdezett HWID, az eszköz nevével), **52** `Döntés:`, **674** `Gyorsítótárból:`, **256** `KIZÁRVA letöltés előtt`, **76** `[REBIND]`, **239** `Kihagyva`. Vagyis a nyomon követhetőség nem csökkent, csak a zaj tűnt el. **Ha ezek bármelyike eltűnik a kódból, a kizárást is felül kell vizsgálni** — a `_CALL_LOG_EXCLUDE` kommentje ezt tételesen felsorolja.

**EGY DOLGOT VISZONT PÓTOLNI KELLETT: a `[CALL]`-réteg a KIVÉTELEKET is naplózza, teljes veremképpel.** A `_catalog_find_driver` **szálban** fut, és a hívó csak egy egysoros DEBUG üzenetben nyelte el a hibáját (`[CATALOG] Hiba: <név> - <e>`) — vagyis a kizárás önmagában **információt vett volna el, nem csak zajt**, méghozzá pont abban a mintában, ami ennek a projektnek a legdrágább hibája volt (2026-09-01, `NameError: title`: a kivétel elnyelődött, az eszköz se a sikeres, se a sikertelen listára nem került, csak eltűnt). A hívó kivétel-ága ezért **WARNING + `exc_info=True`** lett, az eszköz nevével. Nem hamis riasztás: a hálózati hibákat a `_catalog_fetch_rows` már elkapja, ide csak valódi rendellenesség jut el. Élőben ellenőrizve az EREDETI hibával (`NameError: title` beinjektálva): a WARNING, az eszköz neve és a `Traceback` mind kimegy. **Általános szabály a következő kizáráshoz: mielőtt bármit kiveszel a `[CALL]`-rétegből, nézd meg, hogy a hívója hogyan kezeli a KIVÉTELT — ha elnyeli, előbb azt kell megerősíteni.**

**3. A SPOOLER-LEÁLLÍTÁSOS ÚJRAPRÓBÁLKOZÁS MÉRT HATÁRA — a 09-07-i 6. javítás feltevése NEM általános** (`app/drivers_core.py`, `app/gui/autofix.py`). A mechanizmus szerkezetileg hibátlanul futott (felismerés, egyszeri leállítás a kötegre, `finally`-s visszaindítás — a naplóban `net stop Spooler` → `net start Spooler`, pontosan egyszer), **és mégsem segített**:

```
net stop Spooler -> The Print Spooler service was stopped successfully.
pnputil /delete-driver oem0.inf /uninstall /force
   Driver package uninstalled.
   Failed to delete driver package: One or more devices are presently installed using the specified INF.
```

Az ok: a `prnms009.inf` / `prnms006.inf` csomagot **nem a szolgáltatás fájl-zárolása tartja, hanem a nyomtatósor `SWD\PRINTENUM\{...}` ESZKÖZ-CSOMÓPONTJA**, ami a szolgáltatás leállítása után is bejegyezve marad a PnP-ben. Ez a kettő a Windows saját virtuális nyomtatója (Print to PDF / XPS Document Writer), amit a Windows amúgy is visszatesz — **a megmaradásuk ártalmatlan**. A kör ezért **MARAD** (ahol tényleg a szolgáltatás az akadály, ott megoldja), de a 2. kör bukása mostantól **külön kimenetel**: a napló és a képernyő is kimondja, hogy eszköz-csomópont tartja, nem a szolgáltatás. **NEM megoldás a nyomtatósor-eszköz eltávolítása**: az az ügyfél nyomtatóját venné ki a gépből, amit a technikus nem kért — a nyomtató-védelem egész indoklása pont ez. Offline tesztelve mind a három ágra (2. kör sikeres → nincs magyarázó sor; in-use → van; más hiba → sikertelen-lista igen, magyarázat nem), a terepi napló **szó szerinti** pnputil-kimenetével.

**A ZÁRÓ ÚJRAKÖTÉS + ÚJRAINDÍTÁS SZÁNDÉKOS — NE „OPTIMALIZÁLD" EL (explicit user decision, 2026-09-07).** Ugyanebből a naplóból felvetettem, hogy a kör két **USB kompozit szülőt** (`USB\VID_041E&PID_3274` → `ksusbaud.inf`, `USB\VID_1532&PID_00B9` → `rz00b9dev.inf`) rúgott meg, mindkettőnél `pnputil /add-driver ... /install` → **259 (no-op)**, majd `remove-device` + egy extra újraindítás — miközben a fájl saját szabálya szerint a kompozit szülő helyes drivere az `usb.inf` (a gyári driver a `&MI_xx` gyerekekre megy, ezért létezik a `wu_core.is_composite_parent`), tehát a csomag **nem is köthet rá**. **A felhasználó ezt ismerve döntött úgy, hogy a kör marad, ahogy van**, szó szerint: *„nem felesleges, direkt így akarom hogy ami nem gyári driveren megy még azt kösse újra a program, indítsa újra a gépet, majd miután újraindította egy tiszta lappal… még azért leellenőriz mindent végigmegy mindenen, és ha minden oké akkor mehet a store scan és vége"*. A záró láb tehát **terv szerint** egy tiszta boot utáni teljes ellenőrző kör, nem pazarlás. **Az `is_composite_parent` ezért NEM került be a `rebind.py` jelöltszűrőjébe** — ha egy jövőbeli session ezt „javítani" akarja, ez a bekezdés a válasz.

### NYOLC JAVÍTÁS EGY 0 HIBÁS LÁNCBÓL — amikor a program JÓL DOLGOZIK, de ROSSZUL SZÁMOL BE RÓLA (2026-09-08, Build 304, ASRock B450M Pro4)

**Ez a szekció a projekt egyik legfontosabb tanulságát rögzíti: egy hibátlanul lefutott lánc naplója is tele lehet javítanivalóval — csak nem a működésben, hanem abban, amit a program a technikusnak MOND.** A lánc maga kifogástalan volt: 22:58:22 → 23:12:12, **13 perc 32 mp, 15 driver, 5 újraindítás, 0 ERROR, 0 kivétel, 0 időtúllépés, 0 nem várt visszatérési kód**, a végén 0 hibakódos eszköz, és az alaplapi hang ÉS LAN is gyári drivert kapott. A 14 WARNING mind ismert/ártalmatlan. **Mégis nyolc javítás jött ki belőle**, és közülük az egyiknek valódi működési következménye is volt.

**A KÖZÖS MINTA MIND A NYOLCBAN: a képernyőn megjelenő szöveg nem azt mondta, ami a naplóban történt.** Ez ebben a projektben nem kozmetika — pontosan ezek miatt hitte a technikus korábban azt, hogy *„sose lesz minden naprakész"*, miközben a program jól dolgozott.

**1. A PROGRAM A SAJÁT, SIKERES MUNKÁJÁT JELENTETTE HIÁNYOSSÁGKÉNT.** Ez a legsúlyosabb. A napló ugyanabból a láncból:

```
23:05:38  ✅ High Definition Audio Device telepítve!            (Realtek MEDIA 6.0.9136.1)
23:06:08  ✅ ...: gyári driver működik (eddig Microsoft alapdriver volt).
--- a KÖVETKEZŐ láb, UGYANAZ az eszköz, már a saját nevén ---
23:07:43  [CATALOG] ...: a gép saját kulcsán NINCS ÚJABB (6.0.9136.1 <= telepített 6.0.9136.1)
23:07:52  ⚠️ 1 eszközhöz a Microsoft katalógusában NINCS megfelelő csomag:
          • Realtek High Definition Audio
          Ezekhez a gyártó saját driver-oldaláról kell a driver...
```

Az eszköz **naprakész volt, a helyes ASRock-csomaggal, amit ez a lánc rakott fel rá öt perccel korábban** — a program mégis a gyártói oldalra küldte érte a technikust. A `nosource` ág nem különböztette meg a *„ehhez az eszközhöz nincs gyári driver"* esetet a *„már rajta van a gyári, csak újabb nincs"* esettől, pedig a `drv['inbox_now']` mező ott volt a kezében. **Tanulság: minden „nincs hozzá csomag" állítás előtt fel kell tenni a kérdést, hogy MI VAN MOST AZ ESZKÖZÖN.**

**2. UGYANARRÓL AZ ESZKÖZRŐL KÉT KÉPERNYŐ MÁST MONDOTT.** A `PCI standard ISA bridge` a `machine.inf`-en fut, ami rajta van a `HEALTH_REPORT_SKIP_INFS` listán — vagyis a záró egészségjelentés **szándékosan meg sem említette** (nincs teendő). A katalógus-kör viszont ugyanabban a futásban `⚠️ … NINCS megfelelő csomag … menj a gyártó oldalára` teendőt írt ki rá. Ugyanaz a *„két lista ellentmond egymásnak ugyanarról a gépről"* hiba, amit az AOC-monitor esete után ez a fájl már rögzít. A javítás: a záró figyelmeztetés **ugyanazt a szűrőt** használja, mint az egészségjelentés — `wu_core.no_source_is_actionable` (tiszta függvény, offline tesztelve 7 esetre). **A ki nem írt tételek nem tűnnek el**: a naplóba nevesítve, okkal mennek ki, és a tételes mérleg a képernyőn is felsorolja őket.

**3. „…már felment, de az eszköz nem vette át" — A CSOMAG EL SEM INDULT.** A `↷ 2 csomag kihagyva: egy korábbi körben már felment…` sor **mindkét tétele INF-vétós Realtek-csomag** volt, amit a program letöltés után elvetett — soha nem „ment fel". A `tried_keys` három, gyökeresen különböző esetet gyúrt össze (INF-vétó / nem kötött rá / ebben a láncban már próbáltuk), és a szöveg kettőre valótlan volt. **Ugyanennek a függvénynek a saját kommentje 2026-09-03 óta „valótlan"-nak nevezi ezt a mondatot** a letöltési hiba esetére — csak az INF-vétós ág maradt ki az akkori javításból. Az új szöveg okonként bont (`GuiAutofixMixin._catalog_skip_text`, tiszta staticmethod, offline tesztelve).

**4. ROSSZ DARABSZÁM: csomag ≠ eszköz.** `📎 3 eszközhöz volt ugyan csomag…` alatt **2 eszköz** állt (a hangkártya két külön Realtek-csomaggal). A `len(wrong_pkg)` csomagokat számolt, „eszköz" felirattal — vagyis nagyobbnak mutatta a hiányt, mint amekkora.

**5. A DUPLIKÁTUM-TAKARÍTÁS NEM MONDTA MEG, MIT TÖRÖLT (Rule 0 sértés).** A napló ennyi volt: `[DUPDRV] 2 duplikátum-csoport, 2 törölhető régi verzió` → `pnputil /delete-driver oem5.inf` → `✅ oem5.inf törölve (1/2)`. **Az `oemNN.inf` szám önmagában semmit nem árul el, ráadásul újratelepítéskor átszámozódik** — egy „hova lett a driverem?" bejelentés a naplóból megválaszolhatatlan lett volna. Az adat végig ott volt a `groups`-ban. Mostantól minden törlendő csomag WARNING-gal, **név szerint** megy ki a művelet ELŐTT (eredeti INF, gyártó, verzió, dátum + hogy MI marad meg helyette), és a képernyő-szöveg is megnevezi (`dupdrivers_core.duplicate_delete_details`).

**6. UGYANAZT A CSOMAGOT HATSZOR-HARMINCÖTSZÖR KÉRDEZTÜK LE — és ennek VALÓDI MŰKÖDÉSI KÖVETKEZMÉNYE IS VOLT.** A katalógus egy csomagot több GUID alatt publikál (ezt a fájl az NVIDIA 25 bejegyzésénél már rögzíti), a `rows_by_guid` viszont GUID szerint dedupál, tehát mind külön jelöltként került a letöltés-előtti ellenőrzésbe, **mindegyik saját részletlap-letöltéssel**. **Élőben újramérve** (2026-09-08, ugyanazokon a kulcsokon, amiket a lánc használt):

| kulcs | bejegyzés | tényleges csomag | eddigi lekérdezés | most |
|---|---|---|---|---|
| `PCI\VEN_1022&DEV_148A` (AMD PCI) | **75** | **4** (35×+24×+12×+4×) | 12 (a teljes keret) | **4** |
| `PCI\VEN_10DE&DEV_2504` (RTX 3060) | **50** | **3** (32×+14×+4×) | 12 (a teljes keret) | **3** |

Három baj, és a második **nem pazarlás, hanem helyességi kockázat**: (a) 12 részletlap-letöltés 3-4 tény megtudásához — mérve **24 mp eszközönként, LÁBANKÉNT** (a részletlap 44-86 KB, ~2 mp); (b) **elfogyasztotta a `CATALOG_HWID_PROBE_MAX` = 12 keretet néhány valódi csomagon**: a 75 bejegyzésből az első 12 akár mind ugyanaz a csomag lehetett, a maradék 63 pedig **ellenőrzés nélkül** ment tovább (`maradek`) — vagyis a nyertes lehetett olyan sor, amit meg sem néztünk; (c) 256 naplósor / 52 KB, a legrosszabb **48× szó szerint azonos**.

**A DEDUP KULCSA (cím, dátum), ÉS EZ BIZONYÍTOTTAN BIZTONSÁGOS:** négy csoportban 3-3 testvér-GUID részletlapját összehasonlítva a támogatott-azonosító lista **mindig azonos** volt (5, 5, 15, illetve 70 azonosító) — a lista a **csomag** tulajdonsága, nem a bejegyzésé. A verdikt ezért a testvérekre is érvényes, miközben a sorrend és minden későbbi lépés (URL-feloldás, letöltés) **változatlanul az összes GUID-dal dolgozik**: nem veszítünk jelöltet, csak kérést spórolunk.

**ÉS UGYANEZ A DUPLIKÁCIÓ MÉG HÁROM HELYEN OTT VOLT — a javítás közben derült ki, offline teszttel.** Ha egy jövőbeli session hasonlót javít, ezt a négyes listát nézze végig:
1. **a támogatott-azonosító próba** (a fenti, ez volt a kiindulás);
2. **a holtverseny-döntő `Driver Model` próba** (`CATALOG_MODEL_PROBE_MAX` = 10): a holtverseny definíció szerint azonos dátumú, tehát a csomagot a CÍME azonosítja — az `NVIDIA Display Driver Update (32.0.15.9595)` viszont **32 GUID alatt** áll, vagyis a program akár **10 azonos tartalmú lapot** kérdezett le. Most csomagonként egy;
3. **a tartalék-lista (`alt_candidates`)**: szűretlenül mind a 3 (inbox eszköznél 6) hely kitöltődhetett UGYANANNAK a csomagnak a testvéreivel. A telepítő ezt a letöltési URL alapján felismeri („ugyanarra a csomagra mutat") és nem tölti le kétszer — **de addigra a testvér elvette a helyet a következő VALÓDI jelölt elől**, azaz pont a tartalék-mechanizmus lényege veszett el. Most csomagonként egy tétel;
4. **a bizonyítottan KIZÁRT csomagok visszakerültek tartaléknak.** A tartalék-készlet a teljes `scored` halmazból épül, a hwid-próba `kizart` halmaza viszont lokális volt — vagyis ugyanaz a csomag, amiről az imént bizonyítottuk a gyártó SAJÁT listájával, hogy nem ide való, tartalékként visszajött, és a telepítő **le is töltötte volna** (hangnál 11 MB, videokártyánál 1,2 GB), hogy aztán az INF-vizsgálat még egyszer kimondja ugyanazt. `probe_kizart_guids` most mindkét tartalék-ágat szűri. **Ezt az offline teszt fogta meg, nem én** — a „2) a tartalékok is csak az illeszkedő csomag bejegyzései lehetnek" állítás bukott el rajta.

**ÉLŐ, VÉGPONTTÓL VÉGPONTIG FUTÓ ELLENŐRZÉS** (valódi WMI + valódi katalógus, HP EliteDesk 800 G2, telepítés nélkül) — minden eszköznél MINDEN valódi csomag belefér a keretbe, ami korábban nem volt igaz:

| eszköz | bejegyzés | tényleges csomag | lekérdezés (volt → most) |
|---|---|---|---|
| High Definition Audio hangvezérlő | 25 | 5 | 12 → **5** |
| Chipset SMBus - A123 | 24 | 7 | 12 → **7** |
| PCI Express Root Port #7 | 25 | 4 | 12 → **4** |
| Intel HD Graphics 530 | 22 | 7 | 12 → **7** |

A második futás ugyanezekkel a verdiktekkel **NULLA részletlap-letöltésből** ment le (a `catalog_hwids.json` tárból), a napló végén már nem is volt „kiírva" sor — ez a bizonyíték, hogy a tár fog.

**7. A RÉSZLETLAP-GYORSÍTÓTÁR CSAK MEMÓRIÁBAN ÉLT — a lábak viszont külön folyamatok.** Minden láb újraszedte ugyanazt az 50-70 lapot; a katalógus-zárókörök 74 / 27 / 19 / 14 mp-e szinte teljes egészében ez volt. Új: `catalog_hwids.json` (`CATALOG_HWIDS_TTL` = 7 nap), a `catalog_rows.json` mintájára, ugyanazzal a szabállyal — **csak sikeres, nem üres eredményt teszünk el**: a `None` (a lap nem jött le, vagy nincs rajta ilyen szekció) sosem kerül a tárba, különben egy hálózati hiba tartósan „nem eldönthető"-vé betonozná a csomagot. A raw HTML-t **szándékosan NEM** tároljuk (44-86 KB/GUID, 200 GUID = 10-17 MB), csak a kiparse-olt, apró azonosító-listát.

**8. AZ UPDATER 3× LEFUTOTT SIKERES, EGYÉRTELMŰ VÁLASZ UTÁN IS.** Mérve egy induláson: `22:58:23 Letöltött BUILD_NUMBER: 304, Helyi: 304 → Nincs újabb verzió.` majd ugyanez 22:58:26-kor és 22:58:29-kor → **`check_for_updates -> 6.30s`**. A 3 próbálkozás a Fastly CDN elavult példánya ellen született, de a ciklus a definitív válasz után is továbbment. Az új szabály **megtartja a védőhálót, de az árat nem fizeti**: azonos build → azonnal vissza (ez a nyereség); **kisebb** build a szervertől → gyanús (elavult CDN-példány vagy push előtti helyi verziószám-emelés) → újrapróbálás; hiba/nem parse-olható → újrapróbálás. Offline tesztelve mind a három ágra.

**KÉT ELAVULT, MAGABIZTOS KOMMENT IS JAVÍTVA** (a fájl eleji 3. szabály szerint ez a legveszélyesebb állapot):
- `hwscan.py`: *„az ASRock ugyanis egyáltalán nem publikál a Windows Update-re (ezt a CLAUDE.md már rögzíti)"* — a CLAUDE.md ezt **2026-09-02-én visszavonta**, és ez a napló is cáfolja (`a gyártó ehhez a géptípushoz … 25 csomagot publikál`, és a lánc fel is rakta a helyeset).
- `oemdrivers.py`: *„a gyári chipset-/audio-/LAN-driver EGYETLEN forrása az alaplap gyártójának oldala … terepen: ASRock B450M Pro4, ahol a Realtek hang a generikus hdaudio.inf-jén, a LAN pedig az inbox driveren futott a fix után is"* — **ugyanazon a gépen** újramérve mindkettő gyári drivert kapott (`23:05:38` / `23:05:54`). A kártya marad (a gyártói csomag gyakran frissebb, van hozzá vezérlőpult, és marad néhány eszköz katalógus-csomag nélkül), de **kiegészítésként, nem egyetlen forrásként**.

**AMIT ELLENŐRIZTEM ÉS SZÁNDÉKOSAN NEM JAVÍTOTTAM:** az újrakötés-kör két USB kompozit szülőt rúgott meg (`USB\VID_041E&PID_3274` ← `ksusbaud.inf [...&MI_00]`, `USB\VID_1532&PID_00B9` ← `rz00b9dev.inf [...&MI_02]`), **0/2 kötött vissza**, és a napló bizonyítja is, hogy a driver a GYEREK csomóponton él (`23:12:00 Egyezés: Razer Basilisk V3 X HyperSpeed [...&MI_02]`). A párosítás tehát strukturálisan nem köthet — **de ez a felhasználó kifejezett, 2026-09-07-i döntése**, lásd a fenti szekciót. Nem nyúltam hozzá.

### „SOSE LESZ MINDEN NAPRAKÉSZ" — négy külön hiba egy terepi naplóból (2026-09-03, HP EliteDesk 800 G2, Build 294)

Terepi jelentés: *„olyan mintha hiaba telepitgetnem a drivereket folyamatosan sose tudom azt elérni hogy minden naprakész legyen"*, *„a realtek hangkártya drivert felrakta már a program… mégis folyamatosan ajánlgatja"*, *„kiírja hogy hibás a videokartya driverem code 18 azt mondja telepítsem újra… de most telepitettem fel es mukodik"*. **Négy, egymástól független hiba adta össze ezt az élményt, és mind a négy a naplóból bizonyítható.** Ez a szekció azért részletes, mert mindegyik ugyanabba a hibaosztályba tartozik, amit ez a fájl végig üldöz: **a program némán mást csinált, mint amit magáról állított.**

#### 1. A tartós no-bind tár HALOTT volt — `_device_stem` kétszer futott ugyanazon az értéken

**A hiba.** `_no_bind_record` a MÁR letörzselt azonosítót menti (`{'pnp': k[0]}`, ahol `k[0] = _device_stem(...)`), az olvasó oldal viszont **mégegyszer** lefuttatta rajta. A régi `rsplit('\\', 1)[0]` egy egy-backslashes törzsből a puszta **buszneveet** csinálta:

```
írás   : HDAUDIO\FUNC_01&VEN_10EC&DEV_0221&SUBSYS_103C8054&REV_1001   (törzs, helyes)
olvasás: HDAUDIO                                                       (!!)
keresés: HDAUDIO\FUNC_01&VEN_10EC&DEV_0221&SUBSYS_103C8054&REV_1001   (a teljes pnp_id-ből)
```

A két kulcs **soha** nem egyezett. Élőben mérve a napló `known_no_bind={'PCI': [...]}` sora mutatja: a szótár kulcsa a busznév.

**Amit elrontott — három dolog egyszerre:** (a) a `prev_no_bind` jelölés sosem került fel, tehát a **bizonyítottan nem alkalmazható** csomagot a felület minden szken után **újra ELŐRE BEJELÖLVE** ajánlotta (ez a „folyamatosan ajánlgatja" panasz); (b) a bejegyzések nem deduplikálódtak — a gépen ugyanaz az Intel-bejegyzés **négyszer** szerepelt; (c) a sikeres kötés utáni **kivezetés** sem talált rá semmire, tehát a tár csak hízott, sosem tisztult.

**A javítás:** `_device_stem` az első **két** útvonal-elemet tartja meg, így teljes azonosítóra és törzsre ugyanazt adja — **idempotens**, akárhányszor futtatható.

**A TANULSÁG, ami általánosítható: egy normalizáló függvényt úgy kell megírni, hogy a saját kimenetére alkalmazva ugyanazt adja.** Ha ez nem teljesül, előbb-utóbb valaki kétszer futtatja, és a hiba **néma** lesz — itt egy egész kényelmi funkció halt meg tőle úgy, hogy semmi nem hibázott. A 2026-08-31-i eredeti stem-javítás helyes volt, csak nem gondolta végig, hogy az írás és az olvasás **ugyanazt** a függvényt hívja majd.

#### 2. A felületi előtag beszivárgott az azonosító kulcsba

A megjelenítéshez a katalógus-találat címe elé `MS Katalógus: ` kerül (`_catalog_find_driver`), a tartalék-jelöltek viszont a **nyers** címmel jegyződtek fel. Ugyanaz a csomag így **két kulcson** ült (élőben mérve mindkét változat ott volt a fájlban), tehát a dedup és a maszkolás is kettévált rajta. `_nb_title` most levágja az előtagot minden kulcs-számításnál. **Tanulság: egy megjelenítési előtag soha nem lehet része egy azonosító kulcsnak** — ha a kulcs és a kiírt szöveg ugyanabból a mezőből jön, az előbb-utóbb elválik.

#### 3. A program SAJÁT MAGA rontotta el a videokártyát — Code 18 (a takarítás a PnP-felderítés ELŐTT futott)

A napló, szó szerint:

```
13:20:01  4/9 INF illeszkedik erre a gépre - csak azokat telepítjük:
          ['cui_dch.inf', 'igcc_dch.inf', 'iigd_dch.inf', 'IntcDAud.inf']
13:21:04  ✅ Intel(R) HD Graphics 530 telepítve!
13:21:04  a csomag 4 INF-jéből 2 egyetlen jelen lévő eszközön sem használt
          - kivezetés a DriverStore-ból: ['cui_dch.inf', 'igcc_dch.inf']
13:21:05  pnputil /delete-driver oem21.inf   -> deleted
13:21:05  pnputil /delete-driver oem31.inf   -> deleted
13:21:05  pnputil /scan-devices              <- CSAK EZUTÁN!
```

A `cui_dch.inf` / `igcc_dch.inf` a DCH-driver **kísérő komponensei** (Intel Graphics Control Panel / Command Center). A hozzájuk tartozó `SoftwareComponent` eszköz-csomópontokat a Windows csak a PnP **újra-felderítéskor** hozza létre — a takarítás viszont **azelőtt** futott, tehát „egyetlen jelen lévő eszköz sem használja", és kivezette őket. Két másodperccel később a `/scan-devices` létrehozta a csomópontokat, amiknek addigra **már nem volt csomagjuk** → `Code 18: A drivert újra kell telepíteni`. A felület hibás eszközként jelentette, a technikus újratelepítette, a takarítás megint törölte: **végtelen kör, amit a program hozott létre a semmiből.**

**A javítás:** amit a `select_applicable_infs` kiválasztott, azt a takarítás **soha nem veheti ki** (új `selected_infs` paraméter). Miért ez a helyes szabály és miért nem elég a `/scan-devices` előrehozása: a szűkítés a **jelen lévő eszközök hardver-azonosítói** alapján döntött, tehát a program **már kimondta**, hogy ezek az INF-ek ehhez a géphez valók — ha utána a takarítás mégis kiveszi őket, a program **két helyen mond ellent önmagának**. A takarítás eredeti feladata (Razer-eset: 212 INF-ből 1 kell) ettől nem sérül: ott a szűkítés 1 INF-et választ, tehát nincs is mit kivezetni, a `sel is None` ág (nem eldönthető → csillagos telepítés) pedig változatlanul takarít — és pont az a Razer-eset.

**A TANULSÁG: ha két lépés ugyanarról a dologról hoz döntést, a KÉSŐBBI nem bírálhatja felül a korábbit anélkül, hogy tudná, mit döntött az.** És: **egy „jelen lévő eszköz használja-e?" kérdés csak akkor válaszolható meg, ha a PnP-stack már felderítette az eszközöket** — telepítés után ez nem teljesül.

#### 4. Egy 8 MÁSODPERCES DNS-KIESÉS egy egész telepítési kört vitt el

```
13:15:56  1/3 csonka letöltés: 45 194 494 / 338 956 520 byte jött le
13:16:00  2/3 <urlopen error [Errno 11001] getaddrinfo failed>
13:16:03  3/3 ugyanaz          ->  VÉGLEG sikertelen
```

Három próbálkozás fix 3 másodperces szünetekkel = **hét másodperc**, és pont a kiesés ablakába esett. Ugyanez a kiesés sorban megölte a köteg többi elemét is: **HD Graphics, Management Engine, SMBus, AMT SOL, Alaplap erőforrásai, HD Audio vezérlő** — hat eszköz, egyetlen pillanatnyi hálózati hibából, és a naplóból nézve mind „sikertelen csomag"-nak látszik, pedig a csomagokkal semmi baj nem volt.

**Két javítás, és a második a fontosabb:**

- **A hálózati hiba nem égeti el a próbálkozásokat.** `getaddrinfo`/timeout/kapcsolat-hiba esetén a program a meglévő `_wait_for_internet`-tel **megvárja a hálózatot** (ez DNS-t is ellenőriz — lásd a 2026-09-01-i javítást), és az a kör **nem számít bele** a keretbe. Korlátos: `CATALOG_DL_NET_REFUNDS` = 2 jóváírás, különben egy ingadozó kapcsolat végtelen körbe vinne.
- **A LETÖLTÉSI BUKÁS NEM „MEGPRÓBÁLTUK".** Az AutoFix a `catalog_done` listát **szándékosan a telepítés ELŐTT** írja ki, hogy egy crash után ne kezdjen elölről egy több száz MB-os letöltést — csakhogy egy le sem töltött csomag így ugyanúgy megjelöltnek számított, a lánc **többi lába kihagyta** az eszközt, ráadásul a képernyőn a **valótlan** *„már felment, de az eszköz nem vette át"* szöveggel. `_install_catalog_sync` ezért `_catalog_dl_failed`-be gyűjti a csak-letöltésen-bukott tételeket, és az AutoFix **visszavonja** rájuk a jelölést.

**Amit ez a vizsgálat KIZÁRT, és fontos kimondani:** a letöltési bukás **soha nem** számított sikeresnek, és **soha nem** került a tartós no-bind tárba (a `fail` számlálót növelte és `return`-nel kilépett) — tehát a **kézi szken** mindig újra felajánlotta. A hiba kizárólag a **láncon belüli** kihagyás volt. A felület most ki is mondja: *„Ez NEM azt jelenti, hogy a csomag rossz — le sem jött. A következő szkennelés újra felajánlja."*

**A TANULSÁG: külön kell választani a „bizonyítottuk, hogy nem jó" és a „el sem jutott hozzánk" állapotot.** Egy hálózati hiba nem mond semmit a csomagról, tehát nem hozhat létre tartós következményt. Ugyanez az elv máshol is: a `_check_internet` DNS-javítása, a WU-keresés időkorlátja, a katalógus-lekérdezés gyorsítótára — mind arra épül, hogy **hibás lekérdezést sosem teszünk el.**

#### 5. A tartalék-jelöltek KIKAPCSOLTAK, amint az eszköz gyári drivert kapott

**Ez az a hiba, ami a technikus szavaival így hangzott:** *„felajánlott a program egy realtek drivert, feltelepítettem, működik minden, utána kerestem újra, felajánlotta megint egy realtek drivert"*. A két kör között a **program viselkedése változott meg**, nem a gép:

- **1. kör** — az eszköz még a Windows `hdaudio.inf`-jén ült, tehát lefutott az inbox-ág (`if _is_inbox_driver(inst)`), ami a **legspecifikusabb kulcs felől** sorolta a tartalékokat → megtalálta a géphez való HP-csomagot → felment, működik. ✔
- **2. kör** — az eszköz **most már gyári driveren fut**, tehát `_is_inbox_driver` hamis, és a **tartalék-lista fel sem épült** (`alts` csak az azonos dátumú holtverseny-jelöltekből állt, itt: üres). Maradt a dátum-elsőségű nyertes, ami az **általános kulcsról** jött és **más gépgyártó változata** → INF-vétó → *„mind a(z) 1 katalógus-jelölt INF-je más eszközre való"* → „nincs való csomag". Közben a gép saját `&SUBSYS_103C8054` kulcsán **14 sor** várt, amiket a program soha meg sem nézett.

**A hiba oka egy összegyúrt szabály.** A 2026-08-26-i specifikusság-elsőségű tartalék-lista **két** dolgot csinált egyszerre, és mindkettő az inbox-feltétel mögé került:
- a **specifikusság szerinti sorrend** — ez **univerzálisan helyes**: egy sor, ami a gép saját SUBSYS-kulcsáról jött, definíció szerint ehhez a géphez való;
- a **régebbi kiadás elfogadása** — ez **tényleg csak inbox-eszköznél** helyes, különben visszalépés lenne.

Szétválasztva: gyári driveren futó eszköz is kap tartalékot a saját kulcsáról, de **csak a kiadás-kapun átmenő** (nem régebbi) sorokból. Downgrade így sem történhet, és a „felrakom → újra felajánlja → nem megy" kör megszűnik.

**Ugyanebből a felismerésből jött a másik javítás:** ha a legjobb pontszámú jelöltek **mind** ismerten megbukottak, a régi kód feladta (`if usable and len(usable) != len(cands)` — üres `usable` esetén a `cands` érintetlen maradt, tehát újra a rossz csomag nyert). Most **lejjebb lépünk a `scored` halmazban**, mert az a cél, hogy az eszköz kapjon esélyt — lásd a [minden eszköz kapjon drivert](#minden-eszköz-kapjon-drivert--a-tiltólista-soha-nem-megoldás-explicit-user-decision-2026-09-03) alapelvet.

#### Amit a napló még megmutatott, és nem volt hiba

- **`catOrder is not defined`** — a napló ötször tartalmazza. Ez a [nézet-átépítésnél](#a-driver-keresés-nézet-átépítése--öt-dobozból-egy-lista-2026-09-03) leírt, Build 288 óta élő `ReferenceError`, ami a telepített driverek listáját tüntette el. **Élő terepi megerősítése annak a javításnak** — a hiba nem elméleti volt.
- **`[INTEL] Gyári driver ellenőrzés sikertelen: HTTP 403`** — a napló **öt buildet** ölel át (290-294), és ezek a sorok a **2026-09-02 10:28**-as, Build 290-es futásból valók, tehát a videokártya-gyártói ág törlése ELŐTTRŐL. **Ez a fajta tévedés könnyen elkövethető: egy hosszú naplóban mindig ellenőrizd, MELYIK build írta a sort** (`grep "ELINDITVA (Build"`), mielőtt élő hibaként kezelnéd.
- **`pnputil /delete-driver` visszatérési kód `3758096957`** (0xE000023D) a duplikátum-takarításban: a csomag használatban van, a pnputil elutasítja. Ez a takarítás **tervezett** viselkedése (sima `/delete-driver`, se `/uninstall`, se `/force` — ha bármi használja, marad), nem hiba.

#### A „Kihagyott: N" magyarázata a képernyőn

Terepi kérdés: *„7 telepitendo van 5 sikeres 0 sikertelen a maradek 2 vel mi tortenik ilyenkor?"*. A szám önmagában megválaszolatlan kérdés volt, és a technikus joggal olvasta úgy, hogy „2 driver a levegőben maradt". A kör most kimondja, hogy a kihagyott tétel **nem sikertelen telepítés**: vagy időközben naprakésznek bizonyult, vagy a program bizonyította, hogy a csomag más gépgyártó változata — és hogy a következő szkennelés már **nem is ajánlja fel**, mert feljegyeztük. (Ez utóbbi az 1. hiba javítása nélkül **nem volt igaz** — pont ezért érezte a technikus végtelennek a kört.)

