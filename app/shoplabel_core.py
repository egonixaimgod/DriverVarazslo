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
    # A Kijelző és az Állapot sor 2026-10-07 óta CSAK LAPTOPON van (explicit user decision:
    # asztali gépnél sosem töltik ki, és a táblán sem kell) - asztalinál a sablonból is
    # kikerül, ugyanúgy, mint a billentyűzet/akku sor.
    ('display', 'Kijelző', True, '14" 1920x1080 60Hz'),
    ('keyboard', 'Billentyűzet', True, 'Magyar világító'),
    ('battery', 'Akkumulátor', True, '100% ÚJ'),
    ('condition', 'Állapot', True, 'Nagyon szép állapot!'),
    ('warranty', 'Garancia', False, '6 hónap'),
]
NAME_EXAMPLE = 'Dell Latitude 7410'
PRICE_EXAMPLE = '140000'
SALE_PRICE_EXAMPLE = '119000'

# ELŐRE KITÖLTÖTT SÉMÁK (2026-09-25, explicit user decision: *"legyen alapbol ott a szoveg
# felig kitoltve en meg töltsem ki a másik felét"*). A mezők a géptípus kiválasztásakor
# ezzel a szöveggel töltődnek, a `|` (SLOT) jelöli azt a helyet, ahová a technikusnak be
# kell írnia - a felületen a kurzor oda ugrik, a jel maga sosem látszik. Ahol NINCS `|`, az
# egy kész, átírható alapérték (ha nem nyúlnak hozzá, így nyomtatódik).
#
# KÉT SZABÁLY, AMI NÉLKÜL A SÉMA ROSSZABB LENNE A SEMMINÉL:
#   - Egy KI NEM EGÉSZÍTETT séma (pl. puszta "GB DDR4") HIÁNYZÓ MEZŐNEK számít, nem
#     kitöltöttnek (`unfilled_texts`, `validate_machines`) - különben egy elfelejtett mező
#     "GB SSD"-ként kerülne a bolti táblára (4. elv: néma hamis siker).
#   - Kész alapérték csak ott van, ahol a GYENGÉBB állítás az alapeset: a billentyűzet
#     alapból "Magyar", nem "Magyar világító" (egy nem világító gépre ráírni hamis állítás
#     lenne a vevő felé), a világítót egy kattintás adja (PRESETS).
# Laptopon és asztali gépen UGYANAZ a séma (explicit kérés: "asztali gépnél is ugyan ez") -
# a különbség csak az, hogy asztali gépnél a kijelző/billentyűzet/akku/állapot sor nincs.
SLOT = '|'
PREFILL = {
    'cpu': 'Intel® Core™ i|',
    'ram': '|GB DDR4',
    'storage': '|GB SSD',
    'gpu': 'Intel® HD Graphics',
    'display': '|" 1920x1080 60Hz',
    'keyboard': 'Magyar',
    'battery': '|%',
    'condition': 'Szép állapot!',
    'warranty': '6 hónap',
}
# Egy kattintásos séma-váltók a mező alatt: (felirat, séma). Főleg a ® és ™ jelek miatt
# vannak - azokat billentyűzetről beírni a legnehezebb.
PRESETS = {
    'cpu': [('Intel', 'Intel® Core™ i|'), ('AMD', 'AMD Ryzen™ |')],
    'storage': [('SSD', '|GB SSD'), ('HDD', '|GB HDD')],
    'gpu': [('Intel HD', 'Intel® HD Graphics'), ('Intel UHD', 'Intel® UHD Graphics'),
            ('AMD', 'AMD Radeon™ |'), ('NVIDIA', 'NVIDIA® GeForce® |')],
    'keyboard': [('Magyar', 'Magyar'), ('Magyar világító', 'Magyar világító')],
}

# Word nyomtatási paraméterek: A4 twipben (1 mm = 56,69 twip) - "Adott papírméretre: A4".
A4_TWIPS = (11906, 16838)
PAGES_PER_SHEET = (2, 1)          # PrintZoomColumn, PrintZoomRow = "Laponként 2 oldal"
WORD_PRINT_TIMEOUT = 240          # a Word hideg indítása lassú gépen perc is lehet
# A NYOMTATÓ CSÚSZÁSÁNAK KIEGYENLÍTÉSE oldalanként, twipben (1 mm = 56,69 twip; pozitív =
# jobbra, negatív = balra). Cél (2026-10-07, explicit user decision): a kettévágott A4 mindkét
# A5-ös felén MINDEN pontosan középen legyen. A dokumentum maga már pontosan középre van
# szedve (a logót `_center_logos` igazítja, minden más a sablonból eleve középen áll) - ez a
# két szám CSAK a nyomtató saját csúszását egyenlíti ki, és csak nyomtatott próbából mérhető:
# egy középre szedett vonalnál (pl. az ár alatti) eltolás = (jobb oldali hely - bal oldali
# hely) / 2. Az A5 a "laponként 2 oldal" nyomtatásnál nem kicsinyül, tehát a papíron mért mm
# itt is mm.
#   - jobb oldali A5: terepen minden balra csúszott, 4 mm-rel túl jobbra került, 2 mm-t kért;
#     a logó-javítás utáni nyomaton (bal lap hibátlan) még "1 vagy 1,5 mm"-rel jobbra kellett
#     -> a felhasználó kérésére 2 + 1 = 3 mm.
#   - bal oldali A5: a Build 360-as nyomaton (eltolás nélkül) az ár alatti vonalnál BAL
#     oldalt volt több hely, tehát ezt a lapot a nyomtató ~1 mm-rel JOBBRA nyomja -> -1 mm.
#     (A logó ugyanott jobbra lógott - az a sablon 1,37 mm-es logóhibája volt, már javítva.)
RIGHT_PAGE_SHIFT_TWIPS = 170      # ~ +3 mm
LEFT_PAGE_SHIFT_TWIPS = -57       # ~ -1 mm (próbanyomaton hibátlan, 2026-10-07)

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


def template_text(tpl):
    """A séma látható szövege (a SLOT jel nélkül)."""
    return str(tpl or '').replace(SLOT, '')


def unfilled_texts(key):
    """A mező KI NEM EGÉSZÍTETT sémái (minden SLOT-os séma szövege, a gyorsválasztókéi is).
    Ha a beírt érték ezek egyike, a mező hiányzónak számít."""
    tpls = [PREFILL.get(key)] + [t for _lab, t in PRESETS.get(key, [])]
    return sorted({template_text(t).strip() for t in tpls if t and SLOT in t})


def apply_template(template, typed):
    """CLI: a beírt szöveg + a mező sémája -> a végleges érték (None = még kell adat).

      - üres bevitel: a séma, ha kész alapérték; ha van benne kitöltendő hely, None;
      - `=`-lel kezdve: a teljes értéket írják át (pl. `=AMD Ryzen 5 3500U`);
      - egyébként, ha a sémában van kitöltendő hely, a beírt szöveg oda kerül;
        ha nincs, a beírt szöveg a teljes új érték.
    Tiszta függvény (offline tesztelhető)."""
    t = str(typed or '').strip()
    tpl = str(template or '')
    if t.startswith('='):
        return t[1:].strip() or None
    if not t:
        return None if (not tpl or SLOT in tpl) else tpl
    if SLOT in tpl:
        return tpl.replace(SLOT, t, 1)
    return t


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


def price_number(value):
    """A beírt ár számként (`140 000 Ft` -> 140000), vagy None, ha nem tisztán szám."""
    s = str(value or '').strip()
    m = re.fullmatch(r'([\d\s. ]+?)\s*(?:ft|huf|,-)?\.?', s, re.I)
    if not m:
        return None
    digits = re.sub(r'\D', '', m.group(1))
    return int(digits) if digits else None


def sale_percent(old, new):
    """Az akció mértéke egész százalékban (`140000`, `119000` -> 15), vagy None, ha nem
    számolható (nem szám, vagy az új ár nem kisebb a réginél)."""
    o, n = price_number(old), price_number(new)
    if not o or n is None or n >= o:
        return None
    return int(round((o - n) * 100.0 / o))


# AKCIÓS ÁR (2026-10-08, explicit user decision: *"az ár alatt legyen egy pipa ... ha
# bepipalom ... legyen egy új ár, a régit húzza át ... kicsiben, az uj ar pedig legyen
# nagyban és vmi akcios designet"*). A sablon ár-bekezdése és a fölötte álló üres
# térköz-bekezdés helyére egy "kupon" kerül: szaggatott piros keret, benne fent a piros
# AKCIÓ-címke (a kedvezmény százalékával) + az ÁTHÚZOTT régi ár kicsiben, alatta az új ár
# nagyban, pirosban. A szín sötétvörös (C00000), mert a bolt nyomtatója lehet fekete-fehér
# lézer: azon sötétszürkének jön ki, a fehér betűs címke így is olvasható marad.
#
# A KUPON PONTOSAN AKKORA, MINT AZ EREDETI KÉT BEKEZDÉS (rögzített sormagasságokkal), hogy
# a lap alja (a "Garancia | Minőség | Megbízhatóság" sor) akciónál is ugyanott álljon, és a
# két A5 egy vonalban maradjon, ha csak az egyik gép akciós - ugyanaz az elv, mint az
# asztali tábla helykitöltő sorainál. A számok Worddel mérve (lásd a CLAUDE.md-t).
SALE_RED = 'C00000'
SALE_GREY = '7F7F7F'
SALE_BORDER = f'w:val="dashed" w:sz="12" w:space="4" w:color="{SALE_RED}"'
SALE_INDENT_TWIPS = 1050            # a kupon a szedéstükör közepén, ~ 86 mm széles
SALE_TOP_LINE_TWIPS = 333           # címke + áthúzott régi ár sora (exact)
SALE_PRICE_LINE_TWIPS = 720         # az új ár sora (exact)
SALE_BEFORE_TWIPS = 80
SALE_AFTER_TWIPS = 0
# Akció + Windows telepítés: a kupon egy harmadik sort kap, ezért a két felső sor tömörebb,
# hogy a három együtt is pontosan a fenti magasságot adja (Worddel mérve, lásd a CLAUDE.md-t).
SALE_WIN_TOP_LINE_TWIPS = 300
SALE_WIN_PRICE_LINE_TWIPS = 600
SALE_WIN_LINE_TWIPS = 300
SALE_WIN_BORDER_SPACE = 2
SALE_WIN_BEFORE_TWIPS = 7

# WINDOWS TELEPÍTÉSSEL (2026-10-09, explicit user decision: *"oda legyen írva mellé hogy:
# Windows telepítéssel és legyen ott az ár + 15 000 Ft ... az alap gépár legyen nagyba ...
# egy checkbox legyen ami mindig legyen bepipálva, és legyen mellette egy ár ... alapból
# kitöltve 15 000 Ft"*). A nagy ár alatt egy halvány kék "pirula": "Windows telepítéssel:
# <ár + díj>". Akciónál a díj az AKCIÓS árhoz adódik (a vevő azt fizeti), és a sor a kupon
# harmadik sora lesz. A kék (Windows-kék, 0067B8) fekete-fehér nyomtatón sötétszürke.
# Ugyanaz a magasság-szabály, mint az akciónál: a blokk pontosan a sablon két eltűnő
# bekezdésének helyét tölti ki, tehát a lap alja minden kombinációban ugyanott áll.
WIN_BLUE = '0067B8'
WIN_FILL = 'EAF3FC'
WIN_LABEL_GREY = '404040'
WINDOWS_FEE_DEFAULT = '15000'
WIN_BORDER = f'w:val="single" w:sz="6" w:space="1" w:color="{WIN_BLUE}"'
WIN_INDENT_TWIPS = 1250             # a pirula a szedéstükör közepén, ~ 78 mm széles
WIN_PRICE_BEFORE_TWIPS = 120        # akció nélkül: a nagy ár sora fölött
WIN_PRICE_LINE_TWIPS = 760          # akció nélkül: a nagy ár sora (exact)
WIN_GAP_TWIPS = 60                  # a nagy ár és a pirula között
WIN_LINE_TWIPS = 340                # a pirula sora (exact)
NBSP = '\u00a0'


def _sale_rpr(font, size_half_pt, color, extra='', bold=True):
    return (f'<w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}"/>{"<w:b/>" if bold else ""}{extra}'
            f'<w:color w:val="{color}"/><w:sz w:val="{size_half_pt}"/><w:szCs w:val="{size_half_pt}"/></w:rPr>')


def _run(rpr, text):
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_xml_text(text)}</w:t></w:r>'


def _sale_ppr(line_twips, before, after, keep_next, border=None):
    bdr = ''.join(f'<w:{s} {border or SALE_BORDER}/>' for s in ('top', 'left', 'bottom', 'right'))
    return (f'<w:pPr>{"<w:keepNext/>" if keep_next else ""}<w:pBdr>{bdr}</w:pBdr>'
            f'<w:spacing w:before="{before}" w:after="{after}" w:line="{line_twips}" w:lineRule="exact"/>'
            f'<w:ind w:left="{SALE_INDENT_TWIPS}" w:right="{SALE_INDENT_TWIPS}"/><w:jc w:val="center"/></w:pPr>')


def is_windows(machine):
    return bool((machine or {}).get('windows'))


def windows_total(machine):
    """A Windows-telepítéses ár: (akciónál az akciós, egyébként a sima) ár + a telepítés díja.
    None, ha valamelyik nem tisztán szám."""
    base = machine.get('sale_price') if is_sale(machine) else machine.get('price')
    b, f = price_number(base), price_number(machine.get('windows_fee'))
    if b is None or f is None:
        return None
    return b + f


def windows_price_text(machine):
    """A pirula ára: a teljes összeg, ha számolható; ha az ár szöveg (pl. "Érdeklődjön!"),
    a díj "+ 15 000 Ft" alakban - a nemtudást nem írjuk ki egy kitalált végösszegként."""
    tot = windows_total(machine)
    if tot is not None:
        return format_price(str(tot))
    return '+ ' + format_price(machine.get('windows_fee'))


def _windows_runs(machine, label_sz, amount_sz):
    return (_run(_sale_rpr('Arial', label_sz, WIN_LABEL_GREY, bold=False), 'Windows telepítéssel:' + NBSP * 2)
            + _run(_sale_rpr('Arial Black', amount_sz, WIN_BLUE), windows_price_text(machine)))


def windows_block_xml(machine):
    """Akció nélkül, Windows-telepítéssel: a nagy ár (a sablon betűivel) + alatta a pirula.
    Tiszta függvény (offline tesztelhető)."""
    price = ('<w:p><w:pPr><w:keepNext/>'
             f'<w:spacing w:before="{WIN_PRICE_BEFORE_TWIPS}" w:after="0" w:line="{WIN_PRICE_LINE_TWIPS}" w:lineRule="exact"/>'
             '<w:jc w:val="center"/></w:pPr>'
             + _run(_sale_rpr('Arial Black', 56, '000000'), format_price(machine.get('price'))) + '</w:p>')
    bdr = ''.join(f'<w:{s} {WIN_BORDER}/>' for s in ('top', 'left', 'bottom', 'right'))
    win = (f'<w:p><w:pPr><w:pBdr>{bdr}</w:pBdr><w:shd w:val="clear" w:color="auto" w:fill="{WIN_FILL}"/>'
           f'<w:spacing w:before="{WIN_GAP_TWIPS}" w:after="0" w:line="{WIN_LINE_TWIPS}" w:lineRule="exact"/>'
           f'<w:ind w:left="{WIN_INDENT_TWIPS}" w:right="{WIN_INDENT_TWIPS}"/><w:jc w:val="center"/></w:pPr>'
           + _windows_runs(machine, 20, 24) + '</w:p>')
    return price + win


def sale_block_xml(old_price, new_price, machine=None):
    """Az akciós kupon bekezdései (a sablon ár- és térköz-bekezdése helyére): címke + áthúzott
    régi ár, az új ár, és ha a `machine` Windows-telepítéses, harmadik sorként a Windows-ár.
    Tiszta függvény (offline tesztelhető)."""
    win = machine is not None and is_windows(machine)
    pct = sale_percent(old_price, new_price)
    badge = NBSP + 'AKCIÓ' + (f' \u2212{pct}%' if pct else '!') + NBSP
    border = SALE_BORDER.replace('w:space="4"', f'w:space="{SALE_WIN_BORDER_SPACE}"') if win else None
    top = ('<w:p>' + _sale_ppr(SALE_WIN_TOP_LINE_TWIPS if win else SALE_TOP_LINE_TWIPS,
                               SALE_WIN_BEFORE_TWIPS if win else SALE_BEFORE_TWIPS, 0, True, border)
           + _run(_sale_rpr('Arial Black', 20 if win else 22, 'FFFFFF',
                            f'<w:shd w:val="clear" w:color="auto" w:fill="{SALE_RED}"/>'), badge)
           + _run(_sale_rpr('Arial', 22, SALE_GREY), NBSP * 3)
           + _run(_sale_rpr('Arial', 24 if win else 26, SALE_GREY, '<w:strike/>'), format_price(old_price))
           + '</w:p>')
    price = ('<w:p>' + _sale_ppr(SALE_WIN_PRICE_LINE_TWIPS if win else SALE_PRICE_LINE_TWIPS, 0,
                                 0 if win else SALE_AFTER_TWIPS, win, border)
             + _run(_sale_rpr('Arial Black', 50 if win else 60, SALE_RED), format_price(new_price)) + '</w:p>')
    if not win:
        return top + price
    third = ('<w:p>' + _sale_ppr(SALE_WIN_LINE_TWIPS, 0, 0, False, border)
             + _windows_runs(machine, 18, 22) + '</w:p>')
    return top + price + third


def _apply_price(seg, machine):
    """A gép árának beírása a szakasz első szöveges bekezdésébe (a sablon ára). Akciónál a
    bekezdés ÉS a fölötte álló üres térköz-bekezdés helyére a kupon kerül.
    Visszatérés: (seg, ok)."""
    paras = list(_P_RE.finditer(seg))
    i = _first_text(paras)
    if i is None:
        return seg, False
    p = paras[i]
    # Se akció, se Windows-telepítés: a sablon ára, pontosan úgy, mint eddig (bájtra azonos).
    if not is_sale(machine) and not is_windows(machine):
        return seg[:p.start()] + _set_para_text(p.group(0), format_price(machine.get('price'))) + seg[p.end():], True
    start = p.start()
    if i > 0 and not para_text(paras[i - 1].group(0)).strip() and '<w:drawing>' not in paras[i - 1].group(0):
        start = paras[i - 1].start()
    else:
        logging.warning("[BOLTI-TABLA] Az ár fölött nincs üres térköz-bekezdés - az ár-blokk csak az ár "
                        "helyére kerül, a lap alja ezen a lapon kicsit lejjebb csúszhat.")
    block = (sale_block_xml(machine.get('price'), machine.get('sale_price'), machine) if is_sale(machine)
             else windows_block_xml(machine))
    return seg[:start] + block + seg[p.end():], True


def is_sale(machine):
    return bool((machine or {}).get('sale'))


def price_label(machine):
    """Az ár a naplóhoz / üzenethez: `140 000 Ft`, `140 000 Ft -> AKCIÓ 119 000 Ft (-15%)`,
    és ha Windows-telepítéses: `... | Windows telepítéssel: 134 000 Ft (+15 000 Ft)`."""
    s = format_price(machine.get('price'))
    if is_sale(machine):
        pct = sale_percent(machine.get('price'), machine.get('sale_price'))
        s += f" -> AKCIÓ {format_price(machine.get('sale_price'))}" + (f" (-{pct}%)" if pct else '')
    if is_windows(machine):
        s += f" | Windows telepítéssel: {windows_price_text(machine)} (+{format_price(machine.get('windows_fee'))})"
    return s


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


# A csak-laptop sorok magassága a kitöltött laptop-táblán (twip), Word-ben mérve
# (2026-10-07, a sablon sorainak teteje: Kijelző 324,0 / Billentyűzet 348,5 / Akkumulátor
# 372,9 / Állapot 396,7 / Garancia 420,5 pt). Az asztali tábla üres helykitöltő sorai
# PONTOSAN ezt a magasságot kapják: tartalom szerinti magassággal a Word egy üres sort
# más magasnak számol (mérve 25,2 pt a 23,8 helyett), és az ár ~3 pt-tal elcsúszott.
# Ha a sablon sorai változnak, ezt újra kell mérni (a két ár y-pozíciója egyezzen).
SPACER_ROW_TWIPS = {'display': 490, 'keyboard': 488, 'battery': 476, 'condition': 488}


def _blank_row(tr, height_twips=None):
    """Egy sablon-sor LÁTHATATLAN másolata: minden cella szövege egy szóköz, a cellák
    alsó vonala (tcBorders) törölve, és ha meg van adva, PONTOS sormagasság."""
    tr = re.sub(r'<w:tcBorders>.*?</w:tcBorders>', '', tr, flags=re.S)
    if height_twips:
        tr = re.sub(r'<w:trHeight [^>]*/>', f'<w:trHeight w:val="{int(height_twips)}" w:hRule="exact"/>', tr)
    # A Word bekezdés-azonosítóinak egyedinek kell lenniük - a másolatból kivesszük őket.
    tr = re.sub(r' w14:(?:paraId|textId)="[^"]*"', '', tr)
    return _P_RE.sub(lambda m: _set_para_text(m.group(0), ' '), tr)


def _fill_table(tbl, machine):
    kind = machine.get('kind')
    values = machine.get('values') or {}
    rows = _TR_RE.findall(tbl)
    by_label = {_row_label(r): r for r in rows}
    out = tbl
    # Asztali gépnél a csak-laptop sorok helyére a táblázat VÉGÉN (a Garancia alatt) üres,
    # vonal nélküli sor kerül, hogy az ár pontosan ugyanott álljon a lapon, mint egy laptop
    # tábláján (explicit user decision, 2026-10-07: a sorok puszta törlésétől az ár a lap
    # közepe felé csúszott, és a két tábla ára nem esett egy vonalba).
    spacers = []
    for key, label, laptop_only, _ex in FIELDS:
        row = by_label.get(label)
        if row is None:
            raise ValueError(f"A sablonban nem található a(z) '{label}' sor.")
        if laptop_only and kind != KIND_LAPTOP:
            out = out.replace(row, '', 1)
            spacers.append(_blank_row(row, SPACER_ROW_TWIPS.get(key)))
            continue
        cells = _TC_RE.findall(row)
        if len(cells) < 2:
            raise ValueError(f"A sablon '{label}' sorában nincs érték-cella.")
        vcell = cells[1]
        pm = _P_RE.search(vcell)
        new_cell = vcell[:pm.start()] + _set_para_text(
            pm.group(0), normalize_value(key, values.get(key)), keep_lead=True) + vcell[pm.end():]
        out = out.replace(row, row.replace(vcell, new_cell, 1), 1)
    if spacers:
        i = out.rindex('</w:tbl>')
        out = out[:i] + ''.join(spacers) + out[i:]
    return out


def _shift_margins(sect, dx):
    """A szakasz bal margója +dx, a jobb -dx twip: a szedéstükör szélessége nem változik,
    csak az egész oldal tartalma (a margóhoz rögzített logóval együtt) tolódik el - pozitív
    dx jobbra, negatív balra."""
    def fix(m):
        tag = m.group(0)
        left = int(re.search(r'w:left="(\d+)"', tag).group(1))
        right = int(re.search(r'w:right="(\d+)"', tag).group(1))
        d = max(min(int(dx), right), -left)
        tag = re.sub(r'w:left="\d+"', f'w:left="{left + d}"', tag)
        return re.sub(r'w:right="\d+"', f'w:right="{right - d}"', tag)
    return re.sub(r'<w:pgMar [^>]*/>', fix, sect, count=1)


def _center_logos(xml):
    """A margóhoz rögzített logók (a sablon két `wp:anchor` képe) PONTOSAN vízszintes
    középre állítása. A sablonban a két logó nem állt középen (mérve 2026-10-07: az 1.
    oldalé 1,37 mm-rel balra, a 2. oldalé 1,72 mm-rel jobbra), miközben a név, a vonalak,
    az ár és a lábléc tizedmilliméterre középen van. A szedéstükör szélessége (lapszélesség
    - két margó) a lapok eltolásától nem változik, ezért a margóhoz mért eltolás így marad
    érvényes. Visszatérés: (xml, középre tett logók száma)."""
    m = re.search(r'<w:sectPr[ >].*?</w:sectPr>', xml[xml.rindex('<w:sectPr'):], re.S)
    pg_w = int(re.search(r'<w:pgSz [^>]*w:w="(\d+)"', m.group(0)).group(1))
    mar = re.search(r'<w:pgMar [^>]*/>', m.group(0)).group(0)
    left = int(re.search(r'w:left="(\d+)"', mar).group(1))
    right = int(re.search(r'w:right="(\d+)"', mar).group(1))
    text_emu = (pg_w - left - right) * 635          # 1 twip = 635 EMU
    n = 0

    def fix(a):
        nonlocal n
        d = a.group(0)
        cx = re.search(r'<wp:extent cx="(\d+)"', d)
        if not cx or not re.search(r'<wp:positionH relativeFrom="margin">\s*<wp:posOffset>', d):
            return d
        n += 1
        off = (text_emu - int(cx.group(1))) // 2
        return re.sub(r'(<wp:positionH relativeFrom="margin">\s*<wp:posOffset>)-?\d+(</wp:posOffset>)',
                      lambda g: f'{g.group(1)}{off}{g.group(2)}', d, count=1)
    return re.sub(r'<wp:anchor[ >].*?</wp:anchor>', fix, xml, flags=re.S), n


def _shift_pages(between, after, left_dx, right_dx):
    """Az 1. gép oldalát (a nyomtatott A4 BAL oldali A5-ét) `left_dx`, a 2. gépét (JOBB
    oldali A5) `right_dx` twippel jobbra tolja, egymástól függetlenül.

    Ehhez a két oldal külön szakasz lesz: az 1. oldal utolsó bekezdése (a 2. gép logója
    előtti) kapja az 1. oldal szakasz-tulajdonságát, a dokumentum végi szakasz a 2.
    oldalét - mindkettő az eredeti margókból, a saját eltolásával. Visszatérés:
    (between, after, ok)."""
    m_sect = re.search(r'<w:sectPr[ >].*?</w:sectPr>', after, re.S)
    paras = list(_P_RE.finditer(between))
    idx = next((i for i, m in enumerate(paras) if '<w:drawing>' in m.group(0)), None)
    if not m_sect or not idx:
        return between, after, False
    sect = m_sect.group(0)
    p = paras[idx - 1]
    p_xml = p.group(0)
    if '<w:sectPr' in p_xml:
        return between, after, False
    sect1 = _shift_margins(sect, left_dx)
    if '<w:pPr>' in p_xml:
        i = p_xml.index('</w:pPr>')
        new_p = p_xml[:i] + sect1 + p_xml[i:]
    else:
        i = p_xml.index('>') + 1
        new_p = p_xml[:i] + '<w:pPr>' + sect1 + '</w:pPr>' + p_xml[i:]
    between = between[:p.start()] + new_p + between[p.end():]
    after = after[:m_sect.start()] + _shift_margins(sect, right_dx) + after[m_sect.end():]
    logging.info(f"[BOLTI-TABLA] Lapok eltolása jobbra: bal oldali {left_dx} twip ({left_dx / 56.69:.1f} mm), "
                 f"jobb oldali {right_dx} twip ({right_dx / 56.69:.1f} mm).")
    return between, after, True


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
    between, ok_p1 = _apply_price(between, m1)
    between, ok_t2 = _replace_paras(between, _last_text, lambda p: _set_para_text(p, m2.get('name', '')))
    between, ok_br = _replace_paras(
        between, lambda ps: next((i for i, m in enumerate(ps) if '<w:drawing>' in m.group(0)), None),
        _add_page_break_before)
    after, ok_p2 = _apply_price(after, m2)
    between, after, ok_sh = _shift_pages(between, after, LEFT_PAGE_SHIFT_TWIPS, RIGHT_PAGE_SHIFT_TWIPS)
    missing = [n for n, ok in (('1. gép neve', ok_t1), ('1. gép ára', ok_p1), ('2. gép neve', ok_t2),
                               ('2. gép logója', ok_br), ('2. gép ára', ok_p2),
                               ('a jobb oldali lap eltolása (szakasztörés)', ok_sh)) if not ok]
    if missing:
        raise ValueError('A sablonban nem található: ' + ', '.join(missing))

    new_xml = (xml[:b0] + before + _fill_table(tbls[0].group(0), m1) + between
               + _fill_table(tbls[1].group(0), m2) + after)
    new_xml, n_logo = _center_logos(new_xml)
    if n_logo != 2:
        logging.warning(f"[BOLTI-TABLA] {n_logo} logót tudtunk középre tenni a várt 2 helyett "
                        f"(a sablon logója nem a margóhoz rögzített kép?) - a logó a sablon szerinti helyén marad.")
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zout:
        for info in zin.infolist():
            data = new_xml.encode('utf-8') if info.filename == 'word/document.xml' else zin.read(info.filename)
            zout.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    logging.info(f"[BOLTI-TABLA] Sablon kitöltve: 1. {KIND_LABELS.get(m1.get('kind'))} '{m1.get('name')}' "
                 f"({price_label(m1)}), 2. {KIND_LABELS.get(m2.get('kind'))} '{m2.get('name')}' "
                 f"({price_label(m2)})")
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
            v = str(vals.get(key) or '').strip()
            if not v:
                missing.append(f"{i}. gép: {label}")
            elif v in unfilled_texts(key):
                missing.append(f"{i}. gép: {label} (a séma nincs kiegészítve: '{v}')")
        if not str(m.get('price') or '').strip():
            missing.append(f"{i}. gép: ár")
        if is_sale(m):
            sp = str(m.get('sale_price') or '').strip()
            o, n = price_number(m.get('price')), price_number(sp)
            if not sp:
                missing.append(f"{i}. gép: akciós ár")
            elif o is not None and n is not None and n >= o:
                # Egy "akció", ami nem olcsóbb a régi árnál, elírás - a vevő felé hamis
                # állítás lenne, ezért nem nyomtatjuk ki.
                missing.append(f"{i}. gép: az akciós ár ({format_price(sp)}) nem kisebb a régi árnál "
                               f"({format_price(m.get('price'))})")
        if is_windows(m) and price_number(m.get('windows_fee')) is None:
            # A díj számként kell: egy "15e" vagy üres mezőből nem írunk ki kitalált végösszeget.
            missing.append(f"{i}. gép: a Windows-telepítés díja (szám, pl. {WINDOWS_FEE_DEFAULT})")
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
