"""Stabilitás Teszt konstansok: eszközlista, kill-lista, dialógus-kattintási szekvenciák,
Linpack prompt-script, RAM-opciók, energiagazdálkodási beállítás-lista."""

# === AUTO-IMPORTS ===
# === /AUTO-IMPORTS ===



# Stress Teszt / Szervíz Programok: egyenként is indítható programok, kulcs ->
# (megjelenített név, a stresstools.zip-ben keresett fájlnév-változatok). Egy fájlnév-
# bejegyzés lehet pontos név VAGY fnmatch-minta ('*'/'?' joker, pl. a GPU-Z exe-je
# verziószámot hordoz a nevében: GPU-Z.2.68.0.exe) - lásd _find_stress_tool_exes.
# Egy eszköz jelenléte a ZIP-től függ - ha nincs benne, a keresés futásidőben
# "nem található" hibát ad, ami nem kódhiba.
STRESS_TOOLS = {
    'furmark': ('FurMark', ['furmark.exe']),
    'prime95': ('Prime95', ['prime95.exe']),
    'linpack': ('Linpack Xtreme', ['linpackxtreme.exe', 'linpack.exe']),
    # A lista SORRENDJE itt prioritás: a 64 bites verziót preferáljuk, a 32 bites csak
    # akkor indul, ha nincs 64 bites az extracted mappában (lásd _find_stress_tool_exes).
    'hwinfo': ('HWiNFO64', ['hwinfo64.exe', 'hwinfo32.exe']),
    'hdsentinel': ('HD Sentinel', ['hdsentinel.exe', 'hdsentinel_x64.exe', 'hdsentinel64.exe']),
    # 2026-07-ben a ZIP-hez adott szervíz-/infó programok:
    'cpuz': ('CPU-Z', ['cpuz_x64.exe', 'cpuz.exe', 'cpuz*.exe']),
    'gpuz': ('GPU-Z', ['gpu-z*.exe', 'gpuz*.exe']),
    'zentimings': ('ZenTimings', ['zentimings.exe']),
    'nvinspector': ('NVIDIA Profile Inspector', ['nvidiaprofileinspector.exe']),
}

# Amelyik eszköz indításakor érdemes a képernyő-kikapcsolást/alvó módot letiltani
# (_lock_power_for_stress): a hosszan futó terhelők és monitorok. A gyors infó-eszközöknél
# (CPU-Z, GPU-Z, ZenTimings, Profile Inspector) NEM nyúlunk az energiagazdálkodáshoz -
# feleslegesen tiltaná le az alvó módot a program következő indításáig.
STRESS_POWER_LOCK_KEYS = ['furmark', 'prime95', 'linpack', 'hwinfo', 'hdsentinel']

# Egy automatizálási lépés időkorlátja másodpercben: ennyit várunk egy dialógus/gomb
# megjelenésére (_find_pid_window_with_child_text), a Linpack konzolablakára és minden
# egyes konzol-promptjára (_auto_answer_console). Korábban 60 mp volt, de terepen egy
# ERŐS gépen is mértünk már 56 mp-et (FurMark GO gomb, 4 program egyszerre indult) - egy
# lassú, dual-core + HDD-s gépen ez simán túlcsúszik a 60 mp-en, és az automatizálás
# feladta, a program ott ült a beállító képernyőjén. A 180 mp-nek nincs hátránya: ez csak
# FELSŐ korlát, normál esetben a lépések pár mp alatt teljesülnek, a várakozás pedig
# esemény-vezérelt (pollozás), nem fix késleltetés.
STRESS_STEP_TIMEOUT = 180

# Konzolos program (Linpack) prompt-várásának ABSZOLÚT plafonja. A STRESS_STEP_TIMEOUT ott
# a "mozdulatlanság" mérőórája: minden képernyő-változásnál újraindul, mert amíg a konzol
# rajzol, addig a program dolgozik. Terepen (friss/klónozott Win11 24H2+, 2026-07) a Linpack
# az induláskor észlelte, hogy hiányzik a WMIC (opcionális feature lett), és DISM-mel maga
# telepítette - a százalékos csík percekig kúszott, a fix 180 mp lejárt, és az automatizálás
# feladta egy tökéletesen dolgozó program alatt. Ez a plafon csak a VALÓDI végtelen
# várakozást vágja el (pl. egy magától pörgő kimenet, ami sosem ér promptig).
CONSOLE_PROMPT_MAX_WAIT = 900

# "A konzol egy BILLENTYŰRE vár, de nem arra a promptra, amit mi keresünk" eset feloldása
# (_auto_answer_console). Ugyanabból a terepi futásból: a Linpack a hiányzó WMIC miatt egy
# "Press any key to continue . . ." sorral állt meg MÉG A MENÜ ELŐTT, és onnantól semmi nem
# történt - a mi scriptünk a 'select an action' promptra várt, ami sosem jött volna el
# magától. Ha a képernyő CONSOLE_UNBLOCK_AFTER mp-ig mozdulatlan ÉS az utolsó sorok egy
# ilyen "nyomj egy gombot" markert mutatnak (ami nem a most várt prompt), küldünk egy
# Entert - pontosan azt, amit a felhasználó tenne. Legfeljebb CONSOLE_UNBLOCK_MAX-szor, hogy
# egy félreértett képernyőn se kezdjünk vaktában billentyűket szórni.
CONSOLE_UNBLOCK_MARKERS = ('press any key', 'nyomjon meg egy', 'nyomj meg egy')
CONSOLE_UNBLOCK_AFTER = 15
CONSOLE_UNBLOCK_MAX = 3

# Meddig várjuk az ablak-elrendezésnél, hogy egy program a betöltő/elemző képernyője helyett
# a VALÓDI főablakát mutassa (_find_main_window_for_pid), és mely címek árulkodnak arról,
# hogy még csak az átmeneti splash van kint. Terepen (2026-07) a HWiNFO64 a szenzor-
# felderítés alatt (~20-60 mp) csak egy "Elemzés..." dialógust mutat, a főablaka pedig még
# láthatatlan: az egyik futásnál emiatt EGYÁLTALÁN nem találtunk ablakot ("nem található
# ablak a pozicionáláshoz"), a másiknál pedig a program a kis "Elemzés..." dialógust
# helyezte a jobb-alsó negyedbe a szenzor-ablak helyett.
STRESS_MAIN_WINDOW_WAIT = 90
STRESS_SPLASH_TITLE_MARKERS = ('elemzés', 'analy', 'betölt', 'loading', 'please wait',
                               'starting', 'indítás...')

# A "Stress teszt indítása" gomb (start_stress_tests - az egyetlen AUTOMATIZÁLT út:
# dialógus-nyomkodás + 4 részre osztott ablak-elrendezés) csak ezeket a valódi
# terhelés-generáló teszteket indítja - a többi program (HD Sentinel, CPU-Z, GPU-Z,
# ZenTimings, NVIDIA Profile Inspector) csak egyenként (start_stress_tool) érhető el,
# ott viszont SEMMILYEN automatizálás nincs (kifejezett felhasználói kérés).
STRESS_TOOLS_BULK = ['furmark', 'prime95', 'linpack', 'hwinfo']

# A "Minden teszt bezárása" (stop_stress_tests) által név szerint is kilövendő programok -
# biztonsági háló arra az esetre, ha egy folyamatot nem az általunk eltárolt PID-fa alól
# indítottak (pl. UAC 'runas' út, ahol nincs PID-ünk, vagy kézzel indított példány). A
# Linpack tényleges terhelő motorja (linpack_amd64/intel64) és az opcionális HWMonitor a
# Linpack.exe gyerekfolyamatai - a PID-fa kilövése normál esetben elviszi őket, ez itt
# csak tartalék.
STRESS_KILL_IMAGES = [
    'furmark.exe', 'prime95.exe',
    'linpack.exe', 'linpackxtreme.exe', 'linpack_amd64.exe', 'linpack_intel64.exe',
    'linpack_amd32.exe', 'linpack_intel32.exe', 'HWMonitor_x64.exe',
    'hwinfo64.exe', 'hwinfo32.exe',
    'hdsentinel.exe', 'hdsentinel_x64.exe', 'hdsentinel64.exe',
    # A taskkill /IM elfogadja a '*' jokert - a GPU-Z/CPU-Z exe-neve verziófüggő lehet.
    'cpuz*', 'gpu-z*', 'zentimings.exe', 'nvidiaprofileinspector.exe',
]


# Linpack Xtreme RAM-választó menüjének opciói (a program konzolos menüjéből, sorrendben):
# (menüpont szám, GB). Az automatizálás a rendszer teljes RAM-jához a legnagyobb ide illő
# (<= a ténylegesen meglévő RAM) opciót választja - lásd _pick_linpack_ram_option().
LINPACK_RAM_OPTIONS = [(1, 2), (2, 4), (3, 6), (4, 8), (5, 10), (6, 14), (7, 30)]


# GUI programok indítás utáni, egymást követő dialógusablakainak automatikus végignyomkodása
# (lásd _auto_click_sequence). Egy lépés lehet egyetlen felirat, alternatívák listája
# (localizált feliratokhoz - pl. HWiNFO a rendszer nyelvén jelenik meg, "Indítás" vagy "Start"),
# vagy egy dict az alábbi kulcsokkal:
#   'labels':        felirat(ok), amelyik gombot meg kell nyomni
#   'skip_if_found': ha a keresés közben nem a 'labels', hanem ezek egyike kerül elő, a lépés
#                    kattintás nélkül KIMARAD - a Prime95 miatt kell: a GIMPS üdvözlő ("Just
#                    Stress Testing") CSAK a legelső indításkor jelenik meg, a gomb megnyomása
#                    után a prime.txt-be írt StressTester=1 miatt minden további indítás
#                    egyből a "Run a Torture Test" dialógussal (Small FFTs rádiógomb) kezdődik
#   'optional':      ha a lépés dialógusa a saját timeoutján belül nem jelenik meg, az NEM
#                    hiba - a lépés kimarad, a sorozat nem szakad meg
#   'timeout':       a lépés saját keresési időkorlátja mp-ben (alapértelmezés: STRESS_STEP_TIMEOUT)
#   'exact':         csak TELJES felirat-egyezés számít (rövid feliratoknál - 'OK', 'Igen' -
#                    véd a részleges hamis találatoktól, pl. 'ventilátorok' vége 'ok')
STRESS_CLICK_SEQUENCES = {
    'furmark': ['GPU stress test', 'GO'],  # beállító-ablak -> "*** CAUTION ***" figyelmeztetés
    'prime95': [
        {'labels': ['Just Stress Testing'], 'skip_if_found': ['small ffts (tests l1/l2/l3']},  # GIMPS üdvözlő (csak első indításkor)
        'small ffts (tests l1/l2/l3',  # torture test típus rádiógomb
        'OK',
    ],
    'hwinfo': [
        ['Indítás', 'Start'],  # a HWiNFO64.INI SensorsOnly=1 már kiválasztja a módot
        # Indítás után a HWiNFO még feldobhat egy ablakot: terepen (debug leltárból
        # azonosítva) ez a "HWiNFO® 64 Update" frissítés-értesítő volt, aminek a gombja
        # 'Bezárás'/'Close' - de más megerősítő popup (OK/Igen/Yes gombbal) is előfordulhat.
        # Ha 20 mp-en belül megjelenik ezek egyike, lenyomjuk; ha nem, a lépés hang nélkül
        # kimarad. (Az INI-be írt CheckForUpdate=0 elvileg magát az update-ablakot is
        # letiltja - ez a lépés a biztonsági háló, ha az INI-kulcsot nem venné figyelembe.)
        {'labels': ['OK', 'Igen', 'Yes', 'Bezárás', 'Close'], 'optional': True, 'timeout': 20, 'exact': True},
    ],
}

# A Linpack Xtreme v1.1.8 konzolos stressz-teszt menüjének (valódi gépen, a konzol
# képernyőpufferét kiolvasva ÉS a Linpack.exe-be csomagolt .bat forrását elemezve
# ellenőrzött) prompt-sorrendje: (prompt-részlet, válasz, kell-e Enter) hármasok.
# Az automatizálás (_auto_answer_console) minden válasz elküldése ELŐTT megvárja, hogy a
# hozzá tartozó prompt ténylegesen megjelenjen a konzol képernyőjén - vakon, fix időzítéssel
# gépelve egy leterhelt gépen (ahol a menü több mp késéssel jön elő) a válaszok rossz
# prompthoz érkeznek, és a teszt el sem indul.
#
# A "kell-e Enter" flag NEM opcionális finomság: a Linpack indítója egy .bat, amiben a
# menük/kérdések 'choice' paranccsal olvasnak (EGYETLEN billentyű, Enter nélkül), a
# futásszám viszont 'set /p'-vel (teljes sor Enterrel). Ha egy choice-os menünek Enterrel
# együtt küldjük a választ, a choice csak a billentyűt fogyasztja el, az Enter a konzol
# pufferében marad, és a következő 'set /p' üres sorként olvassa be -> a batch
# "if %RUNS% LSS 1" sora szintaktikai hibává válik, a cmd az EGÉSZ szkriptet megszakítja,
# és a Linpack ablaka szó nélkül eltűnik ~1 mp-cel a RAM-válasz után (valós gépen
# bizonyított, sokáig érthetetlen "összeomlás"). A RAM-opció válasza futásidőben kerül a
# listába (lásd _build_linpack_console_script). Üres válasz = csak Enter.
LINPACK_PROMPT_SCRIPT = [
    ('select an action', '2', False),            # főmenü (choice): 2 = Stress Test
    ('amount of ram', None, False),              # RAM-menü (choice): futásidőben kiválasztott opciószám
    ('number of times to run', '10000', True),   # futásszám (set /p!): gyakorlatilag "amíg le nem állítják"
    ('all available threads', 'Y', False),       # choice: minden szál használata
    ('disable sleep mode', 'N', False),          # choice: alvó módot az app maga tiltja (_lock_power_for_stress)
    ('hwmonitor', 'N', False),                   # choice: CPUID HWMonitor nem kell, fut a HWiNFO
    ('press any key', '', True),                 # pause: Enter, ezután indul a teszt
]



# Stabilitás Teszt közben letiltandó energiagazdálkodási beállítások (powercfg alias-ok -
# ezek a kulcsszavak nyelvfüggetlenek, minden Windows-nyelven ugyanígy kell megadni őket).
# SUB_VIDEO/VIDEOIDLE = kijelző kikapcsolása, SUB_SLEEP/STANDBYIDLE = alvó mód,
# SUB_SLEEP/HIBERNATEIDLE = hibernálás.
STRESS_POWER_SETTINGS = [('SUB_VIDEO', 'VIDEOIDLE'), ('SUB_SLEEP', 'STANDBYIDLE'), ('SUB_SLEEP', 'HIBERNATEIDLE')]
STRESS_POWER_REG_KEY = r"SOFTWARE\DriverVarazslo\StressPowerBackup"
