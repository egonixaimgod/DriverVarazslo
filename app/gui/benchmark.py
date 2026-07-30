"""DriverVarázsló GUI - Benchmark: 1 kattintásos, AUTOMATA benchmark-futtatás (Cinebench R20
multi-core -> FurMark GPU) a közös stresstools.zip-ből, automatikus pontszám-kiolvasással,
a gép hardver-adatainak felismerésével és a felhő-ranglista le-/feltöltésével.

A pontszám-bevitel 2026-07-29-től AUTOMATIKUS (explicit felhasználói döntés, a korábbi kézi
beírást leváltva): a Cinebench parancssori módban fut (g_CinebenchCpuXTest=true), a pontszámot
a stdout-jából olvassuk ki ("CB <pont>" sor), majd magától kilép; a FurMark /nogui /benchmark
/log_score módban fut fix ideig, az FPS-t a score-fájljából olvassuk ki. A felhasználótól
EGYETLEN dolgot kérünk, a futás legvégén: a gép ranglista-nevét - beírás után az eredmény
magától feltöltődik. A Heaven (kézi) indítása megmaradt az egyenkénti indító kártyán, de a
ranglista GPU-oszlopa a FurMark FPS lett (a felhő-sorban 'furmark' mező; kompatibilitásból
a régi 'heaven' oszlopba is ugyanez kerül, így a meglévő Google-táblázat séma változatlanul
működik)."""

# === AUTO-IMPORTS ===
import os
import time
import subprocess
import threading
import logging
import tempfile
from datetime import datetime
from app import common
from app.benchmark_defs import BENCH_TOOLS
from app.benchmark_defs import CINEBENCH_TIMEOUT_S
from app.benchmark_defs import FURMARK_BENCH_TIME_MS
from app.benchmark_defs import FURMARK_BENCH_WIDTH
from app.benchmark_defs import FURMARK_BENCH_HEIGHT
from app.benchmark_defs import FURMARK_EXIT_GRACE_S
from app.benchmark_defs import FURMARK_MSAA
from app.benchmark_defs import CINEBENCH_SETTINGS_LABEL
from app.benchmark_defs import FURMARK_SETTINGS_LABEL
from app.benchmark_defs import CLOSE_APPS_PROTECTED
from app.benchmark_defs import CLOSE_APPS_WAIT_S
from app.benchmark_core import build_close_apps_ps
from app.benchmark_core import parse_close_apps_output
from app.benchmark_core import find_bench_tool_exes
from app.benchmark_core import gather_machine_specs
from app.benchmark_core import machine_row_id
from app.benchmark_core import build_cinebench_cmd
from app.benchmark_core import parse_cinebench_output
from app.benchmark_core import build_furmark_cmd
from app.benchmark_core import find_furmark_score_file
from app.benchmark_core import parse_furmark_scores
from app.benchmark_core import fetch_leaderboard as core_fetch_leaderboard
from app.benchmark_core import upload_result as core_upload_result
# === /AUTO-IMPORTS ===


class _BenchCancelled(Exception):
    """A felhasználó megszakította az automata benchmark-futtatást (cancel_benchmark_run)."""


class _BenchAbort(Exception):
    """Az automata futtatás egy lépése nem adott eredményt - a hibaüzenet a felhasználónak szól."""


class GuiBenchmarkMixin:
    """Benchmark nézet: automata benchmark-futtatás pontszám-kiolvasással, benchmark-programok
    egyenkénti portable indítása, hardver-felismerés, felhő-ranglista. A DriverToolApi része
    (összerakás: app/gui/api.py)."""

    def get_benchmark_settings(self):
        """A benchmark BEÁLLÍTÁSAI a nézet számára (szinkron hívás, a nézet betöltésekor).
        A felület ebből írja ki, hogy pontosan mivel fut a mérés - EGY forrásból
        (benchmark_defs.py), hogy a kijelzett és a ténylegesen futtatott beállítás soha ne
        csúszhasson szét. A 'furmark_cmd' a valódi parancssor, hogy a szerviz szemmel is
        ellenőrizhesse (a felületen tooltipként jelenik meg)."""
        return {
            'cinebench': CINEBENCH_SETTINGS_LABEL,
            'furmark': FURMARK_SETTINGS_LABEL,
            'furmark_short': f'{FURMARK_BENCH_WIDTH}×{FURMARK_BENCH_HEIGHT} · {FURMARK_MSAA}× MSAA',
            'furmark_seconds': FURMARK_BENCH_TIME_MS // 1000,
            'furmark_cmd': 'FurMark.exe ' + ' '.join(build_furmark_cmd('')[1:]),
        }

    def load_machine_specs(self):
        """A gép hardver-adatainak (CPU/alaplap/RAM/GPU) felismerése háttérszálon, majd
        a 'machine_specs' eseménnyel a nézetbe küldve. Az eredményt cache-eljük
        (self._bench_specs), hogy a feltöltés ne kérdezze le újra."""
        def worker():
            try:
                specs = gather_machine_specs(self._run)
                self._bench_specs = specs
                self.emit('machine_specs', specs)
            except Exception as e:
                logging.error(f"[BENCHMARK] Hardver-felismerés hiba: {e}")
                self.emit('machine_specs', {
                    'cpu': 'Ismeretlen', 'motherboard': 'Ismeretlen', 'ram': 'Ismeretlen',
                    'gpu': 'Ismeretlen', 'machine_id': '',
                    'machine_name': os.environ.get('COMPUTERNAME', 'PC')})
        threading.Thread(target=worker, daemon=True, name="bench-specs").start()

    def _ensure_bench_exe(self, name):
        """Biztosítja, hogy a megadott benchmark exe elérhető legyen: letölti/kicsomagolja a
        stresstools.zip-et (ha kell), és megkeresi benne az exe-t. Ha nincs meg (pl. régi,
        benchmark nélküli cache maradt a gépen), a markert törölve EGYSZER kényszerít friss
        letöltést. Visszaad: az exe teljes útvonala, vagy None (a hibát toastként jelzi)."""
        display_name = BENCH_TOOLS[name][0]
        is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
        temp_dir = r'C:\DV_Temp' if is_pe else tempfile.gettempdir()
        marker_path = os.path.join(temp_dir, "DriverVarázsló_Stress", ".extract_complete")
        if not os.path.exists(marker_path):
            self.emit('toast', {'message': f'⏳ {display_name}: első indítás, a programcsomag letöltése következik...', 'type': 'info'})

        try:
            stress_dir = self._download_stresstools(progress=self._stress_dl_progress_emitter(display_name))
        finally:
            self.emit('stress_dl_progress', {'active': False})
        if not stress_dir:
            self.emit('toast', {'message': f'❌ Hiba a programcsomag letöltésekor/kicsomagolásakor ({display_name})!', 'type': 'error'})
            return None

        exe_path = find_bench_tool_exes(stress_dir, [name])[name]
        if not exe_path or not os.path.exists(exe_path):
            logging.warning(f"[BENCHMARK] {display_name} nincs a kicsomagolt csomagban - friss letöltés kényszerítése...")
            try:
                if os.path.exists(marker_path):
                    os.remove(marker_path)
            except Exception as e:
                logging.debug(f"[BENCHMARK] marker törlése sikertelen: {e}")
            try:
                stress_dir = self._download_stresstools(progress=self._stress_dl_progress_emitter(display_name))
            finally:
                self.emit('stress_dl_progress', {'active': False})
            exe_path = find_bench_tool_exes(stress_dir, [name])[name] if stress_dir else None

        if not exe_path or not os.path.exists(exe_path):
            self.emit('toast', {'message': f'⚠️ {display_name} nem található a programcsomagban (stresstools.zip)! Ellenőrizd, hogy a ZIP tartalmazza-e.', 'type': 'warning'})
            return None
        return exe_path

    def launch_bench_tool(self, name):
        """Egy benchmark program (cinebench/heaven/furmark) EGYENKÉNTI, portable indítása a
        stresstools.zip-ből. SZÁNDÉKOSAN semmilyen automatizálás: a program csak elindul,
        a felhasználó maga állít be és futtat mindent (a lenti "egyenkénti indítás"
        kártyák hívják). Az automatizált, pontszám-kiolvasós futtatás a run_benchmark_suite."""
        logging.info(f"[API] launch_bench_tool({name})")
        info = BENCH_TOOLS.get(name)
        if not info:
            self.emit('toast', {'message': f'❌ Ismeretlen benchmark: {name}', 'type': 'error'})
            return
        display_name, _ = info

        def worker():
            try:
                exe_path = self._ensure_bench_exe(name)
                if not exe_path:
                    return
                pid = self._launch_stress_exe(exe_path, display_name)
                if pid:
                    if pid > 0:
                        self._stress_pids[name] = pid  # stop_stress_tests innen tudja, mit kell kilőni
                    self.emit('toast', {'message': f'✅ {display_name} elindítva!', 'type': 'success'})
                else:
                    self.emit('toast', {'message': f'❌ Hiba a(z) {display_name} indításakor!', 'type': 'error'})
            except Exception as e:
                logging.error(f"[BENCHMARK] launch_bench_tool hiba ({name}): {e}")
                self.emit('toast', {'message': f'❌ Hiba: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="bench-tool").start()

    # ------------------------------------------------------------------
    # Automata (1 kattintásos) benchmark-futtatás
    # ------------------------------------------------------------------
    def cancel_benchmark_run(self):
        """Az automata benchmark-futtatás megszakítása: a jelzőt beállítjuk, a futó
        benchmark-folyamatot a várakozó ciklus (_bench_wait_process) löki ki."""
        logging.warning("[BENCHMARK] A felhasználó megszakította az automata benchmark-futtatást.")
        self._bench_cancel = True
        self.emit('toast', {'message': '⏹ Benchmark megszakítása...', 'type': 'info'})

    def _close_running_apps_sync(self):
        """A futó felhasználói programok UDVARIAS bezárása a mérés előtt (a benchmark
        indításakor feltett kérdésre adott igenlő válasz után). WM_CLOSE-t küld, nem öl -
        a mentetlen munkájú programok rákérdeznek és nyitva maradnak, azokat jelentjük.
        Minden érintett folyamatot NÉV SZERINT logolunk (destruktív-jellegű lépés).
        Visszaad: (bezárt darabszám, nyitva maradt nevek listája)."""
        skip_ids = [os.getpid()]
        script = build_close_apps_ps(CLOSE_APPS_PROTECTED, skip_ids, CLOSE_APPS_WAIT_S)
        logging.warning(f"[BENCHMARK] Futó programok bezárása (WM_CLOSE, védett lista: "
                        f"{len(CLOSE_APPS_PROTECTED)} név, kihagyott PID-ek: {skip_ids})...")
        res = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                        timeout=CLOSE_APPS_WAIT_S + 60)
        if not res or res.returncode != 0:
            logging.warning(f"[BENCHMARK] A programbezáró szkript nem futott le rendben "
                            f"(returncode={getattr(res, 'returncode', None)}) - a mérés így is folytatódik.")
            return 0, []
        parsed = parse_close_apps_output(res.stdout or '')
        for name, title in parsed['attempted']:
            logging.warning(f"[BENCHMARK] Bezárás kérve: {name} - '{title}'")
        if parsed['closed']:
            logging.info(f"[BENCHMARK] Bezárt programok ({len(parsed['closed'])}): {parsed['closed']}")
        if parsed['stayed']:
            logging.warning(f"[BENCHMARK] NYITVA maradt (valószínűleg mentetlen munka miatt rákérdezett): "
                            f"{parsed['stayed']}")
        if not parsed['ok']:
            logging.warning("[BENCHMARK] A programbezáró szkript nem írt DONE sort - a lista hiányos lehet.")
        return len(parsed['closed']), parsed['stayed']

    def _bench_kill_pid(self, pid, label):
        """Egy benchmark-folyamat (és gyerekfolyamatai) kilövése taskkill-lel. A 128-as kód
        (nincs ilyen folyamat - pl. magától már kilépett) várt kimenet, nem hiba."""
        logging.warning(f"[BENCHMARK] {label} folyamat kilövése (pid={pid})...")
        self._run(['taskkill', '/F', '/T', '/PID', str(pid)], ok_codes=(0, 128))

    def _bench_wait_process(self, proc, timeout_s, label):
        """Egy elindított benchmark-folyamat megvárása megszakítás-figyeléssel: 1 mp-enként
        ellenőrzi a kilépést, a cancel-jelzőt és az időkorlátot (monotonic órával). SZÁNDÉKOSAN
        nem logol iterációnként (forró ciklus). Kimenet: 'exit' (magától kilépett) vagy
        'timeout' (az időkorlát után kilőttük); megszakításkor _BenchCancelled-et dob (a
        folyamatot előbb kilőve)."""
        start = time.monotonic()
        logging.info(f"[BENCHMARK] Várakozás a(z) {label} folyamatra (pid={proc.pid}, plafon={timeout_s:.0f} mp)...")
        while True:
            if proc.poll() is not None:
                logging.info(f"[BENCHMARK] {label} kilépett (returncode={proc.returncode}, "
                             f"{time.monotonic() - start:.1f} mp).")
                return 'exit'
            if self._bench_cancel:
                self._bench_kill_pid(proc.pid, label)
                raise _BenchCancelled()
            if time.monotonic() - start > timeout_s:
                logging.warning(f"[BENCHMARK] {label} nem lépett ki {timeout_s:.0f} mp alatt - kilövés.")
                self._bench_kill_pid(proc.pid, label)
                return 'timeout'
            time.sleep(1)

    def _run_cinebench_capture(self, exe_path):
        """A Cinebench multi-core teszt parancssori futtatása + a pontszám kiolvasása.
        A stdout fájlba megy (PIPE-nál a megtelő puffer beragaszthatná a folyamatot), a
        CLI-mód ablak nélkül fut és a teszt végén magától kilép. Visszaad: pontszám (float)
        vagy None."""
        cmd = build_cinebench_cmd(exe_path)
        out_path = os.path.join(tempfile.gettempdir(), 'dv_cinebench_out.txt')
        logging.info(f"[BENCHMARK] [CMD] Popen futtatása: {subprocess.list2cmdline(cmd)} (stdout -> {out_path})")
        with open(out_path, 'wb') as out_fh:
            proc = subprocess.Popen(cmd, stdout=out_fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=os.path.dirname(exe_path),
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self._stress_pids['cinebench'] = proc.pid  # stop_stress_tests biztonsági hálója
            try:
                self._bench_wait_process(proc, CINEBENCH_TIMEOUT_S, 'Cinebench')
            finally:
                self._stress_pids.pop('cinebench', None)
        try:
            with open(out_path, 'r', encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except OSError as e:
            logging.error(f"[BENCHMARK] Cinebench kimeneti fájl olvasási hiba: {e}")
            return None
        return parse_cinebench_output(text)

    def _run_furmark_capture(self, exe_path):
        """A FurMark parancssori benchmark futtatása fix ideig + az FPS kiolvasása a
        /log_score által írt score-fájlból. Egyes FurMark-verziók a benchmark után
        eredmény-ablakot hagynak fenn - a türelmi idő után a folyamatot kilőjük, a
        score-fájl ilyenkor már kint van. Visszaad: (FPS, ténylegesen renderelt felbontás)
        vagy (None, None), ha nincs értelmezhető eredmény."""
        exe_dir = os.path.dirname(exe_path)
        # time.time() itt SZÁNDÉKOS: fájl-mtime-mal (falióra-bélyeggel) hasonlítjuk össze,
        # nem időtartamot mérünk. 5 mp ráhagyás az óra/fájlrendszer felbontására.
        start_stamp = time.time() - 5
        cmd = build_furmark_cmd(exe_path)
        logging.info(f"[BENCHMARK] [CMD] Popen futtatása: {subprocess.list2cmdline(cmd)}")
        proc = subprocess.Popen(cmd, cwd=exe_dir, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._stress_pids['furmark'] = proc.pid  # stop_stress_tests biztonsági hálója
        try:
            outcome = self._bench_wait_process(
                proc, FURMARK_BENCH_TIME_MS / 1000 + FURMARK_EXIT_GRACE_S, 'FurMark')
        finally:
            self._stress_pids.pop('furmark', None)
        if outcome == 'timeout':
            logging.info("[BENCHMARK] FurMark kilőve a türelmi idő után (ismert viselkedés: "
                         "eredmény-ablak maradhat fenn) - a score-fájlt így is megnézzük.")
        score_file = find_furmark_score_file(exe_dir, min_mtime=start_stamp)
        if not score_file:
            return None, None
        try:
            with open(score_file, 'r', encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except OSError as e:
            logging.error(f"[BENCHMARK] FurMark score-fájl olvasási hiba ({score_file}): {e}")
            return None, None
        res = parse_furmark_scores(text)
        if not res:
            return None, None
        # A ténylegesen renderelt felbontás naplózása: /nogui-val a FurMark ABLAKOS módban
        # fut, és a Windows a munkaterülethez vághatja az ablakot - ha ez megtörténik, a
        # gép FPS-e nem hasonlítható össze a többiével, és ezt csak innen lehet észrevenni.
        expected = f"{FURMARK_BENCH_WIDTH}x{FURMARK_BENCH_HEIGHT}"
        actual = res.get('resolution')
        if actual and actual != expected:
            logging.warning(f"[BENCHMARK] A FurMark NEM a kért felbontáson futott "
                            f"(kért: {expected}, tényleges: {actual}, mód: {res.get('mode')}) - "
                            f"a Windows a munkaterülethez vágta az ablakot, az FPS emiatt nem "
                            f"teljesen összemérhető más gépekével.")
        # Kereszt-ellenőrzés: a FurMark a képkockaszámot adja vissza kilépési kódként is
        # (terepen mérve: returncode=618 mellett [FRAMES=618]) - ha a kettő egyezik, a
        # kiolvasott eredmény bizonyítottan a MOSTANI futásé.
        if res.get('score') is not None and proc.returncode == res['score']:
            logging.info(f"[BENCHMARK] Kereszt-ellenőrzés OK: a kilépési kód ({proc.returncode}) "
                         f"megegyezik a score-fájl képkockaszámával.")
        return res.get('fps'), (actual.replace('x', '×') if actual else None)

    def run_benchmark_suite(self, close_apps=False):
        """A "Benchmark futtatása" gomb: TELJESEN AUTOMATA, 1 kattintásos futtatás
        (explicit felhasználói kérés, 2026-07-29 - az AutoFix mintájára):
        0) ha `close_apps` (a nézet indításkor rákérdez): a futó felhasználói programok
           udvarias bezárása, hogy tiszta gépen mérjünk,
        1) programcsomag biztosítása (letöltés, ha kell) + hardver-felismerés,
        2) Cinebench multi-core teszt parancssorból, a pontszám kiolvasása a kimenetéből
           (a folyamat a teszt végén magától kilép - nem kell kézzel bezárni),
        3) FurMark GPU-benchmark fix ideig (/nogui /benchmark /log_score), az FPS
           kiolvasása a score-fájlból,
        4) az eredmény a 'benchmark_auto_result' eseménnyel a nézetbe kerül, ami EGYETLEN
           dolgot kér: a gép ranglista-nevét - beírás után upload_benchmark_result tölt fel.
        A folyamat állapotát a 'benchmark_progress' események viszik a nézet lépés-sávjára;
        a futás a cancel_benchmark_run-nal szakítható meg. Az egyenkénti indító kártyák
        (launch_bench_tool) ettől függetlenül automatizálás NÉLKÜL működnek."""
        logging.info(f"[API] run_benchmark_suite(close_apps={close_apps})")
        if getattr(self, '_bench_auto_running', False):
            self.emit('toast', {'message': '⏳ A benchmark már fut - várd meg a végét (vagy szakítsd meg)!', 'type': 'warning'})
            return
        self._bench_auto_running = True
        self._bench_cancel = False

        def progress(step, status, text=''):
            self.emit('benchmark_progress', {'step': step, 'status': status, 'text': text})

        def worker():
            power_locked = False
            t0 = time.monotonic()
            try:
                # --- 0/a) Futó programok bezárása (ha a felhasználó kérte) ---
                if close_apps:
                    progress('close', 'run', 'Futó programok bezárása...')
                    closed_n, stayed = self._close_running_apps_sync()
                    if stayed:
                        progress('close', 'done', f'{closed_n} bezárva · {len(stayed)} nyitva maradt '
                                                  f'({", ".join(stayed[:3])}{"…" if len(stayed) > 3 else ""})')
                        self.emit('toast', {'message': f'ℹ️ {len(stayed)} program nyitva maradt (valószínűleg mentetlen munka miatt rákérdezett): '
                                                       f'{", ".join(stayed[:4])}', 'type': 'info'})
                    else:
                        progress('close', 'done', f'{closed_n} program bezárva.')
                else:
                    progress('close', 'done', 'Kihagyva (a gép így is mérhető).')
                if self._bench_cancel:
                    raise _BenchCancelled()

                # --- 0/b) Előkészítés: programcsomag + hardver-adatok ---
                progress('prep', 'run', 'Programcsomag ellenőrzése (első alkalommal letöltés)...')
                cb_exe = self._ensure_bench_exe('cinebench')
                if not cb_exe:
                    raise _BenchAbort('A Cinebench nem található a programcsomagban.')
                fm_exe = self._ensure_bench_exe('furmark')
                if not fm_exe:
                    raise _BenchAbort('A FurMark nem található a programcsomagban.')
                if self._bench_cancel:
                    raise _BenchCancelled()
                specs = getattr(self, '_bench_specs', None)
                if not specs:
                    specs = gather_machine_specs(self._run)
                    self._bench_specs = specs
                    self.emit('machine_specs', specs)
                # Alvó mód/képernyő-kikapcsolás tiltása a futás idejére (a Cinebench alatt
                # percekig nincs felhasználói input - egy alvó gép a teszt közepén megállna).
                self._lock_power_for_stress()
                power_locked = True
                progress('prep', 'done', 'Programok készen, energiagazdálkodás zárolva.')

                # --- 1) Cinebench multi-thread ---
                progress('cinebench', 'run', f'{CINEBENCH_SETTINGS_LABEL} — fut '
                                             f'(több perc is lehet, a gép közben teljes terhelésen)...')
                cb_score = self._run_cinebench_capture(cb_exe)
                if self._bench_cancel:
                    raise _BenchCancelled()
                if cb_score is None:
                    raise _BenchAbort('A Cinebench nem adott ki pontszámot (részletek a debug logban).')
                progress('cinebench', 'done', f'{cb_score:g} pont · {CINEBENCH_SETTINGS_LABEL}')

                # --- 2) FurMark GPU ---
                progress('furmark', 'run', f'{FURMARK_SETTINGS_LABEL} — fut...')
                fm_fps, fm_res = self._run_furmark_capture(fm_exe)
                if self._bench_cancel:
                    raise _BenchCancelled()
                if fm_fps is None:
                    raise _BenchAbort('A FurMark nem írt ki FPS-eredményt (részletek a debug logban).')
                # A ténylegesen renderelt felbontást mutatjuk (nem a kértet): ha a Windows
                # levágta az ablakot, a szerviz LÁSSA, hogy nem a szabvány beállítás futott.
                shown_res = fm_res or f'{FURMARK_BENCH_WIDTH}×{FURMARK_BENCH_HEIGHT}'
                progress('furmark', 'done', f'{fm_fps} FPS · {shown_res} · {FURMARK_MSAA}× MSAA')

                # --- 3) Eredmény a nézetnek: már csak a gép nevét kell beírni ---
                elapsed = time.monotonic() - t0
                logging.info(f"[BENCHMARK] Automata futtatás kész ({elapsed:.0f} mp): "
                             f"Cinebench={cb_score}, FurMark={fm_fps} FPS")
                progress('result', 'run', 'Már csak a gép nevét kell beírni!')
                self.emit('benchmark_auto_result', {
                    'ok': True, 'cinebench': cb_score, 'furmark': fm_fps,
                    'cinebench_settings': CINEBENCH_SETTINGS_LABEL,
                    'furmark_settings': f'{shown_res} · {FURMARK_MSAA}× MSAA · '
                                        f'{FURMARK_BENCH_TIME_MS // 1000} mp · animált háttér',
                    'suggested_name': specs.get('machine_name', '')})
                self.emit('toast', {'message': '🏁 Benchmark kész! Írd be a gép nevét, és megy fel a ranglistára.', 'type': 'success'})
            except _BenchCancelled:
                logging.warning("[BENCHMARK] Automata futtatás MEGSZAKÍTVA a felhasználó által.")
                progress('result', 'error', 'A futtatás megszakítva.')
                self.emit('benchmark_auto_result', {'ok': False, 'cancelled': True})
                self.emit('toast', {'message': '⏹ Benchmark megszakítva.', 'type': 'info'})
            except _BenchAbort as e:
                logging.error(f"[BENCHMARK] Automata futtatás sikertelen: {e}")
                progress('result', 'error', str(e))
                self.emit('benchmark_auto_result', {'ok': False, 'error': str(e)})
                self.emit('toast', {'message': f'❌ {e}', 'type': 'error'})
            except Exception as e:
                logging.error(f"[BENCHMARK] run_benchmark_suite váratlan hiba: {e}", exc_info=True)
                progress('result', 'error', f'Váratlan hiba: {e}')
                self.emit('benchmark_auto_result', {'ok': False, 'error': str(e)})
                self.emit('toast', {'message': f'❌ Hiba a benchmark futtatásakor: {e}', 'type': 'error'})
            finally:
                if power_locked:
                    # A stressz-tesztek mentés/visszaállítás párját használjuk: ha közben
                    # NEM fut másik stressz-teszt, az eredeti energia-beállítások azonnal
                    # visszaállnak (a mentett kulcs törlődik, mint app-induláskor).
                    if not self._stress_pids:
                        self._restore_power_after_stress()
                    else:
                        logging.info("[BENCHMARK] Energia-visszaállítás kihagyva: más stressz-program fut "
                                     "(a stop_stress_tests / következő indulás állítja vissza).")
                self._bench_auto_running = False

        threading.Thread(target=worker, daemon=True, name="bench-suite").start()

    # ------------------------------------------------------------------
    # Felhő-ranglista
    # ------------------------------------------------------------------
    def fetch_leaderboard(self):
        """A felhő-ranglista lekérése háttérszálon, majd a 'leaderboard_data' eseménnyel
        a nézetbe küldve (a hálózati hívás lassú lehet, ezért nem szinkron visszatérés)."""
        def worker():
            data = core_fetch_leaderboard(self._run)
            self.emit('leaderboard_data', data)
        threading.Thread(target=worker, daemon=True, name="bench-lb").start()

    def _bench_existing_row(self, row_id, hw_id):
        """Megnézi - LEGJOBB SZÁNDÉK szerint, hibára némán -, hogy a most induló feltöltés
        MEGLÉVŐ sort frissít-e vagy ÚJat hoz létre, és ezt naplózza. Ez a mező, amit a
        felhő upsertel, tehát pontosan itt dőlt el a terepen bejött néma felülírás - a
        naplóban ezután látszik, melyik eset történt, milyen pontszámokat írunk át.
        Visszaad: a meglévő sor dict-je, vagy None. A feltöltést semmilyen hibája nem
        akadályozhatja meg (ezért fog el mindent)."""
        try:
            data = core_fetch_leaderboard(self._run)
            entries = data.get('entries') or []
            found = None
            for row in entries:
                if str(row.get('machine_id') or '') == row_id:
                    found = row
                    break
            if found is not None:
                logging.info(f"[BENCHMARK] A feltöltés MEGLÉVŐ sort ír át: "
                             f"név={found.get('machine_name')!r}, cinebench={found.get('cinebench')!r}, "
                             f"furmark={(found.get('furmark') or found.get('heaven'))!r}, "
                             f"ts={found.get('ts')!r} (azonosító={row_id!r})")
            else:
                logging.info(f"[BENCHMARK] A feltöltés ÚJ sort hoz létre (azonosító={row_id!r}); "
                             f"a ranglistán most {len(entries)} sor van, egyiket sem írjuk felül.")
            # Régi, CSAK GUID-alapú sor ugyanerről a gépről: a kulcs-formátum váltása előtt
            # feltöltött bejegyzés. Nem bántjuk (nem tudjuk, melyik névhez tartozott), de
            # naplózzuk, mert a táblázatban ez marad ott duplikátumnak.
            if hw_id:
                for row in entries:
                    if str(row.get('machine_id') or '') == str(hw_id):
                        logging.warning(f"[BENCHMARK] A ranglistán van egy RÉGI, csak gép-azonosítós sor "
                                        f"ugyanerről a gépről (név={row.get('machine_name')!r}, "
                                        f"ts={row.get('ts')!r}) - ezt már nem írjuk át, a táblázatban "
                                        "kézzel törölhető, ha nem kell.")
                        break
            return found
        except Exception as e:
            logging.warning(f"[BENCHMARK] A meglévő ranglista-sor ellenőrzése nem sikerült ({e}) - "
                            "a feltöltés ettől függetlenül megy tovább.")
            return None

    def upload_benchmark_result(self, cinebench_score, furmark_fps, name=None):
        """A gép benchmark-eredményének feltöltése a felhő-ranglistára: a (cache-elt vagy
        frissen felismert) hardver-adatokhoz csatolja az automata futtatás pontszámait,
        POST-tal feltölti (upsert a machine_id-re), majd frissíti a ranglistát a nézetben.
        A `name` a felhasználó által megadott gépnév (a ranglistán ez jelenik meg); ha üres,
        a felismert 'proci / RAM / videokártya' összetett név a tartalék. A GPU-eredmény a
        'furmark' mezőbe kerül, és KOMPATIBILITÁSBÓL a régi 'heaven' oszlopba is ugyanez
        íródik - így a meglévő felhő-táblázat sémáját nem kell átalakítani."""
        def worker():
            try:
                specs = getattr(self, '_bench_specs', None) or gather_machine_specs(self._run)
                self._bench_specs = specs
                display_name = (name or '').strip() or specs.get('machine_name', 'PC')
                # A felhő a machine_id-re upsertel, ezért a KULCS a gép + a beírt név együtt
                # (machine_row_id): más névvel ugyanarról a gépről ÚJ sor lesz, nem felülírás
                # (2026-07-30, terepen bejött adatvesztés). A puszta hardver-azonosítót külön
                # mezőben (hw_id) is elküldjük - ha a táblázatban nincs ilyen oszlop, a
                # felhő-oldal egyszerűen eldobja (mint a 'furmark' mezőt is).
                hw_id = specs.get('machine_id', '')
                row_id = machine_row_id(hw_id, display_name)
                existing = self._bench_existing_row(row_id, hw_id)
                entry = {
                    'machine_id': row_id,
                    'hw_id': hw_id,
                    'machine_name': display_name,
                    'cpu': specs.get('cpu', ''),
                    'motherboard': specs.get('motherboard', ''),
                    'ram': specs.get('ram', ''),
                    'gpu': specs.get('gpu', ''),
                    'cinebench': cinebench_score if cinebench_score is not None else '',
                    'furmark': furmark_fps if furmark_fps is not None else '',
                    'heaven': furmark_fps if furmark_fps is not None else '',
                    'ts': datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'build': common.BUILD_NUMBER,
                }
                logging.info(f"[BENCHMARK] Feltöltés a ranglistára: name={display_name!r}, "
                             f"cinebench={cinebench_score}, furmark={furmark_fps}, "
                             f"azonosító={row_id!r} ({'meglévő sor frissítése' if existing else 'új sor'})")
                core_upload_result(self._run, entry)
                # A visszajelzés megmondja, FRISSÍTÉS volt-e vagy új sor: így a szerviz azonnal
                # látja, ha egy korábbi mérését írta át (ugyanazzal a névtel), nem utólag.
                if existing:
                    msg = (f"🔄 A(z) „{display_name}” nevű meglévő eredmény FRISSÍTVE a ranglistán "
                           "(ugyanaz a gép + ugyanaz a név).")
                else:
                    msg = '🏆 Eredmény sikeresen feltöltve a ranglistára (új sor)!'
                self.emit('toast', {'message': msg, 'type': 'success'})
                # Siker: a nézet bezárja a futtató panelt + visszaállítja a gombot.
                self.emit('benchmark_upload_result', {'ok': True})
                # A frissített ranglista automatikus visszaküldése a nézetbe. KÜLÖN try:
                # a feltöltés ekkor MÁR SIKERÜLT, egy elbukó újraolvasás nem jelenthető
                # "Feltöltési hiba"-ként (a szerviz újratöltene egy már feltöltött mérést).
                try:
                    self.emit('leaderboard_data', core_fetch_leaderboard(self._run))
                except Exception as e:
                    logging.warning(f"[BENCHMARK] A feltöltés SIKERÜLT, de a ranglista újraolvasása "
                                    f"nem ({e}) - a nézet a 🔄 gombbal frissíthető.")
            except Exception as e:
                logging.error(f"[BENCHMARK] Feltöltés hiba: {e}")
                self.emit('toast', {'message': f'❌ Feltöltési hiba: {e}', 'type': 'error'})
                # Hiba: a gomb visszaáll, az eredmény-kártya NYITVA marad (a pontok megmaradnak).
                self.emit('benchmark_upload_result', {'ok': False})
        threading.Thread(target=worker, daemon=True, name="bench-upload").start()
