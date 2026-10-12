# Terepi bizonyítékok — Auto-updater, build/kiadás, régi Windows, WebView2-összeomlás

> A CLAUDE.md 2026-10-11 előtti teljes szövegéből **szó szerint áthelyezve**, eredeti sorrendben. A tömör, érvényes szabályok a CLAUDE.md-ben vannak; ha a kettő eltér, a CLAUDE.md a mérvadó, és ezt a fájlt is javítani kell. A szövegben lévő `(#…)` hivatkozások a régi egyfájlos szerkezetre mutatnak: a cél-szekciót `grep`-pel keresd a `docs/terep/` mappában.

### AZ AUTO-UPDATER A GITHUB RELEASES API-RÓL MEGY, A RAW CDN CSAK TARTALÉK (2026-09-17)

**Terepi panasz:** *„fel pusholom, nem mindig talalja meg vagy csak 10 percre ra"*. A felhasználó a saját `Youtube_lelopas` projektjére hivatkozott, ahol ugyanez „szinte egyből" működik — és igaza volt, a két projekt tényleg más forrást kérdez.

**A MÉRÉS, ami az okot megadta** (2026-09-17, `curl -sI` a két végpontra):

| forrás | `Cache-Control` | kiszolgáló |
|---|---|---|
| `raw.githubusercontent.com` (ez volt) | **`max-age=300`** | **Fastly CDN** — `Via: 1.1 varnish`, `X-Served-By: cache-sof1510053-SOF` |
| `api.github.com/repos/…/releases` (ez lett) | `max-age=60` | GitHub API, ETag-alapú |

Tehát nem a kód volt lassú, hanem a forrás: egy CDN edge **öt percig** kiszolgálhatja a push előtti tartalmat, és a `?t=<timestamp>` ezen **nem segít** (a GitHub raw a query stringet nem veszi a cache-kulcsába). A korábbi 3× újrapróbálkozás ezt a tünetet kezelte, az okot nem.

**A MEGOLDÁS a YouTube-projekt bevált mintája**: a kiadás `build-N` címkét kap, az exe pedig melléklet, és a program a Releases API-t kérdezi — ami a kiadás létrehozása után azonnal látszik.

**HÁROM ELTÉRÉS A YOUTUBE-PROJEKTHEZ KÉPEST, MINDHÁROM SZÁNDÉKOS:**

1. **NEM `/releases/latest`, hanem a kiadás-LISTA + `build-N` szűrés.** Ebben a repóban a kiadások egy része nem build, hanem **asset-tároló**: élőben mérve a címkék `mas.zip`, `block.bat`, `stresstools.zip`, `latest_build`, `legujabb_build`, `v103`, `V84` — vagyis a `/releases/latest` most a **`mas.zip`-et** adná vissza, és az updater soha nem találna build-számot. A YouTube-projektben nincsenek ilyen kiadások, ezért ott a `latest` elég. A legnagyobb `build-N` nyer, nem a listaelső: egy utólag feltöltött `stresstools.zip` a build-kiadások *elé* kerülne a sorrendben.
2. **A RAW ÚT MEGMARAD TARTALÉKNAK, és ez nem óvatoskodás.** Három ok: (a) **a Build ≤ 310 példányok CSAK a raw utat ismerik** — ha a rebuild abbahagyná a `driver_tool.py` + `dist/DriverVarazslo.exe` pusholását, azok a gépek soha többé nem frissülnének; (b) az API **óránként 60 kérést** enged IP-nként, egy szervizben pedig több gép ül egy nyilvános IP-n (403 esetén némán a raw-ra esünk); (c) ha egy kiadásnál a `gh release create` kimarad, a raw akkor is friss, csak lassabban. **A rebuild ezért MINDKETTŐT csinálja: push ÉS kiadás.**
3. **Az API definitív válasza után NINCS raw-lekérdezés.** Az API nem CDN-cache-elt, tehát nincs mit „frissebbre" várni — a raw legfeljebb egy elavult példányt adna. Offline teszt bizonyítja, hogy azonos build esetén **egyetlen** HTTP-kérés fut.

**UGYANEZ A HIBA A `bump_build.py`-BAN IS OTT VOLT**: az is a cache-elt raw-ból olvasta a publikált build-számot, tehát két gyors egymás utáni kiadásnál a második a RÉGI számot látta volna — pontosan azt a visszaléptetést okozva (155 → 150), ami ellen az a szkript eredetileg készült. Most három szám maximumából emel: helyi / raw / kiadás.

**HÁROM NÉMA HIBA A CSERE-SZKRIPTBEN, amit ugyanez a kör javított** (`build_swap_bat`, most tiszta függvény — egy elrontott .bat némán nem csinál semmit, a hibája csak egy ügyfél gépén derülne ki):
- **A fix `timeout /t 3` kevés.** Egy PyInstaller onefile exe **két folyamat** (kicsomagoló bootloader + Python gyerek), és a bootloader még fogja a fájlt, amikor a gyerek már kilépett. A `copy` ilyenkor elbukott, a `2>&1` elnyelte, és **a régi verzió indult el újra** — a technikus pedig azt hitte, frissült. A valódi feltétel nem az idő, hanem hogy a fájl **cserélhető-e**: most a PID-re vár, majd ciklusban próbálkozik (~60 mp).
- **A szkript nem hagyott nyomot.** A csere a program kilépése UTÁN fut, tehát a fő napló a legkritikusabb lépésről semmit nem tudott. Most jelentés-fájlba ír, amit a következő indulás beolvas a naplóba (`read_update_report`) — a *„frissítettem, mégis a régi van fent"* bejelentés így válaszolható meg.
- **A .bat UTF-8-cal íródott, a cmd viszont a rendszer kódlapján olvassa.** Ékezetes felhasználónévnél (`C:\Users\Józsi\AppData\Local\Temp\…`) az útvonalak elromlanak, és a szkript némán nem csinál semmit. Most `locale.getpreferredencoding(False)` + `newline=''` (a szövegmód a `\r\n`-t `\r\r\n`-re fordítaná, amitől a cmd nem találja a `goto` címkéket).

**A LETÖLTÖTT FÁJL ELLENŐRZÉSE MOST KAPU, NEM FIGYELMEZTETÉS.** A régi kód egy gyanúsan kicsi fájlra csak WARNING-ot írt, **majd ugyanúgy lecserélte vele a futó programot** — egy 404-es HTML oldalból így „frissítés" lett, a gép pedig indíthatatlan exével maradt. Két olcsó jel dönt: az `MZ` aláírás és az 1 MB-os alsó korlát; bukás esetén a letöltés törlődik és a jelenlegi verzió sértetlen marad.

**ELLENŐRZÉS:** 24 offline állítás a kiadás-parse és az ág-választás minden esetére (több build, asset-tároló keverve, draft, exe nélküli kiadás, rate limit, üres lista), plusz 26 állítás, amiből a `.bat` **valóban lefutott egy homokozóban** (`ping.exe` másolatai „programként"): lecserélte mindkét példányt, naplózott, törölte magát. Ha az updater változik, ez a kettő a regressziós teszt.

#### AZ API VÁLASZA NEM „DEFINITÍV" — A KIADÁS ELMARADHAT (2026-09-18, terepen mérve)

**Terepi bejelentés:** *„az auto update nem működik, egy 313-as buildről nem tudtam felupdatelni 315-re, nem találja meg, nem dobja fel semmi"*. A mérés azonnal megadta az okot, és a hiba **ebben a fájlban dokumentált optimalizálásból** származott:

```
GitHub kiadások:                       build-311, build-312, build-313   <- a 314 és a 315 HIÁNYZIK
raw driver_tool.py BUILD_NUMBER:       315
```

A 2026-09-08-i „azonos build → definitív válasz, azonnal vissza" szabály **hallgatólagosan feltételezte, hogy minden build kap kiadást**. Amikor a rebuild [4/4] lépése kimaradt, az API build-száma elmaradt a raw mögött, és a program magabiztosan azt mondta, hogy *„nincs újabb verzió"* — miközben két build-nyi frissítés állt a raw-on. Reprodukálva (`common.BUILD_NUMBER = 313`): `[UPDATE] Kiadás-szám (API): 313, Helyi: 313` → `{'has_update': False}`.

**AZ ÚJ SZABÁLY: a két forrás közül a NAGYOBB nyer, nem az, amelyik előbb válaszol.** Az API csak akkor zárja le a kérdést, ha **ÚJABB** buildet ad (az a gyors, egykérdéses út); ha nem ad újabbat, a raw-ot **akkor is** megkérdezzük. A 2026-09-08-i nyereség megmarad: ha az API válaszolt, a raw **egyetlen** kérdést kap (a hálózat bizonyítottan él, itt már csak az a kérdés, elmaradt-e a kiadás), nem hármat.

**ÉS A LETÖLTÉS FORRÁSA KÖVETI A DÖNTÉST — ez a rész nem elhagyható.** Ha a raw a frissebb, az exe-t is onnan kell hozni: a build-313 kiadás melléklete egy **313-as exe**, amivel „frissítve" a gép ugyanott maradna, és a következő indulásnál újra frissítést ajánlana — **végtelen kör**. Ezért a raw ág konkrét `exe_url`-t ad vissza, és a `stage_update` `expect_build` védőhálót kapott (egy a kértnél régebbi kiadás mellékletét soha nem tölti le). Ha valaki ezt „egyszerűsítené" vissza, ez a bekezdés a válasz.

**A raw ág `_raw_build()`-ba került** (build-számot ad vissza, nem dict-et), és az „azonos build → nincs újrapróbálkozás" szabály ott változatlanul él. Ellenőrizve három szimulált gépen (313 → van frissítés a raw-ról; 315 → nincs; 316 = push előtti fejlesztői gép → nincs).

> **⚠️ A JAVÍTÁS ÖNMAGÁT NEM TUDJA KÉZBESÍTENI — A KÖVETKEZŐ KIADÁSNÁL A `gh release create` KÖTELEZŐ.** A terepen lévő **Build 311–315** példányok a HIBÁS kódot futtatják: az API-tól 313-at kapnak, és mivel az nem nagyobb a sajátjuknál, „definitív nincs újabb"-bal megállnak — **a raw-ot meg sem kérdezik**. Vagyis amíg nincs `build-N` kiadás N > 315-tel, **egyetlen fielded példány sem fogja megtalálni a frissítést**, akárhányszor pusholunk. A javított kód csak azokra a gépekre jut el, amelyek egy **kiadással** megjelent buildre frissülnek; onnantól a két forrás közül a nagyobb nyer, és egy elmaradt kiadás már csak lassítja (raw, ~5 perc), nem blokkolja a frissítést.

#### ÉS AMIÉRT A KIADÁS ELMARADT: A REBUILD SZKRIPT „SIKERES KIADÁS"-T ÍRT KI A BUKÁSRA IS

A `gh` telepítve volt és be is volt jelentkezve (mérve), a build-314/315 kiadás mégsem jött létre — a szkript pedig **ugyanúgy kiírta a nagy keretes „SIKERES KIADAS" feliratot**, miközben a `[!]` sor pár sorral feljebb elveszett. Ez maga a néma hamis siker, amit ez a fájl mindenhol üldöz. Három javítás:

1. **A `gh` kimenetét nem nyeljük el** (a `>nul 2>&1` csak a *létezés-ellenőrzésen* van), és a bejelentkezés külön ellenőrzött lépés (`gh auth status`) — egy lejárt token hibája különben a kimenetben veszne el.
2. **A kiadás után VISSZAOLVASUNK** (`gh release view`): a verdikt az, hogy ott van-e a GitHubon, nem a `gh` visszatérési kódja. Ugyanaz az elv, mint a programban (bind-ellenőrzés, `verify_failed_installs`).
3. **A záró keret az igazat mondja**: bukásnál „Build N FELTOLTVE, DE A KIADAS KIMARADT", a következménnyel (a gépek a lassabb raw úton értesülnek) és a kézi pótlás parancsával együtt.

**A YouTube-projekt bat-ja NEM volt a megoldás** (a felhasználó felvetette, hogy másoljuk le): élőben összevetve a [4/4] szakasza **gyakorlatilag azonos**, sőt kevésbé robusztus — nincs benne `gh release view` létezés-ellenőrzés, és a `gh release create` végén egy `2>nul` **elnyeli a hibaüzenetet**. A DriverVarázsló szkriptje tehát nem lemásolásra, hanem javításra szorult.

**Ellenőrzés:** a [4/4] szakasz mind a **4 ága lefutott egy homokozóban** egy `gh`-csonkkal (kiadás létrejön / a `create` elbukik / nincs bejelentkezve / a kiadás már létezik), és minden ágon az elvárt keret és a `gh` hibaüzenete jelent meg. **Harness-csapda a következő ilyen teszthez:** a csonk egy `.bat`, és egy `.bat`-ból **`call` nélkül** hívott másik `.bat`-ból a vezérlés **nem tér vissza** — a futás ott véget ér. A valódi `gh.exe`-t ez nem érinti, ezért a `call`-t csak a tesztben, és csak a ténylegesen `gh `-val KEZDŐDŐ sorok elé szabad beszúrni (az echo-zott és a REM-ben álló „gh …" szövegeket nem). Egy `[!]` az `echo`-ban `[]`-ként jelenik meg `enabledelayedexpansion` mellett — ezért `[HIBA]` a jelölés.

### Régi Windows-támogatás (Win 7 / 8 / 8.1)

A bolt Win 7/8/8.1-es gépeken is használni akarja a programot (explicit user decision, 2026-08-13). Az alábbiak **mind mérve vannak** — ne tippelj újra, és ne "egyszerűsítsd" el őket.

- **A jelenlegi exe Windows 8.1 alatt EL SEM INDUL, és ez nem javítható kódból.** A build Python 3.12 + PyInstaller 6.17, a Python 3.9+ pedig már csak Win8.1+ (Win7-en `api-ms-win-core-path-l1-1-0.dll` hiány), a PyInstaller 6 bootloadere szintén dobta a Win7-et. Ilyenkor **log sem születik** — egy "nem indul el, nincs log" jelentés Win7/8.0-n ezt jelenti, ne keresd a hibát a kódban. Jó hír: a forrás **Python 3.8-cal is lefordul** (2026-08-13-i audit: sehol nincs `match`, `list[str]`, `X | None`, `removeprefix`; az egyetlen külső függőség a pywebview), tehát egy külön "legacy" exe reális, a logika érintése nélkül.
- **A GUI-nak KÉT külső feltétele van, és a .NET-et kell előbb nézni.** A pywebview a pythonnet-en át .NET Framework-öt tölt be, a **pythonnet 3.x minimuma .NET 4.7.2** (Release 461808) — a Win8.1 alapból 4.5.1-et, a Win7 SP1 3.5.1-et hoz. Ha hiányzik, a betöltés **NEM Python-kivétel, hanem natív összeomlás**: se `sys.excepthook`, se a 60 mp-es watchdog nem fut le. Terepen (2026-08-13, Win 8.1, Build 269) így nézett ki: a napló utolsó sora **háromszor egymás után** a `[MAIN] webview.start() hívása...`, utána semmi, a felhasználónak pedig annyi látszott, hogy "elindítom és nem történik semmi". Ezért `driver_tool.py` a .NET-et **a WebView2 telepítése ELŐTT** ellenőrzi: enélkül a program előbb letöltene és percekig telepítene egy WebView2-t, hogy utána ugyanúgy összeomoljon. Felülbírálás: `--force-gui`.
- **WebView2: 109 a plafon ezeken a rendszereken** (a Microsoft újabbat nem szolgál ki rájuk), a pywebview 6.1-gyel csomagolt SDK viszont **1.0.2957.106**. A `prereq.log_environment` ezért logolja egymás mellé a runtime és az SDK verzióját — ha a felület egy 4.7.2+ .NET-tel bíró gépen is elszáll, ez a két szám a következő gyanúsított. (A `MIN_WEBVIEW2_MAJOR = 109` kommentje az `ICoreWebView2Environment10`-re hivatkozik; ez az SDK-hoz képest elavult információ.)
- **GUI-összeomlás-őr (`common.gui_*`)**: mivel a natív összeomlást nem lehet minden okra megelőzni, azt garantáljuk, hogy **csak egyszer fordulhasson elő**. A `webview.start()` előtt jelzőfájl (`gui_indulas.json`) készül, a DOM elkészültekor törlődik (`app/gui/base.py: set_window`). Ha a következő induláskor még ott van, egyből a működő CLI jön, érthető üzenettel. A jelző a **környezet ujjlenyomatát** is tárolja (WebView2 + .NET verzió), így egy megjavított gép magától újra megpróbálja a felületet — enélkül egy .NET-frissítés után is örökre CLI-ben ragadna. Sérült/olvashatatlan jelző soha nem blokkol. **Az őr CSAK a natív összeomlás ellen van (2026-09-28):** ha a folyamat túléli a kísérletet (elkapott kivétel, felület nélküli visszatérés), `gui_attempt_failed_cleanly` törli a jelzőt — terepen (Build 344) egy elkapott kivétel után két indulás esett feleslegesen CLI-be, és az üzenet egy .NET 4.8-as gépen is .NET-telepítést javasolt (az üzenet azóta a tényleges .NET-állapothoz igazodik).
- **Letöltés: a PowerShell-fallback három ágú, és a harmadik NEM elméleti** (`common._ps_download_script`). Mérve `powershell -Version 2` motor alatt (ez a Win7 alap-PowerShellje): az `Invoke-WebRequest` ott nem is létezik (PS 3.0+), a **.NET-es utak pedig mind elhasalnak** — WebClient és HttpWebRequest egyaránt, ugyanazzal a *"A konfigurációs rendszer inicializálása sikertelen volt"* hibával —, miközben a natív **`certutil -urlcache -split -f` hibátlanul lehozta a teljes fájlt** (1 695 960 bájt; a `bitsadmin` szintén). Ezért a lánc: IWR → WebClient → certutil, minden ág után **méret**-ellenőrzéssel (a 0 bájtos csonkot a következő ág késznek látná — néma hamis siker). A certutil a rendszer schannel-vermét használja, tehát a tanúsítvány-ellenőrzés teljes értékű marad.
- **A fallbackot minden TLS-hiba kiváltja, nem csak a tanúsítvány-hiba** (`DOWNLOAD_PS_FALLBACK_ERRORS`). Terepen (2026-08-13, Win 8.1) a WebView2 telepítő letöltése `[SSL: UNEXPECTED_EOF_WHILE_READING]`-gel bukott: ez az OpenSSL 3.x szigorítása (az 1.1.1 még elnézte a `close_notify` nélküli bontást), és pont a régi gépek úton lévő eszközei csinálják — miközben ugyanaz az URL a rendszer schannel-vermén simán lejön. DNS-hibára/404-re/időtúllépésre szándékosan **nem** kapcsol be.
- **Minden letöltés a közös letöltőn megy, az auto-updater is.** A `check_for_updates` és a `stage_update` korábban csupasz `urllib` volt: egy régi gépen ugyanaz a TLS-hiba buktatta volna el őket, vagyis **épp az a javítás nem jutott volna el a géphez, ami a hibát orvosolja**. Ha ide új letöltés kerül, a `download_with_cert_fallback`/`fetch_text_with_cert_fallback` az egyetlen helyes út.
- **Hosszú műveletnek látszania kell** (explicit user decision, 2026-08-13): a WebView2 letöltés+telepítés 1-3 perces, korábban teljesen néma szakasz volt (egy "Kattints OK-ra" ablak, majd semmi), és a technikus nem tudta megkülönböztetni a dolgozó telepítőt a kifagyott programtól. A belépési pont ezért `ensure_console()`-lal konzolt nyit, a letöltéshez százalékos sávot ad (`progress_cb`), a telepítés alatt pedig eltelt-idő számlálót. Ez nem "extra ablak": siker esetén a program `os.execv`-vel új folyamatként indul újra, kudarc esetén úgyis a CLI jön, aminek kell a konzol.
- **MÉG NINCS MEGOLDVA — a `pnputil` szintaxisa.** A kód végig a **perjeles** alakot használja (`/delete-driver`, `/add-driver`, `/export-driver`, `/scan-devices`, 104 hívási hely), ami **csak Win10 1607-től** létezik; Win7/8/8.1-en a régi kötőjeles alak kell (`-e`, `-a`, `-d`, `-f -d`), az `/export-driver` (1607+) és a `/scan-devices` (1703+) pedig **egyáltalán nem elérhető**. A `dism /Online /Get-Drivers` viszont Win8-tól már jó. Ha ez sorra kerül, a javítás **egy központi parancsépítő** legyen (`is_legacy_windows()` alapján), nem 104 hívási hely külön-külön — a duplikált logika, aminek egy példánya lemarad, ennek a projektnek a legrégebbi visszatérő hibája.

### ÜRES (FEHÉR/FEKETE) FELÜLET PÁR MÁSODPERCCEL AZ INDULÁS UTÁN = IDEGEN DLL A WEBVIEW2-BEN (2026-09-18, mérve)

> **HA EGY „NEM INDUL EL / ÜRES AZ ABLAK" BEJELENTÉST VIZSGÁLSZ, EZT OLVASD EL ELŐBB. Egy teljes session ment el arra, hogy a saját frontend-változtatásaimat gyanúsítottam, miközben a hiba a gépen futó MSI Afterburner overlay-e volt.**

**A tünet:** a program elindul, a napló szerint mindent hibátlanul lefuttat (`get_init_data`, `load_drivers`, `drivers_loaded`, `get_driver_usage` — mind rendben, kivétel nélkül), a felület viszont **2-4 másodperc után üres**: fullscreenben fehér, ablakosban fekete. A `[JS_UI]` naplóban **egyetlen hiba sincs**, és a JS az „összeomlás" UTÁN is válaszol (a kattintások továbbra is naplózódnak) — mert nem a JS halt meg, hanem a WebView2 **host-folyamata**.

**A DIAGNÓZIS ÚTJA, három lépésben — ezt lehet újra használni:**

1. **Van-e crash dump?** A pywebview futásonként külön temp user-data mappát csinál: `%TEMP%\tmp*\EBWebView\Crashpad\reports\*.dmp`. Ha van, a felület összeomlott, nem „csak nem jelent meg".
   > **AMIT ITT NE KÖVESS EL (én elkövettem): a dumpok HIÁNYA nem bizonyíték.** A tisztán kilépő futások temp-mappája (a dumpjaival együtt) törlődik, ezért az „előző buildből nincs dump, tehát az jó volt" következtetés **hibás** — pontosan ez vitt félre.
2. **Mi a kivétel-kód?** A minidump `ExceptionStream`-je (stream type 6) pár soros Python-nal kiolvasható: `0xC0000005` = access violation, `0xE0000008` = Chromium OOM, `0x80000003` = CHECK-bukás. Itt mindhárom dumpban **ugyanaz a cím**: `0x00000001801490AF` — az azonos cím maga is árulkodó: determinisztikus null-deref, nem véletlen memóriahiba.
3. **MELYIK MODUL az?** A `ModuleList` stream (type 4) megadja minden betöltött modul bázisát és méretét; a hibás címet tartalmazó modul a tettes. **A `0x180000000` bázis x64-en a DLL-ek alapértelmezett preferált bázisa**, tehát már a cím elárulta, hogy nem az exe, hanem egy DLL. A mérés:
   ```
   hibás cím: 0x1801490AF
   >>> A MODUL: C:\Program Files (x86)\RivaTuner Statistics Server\RTSSHooks64.dll
       bázis 0x180000000, eltolás +0x1490AF
   a folyamat 113 moduljából az EGYETLEN nem-rendszer modul
   ```

**A TETTES: a RivaTuner Statistics Server (`RTSSHooks64.dll`)**, ami az MSI Afterburnerrel jön, és **minden renderelő folyamatba beinjektálja magát** az overlay miatt — a WebView2-be is. Ott hal meg. A megoldás a technikusnak: **zárja be az RTSS-t** (vagy RTSS → Add → `DriverVarazloso.exe` + `msedgewebview2.exe` → **Application detection level = None**). A felhasználó bezárta, és a program azonnal hibátlanul indult.

**HÁROM MÉRÉS, AMI KIZÁRTA A SAJÁT KÓDUNKAT — és amit egy hasonló bejelentésnél újra el kell végezni, MIELŐTT bármit átírsz:**

| kísérlet | eredmény |
|---|---|
| a MOSTANI `ui.html` × 3 | 2× összeomlott, 1× nem |
| a **KORÁBBI, commitolt** `ui.html` × 3 | 2× összeomlott, 1× nem |
| **egy üres lap** (`<h1>Trivialis</h1>`) × 4 | 1× összeomlott 3,0 s-nál |

Az üres lap a döntő: ha az is elszáll, **a tartalomnak semmi köze hozzá**. A véletlenszerűség is információ: egy layout-hiba determinisztikus lenne. (A `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--disable-gpu` **nem segít**: 5-ből 4 így is összeomlott — az RTSS a hookjait attól függetlenül beteszi.)

**A REPRODUKCIÓS HARNESS (`repro3.py` minta), és két csapda benne, amibe beleestem:**
- **API-stub NÉLKÜL a teszt értéktelen.** Az első próbám `js_api` nélkül futott: a `pywebviewready` kezelője az első hívásnál kivételt dobott, a `try/catch` elnyelte, és a lap **statikus maradt** — vagyis pont az a szakasz nem futott le, amiben a hibát kerestem. Kell egy `js_api` osztály a valódi metódusnevekkel ÉS a valódi `evaluate_js`-es emit-út.
- **`destroy()`-jal a teszt minden lapra „összeomlást" mutat**, mert a teardown maga is dumpot generál. A helyes mérés: a dumpot **futás közben** figyeljük (0,5 mp-enként), a végén `os._exit(0)` teardown nélkül. Enélkül két egymásnak ellentmondó „eredményt" kaptam, és mindkettő hamis volt.

**ÉRDEMES LENNE (még nincs megírva):** a `[PREREQ]` blokk induláskor vegye észre a futó injektáló overlay-eket (RTSS, és a hasonlók), írja ki a naplóba, és mondja meg a technikusnak, hogy zárja be — különben ez a hiba minden ilyen ügyfélgépen egy néma, üres ablak. Ugyanaz az elv, mint a .NET/WebView2 előfeltétel-ellenőrzésnél: *„gondolkodj elore milyen problemak johetnek elo es az exe indulas elott oldja meg"*.

## Git / release rules (IMPORTANT)

- **Never commit, push, or release on your own initiative** — not even after finishing a fix the user asked for. Leave changes in the working tree, tell the user they're ready, wait for an explicit "commitold/pushold/adj ki buildet" instruction. (A push to `main` is a de-facto deployment: the auto-updater serves `driver_tool.py` and `dist/DriverVarazslo.exe` straight from the repo, and it cannot be rolled back by lowering the build number.)
- **When the user asks for a release, use `rebuild_verzioszam_novelessel_es_github_pushal.bat`** — don't re-implement its steps by hand. It ends with `pause` — run as `cmd /c rebuild_verzioszam_novelessel_es_github_pushal.bat < nul`. Its `git add .` picks up the whole `app/` package; `git add -f dist/DriverVarazslo.exe` force-adds the gitignored exe.
- ~~`build.bat` (build only, no bump/commit/push) is safe to run anytime for local verification.~~ **[2026-09-27, explicit user decision: *„a sima build.bat is adjon mar hozza + 1 etz a build szamhoz"*] A `build.bat` MOSTANTÓL +1-gyel EMELI a `BUILD_NUMBER`-t** (ugyanaz a `bump_build.py`, mint a kiadó szkriptben: a helyi / raw / kiadás-címke szám maximumából, tehát sosem csökkenhet), és a `version_info.txt`-t is szinkronba hozza. Commit, push és GitHub-kiadás továbbra sincs benne. **Következmények:** (a) minden `build.bat`-futás egy számot „elhasznál", tehát egy utána jövő kiadás kihagy számokat (pl. 338 helyi build → a kiadás 339) — ártalmatlan, a szám szigorúan nő; (b) **a `build.bat` már NEM mellékhatás-mentes**: a `driver_tool.py`-t és a `version_info.txt`-t módosítja, tehát Claude helyi ellenőrzéshez (ha a felhasználó egyáltalán kéri) a közvetlen `python -m PyInstaller …` hívást használja, SOHA ne a `build.bat`-ot. Homokozóban tesztelve (a PyInstaller-sor csonkolva): 337 → 338, fájlverzió 1.0.338.0, a valódi projekt érintetlen.
- **Saját hiba (2026-10-11, Build 367): a kiadó szkript a commit előtt megállt.** Egy audit után beírtam a `git add` és a `git commit` közé egy `git diff --cached --stat` sort, hogy látszódjon, mi megy ki. Valódi konzolban a git ezt a lapozóján (`less`) át írja ki, ami billentyűre vár; a technikus „nem működik"-nek látta, és bezárta. Mért állapot utána: `BUILD_NUMBER` 367, friss `dist/DriverVarazslo.exe`, minden stage-elve, commit nincs (`git log` utolsó sora Build 366). A saját ellenőrzésem nem fogta meg, mert a Bash-eszköz kimenete nem terminál, ott a git nem lapoz. Javítva: `git --no-pager diff --cached --stat`. **Az ellenőrzés, ami megfogta volna:** egy `.bat`-ba írt git-parancs mindig `--no-pager`-rel menjen, és az új sort valódi konzolablakban is futtasd le.
- The GitHub remote URL points at the repo's old name (`.../drivervarazslo`); GitHub redirects it — the "This repository moved" notice on push is normal, not an error.

## Build / release

### AZ EXE MÉRETE A BUILD GÉP CSOMAGJAITÓL FÜGGÖTT — 3,4 MB HOLT SÚLY (2026-09-21, Build 323, mérve)

**Terepi megfigyelés:** az exe 15,72 MB-ról **16,58 MB-ra** ugrott, miközben a kódváltozás ~250 sor Python volt. A korábbi buildek deltája 2-3 KB.

**A két exe CArchive-tartalmának összevetése (`PyInstaller.archive.readers.CArchiveReader`) megadta az okot, és az NEM a kód:**

| | Build 322 | Build 323 |
|---|---|---|
| Python | **`python314.dll`** (2,67 MB) | **`python312.dll`** (2,52 MB) |
| pythonnet | `Python.Runtime.dll` + **110 db `System.*.dll` facade** | `Python.Runtime.dll` (a 3.0.5 **csak 4 fájlt szállít**) |
| cryptography | **nincs** | **`_rust.pyd` 3,25 MB** + `bcrypt` 137 KB |
| CArchive-bejegyzés | 169 | 73 |

Vagyis a build gépen a **Python 3.14 eltűnt** (ma már csak `C:\Python312` van), és a 3.12-es környezetben **telepítve van a `cryptography`** (a `paramiko` függősége). A `webview` (pywebview) a `__generate_ssl_cert()` függvényéből importálja — `try/except ImportError`-ban —, és ez a függvény **KIZÁRÓLAG `webview.start(ssl=True)` esetén fut**. A program `webview.start(func=on_start, debug=False)`-t hív, tehát az ág halott. A 3.14-es környezetben a `cryptography` nem volt telepítve, az opcionális import elbukott, és PyInstaller ki sem tette — **ezért függött az exe mérete attól, mi van épp a build gépen telepítve**.

**AMI EBBŐL NEM REGRESSZIÓ, pedig annak látszik:** a 110 hiányzó `pythonnet\runtime\System.*.dll` **nem veszteség** — a telepített pythonnet 3.0.5 runtime mappája élőben ellenőrizve **4 fájlból áll** (`Python.Runtime.dll` + 3 metaadat); a facade-készlet egy másik pythonnet-verzióból származott. A GUI-hoz szükséges minden darab megvan (`clr`, `clr_loader.netfx`, `Python.Runtime.dll`, `ClrLoader.dll`, a WebView2 DLL-ek) — a 3.12-es környezetben élőben is betölt (`clr` OK, `System.Environment.Version` 4.0.30319, `webview.platforms.edgechromium` OK). **És a Build 323 a DOKUMENTÁLT build-Pythonnal készült**, vagyis a 322 volt a kilógó, nem a 323.

**A JAVÍTÁS: `excludes=['cryptography', 'bcrypt', 'paramiko', 'nacl']` a `DriverVarazslo.spec`-ben.** Ellenőrző build a scratchpadbe (a `dist/` érintetlen): **16,58 MB → 12,92 MB, −3,67 MB**, és a kiesett bejegyzések tételesen CSAK a `cryptography`/`bcrypt` és a metaadataik + a `python3.dll` (a stable-ABI forwarder, amit épp a `_rust.pyd` igényelt). Minden kritikus modul és bináris a helyén (PYZ: `clr`, `clr_loader.netfx`, `webview.platforms.edgechromium`, `app.gui.api`, `app.cli.api`, `ssl`; bináris: `Python.Runtime.dll`, `ClrLoader.dll` ×2, WebView2 ×3, `ui.html`). **Az auto-updater minden fielded gépre letölti az exét, tehát a 3,4 MB nem elméleti.**

**ÁLTALÁNOS TANULSÁG:** egy PyInstaller-build kimenete **a build gépen telepített csomagoktól** függ, nem csak a forrástól — egy opcionális, `try/except`-es import a függőségeidben néma méret- (és felület-) különbséget okoz két gép közt. Ha az exe mérete indokolatlanul változik, a `CArchiveReader`-es TOC-összevetés 30 másodperc alatt megadja a választ; a `build/<név>/xref-<név>.html` pedig megmondja, **melyik modul** húzta be a plusz csomagot.

**AMIT EZ NEM VÁLASZOL MEG: a Build 323-at MÉG SOHA NEM FUTTATTUK.** A napló utolsó bejegyzése a build előttről való. Mivel alatta a **teljes Python-futtatókörnyezet kicserélődött** (3.14 → 3.12), egy indítás-próba (elindul-e a GUI, nem esik-e CLI-be) kötelező, mielőtt gép megy vele az ügyfélhez.


There is no test suite and no `requirements.txt`; the only third-party runtime dependency is `pywebview` (import-guarded in `app/common.py` with a clear exit message). The build machine runs Python 3.12 + PyInstaller 6.17 (the README's "Python 3.14" mention is aspirational). Everything is Windows-only and admin-only: the app self-elevates, and most code paths call `dism`/`pnputil`/`powercfg`/WMI, so they cannot be exercised on a non-Windows host at all — a syntax check (`py_compile`) plus importing `app.gui.api` / `app.cli.api` is the most that can be verified without running the exe on a real machine.

- **Full release** (bump, build, commit, push, **GitHub-kiadás**): `rebuild_verzioszam_novelessel_es_github_pushal.bat` from the repo root. It calls `bump_build.py` first, which increments `BUILD_NUMBER` **in `driver_tool.py`** from `max(local, raw-ról olvasott, kiadás-címkékből olvasott)` and keeps `version_info.txt` in sync — never hand-edit `BUILD_NUMBER` to a lower value. **A [4/4] LÉPÉS (`gh release create build-<N>`) NEM DÍSZ: ez teszi a frissítést azonnal láthatóvá** — nélküle a fielded gépek csak a raw CDN-ről értesülnek, ami push után ~5 percig a régi verziót adja (lásd [Az auto-updater](#az-auto-updater-a-github-releases-api-ról-megy-a-raw-cdn-csak-tartalék-2026-09-17)). A szkript ezért a `gh` hiányát külön kiírja, és a push hibáját is ellenőrzi (korábban némán ment tovább). Ha a `gh` nincs telepítve: `winget install GitHub.cli`, majd `gh auth login`.
- **Manual build only**: `python -m PyInstaller --clean --noconfirm DriverVarazslo.spec` (preferred, és ez NEM emel build-számot), or `build.bat` (2026-09-27 óta **+1-gyel emeli a `BUILD_NUMBER`-t**, lásd a Git/release szabályokat) — but `build.bat` ends with `pause`, so from a non-interactive shell it must be `cmd /c build.bat < nul` or it hangs with no visible output. Output: `dist/DriverVarazslo.exe`. The spec's `Analysis(['driver_tool.py'])` follows the `app/` package imports automatically — no `hiddenimports` needed for new `app/` modules as long as they're reachable through normal imports (a module imported only dynamically WOULD need adding). The spec embeds `icon_drivervarazslo.ico` + `version_info.txt` (keep populated/synced — unsigned exe with stale version info trips Defender) and disables UPX for the same reason. The icon is a 6-image multi-resolution ICO (16/32/48/64/128/256 px, 8-bit) since Build 199 — if it's ever regenerated, keep all sizes (the pre-199 icon was a single tiny image). `build.bat` needs `@echo off` + `chcp 65001`.
- **Run from source**: `python driver_tool.py` (GUI, self-elevates via UAC) or `python driver_tool.py --cli`.
- `DriverVarazslo.spec`, `version_info.txt`, and `dist/DriverVarazslo.exe` are intentionally tracked in git — the exe is what the in-app updater downloads.

### 2026-10-12: review-javítások (frissítő, letöltő, indítás)

Kódolvasásból, a felhasználó kérésére; terepen NEM reprodukálva. Teszt: `tests/test_update_truth.py` (5 eset; a 3 hiba-eset elbukik a régi kódon, a 2 jó-út eset mindkettőn átmegy).

- **Csonka letöltés:** a `download_with_cert_fallback` Python-ága nem vetette össze a letöltött bájtokat a `Content-Length`-del, a `http.client` pedig bontott kapcsolatnál kivétel NÉLKÜL ad rövid fájlt (a katalógusnál terepen mérve: 1,22 GB-ból 139 MB). Az updaterben egy 1 MB fölötti, `MZ`-vel kezdődő csonka exe átment volna a kapun (a verzió-erőforrás a fájl végén van, tehát az sem olvasható). Most eltérésnél a csonk törlődik és kivétel jön — minden letöltésre (updater, stresstools, BootFixer, WebView2…).
- **Régi exe a CDN-ről:** a raw úton a `driver_tool.py` és a `dist/DriverVarazslo.exe` KÜLÖN kerül a Fastly-cache-be; a push után a raw már az új BUILD_NUMBER-t mutathatja, miközben az exe még a régi → „frissítés” ugyanarra a buildre, és minden indulás újra felajánlja. Most a `stage_update` a letöltött exe fájlverziójának harmadik tagját (= build, `version_info.txt`, `bump_build.py` tartja szinkronban) összeveti az `expect_build`-del; ha kisebb, a régit törli és „próbáld 5 perc múlva” hibát ad. Ha a verzió nem olvasható, nem dönt (nem blokkol). Élőben ellenőrizve a `dist/DriverVarazslo.exe`-n: `1.0.371.0 -> 371`.
- **A `--cli` indítás kilépése** `sys.exit(0)` volt, nem `cleanup_zombies()` + `os._exit(0)` (8. fejezet); a `cleanup_zombies` ezért feljebb került. Két indítási kivételág `exc_info`-t kapott.

