# Terepi bizonyítékok — Termékelvek: elfogadási feltétel, törlés, minden eszköz kapjon drivert, nulláról újradriverezés

> A CLAUDE.md 2026-10-11 előtti teljes szövegéből **szó szerint áthelyezve**, eredeti sorrendben. A tömör, érvényes szabályok a CLAUDE.md-ben vannak; ha a kettő eltér, a CLAUDE.md a mérvadó, és ezt a fájlt is javítani kell. A szövegben lévő `(#…)` hivatkozások a régi egyfájlos szerkezetre mutatnak: a cél-szekciót `grep`-pel keresd a `docs/terep/` mappában.

### THE ACCEPTANCE CRITERION for the one-click fix: press it, walk away, come back to a finished machine

**Explicit user decision, 2026-08-25 — "EZ A LEGFONTOSABB". This is how every change to the AutoFix chain is judged.** The user's description of the product, verbatim: *"ahogy en ranyomok h 1 kattintásos fix, kimegyek cigizni kavezni 20 percre 1 orara, visszajovok, ott legyen elottem egy hibatlanul feldriverezett gep laptop barmi az uzenettel h sikeresen vegigment 0 hibaval, ne legyen benne hiba"*.

Unpack what that actually demands, because each clause is a hard constraint on design:

- **Zero interaction after the click.** The technician is *not at the machine*. Anything that waits for a human — a prompt, a dialog, a UAC consent mid-chain, a "click OK to continue", an installer that opens a window — is a broken chain, not a minor annoyance. This is why the resume legs are unattended, why every reboot is `shutdown /r /t 0 /f`, why the scheduled task must start on battery, and why a vendor installer may never be run interactively.
- **The machine is FINISHED when they return.** Not "mostly done", not "here are 6 packages you should install by hand from the vendor's site". A closing report that hands the technician a to-do list is the fix failing to do its job — the to-do list is the *last* resort, only for hardware that genuinely has no driver anywhere, and every such line must name the device and the exact remedy.
- **"0 hibával" is the pass condition.** The summary must be able to honestly say everything worked. That means silent false successes are worse than failures here (this file is full of them: `0xC0000142`, the Hungarian pnputil "törlése nem sikerült", the unchecked SumatraPDF return code, the `[kód=4]` false negatives). A chain that reports success while a device sits on a generic driver has failed the criterion even though nothing "errored".
- **Any brand, any machine.** Laptop, desktop, self-built PC, any vendor — see the universality rule below.
- **20-60 minutes is acceptable; a broken result is not.** Duration is a secondary concern the user tolerates. Correctness is not negotiable. Never trade a correctness guard away for speed — but equally, never waste the budget: it is the technician's coffee break, not an unlimited window.

When adding anything to the chain, ask first: *does this still work with nobody sitting at the machine, and can the machine still finish?* If the answer needs a human, it does not belong in the AutoFix — put it in the manual scan view instead.

### MINDEN DRIVERT LEHESSEN TÖRÖLNI — a törlésbe SOHA ne tegyél új szűrőt (explicit user decision, 2026-09-03)

> **HA EZT A FÁJLT MOST OLVASOD ELŐSZÖR, EZ AZ EGYIK LEGFONTOSABB SZABÁLY. Ha egy leletre az jut eszedbe, hogy „ezt a driver-osztályt inkább ne töröljük", ÁLLJ MEG. A válasz NEM. Nem firmware-re, nem tárolóra, nem semmire.**

**A felhasználó szó szerint, miután pontosan ezt a hibát elkövettem:** *„ami third party driver, ott jó ha töröl mindent, nem probléma ha nem megy vissza!!! ilyen ne legyen hogy a third party drivereknél szűrjön bármire is, ott mindig mutassa meg az összes third party drivert… mi van akkor ha a firmware driver miatt nem bootol a gép? Bebootolok egy másik windowsból és a büdös életben nem tudom kitörölni a firmware drivert, mert nem jeleníti meg a program, mert te letiltottad… MINDEN drivert lehessen törölni, ne legyen ilyen korlátozás"* és *„ne is próbálj meg ilyen baromságot csinálni soha többet"*.

**A HÁROM OK, amiért ez nem opcionális:**

1. **EZ A PRODUKTUM LÉNYEGE.** A fix úgy javít, hogy MINDENT letöröl és nulláról újradriverez. Egy néma kizárás nem „óvatosság", hanem a termék működésének megcsonkítása.
2. **AZ OFFLINE JAVÍTÁS AZ UTOLSÓ MENTSVÁR.** Ha egy rossz driver miatt nem indul a gép, a technikus egy másik Windowsból nyitja meg a sérült telepítést, és pont AZT a csomagot akarja kitörölni. Ha elrejted, elvetted tőle az egyetlen javítási utat — a legrosszabb pillanatban.
3. **A DRIVERT NEM FELTÉTLENÜL MI TETTÜK FEL.** Az ügyfél behozza a gépet egy máshonnan telepített, rossz driverrel. Az az érvelés, hogy „ezt úgysem telepítjük, ezért ne is töröljük", ilyenkor a fejére áll: pont az ilyen csomagot kell tudni eltávolítani.

**AMI MA IS ÍGY MŰKÖDIK, és így is kell maradnia** (ellenőrizve 2026-09-03): a `GuiDriversMixin.delete_drivers` **semmit nem szűr** — amit a technikus kipipál, az törlődik, firmware és tároló is; a lista (`load_drivers`) egyetlen szűrője a **nyomtató-pipa**, amit a felhasználó kapcsol, és az is csak elrejt, nem tilt; offline módban ugyanez az út él.

**A „NEM SIKERÜLT TÖRÖLNI" NEM MINDIG VÉGLEGES — A KÉZI TÖRLÉS IS KAP SPOOLER-KÖRT (2026-09-18).** Terepi bejelentés: a technikus kijelölte a nem használt csomagokat, és **14 sikertelen** lett, magyarázat nélkül. Az ok a már dokumentált `0xE000023D` („egy telepített eszköz használja a megadott INF-et"): a nyomtatósorokat a Spooler tartja életben. **Az AutoFix törlési fázisa 2026-09-07 óta ismerte a második kört, a KÉZI törlés nem** — ott a képernyőn csak `❌ {pub} sikertelen` állt, se ok, se teendő. A kör azóta a **közös magban** él (`drivers_core.retry_in_use_deletes` + `in_use_explanation`), és **mindkét hívó ugyanazt hívja**; a régi, AutoFix-beli másolat törölve. A három szabály változatlan (egyszer állítjuk le a Spoolert az egész kötegre, a visszaindítás `finally`-ben van, és **nem szűrünk osztályra**), és a mért határ is (`prnms009.inf` a leállított Spooler mellett is bent maradhat — azt a nyomtatósor *eszköz-csomópontja* tartja). **Ez NEM új szűrő a törlésben, hanem épp fordítva: segít törölni azt, amit eddig nem sikerült** — és ahol tényleg nem megy, ott megnevezi a valódi teendőt (előbb a nyomtatót kell eltávolítani a Beállításokból). Offline tesztelve 8 esetre a terepi pnputil-kimenettel, köztük a magyar (mojibake-elt) szövegre, a megszakításra és a Spooler-visszaindítás bukására.

**AZ EGYETLEN LÉTEZŐ KIVÉTELEK, és miért nem precedens:** a boot-útvonal védelme **kizárólag az AutoFix törlés-fázisában** van (ott senki nem ül a gép előtt, és a futó rendszer boot-driverének törlése `INACCESSIBLE_BOOT_DEVICE`-t adna, amit már semmilyen visszaállításunk nem javít), és még az is **példány-azonosító szerint célzott, nem osztály-alapú**. A többi kivétel mind **nevesített, ember által, a képernyőn meghozott döntés**: nyomtató-pipa, Wi-Fi mód, csomagonkénti opt-out. Egy néma, osztály-alapú kizárás egyikbe sem fér bele.

**EGY MÁR ELKÖVETETT HIBA, hogy felismerhető legyen:** 2026-09-03-án egy terepi naplóból azt olvastam ki, hogy a lánc törli a gyártó firmware-csomagját, amit soha nem telepít vissza — és ebből „logikus" következtetésként osztály-alapú `FW-PROTECT` védelmet írtam a törlés-fázisba. A szabály, ami ezt tiltja, **már benne volt ebben a fájlban**, csak nem néztem meg a változtatás előtt. Ha legközelebb egy javításod egy új szűrő vagy kizárás a törlési oldalon, az szinte biztosan ugyanez a hiba.

### MINDEN ESZKÖZ KAPJON DRIVERT — a tiltólista SOHA nem megoldás (explicit user decision, 2026-09-03)

**Ez a szekció egy GONDOLKODÁSMÓD-szabály, nem egy funkció leírása. A felhasználó szó szerint ezt kérte:** *„arra hogy fel se települ arra nem az a megoldás hogy akkor berakom egy tiltolistaba hogy többet ne dobja be mert akkor a device nem fog kapni drivert sose ez nem jo… az a cél hogy minden kaphon drivert!!! tehat meg kell talalni a fő okot hogy miért dobja be ha nem tudja feltelepíteni, ha meg azért dobja be mert fel tudná akkor meg kell nézni hogy miért nem telepíti mégis fel!!!"* és *„ilyen nincs h sikertelen vagy tiltolista vagy nem is probalunk drivert keresni vmihez feladjuk, ilyen nincs, nem fogadom ezt el, ne is legyen ez a gondolatmenet es ne is probalj meg te se igy kodolni"*.

**A CÉLÁLLAPOT, amit el kell érni:** a technikus lefuttat 2-3 kört (szken → telepítés → szken), és a **2-3. kör után egy olyan képernyő fogadja, hogy MINDEN NAPRAKÉSZ** — és az azért legyen igaz, mert a gépben lévő **összes eszközhöz talált és fel is rakott** drivert a program. Nem azért, mert elfogytak a próbálkozások, nem azért, mert egy lista elrejtette a maradékot.

**HÁROM TILTOTT GONDOLATMENET. Ha egy javításon dolgozva bármelyiken kapod magad, az a javítás rossz:**

1. **„Tegyük listára, hogy ne ajánlja fel többet."** Ettől a lista rövidül, az eszköz viszont **örökre driver nélkül marad** — a probléma nem megoldódott, csak láthatatlanná vált. A `catalog_no_bind.json` **kizárólag** fölösleges *letöltést* spórol (ugyanazt a bizonyítottan alkalmazhatatlan csomagot ne töltsük le 1,2 GB-ban újra); **soha nem zárhat ki egy ESZKÖZT a keresésből**. Ha a legjobb jelöltek mind megbukottak, a helyes válasz **lejjebb lépni a jelöltek közt**, nem feladni — lásd `_catalog_find_driver` `elif not usable:` ága.
2. **„Nem sikerült, hát sikertelen."** Egy bukás mindig két, gyökeresen különböző dolog egyike, és **a kettőt szét kell választani**: (a) *el sem jutott hozzánk* (hálózat, csonka letöltés) → ebből semmi nem következik a csomagról, tartós következménye nem lehet, újra kell próbálni; (b) *bizonyítottuk, hogy nem ide való* (INF-vétó) → akkor **másik jelöltet kell keresni**, nem elkönyvelni a bukást.
3. **„Ehhez úgyse lesz driver, ne is keressünk."** Ez a [governing rule](#what-actually-gets-a-driver--the-inventory) megsértése: eszközt kizárni **kizárólag** akkor szabad, ha nem hardver (`WU_SCAN_IGNORED_CLASSES`), vagy tároló/firmware. Minden más eszközre keresni kell — „maximum nem talál", és **azt ki is kell mondani** a képernyőn a valódi teendővel (gyártói oldal), nem elhallgatni.

**A HELYES KÉRDÉSSOR, ha egy eszköz újra meg újra felbukkan a listán.** Pontosan kettő eset van, és mindkettőnek van gazdája:

| Amit látsz | A kérdés, amit fel KELL tenni | Tipikus valódi ok (mind mért eset ebben a fájlban) |
|---|---|---|
| Felajánlja, de **nem tudja** feltelepíteni | **Miért ajánlja fel, ha nem megy?** | rossz nyertes-választás (általános kulcsról jött, más gépgyártó változata), miközben a gép SAJÁT `SUBSYS_`-kulcsán ott a jó csomag |
| Felajánlja, **fel is menne**, mégsem települ | **Mi akadályozza meg a telepítést?** | a takarítás kivezette a saját telepítésünk INF-jeit; hálózat-kiesés elégette a próbálkozásokat; a „már próbáltuk" jelölés le sem töltött csomagra került |

**Amit ez a szabály KÖVETEL a kódtól, konkrétan:**
- a jelölt-lista **soha ne fogyjon üresre** amiatt, hogy a legjobb pontszámúak megbukottak — lépjünk lejjebb a `scored` halmazban (legspecifikusabb kulcs → jobb OS-pontszám → frissebb dátum);
- **ha az eszköz már gyári driveren fut, attól még kapjon tartalék-jelölteket** a saját kulcsáról (a kiadás-kapu változatlanul véd a visszalépéstől) — ez a 2026-09-03-i Realtek-eset, ahol a tartalék-lista `_is_inbox_driver` mögé volt zárva, és a felrakott driver után a mechanizmus **kikapcsolt**;
- a bukás **oka** utazzon a hívóig (`_catalog_dl_failed` vs. `no_bind`), hogy a hívó helyesen dönthessen az újrapróbálásról;
- ha tényleg nincs csomag, azt **nevesítve, teendővel** kell kiírni (`no_source`), nem egy számként elrejteni.

**És amit TILT:** új „skip/ignore/blacklist" lista bevezetése egy telepítési probléma megoldásaként. Ha egy javításhoz ilyen kell, a javítás nem kész — a valódi okot kell megtalálni.

**UGYANEZ A TÖRLÉSI OLDALON MÉG SZIGORÚBB:** ott ÚJ SZŰRŐ EGYÁLTALÁN NEM LEHET — lásd [MINDEN DRIVERT LEHESSEN TÖRÖLNI](#minden-drivert-lehessen-törölni--a-törlésbe-soha-ne-tegyél-új-szűrőt-explicit-user-decision-2026-09-03). A keresésnél a rossz szűrő egy elmaradt drivert jelent; a törlésnél egy meg nem javítható gépet.

#### A KATALÓGUS RÉSZLETLAPJA FELSOROLJA A TÁMOGATOTT HARDVER-AZONOSÍTÓKAT — letöltés előtt (2026-09-03, élőben mérve)

**Ez szünteti meg a „felajánlja → letölti → elveti → mégis felajánlja" kört, ami napokig mérgezte a terepi élményt.** A `ScopedViewInline.aspx?updateid=<guid>` lapon van egy `<div id="driverhwIDs">` blokk, ami **névszerint felsorolja**, mely hardver-azonosítókat támogatja a csomag — SUBSYS-szinten. Mérve, HP EliteDesk 800 G2 / Realtek ALC221 (eszköz: `…&SUBSYS_103C8054`):

| Csomag | Lap | Idő | Azonosítók | Az eszközé benne van? |
|---|---|---|---|---|
| HP `6.0.1.8335` [2017-12-26] | 44 KB | 1,9 mp | **16** | ✅ igen |
| Generikus `6.0.9980.1` [2026-04-20] | 86 KB | 2,1 mp | **149** | ❌ nem (`103c8266`, `103c8265`, `103c8184`…) |

Vagyis a *„más gépgyártó változata"* eset **letöltés, kicsomagolás és INF-vizsgálat nélkül** eldönthető — 2 másodperc és 86 KB helyett 11 MB (hang) vagy 1,2 GB (videokártya). `_catalog_supported_hwids` a parser, `_catalog_detail_page` a közös, GUID szerint gyorsítótárazott lekérés (a „Driver Model" holtverseny-döntő ugyanezt a lapot használja — két külön kérés pazarlás lenne).

**NÉGY SZABÁLY, ami nélkül ez többet ártana, mint használ:**
1. **Csak NEM ÜRES lista alapján szűrünk.** `None` (nincs ilyen szekció a lapon, vagy nem jött le) = **nem eldönthető** → a jelölt marad. Ugyanaz az elv, mint az `inf_package_applies` `None`-jánál: **sosem vetünk el a nemtudás alapján.**
2. **Ha a szűrés MINDENT kivágna, a szűrés eredményét eldobjuk.** Egy üres jelölt-lista azt állítaná, hogy „nincs hozzá driver" — ezt csak bizonyítottan szabad kimondani, és egy szerveroldali formátumváltozás nem tehet ilyen állítást.
3. **Csak a néhány legjobb jelöltet ellenőrizzük** (`CATALOG_HWID_PROBE_MAX` = 12), különben egy 25 soros holtverseny 25 kérés lenne. A holtverseny dátum szerint rendezett, tehát az első néhány a releváns.
4. **UGYANAZT A CSOMAGOT NEM KÉRDEZZÜK LE TÖBBSZÖR — dedup (cím, dátum) szerint, ÉS A KIZÁRÁS VERDIKTJE A TARTALÉK-LISTÁKRA IS ÉRVÉNYES** (2026-09-08, élőben mérve). A katalógus egy csomagot **több tucat GUID alatt** publikál, a `rows_by_guid` viszont GUID szerint dedupál, tehát a 3. szabály kerete elmehetett néhány valódi csomagra: mérve `PCI\VEN_1022&DEV_148A` = **75 bejegyzés / 4 csomag** (35×+24×+12×+4×), `PCI\VEN_10DE&DEV_2504` = **50 bejegyzés / 3 csomag**. **Ez nem csak pazarlás volt (24 mp eszközönként, lábanként), hanem helyességi kockázat**: a 12-es keret elfogyhatott egyetlen csomag bejegyzésein, és a maradék **ellenőrzés nélkül** ment tovább. ~~A dedup **bizonyítottan biztonságos**: négy csoportban 3-3 testvér-GUID lapját összevetve az azonosító-lista mindig azonos volt (5/5/15/70 azonosító) — a lista a **csomag** tulajdonsága, nem a bejegyzésé.~~ **[EZ AZ ÁLLÍTÁS 2026-09-20-ÁN MEGDŐLT — lásd [A letöltés előtti szűrő három vak ága](#a-letöltés-előtti-szűrő-három-vak-ága--és-egy-megdőlt-állítás-2026-09-20-build-321). A minta szűk volt: ugyanazon cím+dátum alatt LÉTEZIK eltérő HWID-listájú változat, és a kizárás verdiktje ezért nem vihető át vizsgálat nélkül a testvérekre. A dedup mint KÉRÉS-SPÓROLÁS érvényben marad, de a kizárt csoport testvéreiből mintát veszünk.]** A sorrend és minden későbbi lépés (URL-feloldás, letöltés, tartalék-lista) **változatlanul az összes GUID-dal dolgozik**. A kiolvasott listák `catalog_hwids.json`-ban élnek túl a lábak közt (`CATALOG_HWIDS_TTL` = 7 nap; **hibás/üres eredményt sosem teszünk el**). Részletek: [Nyolc javítás egy 0 hibás láncból](#nyolc-javítás-egy-0-hibás-láncból--amikor-a-program-jól-dolgozik-de-rosszul-számol-be-róla-2026-09-08-build-304-asrock-b450m-pro4).

**A parse a NYERS HTML-ből megy, nem a tag-mentesített szövegből** — ez nem stílus: a lap szövegében a „More information" / „Support Url" a HWID-szekció ELŐTT szerepel, tehát egy szöveg-alapú `(.*?)(?:More information|…)` minta pont a listát vágja le (először pontosan ezt írtam, és 0 azonosítót adott). A `<div id="driverhwIDs">` blokk viszont egyértelmű.

#### A HID-KOLLEKCIÓ LÁNCA (`&COL..`) NEM RÉSZHALMAZ — és halmazként LÁTHATATLAN (2026-09-03, terepen bizonyítva)

**Ez az a hiba, ami miatt a program egy touchpad-drivert ajánlott a billentyűzetre, minden szken után újra.** A `_hwid_matches` token-**halmazokat** hasonlít (ez oldotta meg az R9-200 esetet), a HID-kollekciók viszont ismétlődő tagokból állnak, amiket a halmaz kiejt:

```
INF:    HID\VID_044E&PID_1212&COL02          tokenek: {VID_044E, PID_1212, COL02}
eszköz: HID\VID_044E&PID_1212&COL02&COL02    tokenek: {VID_044E, PID_1212, COL02}   <- AZONOS!
```

Pedig ez **két külön eszköz-csomópont**: a `…&COL02` a touchpad-kollekció, a `…&COL02&COL02` a billentyűzet-gyereke. A gépen lévő INF (2,8 KB, `apvhid.inf`) egyetlen azonosítót deklarál — `HID\VID_044E&PID_1212&COL02` —, és a neve kimondja: **„ThinkPad UltraNav driver"**.

A következmény végigkövethető a naplóban: a csomag felment („sikerült"), az eszköz sosem vette át, a Windows helyesen a `keyboard.inf`-en hagyta, az újrakötés-kör pedig ugyanezzel a rossz párosítással 4 eszköz csomópontját távolította el fölöslegesen — és a technikus mindebből annyit látott, hogy *„sose lesz minden naprakész"*.

**A javítás:** a COL-lánc **sorrend és darabszám szerint** azonosít egy kollekciót, ezért arra CSAK a pontos egyezés jó (`_hwid_token_seq`); a többi tokenre a részhalmaz-szabály változatlan. Regresszió-tesztelve a terepen bizonyított esetekre (R9 200 `ven+dev+rev`, amdafd `ven+cc`, USB kompozit `&MI_00`) — azok érintetlenek.

**A TANULSÁG: ha egy azonosító ISMÉTLŐDŐ tagokból is állhat, a halmaz-alapú összehasonlítás némán hazudik.** Nem hibázik, nem dob, csak azonosnak lát két különböző dolgot — és a hiba a *telepítés* szintjén jelentkezik, ahol már senki nem a párosítót gyanúsítja.

#### AZ OSZTÁLYKÓD-KULCSRÓL (`&CC_`) JÖTT CSOMAG NEM MEGY FEL FELÜGYELET NÉLKÜL (2026-09-04, terepen mérve)

**Ez az az eset, ahol MINDEN ellenőrzésünk igazat mondott, és mégis rossz driver ment fel.** A fejlesztői gépen (HP EliteDesk 800 G2 SFF, **asztali gép**) egy Build 295–298 közti kiadás felrakta az `AlpsAlpine - System 10.4200.1616.141`-et az **Intel SMBus vezérlőre**. A Windows saját `setupapi.dev.log`-ja mondja ki mindkét felét:

```
13:19:11  cmd: pnputil /add-driver C:\DV_Temp\driverdoktor_wu\...\ApSmbDrv.inf /install
13:19:12  Strong Name=oem12.inf:...:ApSMBus_Inst.NT:...:pci\ven_8086&dev_a123&cc_0c05
13:19:13.7  HID\Vid_044E&Pid_1212&Col02          <- a driver LEGYÁRTOTT egy tapipadot
```

**Következmény:** a driver egy nem létező ThinkPad UltraNav tapipadot hozott létre **négy szellem-gyerekkel** (egér, billentyűzet, érintőpárna, MTConfig), plusz egy `Automatic` szolgáltatást, ami minden rendszerindításnál elindította az `Apoint.exe`-t → *„Set user settings to driver failed"*. Ráadásul a CLAUDE.md-ben korábban „terepi esetként" rögzített **COL-lánc bug is ennek a következménye volt**, nem független lelet.

**MIÉRT NEM FOGTA MEG SEMMI — és ezt fontos pontosan érteni:**

| jel | mit mondott | igaza volt? |
|---|---|---|
| HWID-egyezés | illeszkedik | ✅ az INF tényleg deklarálja a `cc_0c05`-öt |
| `inf_package_applies` | alkalmazható | ✅ ugyanezért |
| kötés-ellenőrzés | rákötött | ✅ tényleg rákötött |
| Windows saját rangsora | ezt választom | ✅ **hardver**-azonosítós egyezés, míg az Intel csomagja csak **kompatibilis** azonosítón illeszkedik (mérve: `PCI\VEN_8086&DEV_A123` nincs az eszköz HWID-listájában) |

**Egyik sem hazudott.** A tíz döntési pontunk mind azt kérdezi, hogy *„ALKALMAZHATÓ-e"* — és a válasz becsületesen igen volt. Azt egyik sem kérdezi, hogy *„ennek a GÉPNEK szánta-e a gyártó"*. Ez az információ pontosan két helyen él: a WU **szerver-oldali célzásában** (amit a katalógus használatával definíció szerint megkerülünk) és a **katalógus részletlapjának** SUBSYS-szintű listájában — csakhogy az utóbbi üres is lehet, és olyankor szándékosan nem szűrünk.

**A HIBA FELOSZTÁSA — mindkét fél hibázott, és csak a sajátunkat tudjuk javítani:**
- **AlpsAlpine:** (a) SUBSYS nélküli, `&CC_`-kulcsos INF — a Realtek pl. ugyanerre 16, illetve 332 SUBSYS-es azonosítót sorol, tehát szűkíteni lehetett volna; (b) súlyosabb: a driver **nem ellenőrizte, van-e ott hardver**, csak feltételezte és gyártott hozzá csomópontokat. A tág INF a WU-n belül ártalmatlan (ott a célzás fogja be) — a katalógusban nem.
- **Mi:** egy **célzás nélküli archívumból** vettünk csomagot úgy, mintha hordozná a WU célzását.

**AZ AKKORI JAVÍTÁS (`class_code_only`, `_catalog_find_driver`) — 2026-09-21-ÉN TELJESEN VISSZAVONVA, lásd a szekció végét:** a `spec_by_guid` már eddig is tudta, melyik kulcs hozta a sort; ha a nyertes **legspecifikusabb** forráskulcsa `&CC_`-t tartalmazott, a tétel megjelölést kapott. Következmény: az **AutoFix kihagyta** (ott nincs, aki mérlegeljen — [elfogadási feltétel](#the-acceptance-criterion-for-the-one-click-fix-press-it-walk-away-come-back-to-a-finished-machine)), a **kézi szkenben pedig a „🚫 Nem ehhez a géphez való" ÖSSZECSUKOTT csoportba** kerül, nem a fő listába.

**A FŐ LISTÁBÓL VALÓ KIVÉTEL AZONNALI TEREPI VISSZAJELZÉS VOLT** (ugyanaznap): *„ha nem ehhez a géphez való akkor mi a fasznak jeleníti meg a program? kibaszottul feleslegesen van ott… hogy örökké be lehessen pipálni és sose rakja fel?"*. Jogos, és **pontosan ugyanaz a panasz, amiből 2026-09-01-én a `renderDeadOffers` csoport született** — az első változatom (fő listában, pipa nélkül, jelöléssel) ezt a már meghozott döntést írta felül. A helyes válasz a meglévő mintát követi:

| jelölés | hol | miért ott |
|---|---|---|
| `downgrade`, `risky` | **fő lista**, pipa nélkül | van mit MÉRLEGELNI (egy régebbi driver néha épp az, ami kell) |
| `prev_no_bind` (+ akkor: `class_code_only`) | **összecsukott csoport** | nincs mit mérlegelni — a program már eldöntötte |

A feltétel EGY helyen él (`hwIsDeadOffer`, ui.html), mert hat helyen kellett (szűrés, két számláló, fül-kihagyás, „Összes", alap-kijelölés) — egy hetedik ok felvételekor a projekt legrégebbi hibája jönne elő, hogy egy példány lemarad. **2026-09-21 óta egyetlen ok van benne (`prev_no_bind`), és az bizonyíték:** a csoport csak olyan tételt tartalmazhat, amit a program letöltött, felrakott, és az eszköz nem vett át. A letöltés ELŐTT felismert, nem ide való csomag nem itt van, hanem **ki van zárva** — azt a program fel sem ajánlja.

**Négy ok, amiért AKKOR úgy gondoltam, hogy ez nem sérti a [„minden eszköz kapjon drivert"](#minden-eszköz-kapjon-drivert--a-tiltólista-soha-nem-megoldás-explicit-user-decision-2026-09-03) szabályt:** (1) **csomagot** jelöl, nem **eszközt** — az eszköz minden körben, minden forrásból keresésre kerül; (2) **nem rejti el**: a csoport kinyitható, a sor bepipálható és feltelepíthető — a program soha nem hoz olyan döntést a technikus helyett, amit ne tudna felülbírálni; (3) **valódi csomagot nem veszít**, mert ha a csomag tényleg ehhez a géphez való, a gép saját `&SUBSYS_`-kulcsa is behozza, és akkor `spec_by_guid` ott adja a kisebb indexet — a jelölés fel sem kerül; (4) **hibakódos eszköznél nem lép életbe** (ott bármi jobb a semminél, ugyanaz a kivétel, mint a downgrade-védelemnél). **A (2) pont az, ami 2026-09-21-én megdőlt:** a felhasználó döntése szerint egy bizonyítottan nem ide való csomagot felajánlani nem „választási lehetőség", hanem fölösleges kör az ügyfél idejéből — a jelölés helyére KIZÁRÁS került.

**A szintetizált törzs-kulcs (`PCI\VEN_8086&DEV_A123`) NEM osztálykód**, és ez nem részletkérdés: az Intel saját csomagja pont onnan jön, tehát a **kulcs-alapú** jelölés nem érinti. Offline tesztelve mind a hat ágra (valódi eset, SUBSYS-ről is jön, HP-nyertes, hibakódos, törzs-kulcs, részletlap-elsőbbség) + a felület kijelölési logikájára.

> **⚠️ 2026-09-21: AZ EGÉSZ FENTI MECHANIZMUS VISSZAVONVA — UGYANEZ A CSOMAG UGYANEZEN A GÉPEN ÚJRA FELMENT.** Két baja volt. (a) **Vak volt:** mérve a `...&CC_0C0500` kulcs **0 sort** adott, az Alps csomag mind az 5 sora a **törzs-kulcsról** jött, tehát a kulcs-alapú jel pont arra a csomagra nem szólalt meg, amiért született. (b) **Rossz volt a válasz formája is:** a megjelölt csomagot FELAJÁNLOTTA — explicit user decision szerint ami bizonyítottan nem ide való, azt ne is ajánlja fel. Helyette a csomag a **letöltés előtti szűrőben esik ki**, a gyártó saját, publikált azonosító-listája alapján (`wu_core.class_code_only_match`). A mérési adatok és a COL-lánc párhuzamos hibája: [A `class_code_only` vak volt arra a csomagra, amiért íródott](#a-class_code_only-vak-volt-arra-a-csomagra-amiért-íródott--és-a-col-szabály-hiányzott-a-rebindből-2026-09-21-build-322-hp-elitedesk-800-g2).

**MELLESLEG, ÉS TANULSÁGOS: az Intel saját INF-je szerint ennek az eszköznek NEM IS KELL DRIVER** — `%PCI\VEN_8086&DEV_A123Desc%=**Needs_NO_DRV**,PCI\VEN_8086&DEV_A123`, vagyis a csomag pusztán nevet ad neki az Eszközkezelőben. Az egész kör egy olyan eszközre keresett drivert, aminek a chipgyártója kimondja, hogy nem kell.

#### „NINCS HOZZÁ DRIVER" CSAK AKKOR MONDHATÓ, HA A PROGRAM TÉNYLEG MINDENT VÉGIGPRÓBÁLT

**Explicit user decision, 2026-09-03:** *„nem akarom h feladja a program sehol semmilyen esetben se, nem megoldas az ha kiirod az ugyfelnek h nincs hozza driver vagy nem létezik. Persze akkor lehet csak ezt kiírni ha TÉNYLEG MINDENT megprobalt a program es sehonnan se tudott hozzá drivereket keríteni, de mondjuk az hogy atnezi a katalogus elso 3 sorat aztan kiírja a program h nincs hozza driver, ez nem elfogadhato mert ez nem igaz — ahogy az se volt igaz h az asrock lapomhoz nem volt driver, aztán mégis lett."*

**A „nincs hozzá csomag" egy ÁLLÍTÁS a világról, nem egy kényelmi kimenet.** Csak akkor szabad kimondani, ha a program minden elérhető forrást és minden kipróbálatlan jelöltet végigvitt. Amíg van olyan sor, amit **még nem töltöttünk le és nem vizsgáltunk meg**, addig a helyes viselkedés a próbálkozás — akkor is, ha „valószínűleg úgysem jó".

**A KÖR ATTÓL FOGY EL, HOGY PRÓBÁLUNK — nem attól, hogy feltételezünk.** Ez a különbség a két, kívülről hasonlónak látszó megoldás közt:

| Rossz | Jó |
|---|---|
| „a saját kulcson nincs újabb ⇒ naprakész" | „a saját kulcson nincs újabb ⇒ **nézzük az általánost is**" |
| a kör azért fogy el, mert **kihagyunk** | a kör azért fogy el, mert **kipróbáltuk és feljegyeztük** |
| a végállapot **feltételezés** | a végállapot **bizonyíték** |

**A gyakorlati minta, ami ezt megvalósítja** (`_catalog_find_driver`, 2026-09-03): a nyertes-választás a gép saját `SUBSYS_`-kulcsát részesíti előnyben (az az adott géphez való) — **de ha ott nincs újabb, NEM adjuk fel**, hanem kinyitjuk a kört a teljes sorhalmazra, a már bizonyítottan rossz sorok kivételével. Ez nem visz végtelen körbe, mert amit letöltünk és az INF-vizsgálat elvet, azt a tartós no-bind tár megjegyzi — **a kör tehát magától fogy el, két-három kör alatt**, és a végén őszintén kimondható, hogy mindent megpróbáltunk. Ez a mért forgatókönyv:

```
1. kör: saját kulcs -> nincs újabb; általános kulcs -> van kipróbálatlan sor
        => felajánljuk, letöltjük, INF-vizsgálat elveti, feljegyezzük
2. kör: a feljegyzett sorok kiesnek; nincs több kipróbálatlan
        => MOST mondható ki: naprakész / nincs hozzá való csomag
```

**AZ ASROCK-TANULSÁG, amiért ez a szabály létezik.** A projekt korábban leírta, hogy *„az ASRock egyáltalán nem publikál a Windows Update-re"* — aztán egy újramérés kihozta, hogy az ASRock-specifikus kulcsra **41 csomag** van a katalógusban. A „nincs hozzá driver" **téves állítás volt**, és azért maradt fenn hónapokig, mert senki nem mérte újra. Ha a program ilyet mond, annak a naplóból ellenőrizhetőnek kell lennie: **hány kulcsot kérdeztünk le, hány sort kaptunk, hányat próbáltunk ki, és mi lett a sorsuk.**

**Amit ez a kódtól követel:**
- egy „nincs csomag" ág előtt **mindig** legyen ott a kérdés: *van még kipróbálatlan sor / kulcs / forrás?*;
- a **letöltési** bukás (hálózat) nem számít próbálkozásnak — azt újra kell próbálni, nem feljegyezni;
- a végső üzenet **nevesítse az eszközt és a teendőt** (gyártói oldal), és a napló tartalmazza a fenti négy számot, hogy egy „szerintem van hozzá driver" bejelentés eldönthető legyen;
- ha egy mérés megcáfol egy ilyen állítást ebben a fájlban, a **régi szöveget javítani kell** (lásd a fájl elején: az elavult, magabiztos bejegyzés a legveszélyesebb).

### THE PRODUCT'S WHOLE POINT: re-driver from ZERO. Never "back up the old driver and put it back"

**Explicit user decision, 2026-08-25, stated in the strongest terms after a session went the wrong way. This outranks any cleverness about "not losing" a driver. Read it before proposing anything that touches a missing driver.**

The purpose of this program is to take a **badly-drivered machine** and bring it back to life with a **complete, professional, freshly-installed driver set, starting from zero drivers**. The chain deletes every third-party package *except what the technician ticked to keep*, and then **re-drivers the machine from scratch**. The user's words: *"az az egesz programnak a lenyege hogy 0 ról azaz 0 driverrol telepitsen ujra mindent… mindent törlünk kivéve amik be vannak pipálva hogy ne és utána ÚJRADRIVEREZZÜK A GÉPET… NEM VISSZAÁLLÍTGATJUK"*.

**Therefore the following is BANNED, permanently, and must not be reinvented under any name** (exporting, staging, caching, "safety net", "rollback", "keeping a copy just in case"):

> Solving "device X ended up with no driver" by **saving the machine's existing driver package before deletion and reinstalling that same package afterwards.**

Why it is not a solution — and this is the argument that killed it, from the user: **"mi lenne ha nem lenne rajta ezen a thinkpaden alapból a driver? akkor mit állítanál vissza?"** A backup can only ever return what the machine already had. It does nothing for:
- a **fresh Windows install** on the same model (nothing to back up — the exact case the shop handles constantly);
- a machine whose old driver was **broken**, which is usually *why* the fix is being run — restoring it re-creates the fault the technician was paid to remove;
- any machine that never had the driver in the first place.

So a backup/restore scheme *appears* to fix the field report while leaving the actual product requirement — "every laptop must come out fully and correctly drivered" — completely unaddressed. It also quietly converts the fix from "re-driver from zero" into "preserve the status quo", which is the opposite of the product.

**And the decisive point, in the user's own words: the drivers we are deleting are BY DEFINITION the bad ones.** *"nem mentegetunk le az alapbol SZAR VAGY SEMMILYEN driverekbol es azokbol rakunk vissza, ilyen nincs"* — the machine is on the bench precisely because its driver set is wrong, missing or broken. Saving that set and putting it back is not a safety net, it is re-installing the fault. The one-click fix exists to **re-driver a badly-drivered machine from zero**; anything that carries the old state forward defeats the entire product.

**Universality is part of the requirement, not a nice-to-have** (same decision): *"UNIVERZÁLISAN minden gépen működjön ez a fix… ne csak erre a t580-ra"*. A fix that is patched to make one reported model work is not a fix. Never special-case a model, a vendor or a device; extend the **source layer** so every machine benefits — and if a machine genuinely cannot be served, say precisely which device and why, with an actionable remedy.

**The only legitimate exceptions are the technician's own checkboxes** (printer, storage, firmware, Wi-Fi mode, the per-package opt-out list, plus the boot-path protection that exists so the machine can still boot). Those are packages we *never delete*; that is categorically different from deleting a package and then sneaking it back in.

**And those checkboxes exist for OPERATIONAL reasons, not technical ones — understand the reason or you will "simplify" them away.** In the user's words: the printer opt-out is there because *"az ugyfel nem tudja feltelepiteni, en meg nem fogom neki azt ujratelepiteni a nyomtatoja nelkul"* — the printer is not on the bench, so a deleted printer driver cannot be re-installed and tested by the shop at all. Wi-Fi is the same story from a different angle (the machine's own connectivity, and the chain's). The same logic extends to **remote-access tooling**: when the technician is working over remote desktop, that tool's driver must be keepable too, or the fix cuts off the very session running it — which is exactly what the per-package opt-out list in the confirm dialog is for (the field case that created it was a remote-desktop tool's driver, "ninja…", wiped with nobody able to say afterwards what it had belonged to). Every one of these is a **named, human-made, on-screen decision about a package we then never touch**. None of them is a precedent for automatic preservation. The `netdrv_backup` export is the one narrow, pre-existing exception and it stays exactly as narrow as it is: it exists solely so an **offline** machine can get its network back mid-chain and continue the fix — it is a bootstrap for the chain, not a way of preserving drivers.

**The correct direction when a device ends up without a driver is always: find a real SOURCE and install from it.** In priority order: the WU Agent → the Microsoft Update Catalog → the **hardware/OEM vendor's own distribution channel** (Lenovo, Dell, HP model catalogs; NVIDIA/AMD/Intel for GPUs). If none of those has it, the honest outcome is to say so precisely and point the technician at the vendor page — never to paper over it with the machine's own old copy.

**Field case that must NOT be "solved" by backup/restore** (ThinkPad T580, Build 271, 2026-08-24; log in `debug_logok/`): the chain deleted 116 packages and 30 never returned. Most were a cloned image's foreign leftovers (AMD Radeon on an Intel laptop, Dell firmware, Qualcomm Wi-Fi, SunplusIT cameras) — losing those is correct and desirable. The reported symptom was that **the physical mouse buttons above the trackpad were dead** afterwards, and among the packages that never returned were `apmoufiltr.inf` / `apsmbdrv.inf` / `apvhid.inf` (AlpsAlpine, ThinkPad UltraNav) and `bhtpcrdr.inf` / `bhtsddr.inf` (BayHubTech, card reader). **Do not read that adjacency as causation — it is not established** (see the retraction under *OEM driver packs* below: the model's only pointing-device package is Synaptics, at the version already installed). What the case *does* establish is the design point: packages the chain removes may exist in **no** source it currently consults, and the answer to that is another real source — not a backup of the machine's own old copy.

#### OEM driver packs — TELJESEN ELTÁVOLÍTVA 2026-09-02 (a szekció alatta már csak történet)

> **A GYÁRTÓI (Lenovo/Dell/HP) KATALÓGUS-FORRÁS NINCS TÖBBÉ A KÓDBAN** (explicit user decision, 2026-09-02): *„a gyártói katalógust vedd ki teljesen a kódból, nem kell sehova se, az autofixbe se a kézi scan-be… felesleges, mire az lefut, már megvan szinte minden driver az adott géphez"*. Törölve: `app/oemcatalog_core.py`, `app/gui/oemcatalog.py`, a `GuiOemCatalogMixin` mindkét API-osztályból, a lánc `_oem_catalog_round` hívása és az `oem_done` stats-kulcs. **Ami megmaradt: a gépazonosítás**, mert a napló-feltöltés Drive-mappaneve azon áll — átköltöztetve `app/machine_core.py`-ba (`detect_machine`, `is_placeholder`); ha ez elveszett volna, a `logupload_core.machine_label` némán üres nevet adna. Az előzmény: a Dell/HP „teljes gépre szóló" packek már 2026-09-01-én kikapcsolásra kerültek (mérve: 11 perc 33 mp letöltés, majd `[WinError 193]` a CAB-on, tehát tiszta veszteség), így itt gyakorlatilag a Lenovo per-eszköz ág maradt, és a felhasználó azt is elengedte. **Az alábbi szekció a döntés hátterét és a mérési adatokat őrzi — NE olvasd élő leírásként, és ne éleszd újra a forrást „hátha segít" alapon.** A T580-tapipad esete (ahol ez volt az egyetlen forrás) továbbra is igaz marad mint kockázat; a felhasználó ezt ismerve döntött így.

#### OEM driver packs — the universal fourth source (measured 2026-08-25)

**This is the direction the T580 case actually points to, and every endpoint below was verified reachable on the dev box.** Lenovo, Dell and HP all publish *machine-readable* catalogs that map a machine model to its complete, vendor-assembled driver set — which is literally what the user means by *"egy teljesen uj profi driver packkal a 0 ról visszatelepítve"*. Measured facts, so the next session does not have to rediscover them:

| source | endpoint | size | verified |
|---|---|---|---|
| Lenovo per-model package catalog | `https://download.lenovo.com/catalog/<MTM4>_<Win10\|Win11>.xml` | 10–17 KB | 200 for `20L9`/`20LA`, both OS |
| Lenovo SCCM driver-pack catalog | `https://download.lenovo.com/cdrt/td/catalogv2.xml` | 1.3 MB | 200 |
| Dell full component catalog | `https://downloads.dell.com/catalog/CatalogPC.cab` | 2.9 MB | 200 |
| Dell driver-pack catalog | `https://downloads.dell.com/catalog/DriverPackCatalog.cab` | 0.3 MB | 200 |
| HP client driver-pack catalog | `https://ftp.hp.com/pub/caps-softpaq/cmit/HPClientDriverPackCatalog.cab` | 0.2 MB | 200 |

**IMPLEMENTED 2026-08-25** as `app/oemcatalog_core.py` (machine detection + providers + matching + extraction) and `app/gui/oemcatalog.py` (`GuiOemCatalogMixin._oem_catalog_round`), wired into `_run_catalog_closing_round` **after** the Microsoft-Catalog round so it only works on what the universal sources did not solve. **Two safety rules came out of live testing and must not be relaxed:**

> **RÉSZBEN VISSZAVONVA 2026-09-01 (explicit user decision): a TELJES GÉPRE szóló (Dell/HP) driver packek KI VANNAK KAPCSOLVA** — `oemcatalog_core.INSTALL_WHOLE_MACHINE_PACKS = False`. A felhasználó szavaival: *„ez a dell pack baromsag nem is kell bele"*. **A Lenovo per-eszköz ága ÉRINTETLEN és továbbra is fut** — az a `<_PnPID>`-alapú, célzott, kicsi (T580 UltraNav: 27,4 MB), és bizonyítottan az volt az egyetlen forrás, ami a touchpadot megtalálta; a fenti szekció minden mondata **arra** továbbra is érvényes. Amit a kapcsoló kivesz, az kizárólag a `whole_machine: True` packek köre, négy mért ok miatt: (a) **strukturálisan nem tud dönteni** — a pack `pnpids`-e üres, tehát nincs eszköze (`dev is None`), amihez a verzióját mérni lehetne, `_oem_already_current` ezért definíció szerint mindig „telepítsd"-et ad, vagyis a program nem tudja és nem is tudhatja meg, hogy hoz-e bármi újat; (b) **az idő** — mérve a Latitude 5580-on a `5580-win10-A13-JRG2W.CAB` letöltése **11 perc 33 mp**, a 82 perces lánc egyhetede (a HP `sp92706` ugyanez volt lábanként ~7 perccel, amíg az `oem_done` mankó meg nem született); (c) **ki sem tudta csomagolni**: a Dell whole-machine packja **`.CAB`**, az `extract_vendor_package` viszont mind a 4 kapcsolókészletével önkicsomagoló `.exe`-t feltételez → `[WinError 193] %1: nem Win32 alkalmazás`, négyszer, majd `⚠️ nem sikerült kicsomagolni` — vagyis a 11,5 perc **teljes egészében veszteség volt**, minden Dell gépen, minden láncon; (d) **amit hozna, azt a katalógus úgyis hozza**: ugyanabban a futásban a Microsoft Update Catalog 16 drivert telepített (chipset, hang, LAN, Wi-Fi, videó, ME, I2C) célzottan, HWID alapján, verzió- és kötés-ellenőrzéssel, míg a gépgyártói pack ugyanezt egy régebbi, egybegyúrt kiadásban tartalmazza. Asztali/összerakott gépre amúgy sincs ilyen pack. A kikapcsolás **a provider szintjén** történik (`find_oem_packages`), tehát Dell/HP gépen a katalógus **letöltése is elmarad**; a `match_packages_to_devices` whole_machine ága védőhálóként megmaradt egy jövőbeli új szolgáltatóra. Visszakapcsolás egyetlen sor — **de akkor a (c) pont CAB-hibáját is javítani kell**, különben Dell gépen ismét 11 perc megy a semmibe.

- **A vendor catalog is NOT a list of drivers — it is full of firmware flashers and utilities, and installing those unattended can brick hardware.** Measured on the T580's own catalog: the first cut of the matcher (rule: "no hardware IDs ⇒ applies to the whole machine") selected *Fibocom WWAN **Firmware** Update Tool*, *Thunderbolt **Firmware** Update Tool*, *Intel Management Engine **Firmware***, *Lenovo NVMe SSD **Firmware** Update Utility*, dock FW utilities and *NVIDIA GeForce Experience **Software***. An unattended chain would have flashed BIOS/Thunderbolt/SSD/TPM — exactly what the firmware checkbox forbids and what this file documents as irreversible. Fixed by `is_firmware_package` / `is_software_package` (deliberately generous — a wrongly skipped driver costs nothing, the other direction costs the customer's hardware), by honouring `_autofix_allow_firmware` here too, and by the rule that **a package with no declared hardware IDs is installed only when the provider explicitly marked it a whole-machine driver pack** (`whole_machine`, Dell/HP only). Lenovo's real drivers all declare their IDs (measured: 45 of 56), so a missing ID there *means* "not a device driver". Result on the T580: 12 "matches" → **1**, the correct UltraNav package.
- **Whole-machine driver packs exist once per Windows build — take only the newest.** Measured: HP ProBook 440 G5 returns **6** packs (1703…2004), each several hundred MB. `_keep_newest_pack` keeps one (highest SoftPaq number for HP, newest release date for Dell); HWID-matched per-device packages are never affected.

Also verified live: on a machine whose model is **not** in the vendor catalog (this dev box, an HP EliteDesk 800 G2 SFF — dropped from HP's current driver-pack catalog) the round returns an empty list and says so, rather than failing. Its board id `8054` appears 10× in the HP catalog but only inside `<ProductId>2101278054</ProductId>` — a substring coincidence the matcher correctly ignores, because it compares against the comma-split `<SystemId>` list and not the raw text.

Findings that shape the implementation:

- **The catalogs declare HARDWARE IDs, and that is the matching key — not the extracted INF.** A Lenovo package descriptor carries `<_PnPID><![CDATA[ACPI\VEN_LEN&DEV_009B]]></_PnPID>` entries, i.e. the vendor states exactly which devices the package serves, so the existing `_hwid_matches` machinery applies directly. Do **not** match on the extracted INF: measured on the T580's own UltraNav package, the extracted `SynPD.inf` yields only `HID\…` ids and `inf_package_applies` returns `None` — the ACPI id the device actually presents appears *only* in the catalog metadata. Keep `inf_package_applies` as the non-blocking sanity check it already is.
- **Vendor packages extract WITHOUT installing, so no unknown installer ever has to be run.** The descriptor gives the command (`n20gx20w.exe /VERYSILENT /DIR=%PACKAGEPATH% /EXTRACT="YES"`), and it works: measured 27.4 MB downloaded in 0.8 s, extracted in 7.5 s, rc=0, 111 files including the INF. So an OEM package installs on the **same well-understood `pnputil /add-driver /install` path** as a Microsoft Catalog cab, with the same guards (bind check, `_cleanup_unused_staged_infs`). **This is what resolves the old deferral** recorded under *Vendor-site driver sources* ("blind-shipping untested silent OEM installers … is riskier than the link-out") — we are not shipping their installer, only their INF payload. That old note is therefore superseded for Lenovo/Dell/HP, and the link-out stays as the fallback for vendors with no catalog.
- **A caution the same measurement produced — do not repeat my mistake.** I attributed the T580's dead trackpad buttons to the deleted AlpsAlpine packages (`apmoufiltr.inf`/`apsmbdrv.inf`/`apvhid.inf`). The Lenovo catalog then showed that the model's *only* pointing-device package is **Synaptics UltraNav 19.3.4.228** — exactly the version the log shows was already installed on `ACPI\LEN009B`. So the Alps attribution is **not supported by evidence** and must not be treated as settled; the device reported error code 0 throughout, and the real cause is still open. Same lesson as the colour-profile retraction: separate what the log *proves* from what it merely *suggests*.
- **Machine identification must be universal**, not vendor-special-cased: `Win32_ComputerSystem` (Manufacturer/Model), `Win32_ComputerSystemProduct` (Name, `IdentifyingNumber` = Dell service tag), `Win32_BaseBoard` (self-built desktops — the existing `BOARD_VENDOR_PAGES` fallback already handles those), `Win32_BIOS`. Lenovo's key is the 4-character MTM prefix of `Win32_ComputerSystem.Model` (`20L9S0KM00` → `20L9`).
- **Honest coverage limit:** machine-readable catalogs exist for Lenovo/Dell/HP. For other brands and self-built desktops the universal sources remain WU → Microsoft Update Catalog → GPU vendors, and the closing report must name any device none of them could serve. Chasing per-vendor scraping for the long tail is not worth the maintenance; extending the *provider list* is.

One diagnostic finding from that investigation is worth keeping regardless of approach: **`collect_driver_usage` reads `Win32_PnPSignedDriver`, which only ever names a device's FUNCTION driver.** Filter drivers (`apmoufiltr.inf`), class filters and extension INFs never appear there, so they always look like "no present device uses this" — which is how the T580's UltraNav filter got reported as "probably an application's driver, reinstall the program". Never treat an empty `devices` list as evidence that a package is unused.

> **RÉSZBEN MEGOLDVA 2026-09-17 — a korlát megszűnt, a TANULSÁG viszont áll.** A `collect_driver_usage` már nem a `Win32_PnPSignedDriver`-t kérdezi, hanem az [`app/driverusage_core.py`](#mit-használ-a-gép--egy-mag-a-driverek-nézetnek-és-a-fix-törlési-előnézetének-2026-09-17) közös magját, ami a function driver mellett a **futó kernel-szolgáltatást** és a **filter-regisztrációt** is nézi — tehát a fenti eszközök többsége már NEM látszik használatlannak (mérve: +6 csomag, köztük egy vírusirtó négy filter-drivere). Amit **nem old meg**: a jel továbbra sem teljes (egy sosem induló, sosem regisztrált driver nem hagy nyomot), ezért az utolsó mondat változatlanul érvényes — **üres `devices` lista SOHA nem bizonyíték arra, hogy egy csomag használatlan**. A mag ezért ad külön `unknown` állapotot a `unused` mellett.

### TÁROLÓ ÉS FIRMWARE: VÉGLEGESEN TILTVA, NINCS HOZZÁ KAPCSOLÓ (2026-09-02)

**Explicit user decision:** *„tüntesd el innen a tároló vezérlők illetve a firmware frissítések kapcsolót, ne lehessen bekapcsolni sose… ahogy az autofixnél úgy a manual driver scannél is! inkább ne tudják bekapcsolni az ügyfelek mert abbol sok baj lehet"*. A 2026-07-27/28-i „két checkbox, alapból KI" megoldás ezzel **visszavonva** — a kapcsolók eltűntek, a mögöttes szűrők változatlanok.

- **A KÉT KOCKÁZAT, AMIÉRT NEM VOLT TARTHATÓ EGY BEKAPCSOLHATÓ KAPCSOLÓ.** Rossz tároló-driver után a Windows **el sem indul** (`INACCESSIBLE_BOOT_DEVICE`), és azt már csak helyreállító adathordozóról lehet javítani — miközben az AutoFix **felügyelet nélkül** fut, tehát a hiba akkor derül ki, amikor a technikus visszaér a kávézásból; a firmware-írás pedig visszafordíthatatlan (sem visszaállítási pont, sem Windows-újratelepítés nem hozza vissza), és egy megszakadt flash hardveresen teszi tönkre az eszközt. Egy alapból kikapcsolt kapcsoló ezekre nem elég védelem: elég egyszer bekapcsolva hagyni.
- **NÉGY HELYEN KELLETT KIVENNI, ÉS EBBŐL KETTŐT KÖNNYŰ ELFELEJTENI.** (a) `ui.html` AutoFix-dialógus (`mc-allow-storage`/`mc-allow-firmware` + a `showAutoFixConfirm` olvasásai, a `delSetLoading` id-listája, a `startAutoFix` változói); (b) `ui.html` kézi szken (`hw-allow-storage`/`hw-allow-firmware`, a nézetbe lépéskori visszaállító ciklus, a `start_hw_scan` hívás argumentumai); (c) **`app/cli/menu.py` — a CLI mindkét helyen KÉRDEZTE** (`_hwscan_flow` és az AutoFix-folyamat), és mivel a CLI 2026-08-29 óta teljes értékű felület, enélkül a kapcsoló csak a grafikus felületről tűnt volna el; (d) **`app/gui/base.py` `--allow-storage-drivers` / `--allow-firmware` argv-kapcsolói**, amiket egy régi, még ki nem futott ütemezett feladat hordozhat.
- **A ZÁRÁS EGY KÖZÖS KAPUN VAN, NEM A HÍVÁSI HELYEKEN.** `run_autofix` workerében az `if/else` **után** (tehát a resume-ág és a dialógus-ág után egyaránt), illetve `start_hw_scan` elején egyetlen `allow_storage = False` / `allow_fw = False` pár. Ez az egy pont semlegesíti a JS-paramétert, a CLI-hívást és a régi ütemezett feladat argumentumát is — szemben azzal, ha hívási helyenként írnánk át, ahol a projekt legrégebbi visszatérő hibája (egy példány lemarad) azonnal visszatérne. Ha nem-alapértelmezett érték érkezik, azt **WARNING-gal kilogozzuk**: az onnantól hívási hiba, nem felhasználói döntés.
- **A PARAMÉTEREK ÉS A TELJES LOGIKA SZÁNDÉKOSAN A HELYÜKÖN MARADTAK** (explicit user decision: *„ha pedig a jövőben akarnék ezekkel bármit csinalni akkor ott lesz a kod csak vissza kell rakni… és akkor nem gubaszolodik semmi össze a kódban"*). A `wu_core` szűrői (`filter_autofix_risky_devices`, `filter_firmware_updates`, `is_generic_replace_candidate`, `deep_catalog_candidates`), a piros `⛔ KOCKÁZATOS` jelölés és a lánc-flagek érintetlenek. Újra kapcsolhatóvá tétel = a két értékadás törlése + a felületi kapcsolók visszatétele; semmit nem kell újraírni.
- **A CSOMAG-SZINTŰ FIRMWARE-SZŰRÉS TOVÁBBRA IS ÉLŐ KÓD, ne töröld „halottként".** Egy SSD-firmware a **tárolóvezérlőhöz**, egy dokkoló-firmware egy **USB-eszközhöz** párosul — az eszköz-osztály szűrője tehát nem fogja meg. `is_firmware_update` + `filter_firmware_updates` ezért ugyanúgy kell, és a `FIRMWARE_CLASS_LABEL` jelölés is megjelenhet egy nem-firmware-osztályú eszköz során.
- **A KÉPERNYŐN MEGJELENŐ MAGYARÁZATOT IS ÁT KELLETT ÍRNI, KÜLÖNBEN HAZUDIK.** A `skipped_risky` sor 2026-09-02-ig azt mondta: *„kapcsold be a pipát, ha ezekre is keressen"* — a pipa azóta nem létezik, tehát az útmutatás nem létező felületre küldene. Az új szöveg kimondja, hogy ez szándékos, és megadja a valódi teendőt (gyártói oldal, kézzel). Ugyanez a kézi szken nézetében egy `hw-risk-note` magyarázó sor, a két csúszka helyén: a *„miért nem talált az SSD-mhez drivert?"* kérdésre a felületen is kell válasz.

### ~~AMIT SOSEM TELEPÍTÜNK, AZT NE IS TÖRÖLJÜK~~ — MEGÍRVA ÉS UGYANAZNAP VISSZAVONVA (2026-09-03)

**Ez a szabály MEGSZÜLETETT, KÓDBA IS KERÜLT (`FW-PROTECT`), MAJD MÉG AZNAP VISSZAVONTUK. A tanulság az, hogy miért volt rossz — ezért marad itt.**

**Ami a naplóban látszott** (HP Pavilion x360, Build 299, egyébként 0 ERROR-os, 1 óra 12 perces lánc):

```
15:48:10  Törölve: oem73.inf (insydesystemfirmware.inf) - HP Inc. [Firmware]
16:47:09  REGRESSZIÓ: ELŐTTE oem73.inf (HP Inc. 15.18.0.0) -> MOST c_firmware.inf (Microsoft)
16:47:09  a csomag nincs stage-elve, nincs mit újrakötni
```

**A téves következtetésem:** mivel a firmware-TELEPÍTÉS 2026-09-02 óta véglegesen tiltva, a törlés viszont elviszi a gyártó firmware-csomagját, ez „egyirányú veszteség", tehát osztály-alapú védelmet kell tenni a törlés-fázisba.

**MIÉRT VOLT EZ ROSSZ — két, egymástól független ok, mindkettő döntő:**

1. **A TERMÉK LÉNYEGÉT SÉRTETTE.** Ez a fájl külön szabályként rögzíti: *„Deleting EVERY third-party package is the whole point of AutoFix — never »improve« it into selective deletion… no »skip drivers that look important« filter may be added."* A törlésnek pontosan egy fajta kivétele van: **nevesített, ember által, a képernyőn meghozott döntés** (nyomtató-pipa, Wi-Fi mód, csomagonkénti opt-out) plusz a boot-útvonal védelme — és még az is példány-azonosító szerint célzott, nem osztály-alapú. Egy néma, osztály-alapú kizárás egyik kategóriába sem fér bele. A CLAUDE.md-t olvastam, a szabályt mégsem ellenőriztem a változtatás előtt: ez a hiba maga is tanulság.
2. **AZ OFFLINE JAVÍTÁST TETTE VOLNA LEHETETLENNÉ** (a felhasználó érve, és ez a nyomósabb): *„mi van akkor, ha a firmware driver miatt nem bootol a gép? Bebootolok egy másik Windowsból, és a büdös életben nem tudom kitörölni, mert nem jeleníti meg a program."* A program egyik fő funkciója pont az, hogy egy **offline** Windows-kötetről is lehessen csomagot törölni. Ha egy osztályt elrejtünk a törlésből, azzal a technikustól veszünk el egy javítási utat — méghozzá pont abban a helyzetben, amikor a legnagyobb szüksége van rá.

**Ami viszont IGAZ MARAD a leletből, és nem hiba:** a gyártói firmware-csomag törlődik és nem jön vissza. Ez **nem kár, hanem a tervezett működés** — a felhasználó szavaival: *„ami third party driver, ott jó ha töröl mindent, nem probléma ha nem megy vissza."* A lánc ezt már ma is őszintén jelenti (regresszió-sor + a „nem került vissza" lista), tehát a technikus tud róla; ennél többet nem kell tenni.

**A VALÓDI, ÁLTALÁNOSÍTHATÓ TANULSÁG:** ha egy leletre a válaszod egy ÚJ SZŰRŐ vagy KIZÁRÁS, előbb keresd meg ebben a fájlban, hogy az adott lista miért olyan amilyen. Ennek a projektnek a szűrői mind vérrel írt szabályok — és a törlés-oldali szűrés kifejezetten tiltott. Egy „logikusnak tűnő" új kizárás itt szinte mindig egy már meghozott termékdöntés felülírása.

### 2026-10-12: review-javítások a törlő funkciókban (szűrő NÉLKÜL)

Kódolvasásból, a felhasználó kérésére („javíts, írd le, menj tovább”). Terepen NEM reprodukálva; teszt: `tests/test_delete_truth.py` (13 eset, 12 elbukik a régi kódon `git stash`-sel ellenőrizve; a 13. azt őrzi, hogy a `/force` kör megmaradt).

- **Időkorlát nélküli `pnputil /delete-driver`:** a kézi törlés (`delete_drivers`) és a duplikátum-/kiváltott-csomag takarítás (ez a lánc végén, felügyelet nélkül fut) nem kapott időkorlátot, holott a 11. fejezet mindenre előírja. Most `DELETE_DRIVER_TIMEOUT`; a kézi törlés a beragadást („indítsd újra, utána töröld”) külön mondja. Az offline DISM-törlés szándékosan korlát nélkül marad.
- **A duplikátum-takarítás saját sikerfeltétele** (`rc == 0 or 'deleted'/'törölve' in stdout`): a 3010 ok_code volt, de nem siker, és a magyar 3010-es szöveg egyiket sem tartalmazza → bukásnak vette, `/force`-szal újrapróbálta a már törölt csomagot, és „❌ sikertelen”-t írt. Most a közös `delete_succeeded`. **A `/force` kör megmaradt minden nem-sikerre** — egy első változatban kihagytam a „használatban” esetre, és még a tesztek előtt visszavettem: az egy új szűrő lett volna a törlésben (1. elv).
- **Szellemeszköz-szkript:** `$code -eq 3010` = siker (ugyanaz az ok); DONE sor nélkül véget ért szkript „NEM ért végig” (GUI és lánc), nem „✅ befejeződött”.
- **Force-törlés:** a `found_any` bool akkor is igaz volt, ha az `rmtree`/`del` elbukott → „✅ törölve (force)”. Most visszaolvasás: `'deleted'`/`'remains'`/`'notfound'`.
- **Kézi törlés hibaága:** a „❌ sikertelen” mellé most a `delete_failure_text` oka kerül; a WARNING-sor és a kivételág `exc_info`-ja hiányzott.
- **DISM-lista:** az offline lista is jelenti az ismeretlen nem-0 kódot (`_list_failure`, közös az online-nal); a `DISM_ERROR_HINTS` számra végződő mintái számhatárt kapnak (az `error: 2` az `Error: 216`-ra is illeszkedett). A duplikátum-nézet és a záró duplikátum-takarítás egy elbukott listát nem mond „nincs duplikátum”-nak.
- **Temp-takarítás:** a `stop_services` a „futott-e előtte” sort a leállítás ELŐTT írja ki (időtúllépésnél a részleges kimenetből is visszaindítható, amit leállítottunk); a kihagyott-minta lista mindenhol `FAILED_SAMPLE_MAX`-ra korlátozva.

