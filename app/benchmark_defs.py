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
# 1024x768 + 8x MSAA + Xtreme burn-in (explicit felhasználói döntés, 2026-07-31). A cél EGY
# mondatban: a mérés vigye el a kártyát a falig, hogy a szám a VIDEOKÁRTYÁRÓL szóljon, ne a
# processzorról - DE úgy, hogy MINDEN gépen ugyanaz a képméret renderelődjön.
#
# A felbontás azért maradt 1024x768 (és nem 4K vagy 1080p lett), mert MÉRÉS mondta meg, hogy
# az ablakos FurMark a KÉPERNYŐNÉL nagyobb képet nem renderel - lásd a lenti mérési blokkot.
# A terhelést a 8x MSAA + Xtreme burn-in adja, nem a felbontás:
#   - a képkockánkénti mintaszám 786k -> 6.3M (8x), plusz az Xtreme burn-in emeli a szőrzet
#     terhelését; a régi 1024x768/0x MSAA-hoz képest ez nagyságrendileg 10x GPU-munka,
#     tehát a driver/CPU képkocka-beküldése már nem lehet a szűk keresztmetszet;
#   - 1024x768 MINDEN szerviz-monitoron elfér (még egy 1366x768-as laptopon is), tehát nincs
#     olyan gép, amelyik a saját kijelzőjéhez vágva, kisebb képen mérne.
# Következmények:
#   - a ranglista RÉGI sorai (1024x768/0x MSAA, Xtreme nélkül) NEM hasonlíthatók az újakhoz -
#     a táblában nincs beállítás-oszlop, tehát a régi sorokat kézzel kell törölni;
#   - régi/kis VRAM-ú kártyán a driver csendben lejjebb veheti a mintavételt, ezért a
#     score-fájl [MSAA=..] ÉS [Resolution=..] mezőjét is kiolvassuk, eltérésnél WARNING megy a
#     logba, és a felületen a TÉNYLEGESEN renderelt érték jelenik meg - a torzítás sosem néma.
#
# A STRESSZ-TESZT tömeges indítása SZÁNDÉKOSAN nem ezt a felbontást használja (lásd
# app/stress_defs.py: STRESS_TOOL_ARGS): ott 4K-t kérünk, mert ott nincs összehasonlítás, csak
# terhelés - a "vágja a monitorához" viselkedés ott egyenesen jó, azt jelenti, hogy a lehető
# legnagyobb képet rendereli az adott gépen.
#
# A RÉGI SZERVIZ-SCRIPT ÖRÖKSÉGE TELJESEN MEGSZŰNT (explicit felhasználói döntés, 2026-07-31).
# Korábban a felbontás/háttér azért volt olyan, amilyen, mert a kollégák fejből ismerték a
# hozzá tartozó FPS-tartományokat; ez a szempont elesett. Az animált háttér
# (/enable_dyn_bkg + /bkg_img_id) is emiatt került ki: a szőrzet mellett elhanyagolható
# terhelés, viszont egy plusz változó a gépek összehasonlításában. Ami maradt a parancssorban,
# az mind ÜZEMELTETÉS, nem képi beállítás: /nogui (ne várjon a beállító ablakban), /benchmark
# + /max_time (kötött hosszú mérés), /log_score (ebből olvassuk ki az eredményt),
# /disable_catalyst_warning (AMD-figyelmeztetés ne blokkolja a felügyelet nélküli futást),
# /nomenubar (ne takarja a menüsor a képet).
#
# Ablakos mód, teljes képernyő SZÁNDÉKOSAN nincs: egy nem támogatott felbontású panelen a
# módváltás elbukhat, és akkor NINCS eredmény - egy ablak rosszabb esetben is ad számot.
#
# ===========================================================================================
# A KÉPERNYŐNÉL NAGYOBB KÉPET AZ ABLAKOS FURMARK NEM RENDEREL - MÉRVE, NE VITASD ÚJRA
# ===========================================================================================
# Ez a szakasz azért van itt, mert a FurMark FELÜLETE MÁST MOND, MINT AMIT CSINÁL, és emiatt
# a projektben már kétszer született téves következtetés. A program fejléce ("Burn-in test,
# 3840x2160 (8X MSAA)") és a furmark-gpu-monitoring.xml width/height mezője a KÉRT beállítást
# írja ki - nem a ténylegesen renderelt képet. Az EGYETLEN megbízható forrás a /log_score
# által írt score-fájl [Resolution=..] mezője.
#
# A döntő mérés (2026-07-31, a csomagban lévő FurMark 1.39.3.0, Intel HD Graphics 530, 1080p
# monitor, azonos beállítások: /nogui /benchmark /max_time=10000 /msaa=8, Xtreme nélkül):
#     kért 3840x2160  ->  [Resolution=1924x1061]  ->  13 képkocka / 10 mp
#     kért 1280x720   ->  [Resolution=1264x681]   ->  28 képkocka / 10 mp
#     kért 3840x2160  ->  [Resolution=1924x1061]  ->  14 képkocka / 10 mp   (ismétlés)
# Ha tényleg 4K-ban renderelt volna, a 720p-s kérés UGYANANNYI képkockát adott volna. Kétszer
# annyit adott, és a pixelarány (2.04 Mpx vs 0.86 Mpx = 2.37x) pontosan megmagyarázza a
# sebességkülönbséget. Vagyis: a kép az ABLAK KLIENS-TERÜLETE, amit a rendszer a képernyőhöz
# igazít - egy 1080p monitoron ~1924x1061 a plafon, akárhány K-t kérünk.
#
# EBBŐL KÖVETKEZIK a felbontás-választás: ha a névleges méret nagyobb a kijelzőnél, akkor a
# ranglista nem gépeket, hanem MONITOROKAT hasonlítana (egy 4K-s monitoros gép négyszer annyi
# képpontot renderelne, mint egy 1080p-s), ami sokkal rosszabb torzítás bárminél, ami eddig
# ebben a fájlban szerepelt. Ezért kell olyan méret, ami MINDEN gépen elfér: 1024x768.
#
# (Történeti helyesbítés: a 2026-07-29-i mérés - kért 1920x1080 -> [Resolution=1898x1024] -
# sokáig "a Windows a munkaterülethez vágta az ablakot"-ként szerepelt itt, holott
# 1920-1898 = 22 és 1080-1024 = 56 pontosan az ugyanazon a gépen mért ablakkeret, vagyis ott
# nem csonkulás volt, hanem kliens-terület. A mostani mérés a másik esetet mutatja: 3840-1924
# már nem lehet ablakkeret. Mindkettő ugyanarra tanít: a score-fájl dönt, semmi más.)
FURMARK_BENCH_TIME_MS = 10000       # explicit felhasználói kérés: 10 mp elég az FPS-hez
FURMARK_BENCH_WIDTH = 1024
FURMARK_BENCH_HEIGHT = 768
FURMARK_MSAA = 8
# Xtreme burn-in: "sets FurMark in a mode that overburns the GPU" (Geeks3D dokumentáció) -
# a szőrzet-renderelés terhelését emeli tovább, nagyobb fogyasztással. Explicit felhasználói
# döntés (2026-07-31), és egyben a szerviz kézi gyakorlata: a gépen talált FurMark saját
# startup_options.xml-jében xtreme_burn_in="1" van, tehát kézzel is így nyomatják. A ranglistát
# nem zavarja (minden gépen ugyanaz), viszont TUDNI kell: ez egy "power virus" mód - gyenge
# tápon vagy haldokló kártyán a gép összeeshet mérés közben, és akkor nincs eredmény.
# (A GUI-ban mellette lévő post-FX-hez az 1.x-ben NINCS parancssori kapcsoló, csak
# startup_options.xml - ezért azt szándékosan nem erőltetjük bele a mérésbe.)
FURMARK_CLI_ARGS = ['/nogui', '/benchmark', f'/max_time={FURMARK_BENCH_TIME_MS}',
                    f'/width={FURMARK_BENCH_WIDTH}', f'/height={FURMARK_BENCH_HEIGHT}',
                    f'/msaa={FURMARK_MSAA}', '/xtreme_burning',
                    '/log_score', '/disable_catalyst_warning', '/nomenubar']

# A felületen megjelenő beállítás-leírás. EGY forrásból megy a UI-ba
# (get_benchmark_settings), hogy a kiírt és a ténylegesen futtatott beállítás soha ne
# csúszhasson szét - a szerviznek pontosan tudnia kell, mivel készült a szám.
FURMARK_SETTINGS_LABEL = (f'{FURMARK_BENCH_WIDTH}×{FURMARK_BENCH_HEIGHT} · {FURMARK_MSAA}× MSAA · '
                          f'Xtreme burn-in · {FURMARK_BENCH_TIME_MS // 1000} mp · ablakos')

# ---------------------------------------------------------------------------
# HÁROM MÉRŐFUTÁS, A JOBBIK SZÁMÍT (terepen mérve 2026-07-30, explicit felhasználói döntés;
# 2-ről 3-ra emelve 2026-07-31, a 4K + 8x MSAA beállítással együtt)
# ---------------------------------------------------------------------------
# Egyetlen futás megbízhatatlan, és pont a szerviz TIPIKUS esetében az: az első mérés egy
# gépen közvetlenül a 621 MB-os stresstools.zip letöltése+kicsomagolása UTÁN történik, tehát
# a FurMark.exe életében először fut onnan - hideg fájlcache, a Defender ekkor szkenneli a
# frissen kiírt exe-ket, és az NVIDIA shader-cache is üres (első futásnál fordít).
# Terepen mért adatok ugyanazon a gépen (RTX 3060, azonos parancssor, azonos felbontás):
#     friss kicsomagolás után:  1331 képkocka -> 133.1 FPS   <-- ez ment fel a ranglistára
#     tiszta gépen:             1812 képkocka -> 181.2 FPS
#     3 egymás utáni ellenőrző futás: 185.5 / 184.2 / 183.5 FPS  (szórás 1.1%)
# Vagyis a FurMark mint mérőeszköz STABIL (~1%), csak az első futás esik ~28%-ot - mintha a
# 10 mp-es ablak első ~2.6 másodperce nem rendelt volna semmit. Ugyanannak a körnek a
# Cinebench-e is 3.3%-kal alacsonyabb lett, tehát tényleg háttérterhelés volt.
# Ezért a mérés HÁROMSZOR fut le teljes hosszban, és a MAGASABB FPS számít: az első futás a
# "cold run" (a FurMark ekkor tölti be magát, fordítja a shadereket, foglalja a 4K-s 8x MSAA
# képpuffert), a maradék kettő az igazi mérés - így egy véletlen háttérterhelés (Windows
# Update, Defender, indexelő) sem tudja egyetlen mérésnél elrontani az eredményt. A 4K + 8x
# MSAA váltás után ez a bemelegítés még fontosabb: nagyobb a betöltendő állapot, és a driver
# az első futáskor allokálja a lényegesen nagyobb puffereket. MINDEN futás bekerül a logba -
# a közöttük lévő nagy eltérés maga is információ a szerviznek. Költség: kb. +13 mp futásonként.
FURMARK_BENCH_RUNS = 3

# PÓT-FUTÁS a gép LEGELSŐ mérésénél (terepen mérve 2026-07-30, Build 247). Amíg a gép
# ablakkeretét nem ismerjük, az első futás még a NÉVLEGES ablakméretet kéri - abból tanuljuk
# meg a keretet -, tehát az a futás KISEBB képen készül, és nem versenyezhet a kompenzált
# futásokkal. Ha emiatt nem marad elég összemérhető eredmény, még ennyi pót-futás indulhat.
# Csak a gép legelső mérését érinti (utána a keret már mentve van).
FURMARK_BENCH_MAX_EXTRA_RUNS = 1

# Hány ÖSSZEMÉRHETŐ (a névleges felbontáson készült) eredmény kell ahhoz, hogy ne induljon
# pót-futás. Kettő: ennyiből már van mit összehasonlítani, tehát egy véletlen háttérterhelés
# nem tudja egyedül eldönteni a végeredményt. SZÁNDÉKOSAN nem FURMARK_BENCH_RUNS (=3): a gép
# legelső mérésénél az első futás úgyis a "cold run" (ekkor tanuljuk a keretet is), tehát a
# 3 tervezett futásból 2 összemérhető eredmény pontosan a kívánt "bemelegítés + 2 mérés"
# felállás - a küszöböt 3-ra állítva minden új gép feleslegesen futna egy negyediket is.
FURMARK_MIN_COMPARABLE_RUNS = 2

# ---------------------------------------------------------------------------
# ABLAKKERET-KOMPENZÁCIÓ (terepen mérve 2026-07-30, megerősítve 2026-07-31)
# ---------------------------------------------------------------------------
# CÉL: a RENDERELT kép minden gépen pontosan FURMARK_BENCH_WIDTH x FURMARK_BENCH_HEIGHT
# legyen - csak így hasonlítható össze két gép FPS-e.
# A /width és /height az ABLAK méretét állítja, a FurMark viszont a KLIENS-területre renderel
# (ablakkeret + címsor nélkül), és a keret mérete GÉPFÜGGŐ. Élőben mért értékek:
#     125%-ra skálázott gép: keret 22x56 px  -> kért 1024x768 -> renderelt 1002x712
#                                             -> kért 1046x824 -> renderelt PONTOSAN 1024x768
#     100%-os gép:           keret 16x39 px  -> kért 1280x720 -> renderelt 1264x681
# Kompenzáció nélkül tehát ugyanaz a parancssor a két gépen 1002x712-t és 1008x729-et
# renderelne: ~3% pixelkülönbség, azaz a magasabb DPI-jű gép INGYEN kapna pár FPS-t. (A
# 125%-os gépen mérve: kompenzálva 184 -> 174.6 FPS, vagyis a régi sorok ~5%-kal optimisták.)
# Ezért az első futásból kiolvassuk a keretet (kért - renderelt), elmentjük a gépre, és a
# további futások ennyivel nagyobb ablakot kérnek.
FURMARK_FRAME_DELTA_FILE = 'furmark_frame_delta.json'
# Hihetőségi korlát a tanult keretméretre. Egy ablakkeret + címsor néhány tíz képpont; ennél
# nagyobb különbség nem keret, hanem CSONKULÁS - azt tanulásként elfogadva a következő futás
# még nagyobb ablakot kérne, ami még jobban csonkulna: elszabaduló visszacsatolás. A korlátok
# a valóságból jönnek: 22x56 (125% DPI) és 16x39 (100% DPI) a mért keretek, tehát a 64x120 még
# egy ~250%-ra skálázott kijelzőt is elbír, viszont egy 1366x768-as laptopon a függőleges
# csonkulás (768 kért -> 600 renderelt, azaz 168) MÁR NEM fér bele - és pont ez volt az az
# eset, amit az offline teszt kibuktatott. Aki ezt fellazítja, visszahozza a visszacsatolást.
# Második védővonal a munkaterület-ellenőrzés (plan_furmark_size): ha a kompenzált ablak nem
# fér ki a képernyőre, nem kompenzálunk. Ez az 1024x768-as névleges méretnél értelmes és
# elégséges - minden létező kijelzőn elfér, tehát a "nem fér ki" tényleg csak csonkulást
# jelenthet. (2026-07-31-én rövid ideig 3840x2160 volt a névleges méret, ahol ez az őr mindig
# tüzelt volna; az a felbontás azóta kikerült, mert MÉRVE nem is renderelődött - lásd fentebb.)
FURMARK_FRAME_DELTA_MAX_X = 64
FURMARK_FRAME_DELTA_MAX_Y = 120

# ---------------------------------------------------------------------------
# MÁR FUTÓ STRESSZ-/BENCHMARK PROGRAMOK A MÉRÉS ELŐTT (terepen mérve 2026-07-30)
# ---------------------------------------------------------------------------
# A CLOSE_APPS_PROTECTED-es udvarias bezárás CSAK látható főablakos felhasználói programokat
# érint, és a terepen pont a lényeget hagyta ki: a szerelő 22:43-kor kézzel elindított egy
# FurMarkot, 22:44-kor rányomott a benchmarkra, és a Cinebench 11 mp után eredmény nélkül
# kilépett ("nem található 'CB <pont>' sor"), mert a GPU-t egy másik program terhelte. A
# mérés előtt ezért NÉV SZERINT kilőjük a stressz-/benchmark programokat: ezek nem a
# felhasználó munkája (nincs mit elveszíteni), és a jelenlétük érvénytelenné teszi a mérést.
BENCH_PRE_KILL_IMAGES = [
    # a stressz-teszt nézet programjai (load generátorok + szenzor-monitorok: a HWiNFO/GPU-Z
    # folyamatos szenzor-lekérdezése is beleszól a mérésbe)
    'furmark', 'prime95', 'linpack', 'linpack*', 'hwinfo64', 'hwinfo32', 'hwmonitor*',
    'hdsentinel*', 'cpuz*', 'gpu-z*', 'zentimings', 'nvidiaprofileinspector',
    # a benchmark programok korábbi, kézzel indított példányai (egyenkénti indító kártyák)
    'cinebench*', 'heaven*',
]

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
