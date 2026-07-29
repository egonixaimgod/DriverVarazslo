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
# furmark-scores.txt-be kerül - EZT parseoljuk, nem a képernyőt). A felbontás FIX,
# hogy a ranglista FPS-értékei összehasonlíthatóak legyenek gépek között.
#
# A felbontás 1280x720, NEM 1920x1080 (terepen mérve, 2026-07-29): a FurMark ABLAKOS
# módban fut (/nogui), és az ablakot a Windows a munkaterülethez vágja - egy 1080p
# monitoron a kért 1920x1080-ból [Resolution=1898x1024] lett. Az igazi baj nem a 22
# képpont, hanem hogy a csonkítás MÉRETE monitorfüggő: egy 1366x768-as laptopon
# ~1350x690 lenne, azaz feleannyi képpont -> irreálisan magas FPS és értelmetlen
# ranglista. Az 1280x720 (+ ablakkeret ~1296x760) gyakorlatilag minden shopban látott
# kijelző munkaterületébe befér, így minden gép UGYANANNYI képpontot renderel. A
# ténylegesen renderelt felbontást a score-fájlból kiolvassuk és logoljuk, tehát ha
# egy gépen mégis csonkul, az a logból kiderül (nem néma torzítás).
# Teljes képernyős mód szándékosan NINCS: egy nem támogatott felbontású panelen a
# módváltás elbukhat, és akkor NINCS eredmény - a kisebb ablak rosszabb esetben is ad.
FURMARK_BENCH_TIME_MS = 10000       # explicit felhasználói kérés: ~5-10 mp futás
FURMARK_BENCH_WIDTH = 1280
FURMARK_BENCH_HEIGHT = 720
FURMARK_CLI_ARGS = ['/nogui', '/benchmark', f'/max_time={FURMARK_BENCH_TIME_MS}',
                    f'/width={FURMARK_BENCH_WIDTH}', f'/height={FURMARK_BENCH_HEIGHT}',
                    '/msaa=0', '/log_score', '/disable_catalyst_warning', '/nomenubar']

# A FurMark folyamat kilépésére/pontszám-fájljára várt TÖBBLET-idő a max_time felett,
# másodpercben (induló ablak + shader-fordítás + a score-fájl kiírása). Ha letelik és a
# folyamat még él, kilőjük - egyes FurMark-verziók a benchmark után eredmény-ablakot
# hagynak fenn, ami sosem lépne ki magától.
FURMARK_EXIT_GRACE_S = 90


# ============================================================================
# "Minden program bezárása a mérés előtt" (a benchmark indítása előtti kérdés)
# ============================================================================
# A háttérben futó programok (böngésző, Steam, Discord, frissítők) elveszik a CPU-t/GPU-t
# és a memóriát, ezért a mérés alacsonyabb és futásonként ingadozó lesz. A benchmark
# indításakor ezért megkérdezzük, bezárhatjuk-e őket (explicit felhasználói kérés).
#
# KÉT SZABÁLY, amit nem szabad fellazítani:
#   1. A bezárás GRACEFUL (WM_CLOSE / CloseMainWindow), SOHA nem taskkill: egy mentetlen
#      dokumentumú program így felteszi a "menti?" kérdést és NYITVA marad - a felhasználó
#      munkája nem veszhet el egy benchmark kedvéért. A nyitva maradókat jelentjük, nem
#      erőltetjük.
#   2. Csak LÁTHATÓ FŐABLAKKAL rendelkező folyamatokat érintünk, és a védett listát soha.
#      A shell (explorer), a saját appunk és a WebView2 gyerekfolyamatai nélkül a gép
#      használhatatlanná / a program vakká válna futás közben.
CLOSE_APPS_PROTECTED = [
    # A saját programunk és a beágyazott böngésző-motorja (forrásból futtatva python is)
    'drivervarazslo', 'msedgewebview2', 'python', 'pythonw',
    # Windows shell / rendszerfelület - ezek bezárása használhatatlan asztalt hagyna
    'explorer', 'dwm', 'sihost', 'ctfmon', 'searchhost', 'searchui', 'searchapp',
    'startmenuexperiencehost', 'shellexperiencehost', 'textinputhost', 'lockapp',
    'applicationframehost', 'systemsettings', 'winlogon', 'csrss', 'services', 'lsass',
    # Rendszer-gazdafolyamatok: ezeknek lehet CÍM NÉLKÜLI ablak-leírójuk (élő próbán az
    # svchost bekerült a célok közé!). A szkript ablakcím-feltétele már kiszűri őket, ez
    # a második védővonal - egy svchost-nak küldött WM_CLOSE szolgáltatást állíthat le.
    'svchost', 'dllhost', 'rundll32', 'conhost', 'fontdrvhost', 'audiodg', 'smss',
    'wininit', 'spoolsv', 'wudfhost', 'taskhostw', 'runtimebroker',
    # A szerelő diagnosztikai ablakai, amiket bosszantó lenne elveszíteni
    'taskmgr', 'perfmon', 'resmon', 'mmc', 'regedit', 'eventvwr',
]

# Mennyit várunk a WM_CLOSE után, mielőtt megnézzük, mi maradt nyitva (mp).
CLOSE_APPS_WAIT_S = 4

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
