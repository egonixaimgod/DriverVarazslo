"""Bolti tábla nyomtatás - a mag (2026-09-25, explicit user decision).

A bolt a pulton lévő gépekhez A5-ös "árlapot" nyomtat a saját Word-sablonjából
(`bolti_tabla_sablon.docx`, a program mellé csomagolva): logó, gépnév, specifikáció-
táblázat, ár. A sablon KÉT A5 oldalt tartalmaz (két gép), és a technikus így nyomtatja:
Word -> Nyomtatás -> "Laponként 2 oldal" + "Adott papírméretre: A4". A felhasználó
kérése szó szerint: *"egy az egyben azt kell kinyomni amit küldtem csak az informaciokat
tudjam atirni"* - ezért:

  1. A SABLONT NEM RAJZOLJUK ÚJRA, hanem kitöltjük (`fill_template`): a docx XML-jében
     csak a szövegeket cseréljük, minden formázás (logó eltolása, betűk, szegélyek,
     margók, A5 oldalméret) a sablonból jön, érintetlenül.
  2. A NYOMTATÁST MAGA A WORD VÉGZI (COM-on át, `build_word_print_ps`), pontosan azokkal
     a beállításokkal, amiket a technikus kézzel választana: PrintZoomColumn=2,
     PrintZoomRow=1 ("laponként 2 oldal"), PrintZoomPaperWidth/Height = A4 twipben
     ("adott papírméretre: A4"). Mérve (2026-09-25): a Microsoft Print to PDF-re küldve
     EGY db A4 (fekvő, 842x595 pt) lap lett, rajta a két A5 oldal, a sablon pontos
     kinézetével.
  3. A NYOMTATÓT NEM A WINDOWS ALAPÉRTELMEZETTJE DÖNTI EL. A `Application.ActivePrinter`
     beállítása átállítaná a rendszer alapértelmezett nyomtatóját is (ismert Word-
     viselkedés), ezért a `WordBasic.FilePrintSetup` hívást használjuk
     `DoNotSetAsSysDefault=1`-gyel. Mérve: az alapértelmezett nyomtató a nyomtatás
     előtt és után ugyanaz maradt.

KÉT ÉPÍTÉSI HIBA A SABLONBAN, amit a kitöltés kezel (mindkettő mérve, 2026-09-25):
  - NINCS OLDALTÖRÉS: a 2. gép csak azért kerül a 2. oldalra, mert az 1. oldal
    tartalma kitölti az A5-öt. Asztali gépnél 2 sor kiesik (billentyűzet, akku) - a
    2. gép logója enélkül felcsúszna az 1. oldalra. A kitöltés ezért "oldaltörés előtte"
    jelölést tesz a 2. gép logó-bekezdésére (`pageBreakBefore`), ami a sablon eredeti
    esetén semmit nem változtat (a logó ott is új oldalon kezdődik).
  - VEZETŐ SZÓKÖZÖK: néhány érték-cellában az érték előtt egy (vagy két) szóköz áll
    (Videókártya, Kijelző, Billentyűzet, Akkumulátor, Garancia). Ezt MEGTARTJUK, hogy
    a kinyomtatott lap egyezzen azzal, amit a technikus a Wordben átírva kapna.

A Word-os oldalszámot nyomtatás ELŐTT ellenőrizzük: ha nem pontosan 2 (pl. egy túl
hosszú "Állapot" szöveg átlógott a következő oldalra), NEM nyomtatunk - egy elcsúszott
lap rosszabb, mint egy hibaüzenet (4. elv).

Ez a modul nem hív subprocesst magától; a Word/nyomtató-lekérdezés a hívó `run`-ján megy.
"""
import io
import os
import re
import json
import zipfile
import logging
from app.common import _ps_quote

# A sablon a program mellé csomagolva (DriverVarazslo.spec -> datas).
TEMPLATE_FILENAME = 'bolti_tabla_sablon.docx'

# A két géptípus. A felület és a CLI is ebből dolgozik.
KIND_LAPTOP = 'laptop'
KIND_DESKTOP = 'desktop'
KIND_LABELS = {KIND_LAPTOP: 'Laptop', KIND_DESKTOP: 'Asztali PC'}

# A táblázat sorai A SABLON SORRENDJÉBEN: (kulcs, a sablonban álló címke, csak laptopon?,
# példa a beviteli mezőbe). A címke alapján keressük meg a sort a sablonban, tehát ha
# valaki a sablonban átírja egy sor címkéjét, a kitöltés kifejezett hibát ad (nem
# hagyja némán üresen).
FIELDS = [
    ('cpu', 'Processzor', False, 'Intel® Core™ i7-10610U'),
    ('ram', 'Memória', False, '16GB DDR4'),
    ('storage', 'Tárhely', False, '256GB SSD'),
    ('gpu', 'Videókártya', False, 'Intel® UHD Graphics'),
    ('display', 'Kijelző', False, '14" 1920x1080 60Hz'),
    ('keyboard', 'Billentyűzet', True, 'Magyar világító'),
    ('battery', 'Akkumulátor', True, '100% ÚJ'),
    ('condition', 'Állapot', False, 'Nagyon szép állapot!'),
    ('warranty', 'Garancia', False, '6 hónap'),
]
NAME_EXAMPLE = 'Dell Latitude 7410'
PRICE_EXAMPLE = '140000'

# Word nyomtatási paraméterek: A4 twipben (1 mm = 56,69 twip) - "Adott papírméretre: A4".
A4_TWIPS = (11906, 16838)
PAGES_PER_SHEET = (2, 1)          # PrintZoomColumn, PrintZoomRow = "Laponként 2 oldal"
WORD_PRINT_TIMEOUT = 240          # a Word hideg indítása lassú gépen perc is lehet

_W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_P_RE = re.compile(r'<w:p[ >].*?</w:p>', re.S)
_R_RE = re.compile(r'<w:r[ >].*?</w:r>', re.S)
_T_RE = re.compile(r'<w:t(?: [^>]*)?>([^<]*)</w:t>')
_TBL_RE = re.compile(r'<w:tbl>.*?</w:tbl>', re.S)
_TR_RE = re.compile(r'<w:tr[ >].*?</w:tr>', re.S)
_TC_RE = re.compile(r'<w:tc>.*?</w:tc>', re.S)
# XML 1.0-ban tiltott vezérlőkarakterek (a TAB/LF/CR kivételével) - egy bemásolt szövegben
# előfordulhatnak, és egyetlen ilyen karakter olvashatatlanná tenné a docx-et a Word számára.
_XML_BAD = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def fields_for(kind):
    """Az adott géptípushoz bekérendő sorok (kulcs, címke, példa)."""
    return [(k, lab, ex) for k, lab, laptop_only, ex in FIELDS
            if kind == KIND_LAPTOP or not laptop_only]


def _xml_text(s):
    s = _XML_BAD.sub('', str(s or ''))
    s = s.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _unxml(s):
    return (s.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
            .replace('&apos;', "'").replace('&amp;', '&'))


def para_text(p_xml):
    return _unxml(''.join(_T_RE.findall(p_xml)))


def format_price(value):
    """Ár a sablon alakjában: `140000` / `140 000` / `140.000 Ft` -> `140 000 Ft`.

    Ha a beírt szöveg nem tisztán szám (pl. "Érdeklődjön!"), VÁLTOZATLANUL hagyjuk -
    a technikus szándékát nem írjuk felül."""
    s = str(value or '').strip()
    m = re.fullmatch(r'([\d\s. ]+?)\s*(?:ft|huf|,-)?\.?', s, re.I)
    if not m:
        return s
    digits = re.sub(r'\D', '', m.group(1))
    if not digits:
        return s
    return f"{int(digits):,}".replace(',', ' ') + ' Ft'


def normalize_value(key, value):
    """A Word automatikus javítását pótoljuk ott, ahol a sablon is azt mutatja:
    a hüvelyk jele a kijelzőnél `14"` -> `14”` (a sablonban így áll)."""
    s = str(value or '').strip()
    if key == 'display':
        s = re.sub(r'(\d)\s*"', '\\1”', s)
    return s


def _set_para_text(p_xml, text, keep_lead=False):
    """Egy bekezdés szövegének cseréje a FORMÁZÁS megtartásával.

    A bekezdés-tulajdonság (pPr) marad; a futások (run) helyére EGY futás kerül, az
    első, nem csak szóközt tartalmazó eredeti futás karakterformázásával (rPr). A
    `keep_lead` az eredeti szöveg vezető szóközeit megőrzi (lásd a modul fejlécét)."""
    runs = _R_RE.findall(p_xml)
    text_runs = [r for r in runs if _T_RE.search(r)]
    tmpl = next((r for r in text_runs if para_text(r).strip()), None) or \
        (text_runs[0] if text_runs else (runs[0] if runs else ''))
    m = re.search(r'<w:rPr>.*?</w:rPr>', tmpl, re.S)
    rpr = m.group(0) if m else ''
    lead = ''
    if keep_lead:
        orig = para_text(p_xml)
        lead = orig[:len(orig) - len(orig.lstrip())]
    body = _R_RE.sub('', p_xml)
    body = re.sub(r'<w:proofErr [^>]*/>', '', body)
    new_run = f'<w:r>{rpr}<w:t xml:space="preserve">{_xml_text(lead + text)}</w:t></w:r>'
    return body[:body.rindex('</w:p>')] + new_run + '</w:p>'


def _add_page_break_before(p_xml):
    """`<w:pageBreakBefore/>` a bekezdés elé. A séma sorrendje szerint a pPr-en belül a
    pStyle/keepNext/keepLines UTÁN kell állnia, minden más előtt."""
    if '<w:pageBreakBefore' in p_xml:
        return p_xml
    if '<w:pPr>' not in p_xml:
        i = p_xml.index('>') + 1
        return p_xml[:i] + '<w:pPr><w:pageBreakBefore/></w:pPr>' + p_xml[i:]
    i = p_xml.index('<w:pPr>') + len('<w:pPr>')
    m = re.match(r'(?:<w:pStyle [^>]*/>|<w:keepNext/>|<w:keepLines/>)*', p_xml[i:])
    i += m.end()
    return p_xml[:i] + '<w:pageBreakBefore/>' + p_xml[i:]


def _replace_paras(seg, picker, fn):
    """A `seg` szakasz bekezdései közül a `picker(list)` által választottat `fn`-nel cseréli."""
    paras = list(_P_RE.finditer(seg))
    idx = picker(paras)
    if idx is None:
        return seg, False
    m = paras[idx]
    return seg[:m.start()] + fn(m.group(0)) + seg[m.end():], True


def _first_text(paras):
    return next((i for i, m in enumerate(paras) if para_text(m.group(0)).strip()), None)


def _last_text(paras):
    idx = [i for i, m in enumerate(paras) if para_text(m.group(0)).strip()]
    return idx[-1] if idx else None


def _row_label(tr):
    cells = _TC_RE.findall(tr)
    return para_text(cells[0]).strip() if cells else ''


def _fill_table(tbl, machine):
    kind = machine.get('kind')
    values = machine.get('values') or {}
    rows = _TR_RE.findall(tbl)
    by_label = {_row_label(r): r for r in rows}
    out = tbl
    for key, label, laptop_only, _ex in FIELDS:
        row = by_label.get(label)
        if row is None:
            raise ValueError(f"A sablonban nem található a(z) '{label}' sor.")
        if laptop_only and kind != KIND_LAPTOP:
            out = out.replace(row, '', 1)
            continue
        cells = _TC_RE.findall(row)
        if len(cells) < 2:
            raise ValueError(f"A sablon '{label}' sorában nincs érték-cella.")
        vcell = cells[1]
        pm = _P_RE.search(vcell)
        new_cell = vcell[:pm.start()] + _set_para_text(
            pm.group(0), normalize_value(key, values.get(key)), keep_lead=True) + vcell[pm.end():]
        out = out.replace(row, row.replace(vcell, new_cell, 1), 1)
    return out


def fill_template(template_bytes, machines):
    """A sablon kitöltése két gép adataival. Visszatérés: a kész docx bájtjai.

    `machines`: pontosan 2 elem, mindegyik {kind, name, price, values:{kulcs: szöveg}}.
    Tiszta függvény (offline tesztelhető)."""
    if len(machines) != 2:
        raise ValueError('Pontosan két gép adatai kellenek.')
    zin = zipfile.ZipFile(io.BytesIO(template_bytes))
    xml = zin.read('word/document.xml').decode('utf-8')
    tbls = list(_TBL_RE.finditer(xml))
    if len(tbls) != 2:
        raise ValueError(f"A sablonban 2 táblázat kellene (egy gépenként), de {len(tbls)} van.")
    b0 = xml.index('<w:body>') + len('<w:body>')
    before = xml[b0:tbls[0].start()]
    between = xml[tbls[0].end():tbls[1].start()]
    after = xml[tbls[1].end():]
    m1, m2 = machines

    before, ok_t1 = _replace_paras(before, _last_text, lambda p: _set_para_text(p, m1.get('name', '')))
    # A két gép közti szakasz: az 1. gép ára (első szöveges bekezdés), a 2. gép neve
    # (utolsó szöveges bekezdés), és a 2. gép logója (a rajzot tartalmazó bekezdés).
    between, ok_p1 = _replace_paras(between, _first_text, lambda p: _set_para_text(p, format_price(m1.get('price'))))
    between, ok_t2 = _replace_paras(between, _last_text, lambda p: _set_para_text(p, m2.get('name', '')))
    between, ok_br = _replace_paras(
        between, lambda ps: next((i for i, m in enumerate(ps) if '<w:drawing>' in m.group(0)), None),
        _add_page_break_before)
    after, ok_p2 = _replace_paras(after, _first_text, lambda p: _set_para_text(p, format_price(m2.get('price'))))
    missing = [n for n, ok in (('1. gép neve', ok_t1), ('1. gép ára', ok_p1), ('2. gép neve', ok_t2),
                               ('2. gép logója', ok_br), ('2. gép ára', ok_p2)) if not ok]
    if missing:
        raise ValueError('A sablonban nem található: ' + ', '.join(missing))

    new_xml = (xml[:b0] + before + _fill_table(tbls[0].group(0), m1) + between
               + _fill_table(tbls[1].group(0), m2) + after)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zout:
        for info in zin.infolist():
            data = new_xml.encode('utf-8') if info.filename == 'word/document.xml' else zin.read(info.filename)
            zout.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    logging.info(f"[BOLTI-TABLA] Sablon kitöltve: 1. {KIND_LABELS.get(m1.get('kind'))} '{m1.get('name')}' "
                 f"({format_price(m1.get('price'))}), 2. {KIND_LABELS.get(m2.get('kind'))} '{m2.get('name')}' "
                 f"({format_price(m2.get('price'))})")
    return out.getvalue()


def validate_machines(machines):
    """A kötelező mezők ellenőrzése. Visszatérés: a hiányzók listája (üres = rendben)."""
    missing = []
    if not isinstance(machines, list) or len(machines) != 2:
        return ['pontosan 2 gép adatai']
    for i, m in enumerate(machines, 1):
        kind = (m or {}).get('kind')
        if kind not in KIND_LABELS:
            missing.append(f"{i}. gép: típus (laptop / asztali)")
            continue
        if not str(m.get('name') or '').strip():
            missing.append(f"{i}. gép: név")
        vals = m.get('values') or {}
        for key, label, _ex in fields_for(kind):
            if not str(vals.get(key) or '').strip():
                missing.append(f"{i}. gép: {label}")
        if not str(m.get('price') or '').strip():
            missing.append(f"{i}. gép: ár")
    return missing


# ---------------------------------------------------------------- nyomtatók
PRINTERS_PS = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "@(Get-CimInstance Win32_Printer | Select-Object Name,Default,WorkOffline) | ConvertTo-Json -Compress"
)


def list_printers(run):
    """A Windowsban hozzáadott nyomtatók (ugyanaz a lista, amit a Word nyomtatás-menüje
    mutat). Visszatérés: [{name, default, offline}] - vagy None, ha a lekérdezés elbukott
    (a 'nincs nyomtató' és a 'nem tudtuk megkérdezni' két külön állapot)."""
    res = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', PRINTERS_PS],
              timeout=60)
    out = (getattr(res, 'stdout', '') or '').strip()
    try:
        data = json.loads(out) if out else []
    except ValueError:
        logging.warning(f"[BOLTI-TABLA] A nyomtató-lista nem értelmezhető: {out[:300]}")
        return None
    if isinstance(data, dict):
        data = [data]
    printers = [{'name': str(p.get('Name') or ''), 'default': bool(p.get('Default')),
                 'offline': bool(p.get('WorkOffline'))} for p in data if p and p.get('Name')]
    if not printers and getattr(res, 'returncode', 0) != 0:
        return None
    printers.sort(key=lambda p: p['name'].lower())
    logging.info(f"[BOLTI-TABLA] Hozzáadott nyomtatók ({len(printers)}): "
                 + '; '.join(p['name'] + (' [alapért.]' if p['default'] else '')
                             + (' [offline]' if p['offline'] else '') for p in printers))
    return printers


def word_installed():
    """Van-e a gépen COM-on át indítható Microsoft Word (registry, subprocess nélkül)."""
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'Word.Application\CLSID'))
        return True
    except OSError:
        return False


def build_word_print_ps(docx_path, printer, copies=1):
    """A Word COM-os nyomtató szkript. Sor-protokoll a kimeneten:
    `PAGES|n`, `PRINTER|<ActivePrinter>`, `DEFAULT|<rendszer alapért.>`, `OK`,
    vagy `ERR|<szakasz>|<üzenet>`.

    Három védőháló van benne, mindegyik a 4. elvből (néma hamis siker > minden más hiba):
      - oldalszám != 2 -> nem nyomtat (a tartalom átlógott egy harmadik oldalra);
      - a beállított nyomtató visszaolvasva nem a kért -> nem nyomtat;
      - `Background=$false`: a PrintOut megvárja, amíg a feladat a spoolerbe kerül, csak
        utána zárjuk be a Wordöt (háttér-nyomtatásnál a Quit megszakítaná).
    A Wordöt csak akkor zárjuk be, ha MI indítottuk üresen - ha egy már futó példányhoz
    csatlakoztunk, a felhasználó ott nyitott dokumentumaihoz nem nyúlunk."""
    zc, zr = PAGES_PER_SHEET
    pw, ph = A4_TWIPS
    return f"""
$ErrorActionPreference = 'Stop'
$path = '{_ps_quote(docx_path)}'
$printer = '{_ps_quote(printer)}'
$word = $null; $doc = $null; $pre = 0; $stage = 'word'
try {{
  "DEFAULT|$((Get-CimInstance Win32_Printer -Filter 'Default=TRUE' -ErrorAction SilentlyContinue).Name)"
  $word = New-Object -ComObject Word.Application
  $pre = $word.Documents.Count
  if ($pre -eq 0) {{ $word.Visible = $false }}
  $word.DisplayAlerts = 0
  $stage = 'open'
  $doc = $word.Documents.Open($path, $false, $true, $false)
  $stage = 'pages'
  $pages = $doc.ComputeStatistics(2)
  "PAGES|$pages"
  if ($pages -ne 2) {{ "ERR|pages|$pages"; return }}
  $stage = 'printer'
  [void][System.__ComObject].InvokeMember('FilePrintSetup', [Reflection.BindingFlags]::InvokeMethod, $null, $word.WordBasic, @($printer, 1), $null, $null, [string[]]@('Printer', 'DoNotSetAsSysDefault'))
  $ap = [string]$word.ActivePrinter
  "PRINTER|$ap"
  if (-not ($ap -eq $printer -or $ap.StartsWith("$printer "))) {{ "ERR|printer|$ap"; return }}
  $stage = 'print'
  [void][System.__ComObject].InvokeMember('PrintOut', [Reflection.BindingFlags]::InvokeMethod, $null, $doc, @($false, {int(copies)}, {zc}, {zr}, {pw}, {ph}), $null, $null, [string[]]@('Background', 'Copies', 'PrintZoomColumn', 'PrintZoomRow', 'PrintZoomPaperWidth', 'PrintZoomPaperHeight'))
  "OK"
}} catch {{
  "ERR|$stage|$($_.Exception.Message)"
}} finally {{
  if ($doc) {{ try {{ $doc.Close(0) }} catch {{}} }}
  if ($word) {{
    try {{ if ($pre -eq 0 -and $word.Documents.Count -eq 0) {{ $word.Quit(0) }} }} catch {{}}
    try {{ [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word) }} catch {{}}
  }}
  "DEFAULT_AFTER|$((Get-CimInstance Win32_Printer -Filter 'Default=TRUE' -ErrorAction SilentlyContinue).Name)"
}}
"""


def parse_word_print_output(text):
    """A szkript kimenete -> {ok, pages, printer, error_stage, error, default, default_after}."""
    r = {'ok': False, 'pages': None, 'printer': None, 'error_stage': None, 'error': None,
         'default': None, 'default_after': None}
    for line in (text or '').splitlines():
        line = line.strip()
        if line == 'OK':
            r['ok'] = True
        elif line.startswith('PAGES|'):
            try:
                r['pages'] = int(line.split('|', 1)[1])
            except ValueError:
                pass
        elif line.startswith('PRINTER|'):
            r['printer'] = line.split('|', 1)[1]
        elif line.startswith('DEFAULT_AFTER|'):
            r['default_after'] = line.split('|', 1)[1]
        elif line.startswith('DEFAULT|'):
            r['default'] = line.split('|', 1)[1]
        elif line.startswith('ERR|'):
            parts = line.split('|', 2)
            r['error_stage'] = parts[1] if len(parts) > 1 else '?'
            r['error'] = parts[2] if len(parts) > 2 else ''
    if r['error_stage']:
        r['ok'] = False
    return r


def error_text(parsed, printer):
    """Emberi nyelvű hibaüzenet a szkript szakasza szerint."""
    st, msg = parsed.get('error_stage'), parsed.get('error') or ''
    if st == 'word':
        return ("A Microsoft Word nem indítható ezen a gépen (nincs telepítve, vagy nincs "
                f"aktiválva). A bolti tábla a Worddel nyomtat. Részletek: {msg}")
    if st == 'open':
        return f"A Word nem tudta megnyitni a kitöltött táblát: {msg}"
    if st == 'pages':
        return (f"A kitöltött tábla {msg} oldal lett 2 helyett - valamelyik szöveg túl hosszú, "
                "és átlógott a következő oldalra. Rövidítsd (jellemzően az Állapot vagy a gép "
                "neve), és próbáld újra. NEM nyomtattam semmit.")
    if st == 'printer':
        return (f"A Word nem tudta a(z) '{printer}' nyomtatót kiválasztani (a Word szerint most: "
                f"'{msg}'). NEM nyomtattam, nehogy rossz nyomtatóra menjen.")
    if st == 'print':
        return f"A nyomtatás a Wordben hibára futott: {msg}"
    return f"Ismeretlen hiba a nyomtatás közben ({st}): {msg}"
