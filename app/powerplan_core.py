"""Energiaséma teljesítmény-módba állítása - a lánc záró lépése.

MIÉRT (explicit user decision, 2026-09-01): a szervizből kiadott gép ne legyen lassú.
A felhasználó szavaival: *"azt allitsa mar be az autofix vegen h a laptopok se legyenek
lassuak utana, mehet a teljesitmeny centrikus meg minden maxra huzva, ne saveljen aramot
a gep utana"*. A frissen bedriverezett gép legrosszabb visszajelzése az, hogy "gyorsabb
volt, mielőtt behoztam" - miközben a valódi ok jellemzően nem a driver, hanem az, hogy a
Windows Kiegyensúlyozott vagy Energiatakarékos sémán áll.

A DÖNTÉS ÉRDEMI RÉSZE A SÉMA-VÁLTÁS: a "Nagy teljesítményű" séma definíció szerint hozza
a processzor 100%-os minimumát, a kikapcsolt PCIe-energiagazdálkodást és a nem parkoló
lemezt. A modul ezen felül EXPLICIT is beállítja ezeket - egy OEM-image ugyanis
felülírhatja a séma alapértékeit (mérve nem egyszer: Dell/Lenovo gyári image saját
értékeket tesz a beépített sémákba is), és ilyenkor a puszta séma-váltás nem elég.

AMIHEZ SZÁNDÉKOSAN NEM NYÚLUNK: az alvás- és kijelző-időzítők. Azok NEM lassítják a
gépet, viszont az elállításuk valódi kellemetlenség az ügyfélnek (soha el nem alvó
képernyő, fölösleges melegedés), és a legtöbben hibaként hoznák vissza. A "ne legyen
lassú" kérést a processzor/busz/lemez beállításai teljesítik, nem az alvás tiltása.

NYELVFÜGGETLENSÉG: a `powercfg /getactivescheme` kimenete LOKALIZÁLT ("Energiaséma
GUID-ja:" magyarul), ezért kizárólag a GUID-ot olvassuk ki regexszel, és soha nem hozunk
döntést a szövegből. Ugyanaz a szabály, ami miatt a `delete_succeeded` magyar szótöve
egyszer már hamis sikert okozott ebben a projektben; a `powercfg` alias-kulcsszavak
(SUB_PROCESSOR, PROCTHROTTLEMIN...) viszont MINDEN nyelven azonosak, azok biztonságosak.

Ez a modul semmit nem dob: minden hiba naplózódik és a hívó `applied`/`failed` listát kap.
Egy energiaséma-beállítás soha nem buktathat el egy egyébként sikeres driver-láncot.
"""

# === AUTO-IMPORTS ===
import re
import logging
# === /AUTO-IMPORTS ===

# A Windows beépített "Nagy teljesítményű" sémája. A GUID minden Windows-nyelven azonos.
HIGH_PERFORMANCE_GUID = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
BALANCED_GUID = '381b4222-f694-41f0-9685-ff5bb260df2e'
POWER_SAVER_GUID = 'a1841308-3541-4fab-bc81-f71556f20b4a'
# "Végső teljesítmény" (Ultimate Performance) - a Nagy teljesítményűnél is agresszívabb.
ULTIMATE_PERFORMANCE_GUID = 'e9a42b02-d5df-448d-aa00-03f14749eb61'

# A MÁR TELJESÍTMÉNYRE HANGOLT SÉMA MARAD (2026-09-28, terepi napló, Build 344): a gépen
# egy "Revision - Ultra Performance" egyedi séma futott (egy hangolt Windows-kiadásé), és a
# lánc lecserélte a sima Nagy teljesítményűre - ami egy hangolt sémánál visszalépés lehet
# (az egyedi séma a mi négy beállításunkon túl is tartalmazhat hangolást). Megtartjuk, ha
# a Végső teljesítmény, vagy egy EGYEDI séma, amelyben a processzor minimuma MÁR 100%
# (ez a "ne spóroljon" legbiztosabb, nyelvfüggetlen jele); a négy maximum-beállítás
# ettől függetlenül rákerül, tehát a "maxra húzva" követelmény teljesül.
BUILTIN_NON_PERFORMANCE = {BALANCED_GUID, POWER_SAVER_GUID}

# A BEÉPÍTETT "TELJESÍTMÉNYCENTRIKUS" (Nagy teljesítményű) SÉMA A ReviOS ÉRTÉKEIVEL
# (2026-09-28, explicit user decision: *"kajak legyen az teljesítménycentrikus... ami a revios
# teljesítményprofiljába van azt kéne leklónozni"*, majd: *"nem kell drivervarazslo maximalis
# séma, a sima teljesítménycentrikust állítsa át erre"*).
#
# A ReviOS "Revision - Ultra Performance" sémája (forrás: github.com/meetrevision/revision-tool,
# src/lib/features/tweaks/performance/performance_service.dart) a Windows rejtett "Végső
# teljesítmény" sablonjának másolata, plusz az alábbi processzor/USB beállítások. SAJÁT SÉMÁT
# NEM HOZUNK LÉTRE (egy köztes változat ezt csinálta - a felhasználó elvetette): a beállítások a
# beépített Nagy teljesítményű sémára kerülnek, így az ügyfél gépén nem jelenik meg egy
# ismeretlen nevű séma, és a Windows-os "Teljesítménycentrikus" név marad.

SUB_PROCESSOR_GUID = '54533251-82be-4824-96c1-47b60b740d00'
# A USB-beállításoknak nincs stabil alias-a, GUID-dal kell hivatkozni rájuk.
USB_SUBGROUP_GUID = '2a737441-1930-4402-8d77-b2bebba308a3'
USB_SELECTIVE_SUSPEND_GUID = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'
USB3_LINK_POWER_GUID = 'd4e98f31-5ffe-4ce1-be31-1b38b384c009'

# A teljesítményt ÉRDEMBEN befolyásoló beállítások: (alcsoport, beállítás, AC, DC, címke).
#
# A REJTETT beállítások (magparkolás, órajel-politika, C-state küszöb, USB 3 LPM) GUID-dal
# mennek: MÉRVE (2026-09-28, Win10 19045) a `powercfg` ezekre NEM ismer alias-t (a
# `/aliases` nem listázza őket), és a sima `/query` sem mutatja - GUID-dal viszont olvashatók
# (`/qh`) és írhatók. A látható beállítások alias-a nyelvfüggetlen, azok maradnak.
#
# AZ ÉRTÉKEK A ReviOS-ÉI, egy kivétellel: a ReviOS CSAK AC-t (hálózati üzem) állít, mi a DC-t
# (akkumulátor) is, mert a 2026-09-01-i kérés az volt, hogy a gép akkumulátoron se legyen lassú.
# A C-state küszöb (IDLEPROMOTE 100 / IDLEDEMOTE 80) a ReviOS "C6 kikapcsolása" opciója: a mag
# ritkábban esik mély alvóállapotba (kérés: "ne menjen idle-be a proci"), a 20%-os rés pedig
# megakadályozza, hogy a mag másodpercenként többször ugráljon az állapotok közt (hangakadás).
# A teljes idle-tiltást (IDLEDISABLE) SZÁNDÉKOSAN nem kapcsoljuk be: attól a processzor
# folyamatosan teljes teljesítményen fűt, egy laptop pedig perceken belül túlmelegszik.
#
# A DISKIDLE=0 jelentése "soha" (nem nulla perc): a lemez leparkolása utáni felpörgés az
# egyik legjobban ÉRZÉKELHETŐ lassulás egy HDD-s gépen.
PERFORMANCE_SETTINGS = [
    ('SUB_PROCESSOR', 'PROCTHROTTLEMIN', 100, 100, 'processzor minimális állapota'),
    ('SUB_PROCESSOR', 'PROCTHROTTLEMAX', 100, 100, 'processzor maximális állapota'),
    # Magparkolás tiltása (CPMINCORES; a ...584 = CPMINCORES1 a hibrid CPU P-magjaié).
    (SUB_PROCESSOR_GUID, '0cc5b647-c1df-4637-891a-dec35c318583', 100, 100, 'magparkolás kikapcsolva'),
    (SUB_PROCESSOR_GUID, '0cc5b647-c1df-4637-891a-dec35c318584', 100, 100, 'magparkolás kikapcsolva (P-magok)'),
    # Órajel-politika: emelés azonnal a maximumra (Rocket=2), csökkentés lépésenként (Single=1),
    # alacsony küszöbökkel (10% / 8%) - PERFINCPOL, PERFDECPOL, PERFINCTHRESHOLD, PERFDECTHRESHOLD.
    (SUB_PROCESSOR_GUID, '465e1f50-b610-473a-ab58-00d1077dc418', 2, 2, 'órajel-emelés azonnal a maximumra'),
    (SUB_PROCESSOR_GUID, '40fbefc7-2e9d-4d25-a185-0cfd8574bac6', 1, 1, 'órajel-csökkentés lépésenként'),
    (SUB_PROCESSOR_GUID, '06cadf0e-64ed-448a-8927-ce7bf90eb35d', 10, 10, 'órajel-emelés küszöbe 10%'),
    (SUB_PROCESSOR_GUID, '12a0ab44-fe28-4fa9-b3bd-4b64f44960a6', 8, 8, 'órajel-csökkentés küszöbe 8%'),
    # Mély alvóállapot (C-state) küszöbe - IDLEPROMOTE / IDLEDEMOTE.
    (SUB_PROCESSOR_GUID, '7b224883-b3cc-4d79-819f-8374152cbe7c', 100, 100, 'mély alvóállapot ritkítva (C-state küszöb 100%)'),
    (SUB_PROCESSOR_GUID, '4b92d758-5a24-4851-a470-815d78aee119', 80, 80, 'mély alvóállapotból visszalépés küszöbe 80%'),
    ('SUB_PCIEXPRESS', 'ASPM', 0, 0, 'PCI Express energiagazdálkodás'),
    ('SUB_DISK', 'DISKIDLE', 0, 0, 'merevlemez leállítása'),
    # Kikapcsolva: az USB-eszközök (egér, billentyűzet, dokkoló) nem "ébredeznek" használatkor.
    (USB_SUBGROUP_GUID, USB_SELECTIVE_SUSPEND_GUID, 0, 0, 'USB szelektív felfüggesztés'),
    (USB_SUBGROUP_GUID, USB3_LINK_POWER_GUID, 0, 0, 'USB 3 kapcsolat-energiagazdálkodás'),
]

_GUID_RE = re.compile(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                      r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})')


# A `powercfg` NATÍV exe, tehát a KONZOL (OEM) kódlapján ír - magyar Windowson cp852 -,
# a `_run` viszont utf-8-cal dekódol. A séma NEVE ettől a memóriában romlik el, és
# terepen pontosan így került a záró jelentésbe: `⚡ Energiaséma: "KiegyensŁlyozott"`
# (2026-09-04, két különböző gépen, mindkettőnél magyar sémanévvel - az angol nevűnél
# nincs baj, ezért nem tűnt fel korábban). A `ps_force_utf8` itt nem segít: az csak a
# PowerShell `-Command` hívásokat kényszeríti UTF-8-ra, a natív exe-ket szándékosan nem.
#
# A Python `'oem'` kodekje pontosan a konzol aktuális OEM kódlapját használja, tehát
# gépfüggetlen (mérve: a nyers bájt `Teljes\xa1tm\x82nycentrikus` -> 'Teljesítménycentrikus').
# CSAK a szöveget visszaadó hívásoknál kell; a GUID ASCII, azt semmi nem rontja el.
POWERCFG_ENCODING = 'oem'


def _run_powercfg_text(run_fn, cmd):
    """Egy `powercfg` hívás, aminek a SZÖVEGES kimenete is számít (nem csak a kódja).

    Az OEM-dekódolás hibája sosem akaszthatja meg a műveletet: ha a `run_fn` nem fogadja
    az `encoding` paramétert (más hívó, teszt-csonk) vagy a kodek nem elérhető, visszaesünk
    a sima hívásra - olyankor a név mojibake marad, de a GUID és a művelet változatlanul jó."""
    try:
        return run_fn(cmd, encoding=POWERCFG_ENCODING)
    except (TypeError, LookupError, ValueError) as e:
        logging.debug(f"[POWER] Az OEM-dekódolás nem elérhető ({e}) - sima hívás, "
                      f"a séma NEVE ékezetes betűknél romolhat.")
        return run_fn(cmd)


def parse_active_scheme(stdout):
    """A `powercfg /getactivescheme` kimenetéből a GUID + a zárójeles séma-név.

    Tiszta függvény, offline tesztelhető. A NÉV csak naplózásra/kiírásra való - döntést
    soha nem hozunk belőle, mert lokalizált."""
    if not stdout:
        return None, ''
    m = _GUID_RE.search(stdout)
    if not m:
        return None, ''
    name = ''
    # A záró `*` az aktív sémát jelöli a `powercfg /list` kimenetében
    # ("... (Teljesítménycentrikus) *"). A /getactivescheme nem teszi ki, de a parser
    # mindkét kimenetet kapja, ezért megengedjük.
    nm = re.search(r'\(([^)]*)\)\s*\*?\s*$', stdout.strip())
    if nm:
        name = nm.group(1).strip()
    return m.group(1).lower(), name


def read_active_scheme(run_fn):
    """Az aktív energiaséma (guid, név). Hiba esetén (None, '')."""
    try:
        res = _run_powercfg_text(run_fn, ['powercfg', '/getactivescheme'])
        if res.returncode != 0:
            logging.warning(f"[POWER] Az aktív séma lekérdezése sikertelen "
                            f"(returncode={res.returncode}): {(res.stderr or '')[:200]}")
            return None, ''
        guid, name = parse_active_scheme(res.stdout)
        logging.info(f"[POWER] Jelenlegi energiaséma: {guid} ({name or 'névtelen'})")
        return guid, name
    except Exception as e:
        logging.warning(f"[POWER] Az aktív séma lekérdezése kivételre futott: {e}")
        return None, ''


def ensure_high_performance_scheme(run_fn):
    """A Nagy teljesítményű séma aktívvá tétele. Visszatérés: True, ha sikerült.

    Ha a séma nincs a gépen, LÉTREHOZZUK a beépített sablonból (`/duplicatescheme`) -
    számos OEM-image, és a modern készenlétet (Modern Standby) használó laptopok egy
    része, egyszerűen nem listázza. A duplikálás után a séma új GUID-ot kap, ezért az
    aktiválás annak a GUID-jával megy, nem a sablonéval."""
    res = run_fn(['powercfg', '/setactive', HIGH_PERFORMANCE_GUID])
    if res.returncode == 0:
        logging.info("[POWER] A Nagy teljesítményű séma aktiválva.")
        return True

    logging.info(f"[POWER] A Nagy teljesítményű séma nincs a gépen "
                 f"(returncode={res.returncode}) - létrehozás a beépített sablonból.")
    # Szöveges kimenetet parse-olunk belőle (GUID), ezért ez is az OEM-dekódolt úton megy -
    # a GUID ugyan ASCII, de egy helyen egy szabály: ami szöveget ad vissza, az így fut.
    dup = _run_powercfg_text(run_fn, ['powercfg', '/duplicatescheme', HIGH_PERFORMANCE_GUID])
    if dup.returncode != 0:
        logging.warning(f"[POWER] A séma létrehozása sem sikerült "
                        f"(returncode={dup.returncode}): {(dup.stdout or '')[:200]}")
        return False
    new_guid, _ = parse_active_scheme(dup.stdout)
    if not new_guid:
        logging.warning("[POWER] A létrehozott séma GUID-ja nem olvasható ki a kimenetből.")
        return False
    res2 = run_fn(['powercfg', '/setactive', new_guid])
    ok = res2.returncode == 0
    logging.info(f"[POWER] Létrehozott séma ({new_guid}) aktiválása: "
                 f"{'sikeres' if ok else 'SIKERTELEN'}")
    return ok


def parse_setting_indexes(stdout):
    """A `powercfg /query <séma> <alcsoport> <beállítás>` kimenetéből (AC, DC) egész érték.

    A címkék lokalizáltak, a hexa értékek SORRENDJE nem: az utolsó két `0x........` az AC és
    a DC index (ugyanaz a pozicionális szabály, amivel a stressz-teszt energiazára is
    olvas). Tiszta függvény. (None, None), ha nem olvasható."""
    vals = re.findall(r'0x([0-9a-fA-F]{8})\b', stdout or '')
    if len(vals) < 2:
        return None, None
    return int(vals[-2], 16), int(vals[-1], 16)


def keep_current_scheme(guid, proc_min_ac):
    """Megtartjuk-e az aktív sémát (a Nagy teljesítményű helyett)? Tiszta függvény.

    Igen: a Végső teljesítmény, vagy egy EGYEDI séma, amelyben a processzor minimuma már
    100% (AC). A beépített Kiegyensúlyozott/Energiatakarékos soha; a Nagy teljesítményű
    úgyis az, amire váltanánk."""
    g = (guid or '').lower()
    # A beépített Nagy teljesítményű sem "marad": arra a normál út fut (újra-aktiválás +
    # a ReviOS-értékek), ami ugyanoda vezet.
    if not g or g in BUILTIN_NON_PERFORMANCE or g == HIGH_PERFORMANCE_GUID:
        return False
    if g == ULTIMATE_PERFORMANCE_GUID:
        return True
    return proc_min_ac == 100


def apply_performance_plan(run_fn, log=None):
    """A gép teljesítmény-módba állítása. Visszatérés: eredmény-dict.

    Kulcsok: ok (bool - a séma aktív lett-e), previous_guid, previous_name,
    applied (címkék listája), failed (címkék listája).

    SOHA NEM DOB: a hívó egy már sikeres lánc legvégén hívja, és egy energiaséma-hiba
    nem teheti hibássá a driver-telepítést."""
    say = log or (lambda _m: None)
    out = {'ok': False, 'previous_guid': None, 'previous_name': '',
           'applied': [], 'failed': [], 'kept': False}
    try:
        prev_guid, prev_name = read_active_scheme(run_fn)
        out['previous_guid'], out['previous_name'] = prev_guid, prev_name

        proc_min_ac = None
        if prev_guid and prev_guid not in BUILTIN_NON_PERFORMANCE and prev_guid != HIGH_PERFORMANCE_GUID:
            q = _run_powercfg_text(run_fn, ['powercfg', '/query', 'SCHEME_CURRENT',
                                            'SUB_PROCESSOR', 'PROCTHROTTLEMIN'])
            proc_min_ac, _dc = parse_setting_indexes(getattr(q, 'stdout', ''))
            logging.info(f"[POWER] Egyedi/nem beépített séma: processzor minimuma (AC) = "
                         f"{proc_min_ac if proc_min_ac is not None else 'nem olvasható'}%")
        if keep_current_scheme(prev_guid, proc_min_ac):
            out['kept'] = True
            out['ok'] = True
            logging.info(f"[POWER] A jelenlegi séma MARAD ({prev_guid}, {prev_name or 'névtelen'}) - "
                         f"már teljesítményre hangolt; a maximum-beállítások rá kerülnek.")
        elif not ensure_high_performance_scheme(run_fn):
            say('⚠️ A Teljesítménycentrikus energiasémát nem sikerült beállítani - a gép '
                'energiabeállításai változatlanok maradtak.')
            return out
        else:
            out['ok'] = True

        # Minden beállítás, a rejtettek GUID-dal. Egy hiányzó beállítás nem hiba: több
        # gép- és Windows-kiadásfüggő (pl. a SUB_PCIEXPRESS asztali gépeken, a P-mag
        # magparkolás nem hibrid processzoron hiányozhat).
        for sub, setting, ac, dc, label in PERFORMANCE_SETTINGS:
            ok_ac = run_fn(['powercfg', '/setacvalueindex', 'SCHEME_CURRENT',
                            sub, setting, str(ac)]).returncode == 0
            ok_dc = run_fn(['powercfg', '/setdcvalueindex', 'SCHEME_CURRENT',
                            sub, setting, str(dc)]).returncode == 0
            if ok_ac or ok_dc:
                out['applied'].append(label)
                logging.info(f"[POWER] Beállítva: {label} (AC={ac} {'ok' if ok_ac else 'HIBA'}, "
                             f"DC={dc} {'ok' if ok_dc else 'HIBA'})")
            else:
                out['failed'].append(label)
                logging.info(f"[POWER] Nem elérhető ezen a gépen: {label} ({sub}/{setting})")

        # A módosítások CSAK egy újbóli /setactive után lépnek életbe - e nélkül a
        # beállítások bekerülnek a sémába, de a futó rendszerre nem érvényesülnek.
        run_fn(['powercfg', '/setactive', 'SCHEME_CURRENT'])

        # VISSZAOLVASÁS (4. elv: a verdikt a tényleges állapot, nem a visszatérési kód).
        # `/qh`: a rejtett beállításokat is mutatja (a sima `/query` nem - mérve).
        by_label = {s[4]: s for s in PERFORMANCE_SETTINGS}
        for label in list(out['applied']):
            sub, setting, ac, _dc, _l = by_label[label]
            q = _run_powercfg_text(run_fn, ['powercfg', '/qh', 'SCHEME_CURRENT', sub, setting])
            got_ac, got_dc = parse_setting_indexes(getattr(q, 'stdout', ''))
            if got_ac is not None and got_ac != ac:
                out['applied'].remove(label)
                out['failed'].append(label)
                logging.warning(f"[POWER] Visszaolvasva NEM a kért érték: {label} "
                                f"(kért AC={ac}, olvasott AC={got_ac}, DC={got_dc})")
        logging.info(f"[POWER] Teljesítmény-mód kész. Beállítva (visszaolvasva): {len(out['applied'])}, "
                     f"nem elérhető / nem vette át: {len(out['failed'])}.")
    except Exception as e:
        logging.warning(f"[POWER] A teljesítmény-mód beállítása kivételre futott "
                        f"(a lánc ettől még sikeres): {e}", exc_info=True)
    return out
