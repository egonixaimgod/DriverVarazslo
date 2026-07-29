"""Benchmark nézet konstansai: a két benchmark program (Cinebench R20, Unigine Heaven)
kulcsai + a stresstools.zip-ben keresett fájlnevek, és a felhő-ranglista alap-végpontja."""

# === AUTO-IMPORTS ===
# === /AUTO-IMPORTS ===


# A benchmark programok a KÖZÖS stresstools.zip-ből jönnek (ugyanúgy, mint a
# stressztesztek): kulcs -> (megjelenített név, keresett exe-fájlnevek / fnmatch-minták).
# A finder (benchmark_core.find_bench_tool_exes) os.walk-kal keresi őket a kicsomagolt
# mappában, tehát TETSZŐLEGES almappában lehetnek - a ZIP-be pl. így érdemes tenni:
#   CinebenchR20\Cinebench.exe   és   Heaven\Heaven.exe
# Egy fájlnév-bejegyzés lehet pontos név vagy fnmatch-minta ('*'/'?' joker) - utóbbi arra
# az esetre, ha az exe a nevében verziószámot hordozna.
# A Heaven indítója egy .bat (heaven.bat) - a subprocess.Popen CREATE_NEW_CONSOLE-lal a
# batch fájlt is rendesen lefuttatja Windowson (ellenőrizve), tehát a _launch_stress_exe
# módosítás nélkül elindítja. A .bat a prioritás; az .exe csak tartalék, ha egy jövőbeli
# ZIP mégis exe-t tenne be.
BENCH_TOOLS = {
    'cinebench': ('Cinebench R20', ['cinebench.exe', 'cinebench*.exe']),
    'heaven': ('Unigine Heaven', ['heaven.bat', 'heaven.exe']),
    # Az AUTOMATA benchmark-futtatás (run_benchmark_suite) GPU-tesztje a FurMark
    # (ugyanaz az exe, amit a Stress Teszt nézet használ a stresstools.zip-ből) -
    # parancssori benchmark módban fut (/nogui /benchmark), a Heaven-nel ellentétben
    # ugyanis megbízhatóan, fájlba írva adja vissza a pontszámot (/log_score).
    'furmark': ('FurMark', ['furmark.exe']),
}

# ============================================================================
# Automata (1 kattintásos) benchmark-futtatás konstansai (run_benchmark_suite)
# ============================================================================

# Cinebench R20/R23 parancssori kapcsolók: a multi-core CPU-teszt lefut, a pontszám a
# stdout-ra kerül ("CB <pontszám>" sor), majd a program MAGÁTÓL kilép - nincs GUI.
# A g_acceptDisclaimer az R23-nál kötelező az EULA-ablak kihagyásához, az R20 pedig
# szó nélkül elfogadja (ismeretlen kapcsolót a Cinebench nem kifogásol).
CINEBENCH_CLI_ARGS = ['g_CinebenchCpuXTest=true', 'g_acceptDisclaimer=true']

# A Cinebench multi-core teszt felső időkorlátja másodpercben. Erős gépen 1-3 perc a
# lefutás, de egy régi 2-magos irodai gépen 15+ perc is lehet - a plafon szándékosan
# bő (a stressz-automatizálás "err long, not short" elve), csak a valódi beragadást
# vágja el.
CINEBENCH_TIMEOUT_S = 2400

# FurMark 1.x parancssori benchmark: /nogui (nincs beállító ablak), /benchmark +
# /max_time (ennyi ms után vége), /log_score (az eredmény a FurMark mappájába írt
# FurMark-Scores.txt-be kerül - EZT parseoljuk, nem a képernyőt). A felbontás FIX,
# hogy a ranglista FPS-értékei összehasonlíthatóak legyenek gépek között.
FURMARK_BENCH_TIME_MS = 10000       # explicit felhasználói kérés: ~5-10 mp futás
FURMARK_BENCH_WIDTH = 1920
FURMARK_BENCH_HEIGHT = 1080
FURMARK_CLI_ARGS = ['/nogui', '/benchmark', f'/max_time={FURMARK_BENCH_TIME_MS}',
                    f'/width={FURMARK_BENCH_WIDTH}', f'/height={FURMARK_BENCH_HEIGHT}',
                    '/msaa=0', '/log_score', '/disable_catalyst_warning', '/nomenubar']

# A FurMark folyamat kilépésére/pontszám-fájljára várt TÖBBLET-idő a max_time felett,
# másodpercben (induló ablak + shader-fordítás + a score-fájl kiírása). Ha letelik és a
# folyamat még él, kilőjük - egyes FurMark-verziók a benchmark után eredmény-ablakot
# hagynak fenn, ami sosem lépne ki magától.
FURMARK_EXIT_GRACE_S = 90

# A felhő-ranglista HTTP-végpontja (Google Apps Script webalkalmazás /exec URL-je, vagy
# bármely más, ugyanezt a protokollt beszélő backend). ÜRESEN hagyva a funkció "nincs
# beállítva" állapotban van. A végpont a felületről is megadható (Benchmark nézet ->
# "⚙️ Ranglista végpont beállítása"), az felülírja ezt az alapértéket (mentés:
# <app_data>\benchmark_endpoint.txt). Ha ide beírsz egy fix URL-t, az lesz az alapértelmezett
# minden gépen, amíg felül nem írják a felületről.
#
# A protokoll (lásd benchmark_leaderboard_setup.md):
#   GET  -> a teljes ranglista JSON tömbként (soronként egy gép objektuma),
#   POST -> (JSON body) egy gép eredményének beszúrása/frissítése (upsert a machine_id
#           mezőre - ugyanarról a gépről újra feltöltve a meglévő sor frissül).
BENCHMARK_API_URL_DEFAULT = "https://script.google.com/macros/s/AKfycbx87TlJfSvcq5mbXVTYUaH5cvGN5PpH5zS6xoLY_r9B-53ijPA73S-x6yFxQU33by6p/exec"
