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

# Cinebench R20/R23 parancssori kapcsolók: a MULTI-THREAD (minden processzormagot és
# szálat terhelő) CPU-teszt fut le - ez a "CPU (Multi Core)" pontszám, a Cinebench
# alapértelmezett fő mérése. (Az egyszálas mérés a g_CinebenchCpu1Test=true lenne - azt
# szándékosan NEM futtatjuk: a ranglista a gép össz-teljesítményét hasonlítja.)
# A pontszám a stdout-ra kerül ("CB <pontszám>" sor), majd a program MAGÁTÓL kilép.
# A g_acceptDisclaimer az R23-nál kötelező az EULA-ablak kihagyásához, az R20 pedig
# szó nélkül elfogadja (ismeretlen kapcsolót a Cinebench nem kifogásol).
CINEBENCH_CLI_ARGS = ['g_CinebenchCpuXTest=true', 'g_acceptDisclaimer=true']

# A felületen megjelenő beállítás-leírás (EGY forrásból, hogy a kijelzett és a ténylegesen
# futtatott beállítás soha ne csúszhasson szét).
CINEBENCH_SETTINGS_LABEL = 'CPU Multi-Thread (minden mag és szál)'

# A Cinebench multi-core teszt felső időkorlátja másodpercben. Erős gépen 1-3 perc a
# lefutás, de egy régi 2-magos irodai gépen 15+ perc is lehet - a plafon szándékosan
# bő (a stressz-automatizálás "err long, not short" elve), csak a valódi beragadást
# vágja el.
CINEBENCH_TIMEOUT_S = 2400

# FurMark 1.x parancssori benchmark: /nogui (nincs beállító ablak), /benchmark +
# /max_time (ennyi ms után vége), /log_score (az eredmény a FurMark mappájába írt
# furmark-scores.txt-be kerül - EZT parseoljuk, nem a képernyőt). A beállítások FIXEK,
# hogy a ranglista FPS-értékei összehasonlíthatóak legyenek gépek között.
#
# A BEÁLLÍTÁSOK A SZERVIZ RÉGI, KÉZI FURMARK-SCRIPTJÉT KÖVETIK (explicit felhasználói
# döntés, 2026-07-29): a kollégák évek óta 1024-es ablakban, animált háttérrel futtatják
# a kártyákat, és FEJBŐL ISMERIK a szokásos FPS-tartományokat - a benchmarknak ehhez a
# megszokáshoz kell igazodnia, nem fordítva. Ezért: 1024x768, 0x MSAA, /enable_dyn_bkg=1
# + /bkg_img_id=2 (a régi script animált háttere). Amiben SZÁNDÉKOSAN eltérünk:
#   - a magasság 768, nem a script 728-a (kifejezett felhasználói döntés) - kb. 5%-kal
#     több képpont, tehát a mi FPS-ünk kicsivel a megszokott alatt lesz;
#   - a futásidő 10 mp (a régi script végtelen): elég az FPS kiolvasásához, viszont a
#     kártya még HIDEG, teljes boost órajelen - egy percekig sütött kártyánál mért
#     értékhez képest ez magasabb szám. A ranglistát ez nem zavarja (minden gépen
#     ugyanaz a beállítás), a régi script-tapasztalattal viszont csak nagyságrendileg
#     vethető össze.
#
# Ablakos mód, teljes képernyő SZÁNDÉKOSAN nincs: egy nem támogatott felbontású panelen
# a módváltás elbukhat, és akkor NINCS eredmény - egy kisebb ablak rosszabb esetben is
# ad számot. Figyelendő viszont (terepen mérve 2026-07-29): a Windows a munkaterülethez
# VÁGJA az ablakot - a kért 1920x1080-ból [Resolution=1898x1024] lett egy 1080p
# monitoron. Az 1024x768 egy 1366x768-as laptopon szintén csonkulna (~1024x690), ezért a
# ténylegesen renderelt felbontást a score-fájlból kiolvassuk, és eltérésnél WARNING
# kerül a logba - a torzítás soha nem néma.
FURMARK_BENCH_TIME_MS = 10000       # explicit felhasználói kérés: 10 mp elég az FPS-hez
FURMARK_BENCH_WIDTH = 1024
FURMARK_BENCH_HEIGHT = 768
FURMARK_MSAA = 0
# A régi szerviz-script animált háttere (a bkg_img_id=2 a FurMark 1.19+ alapértelmezett
# képe). Kis plusz terhelés, de a megszokott FPS-ekhez ez is hozzátartozik.
FURMARK_DYN_BKG_ARGS = ['/enable_dyn_bkg=1', '/bkg_img_id=2']
FURMARK_CLI_ARGS = ['/nogui', '/benchmark', f'/max_time={FURMARK_BENCH_TIME_MS}',
                    f'/width={FURMARK_BENCH_WIDTH}', f'/height={FURMARK_BENCH_HEIGHT}',
                    f'/msaa={FURMARK_MSAA}'] + FURMARK_DYN_BKG_ARGS + [
                    '/log_score', '/disable_catalyst_warning', '/nomenubar']

# A felületen megjelenő beállítás-leírás. EGY forrásból megy a UI-ba
# (get_benchmark_settings), hogy a kiírt és a ténylegesen futtatott beállítás soha ne
# csúszhasson szét - a szerviznek pontosan tudnia kell, mivel készült a szám.
FURMARK_SETTINGS_LABEL = (f'{FURMARK_BENCH_WIDTH}×{FURMARK_BENCH_HEIGHT} · {FURMARK_MSAA}× MSAA · '
                          f'{FURMARK_BENCH_TIME_MS // 1000} mp · animált háttér · ablakos')

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
