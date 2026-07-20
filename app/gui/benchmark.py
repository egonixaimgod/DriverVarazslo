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

    def launch_bench_tool(self, name):
        """Egy benchmark program (cinebench/heaven) portable indítása a stresstools.zip-ből.
        Az egyenkénti stressztool-indításhoz hasonlóan: csendben indul (toast + esetleg
        letöltés-sáv), automatizálás nélkül - a felhasználó maga futtatja le a benchmarkot
        és jegyzi fel a pontszámot. Ha a kicsomagolt csomagban nincs meg a benchmark exe
        (pl. egy régi, benchmark nélküli cache maradt a gépen), egyszer kényszerítünk friss
        letöltést a csomag-marker törlésével."""
        logging.info(f"[API] launch_bench_tool({name})")
        info = BENCH_TOOLS.get(name)
        if not info:
            self.emit('toast', {'message': f'❌ Ismeretlen benchmark: {name}', 'type': 'error'})
            return
        display_name, _ = info

        def worker():
            try:
                is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
                temp_dir = r'C:\DV_Temp' if is_pe else tempfile.gettempdir()
                stress_root = os.path.join(temp_dir, "DriverVarázsló_Stress")
                marker_path = os.path.join(stress_root, ".extract_complete")
                if not os.path.exists(marker_path):
                    self.emit('toast', {'message': f'⏳ {display_name}: első indítás, a programcsomag letöltése következik...', 'type': 'info'})

                try:
                    stress_dir = self._download_stresstools(progress=self._stress_dl_progress_emitter(display_name))
                finally:
                    self.emit('stress_dl_progress', {'active': False})
                if not stress_dir:
                    self.emit('toast', {'message': f'❌ Hiba a programcsomag letöltésekor/kicsomagolásakor ({display_name})!', 'type': 'error'})
                    return

                exe_path = find_bench_tool_exes(stress_dir, [name])[name]
                if not exe_path or not os.path.exists(exe_path):
                    # Régi (benchmark nélküli) cache gyanú: a markert törölve egyszer
                    # kényszerítünk friss letöltést, hátha az új ZIP már tartalmazza.
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
                    return

                pid = self._launch_stress_exe(exe_path, display_name)
                if pid:
                    if pid > 0:
                        self._stress_pids[name] = pid  # stop_stress_tests innen tudja, mit kell kilőni
                    self.emit('toast', {'message': f'✅ {display_name} elindítva! Futtasd le, majd írd be a pontszámot.', 'type': 'success'})
                else:
                    self.emit('toast', {'message': f'❌ Hiba a(z) {display_name} indításakor!', 'type': 'error'})
            except Exception as e:
                logging.error(f"[BENCHMARK] launch_bench_tool hiba ({name}): {e}")
                self.emit('toast', {'message': f'❌ Hiba: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="bench-tool").start()

    def fetch_leaderboard(self):
        """A felhő-ranglista lekérése háttérszálon, majd a 'leaderboard_data' eseménnyel
        a nézetbe küldve (a hálózati hívás lassú lehet, ezért nem szinkron visszatérés)."""
        def worker():
            data = core_fetch_leaderboard(self._run)
            self.emit('leaderboard_data', data)
        threading.Thread(target=worker, daemon=True, name="bench-lb").start()

    def upload_benchmark_result(self, cinebench_score, heaven_score, note=None):
        """A gép benchmark-eredményének feltöltése a felhő-ranglistára: a (cache-elt vagy
        frissen felismert) hardver-adatokhoz csatolja a felhasználó által beírt pontszámokat,
        POST-tal feltölti (upsert a machine_id-re), majd frissíti a ranglistát a nézetben."""
        def worker():
            try:
                specs = getattr(self, '_bench_specs', None) or gather_machine_specs(self._run)
                self._bench_specs = specs
                entry = {
                    'machine_id': specs.get('machine_id', ''),
                    'machine_name': specs.get('machine_name', 'PC'),
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
