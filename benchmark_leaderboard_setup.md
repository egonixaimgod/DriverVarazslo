# Benchmark ranglista — felhő-backend beállítása (Google Sheet + Apps Script)

A Benchmark nézet közös felhő-ranglistája egy nagyon egyszerű HTTP-végpontot használ:

- **GET** → visszaadja a teljes ranglistát JSON tömbként (soronként egy gép),
- **POST** (JSON body) → beszúr / frissít egy gép eredményét a `machine_id` alapján
  (ugyanarról a gépről újra feltöltve a meglévő sor **frissül**, nem duplikálódik).

A legegyszerűbb, ingyenes megoldás erre egy **Google Sheet + Google Apps Script
webalkalmazás**. Nincs szerver, nincs költség, és az adatokat táblázatként te is látod,
szerkesztheted. (Ha később skálázni akarsz vagy szigorúbb védelmet szeretnél a spam ellen,
ugyanezt a GET/POST protokollt beszélő Supabase / Cloudflare Worker backendre lecserélheted
— a programban csak a végpont URL-t kell átírni.)

---

## 1. Google Sheet létrehozása

1. Menj a <https://sheets.google.com> oldalra, hozz létre egy **új üres táblázatot**.
2. Nevezd el pl. `DriverVarazslo Benchmark`-nak (mindegy, csak neked legyen egyértelmű).

Nem kell fejlécet vagy oszlopokat kézzel felvenned — a script létrehozza a `Leaderboard`
munkalapot és a fejlécet az első használatkor.

## 2. Apps Script hozzáadása

1. A táblázatban: **Bővítmények → Apps Script** (Extensions → Apps Script).
2. Töröld ki a `Code.gs`-ben lévő mintakódot, és **másold be az alábbi teljes kódot**:

```javascript
const SHEET_NAME = 'Leaderboard';
const HEADERS = ['machine_id','machine_name','cpu','motherboard','ram','gpu','cinebench','heaven','ts','build'];

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) { sh = ss.insertSheet(SHEET_NAME); }
  if (sh.getLastRow() === 0) { sh.appendRow(HEADERS); }
  return sh;
}

// GET: a teljes ranglista JSON tömbként (soronként egy gép objektuma).
function doGet() {
  const sh = getSheet_();
  const values = sh.getDataRange().getValues();
  const rows = [];
  for (let i = 1; i < values.length; i++) {
    const r = {};
    HEADERS.forEach((h, j) => { r[h] = values[i][j]; });
    rows.push(r);
  }
  return ContentService
    .createTextOutput(JSON.stringify(rows))
    .setMimeType(ContentService.MimeType.JSON);
}

// POST: egy gép eredményének beszúrása / frissítése (upsert a machine_id-re).
function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const data = JSON.parse(e.postData.contents);
    const sh = getSheet_();
    const values = sh.getDataRange().getValues();
    let rowIndex = -1;
    for (let i = 1; i < values.length; i++) {
      if (String(values[i][0]) === String(data.machine_id)) { rowIndex = i + 1; break; }
    }
    const row = HEADERS.map(h => (data[h] !== undefined && data[h] !== null) ? data[h] : '');
    if (rowIndex > 0) {
      sh.getRange(rowIndex, 1, 1, HEADERS.length).setValues([row]);
    } else {
      sh.appendRow(row);
    }
    return ContentService
      .createTextOutput(JSON.stringify({ ok: true }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}
```

3. Mentsd el (💾 vagy Ctrl+S).

## 3. Webalkalmazásként közzététel

1. Jobb fent: **Telepítés → Új telepítés** (Deploy → New deployment).
2. A fogaskerék ⚙️-nél válaszd a **Webalkalmazás** (Web app) típust.
3. Beállítások:
   - **Leírás**: pl. `benchmark v1` (mindegy).
   - **Végrehajtás mint** (Execute as): **Én / Me**.
   - **Hozzáférés** (Who has access): **Bárki / Anyone**.
     > Ez azért kell, hogy a program bejelentkezés nélkül is tudjon írni/olvasni. A
     > végpont így nyilvánosan írható — egy szervizes ranglistához ez vállalható; ha
     > gond a spam, később tehetsz elé egy egyszerű megosztott jelszót / titkos kulcsot.
4. **Telepítés** → engedélyezd a hozzáférést (a Google figyelmeztet, hogy a script a
   táblázatodhoz fér hozzá — ez rendben van, a sajátodhoz).
5. Másold ki a kapott **Webalkalmazás URL-t**. Ez `https://script.google.com/macros/s/....../exec`
   formátumú — **a végén `/exec`-re kell végződnie** (ne a `/dev` verziót használd).

> Ha később módosítod a scriptet, a **Telepítés → Telepítések kezelése**-nél a meglévő
> telepítést **szerkeszd** és adj ki új verziót — így az `/exec` URL változatlan marad.

## 4. Beállítás a programban

A végpont **fixen a programba van drótozva** — minden legyártott exébe alapból ugyanaz az
URL kerül, semmit nem kell gépenként beállítani. Ha módosítani akarod (pl. új Sheet, új
deployment), egyetlen sort írsz át a forrásban:

```python
# app/benchmark_defs.py
BENCHMARK_API_URL_DEFAULT = "https://script.google.com/macros/s/....../exec"
```

Utána újra kell buildelni/kiadni. (Szándékosan nincs futásidejű felületi beállítás: a
végpont nem átírható a felhasználók által, mindig a beépített URL megy.)

---

## A két benchmark program a stresstools.zip-be

A programok portable módban indulnak a **közös `stresstools.zip`-ből** (ugyanabból, amiből
a stressztesztek). Tedd bele mindkettőt úgy, hogy ezek az exe-nevek meglegyenek benne
(bármelyik almappában lehetnek — a kereső rekurzívan megtalálja):

- **Cinebench R20** → `Cinebench.exe` (pl. `CinebenchR20\Cinebench.exe`)
- **Unigine Heaven** → `heaven.bat` (pl. `Heaven\heaven.bat`) — a Heaven indítója egy batch fájl; a program `CREATE_NEW_CONSOLE`-lal rendesen lefuttatja

Ezután töltsd fel az új `stresstools.zip`-et a szokásos GitHub release asset helyére.

> **Fontos:** ha egy gépen korábban már használták a stresszteszt funkciót, ott a régi
> (benchmark nélküli) csomag cache-elve lehet. A Benchmark-indítás ezt magától kezeli: ha
> nem találja a benchmark exe-t a kicsomagolt csomagban, egyszer kényszerít egy friss
> letöltést (az új ZIP-ből). Tehát elég az új ZIP-et feltölteni, a gépeken nem kell kézzel
> takarítani.
