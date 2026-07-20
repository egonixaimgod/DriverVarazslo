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
}

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
