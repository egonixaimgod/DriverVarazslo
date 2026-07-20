"""DriverVarázsló GUI - Benchmark: a két benchmark program (Cinebench R20, Unigine Heaven)
portable indítása a közös stresstools.zip-ből, a gép hardver-adatainak felismerése, és a
felhő-ranglista le-/feltöltése.

A pontszám bevitele SZÁNDÉKOSAN kézi (a felhasználó a lefuttatott benchmark eredmény-
képernyőjéről írja be a felületre): a Cinebench/Heaven pontszámának automatikus, megbízható
kiolvasása a programok GUI-jából/renderelt overlay-éből törékeny lenne - a hardver-adatok
viszont automatikusan kitöltődnek, és a feltöltés egy gombbal megy. (Későbbi bővítés lehet
a Cinebench parancssori pontszám-kiolvasása, ha a terep megköveteli.)"""

# === AUTO-IMPORTS ===
import os
import subprocess
import threading
import logging
import tempfile
from datetime import datetime
from app import common
from app.benchmark_defs import BENCH_TOOLS
from app.benchmark_core import find_bench_tool_exes
from app.benchmark_core import gather_machine_specs
from app.benchmark_core import fetch_leaderboard as core_fetch_leaderboard
from app.benchmark_core import upload_result as core_upload_result
# === /AUTO-IMPORTS ===


class GuiBenchmarkMixin:
    """Benchmark nézet: benchmark-programok portable indítása, hardver-felismerés,
    felhő-ranglista. A DriverToolApi része (összerakás: app/gui/api.py)."""

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
        """Egy benchmark program (cinebench/heaven) EGYENKÉNTI, portable indítása a
        stresstools.zip-ből. SZÁNDÉKOSAN semmilyen automatizálás: a program csak elindul,
        a felhasználó maga állít be és futtat mindent (a lenti "egyenkénti indítás"
        kártyák hívják). Az automatizált, szekvenciális futtatás a run_benchmark_suite."""
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

    def run_benchmark_suite(self):
        """A "Benchmark futtatása" gomb AUTOMATIZÁLT, szekvenciális futtatása:
        1) elindítja a Cinebench R20-at a több-magos (multi-core) CPU-teszttel automatikusan
           (parancssori kapcsoló: g_CinebenchCpuXTest=true),
        2) MEGVÁRJA, amíg a felhasználó KÉZZEL bezárja a Cinebench-et (a folyamat kilépését),
        3) majd MAGÁTÓL elindítja a Unigine Heaven-t.
        A pontszámokat a felhasználó a lefuttatott tesztek eredmény-képernyőjéről írja be
        (a pontszám megbízható automatikus kiolvasása a program tesztelése nélkül kockázatos
        lenne). Az egyenkénti indító kártyák (launch_bench_tool) ettől függetlenül
        automatizálás NÉLKÜL, csak elindítják a programot."""
        logging.info("[API] run_benchmark_suite()")

        def worker():
            try:
                # 1) Cinebench multi-core teszttel
                cb_exe = self._ensure_bench_exe('cinebench')
                if not cb_exe:
                    return
                try:
                    proc = subprocess.Popen([cb_exe, 'g_CinebenchCpuXTest=true'],
                                            creationflags=subprocess.CREATE_NEW_CONSOLE,
                                            cwd=os.path.dirname(cb_exe))
                    self._stress_pids['cinebench'] = proc.pid
                    logging.info(f"[BENCHMARK] Cinebench (multi-core) elindítva, pid={proc.pid}")
                except Exception as e:
                    logging.error(f"[BENCHMARK] Cinebench indítási hiba: {e}")
                    self.emit('toast', {'message': f'❌ Hiba a Cinebench indításakor: {e}', 'type': 'error'})
                    return
                self.emit('toast', {'message': '🧠 Cinebench elindult (multi-core teszt fut). Ha kész, ZÁRD BE — utána magától indul a Heaven.', 'type': 'info'})

                # 2) Megvárjuk, amíg a felhasználó bezárja a Cinebench-et
                try:
                    proc.wait()
                except Exception as e:
                    logging.debug(f"[BENCHMARK] Cinebench proc.wait hiba: {e}")
                self._stress_pids.pop('cinebench', None)
                logging.info("[BENCHMARK] Cinebench bezárva - Heaven indul.")

                # 3) Heaven automatikus indítása
                hv_exe = self._ensure_bench_exe('heaven')
                if not hv_exe:
                    return
                pid = self._launch_stress_exe(hv_exe, 'Unigine Heaven')
                if pid:
                    if pid > 0:
                        self._stress_pids['heaven'] = pid
                    self.emit('toast', {'message': '🎮 Cinebench kész — Heaven elindult! Futtasd le (1080p), majd írd be a két pontszámot és töltsd fel.', 'type': 'success'})
                else:
                    self.emit('toast', {'message': '❌ Hiba a Heaven indításakor!', 'type': 'error'})
            except Exception as e:
                logging.error(f"[BENCHMARK] run_benchmark_suite hiba: {e}")
                self.emit('toast', {'message': f'❌ Hiba a benchmark futtatásakor: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="bench-suite").start()

    def fetch_leaderboard(self):
        """A felhő-ranglista lekérése háttérszálon, majd a 'leaderboard_data' eseménnyel
        a nézetbe küldve (a hálózati hívás lassú lehet, ezért nem szinkron visszatérés)."""
        def worker():
            data = core_fetch_leaderboard(self._run)
            self.emit('leaderboard_data', data)
        threading.Thread(target=worker, daemon=True, name="bench-lb").start()

    def upload_benchmark_result(self, cinebench_score, heaven_score, name=None):
        """A gép benchmark-eredményének feltöltése a felhő-ranglistára: a (cache-elt vagy
        frissen felismert) hardver-adatokhoz csatolja a felhasználó által beírt pontszámokat,
        POST-tal feltölti (upsert a machine_id-re), majd frissíti a ranglistát a nézetben.
        A `name` a felhasználó által megadott gépnév (a ranglistán ez jelenik meg); ha üres,
        a felismert 'proci / RAM / videokártya' összetett név a tartalék."""
        def worker():
            try:
                specs = getattr(self, '_bench_specs', None) or gather_machine_specs(self._run)
                self._bench_specs = specs
                display_name = (name or '').strip() or specs.get('machine_name', 'PC')
                entry = {
                    'machine_id': specs.get('machine_id', ''),
                    'machine_name': display_name,
                    'cpu': specs.get('cpu', ''),
                    'motherboard': specs.get('motherboard', ''),
                    'ram': specs.get('ram', ''),
                    'gpu': specs.get('gpu', ''),
                    'cinebench': cinebench_score if cinebench_score is not None else '',
                    'heaven': heaven_score if heaven_score is not None else '',
                    'ts': datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'build': common.BUILD_NUMBER,
                }
                core_upload_result(self._run, entry)
                self.emit('toast', {'message': '🏆 Eredmény sikeresen feltöltve a ranglistára!', 'type': 'success'})
                # Siker: a nézet bezárja a futtató panelt + üríti a mezőket + visszaállítja a gombot.
                self.emit('benchmark_upload_result', {'ok': True})
                # A frissített ranglista automatikus visszaküldése a nézetbe.
                self.emit('leaderboard_data', core_fetch_leaderboard(self._run))
            except Exception as e:
                logging.error(f"[BENCHMARK] Feltöltés hiba: {e}")
                self.emit('toast', {'message': f'❌ Feltöltési hiba: {e}', 'type': 'error'})
                # Hiba: a gomb visszaáll, a panel NYITVA marad (a beírt pontok megmaradnak).
                self.emit('benchmark_upload_result', {'ok': False})
        threading.Thread(target=worker, daemon=True, name="bench-upload").start()
