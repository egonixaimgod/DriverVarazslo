"""DriverVarázsló GUI - Szervíz Programok telepítése: a stresstools.zip-ből kiválasztott
programok mappájának bemásolása a Program Files (x86) alá + asztali parancsikon készítése.
A telepítések nyilvántartása a <app_data>/installed_tools.json fájlban él - erről tudja a
UI a "Telepítve" jelvényt, és erről tudja a start_stress_tool, hogy a telepített példányt
indítsa a temp-beli portable helyett."""

# === AUTO-IMPORTS ===
import os
import json
import shutil
import logging
import time
from app.common import _app_data_dir, _ps_quote
from app.stress_defs import STRESS_TOOLS
# === /AUTO-IMPORTS ===


class GuiToolsInstallMixin:
    """Szervíz Programok telepítése (Program Files + asztali parancsikon). A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _tools_install_root(self):
        """A telepítési gyökér: Program Files (x86) - kifejezett felhasználói kérés.
        64 bites Windowson mindig létezik; ha mégsem (elméleti eset), a sima Program
        Files-ra esünk vissza."""
        return (os.environ.get('ProgramFiles(x86)')
                or os.environ.get('ProgramFiles')
                or r'C:\Program Files (x86)')

    def _tool_install_record_path(self):
        return os.path.join(_app_data_dir(), 'installed_tools.json')

    def _load_tool_install_records(self):
        try:
            path = self._tool_install_record_path()
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logging.warning(f"[TOOLSINSTALL] installed_tools.json olvasási hiba (üresként kezelve): {e}")
        return {}

    def _save_tool_install_records(self, records):
        try:
            with open(self._tool_install_record_path(), 'w', encoding='utf-8') as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning(f"[TOOLSINSTALL] installed_tools.json írási hiba: {e}")

    def _find_installed_tool_exe(self, key):
        """A korábban telepített program exe-je, ha a nyilvántartás szerint telepítve van
        ÉS az exe ténylegesen létezik is még (a felhasználó kézzel törölhette a mappát -
        ilyenkor None, és a hívó visszaesik a portable útra)."""
        rec = self._load_tool_install_records().get(key)
        if rec and rec.get('exe') and os.path.exists(rec['exe']):
            return rec['exe']
        return None

    def get_service_tools_status(self):
        """Szinkron UI-hívás: {kulcs: {'installed': bool, 'path': mappa}} minden
        STRESS_TOOLS-beli programra - a program-kártyák "Telepítve" jelvényéhez."""
        out = {}
        records = self._load_tool_install_records()
        for key in STRESS_TOOLS:
            rec = records.get(key)
            installed = bool(rec and rec.get('exe') and os.path.exists(rec['exe']))
            out[key] = {'installed': installed, 'path': (rec or {}).get('dir')}
        return out

    def _tool_source_root(self, stress_dir, exe_path):
        """A programhoz tartozó, TELJES egészében átmásolandó forrásmappa a kicsomagolt
        csomagon belül. A ZIP-ben minden program a saját legfelső szintű mappájában él
        (CPU-Z, FurMark, ...), de a ZIP egy 'stresstools' gyökérmappát is tartalmazhat
        (a jelenlegi kiadás így épül) - azt átugorjuk. Ha az exe váratlanul közvetlenül
        a gyökérben lenne, None-t adunk vissza (a hívó ilyenkor csak magát az exe-t
        másolja egy saját nevű mappába, nehogy a TELJES csomagot telepítse)."""
        rel_parts = os.path.relpath(exe_path, stress_dir).split(os.sep)
        dir_parts = rel_parts[:-1]
        base = stress_dir
        if dir_parts and dir_parts[0].lower() == 'stresstools':
            base = os.path.join(base, dir_parts[0])
            dir_parts = dir_parts[1:]
        if not dir_parts:
            return None
        return os.path.join(base, dir_parts[0])

    def _create_desktop_shortcut(self, lnk_name, target_exe):
        """Parancsikon a KÖZÖS (Public) Asztalra - admin jogon futunk, és így minden
        felhasználói fióknál megjelenik. A .lnk-t a WScript.Shell COM objektummal
        készítjük PowerShellből (Pythonból nincs beépített .lnk-írás)."""
        desktop = os.path.join(os.environ.get('PUBLIC', r'C:\Users\Public'), 'Desktop')
        os.makedirs(desktop, exist_ok=True)
        lnk_path = os.path.join(desktop, f"{lnk_name}.lnk")
        ps_cmd = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$lnk = $ws.CreateShortcut('{_ps_quote(lnk_path)}'); "
            f"$lnk.TargetPath = '{_ps_quote(target_exe)}'; "
            f"$lnk.WorkingDirectory = '{_ps_quote(os.path.dirname(target_exe))}'; "
            f"$lnk.IconLocation = '{_ps_quote(target_exe)},0'; "
            "$lnk.Save()"
        )
        res = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd])
        if res is None or res.returncode != 0 or not os.path.exists(lnk_path):
            raise Exception("a parancsikon létrehozása nem sikerült")
        return lnk_path

    def install_service_tools(self, keys):
        """A kiválasztott programok telepítése: stresstools.zip letöltése (ha még nincs
        cache-elve), a program mappájának másolása a Program Files (x86) alá, asztali
        parancsikon, nyilvántartás-frissítés. Újratelepítésnél (már létező célmappa) a
        fájlokat felülírjuk, de idegen fájlt nem törlünk (copytree dirs_exist_ok)."""
        logging.info(f"[API] install_service_tools({keys})")
        keys = [k for k in (keys or []) if k in STRESS_TOOLS]
        if not keys:
            self.emit('toast', {'message': '⚠️ Nincs kiválasztott program a telepítéshez!', 'type': 'warning'})
            return

        def worker():
            try:
                self.emit('task_start', {'task': 'toolsinstall', 'title': 'Szervíz Programok Telepítése'})
                self.emit('task_progress', {'task': 'toolsinstall', 'log': '🌐 Programcsomag előkészítése (első alkalommal letöltés)...', 'indeterminate': True})

                dl_state = {'last': 0.0}

                def dl_progress(phase, done, total):
                    now = time.time()
                    if now - dl_state['last'] < 0.4 and not (total and done >= total):
                        return
                    dl_state['last'] = now
                    if phase == 'download':
                        if total:
                            self.emit('task_progress', {'task': 'toolsinstall', 'current': done, 'total': total,
                                                        'status': f'🌐 Letöltés: {done / 1048576:.0f} / {total / 1048576:.0f} MB'})
                        else:
                            self.emit('task_progress', {'task': 'toolsinstall', 'indeterminate': True,
                                                        'status': f'🌐 Letöltés: {done / 1048576:.0f} MB'})
                    else:
                        self.emit('task_progress', {'task': 'toolsinstall', 'current': done, 'total': total,
                                                    'status': f'📦 Kicsomagolás: {done}/{total} fájl'})

                stress_dir = self._download_stresstools(progress=dl_progress)
                if not stress_dir:
                    raise Exception("Hiba a programcsomag letöltésekor vagy kicsomagolásakor (Helytelen ZIP / Nincs net).")

                found = self._find_stress_tool_exes(stress_dir, keys)
                install_root = self._tools_install_root()
                records = self._load_tool_install_records()
                ok_count = 0
                fail_count = 0

                for key in keys:
                    display_name, _ = STRESS_TOOLS[key]
                    try:
                        src_exe = found.get(key)
                        if not src_exe or not os.path.exists(src_exe):
                            self.emit('task_progress', {'task': 'toolsinstall', 'log': f'⚠️ {display_name}: nem található a csomagban, kihagyva.'})
                            fail_count += 1
                            continue

                        self.emit('task_progress', {'task': 'toolsinstall', 'log': f'📂 {display_name}: másolás a Program Files (x86) alá...'})
                        src_root = self._tool_source_root(stress_dir, src_exe)
                        if src_root:
                            dest_dir = os.path.join(install_root, os.path.basename(src_root))
                            shutil.copytree(src_root, dest_dir, dirs_exist_ok=True)
                            rel_exe = os.path.relpath(src_exe, src_root)
                            dest_exe = os.path.join(dest_dir, rel_exe)
                        else:
                            # Az exe közvetlenül a csomag gyökerében volt (nem várt eset) -
                            # csak magát a fájlt másoljuk egy saját nevű mappába.
                            dest_dir = os.path.join(install_root, display_name)
                            os.makedirs(dest_dir, exist_ok=True)
                            dest_exe = os.path.join(dest_dir, os.path.basename(src_exe))
                            shutil.copy2(src_exe, dest_exe)

                        if not os.path.exists(dest_exe):
                            raise Exception(f"a másolás után nem található az exe: {dest_exe}")

                        if key == 'hwinfo':
                            # Az automatizált stress-teszt futás SensorsOnly INI-je ne
                            # kerüljön be a telepített példányba - telepítve a felhasználó
                            # maga választ módot induláskor.
                            try:
                                ini_path = os.path.join(os.path.dirname(dest_exe), "HWiNFO64.INI")
                                if os.path.exists(ini_path):
                                    with open(ini_path, 'r') as f:
                                        content = f.read()
                                    if content == "[Settings]\nSensorsOnly=1\nCheckForUpdate=0\n":
                                        os.remove(ini_path)
                            except Exception as e:
                                logging.debug(f"[TOOLSINSTALL] HWiNFO64.INI takarítása kihagyva: {e}")

                        self.emit('task_progress', {'task': 'toolsinstall', 'log': f'🔗 {display_name}: asztali parancsikon készítése...'})
                        lnk_path = self._create_desktop_shortcut(display_name, dest_exe)

                        records[key] = {'dir': dest_dir, 'exe': dest_exe, 'lnk': lnk_path}
                        self._save_tool_install_records(records)
                        ok_count += 1
                        self.emit('task_progress', {'task': 'toolsinstall', 'log': f'✅ {display_name} telepítve: {dest_dir}'})
                    except Exception as tool_err:
                        fail_count += 1
                        logging.error(f"[TOOLSINSTALL] {display_name} telepítési hiba: {tool_err}")
                        self.emit('task_progress', {'task': 'toolsinstall', 'log': f'❌ {display_name}: telepítési hiba - {tool_err}'})

                if fail_count == 0:
                    self.emit('task_complete', {'task': 'toolsinstall', 'status': f'✅ {ok_count} program telepítve, a parancsikonok kint vannak az Asztalon.'})
                elif ok_count > 0:
                    self.emit('task_complete', {'task': 'toolsinstall', 'status': f'⚠️ {ok_count} program telepítve, {fail_count} sikertelen (részletek a fenti naplóban).'})
                else:
                    self.emit('task_complete', {'task': 'toolsinstall', 'status': '❌ Egyetlen program telepítése sem sikerült.'})
            except Exception as e:
                logging.error(f"[TOOLSINSTALL] install_service_tools hiba: {e}")
                self.emit('task_error', {'task': 'toolsinstall', 'error': f'Hiba: {str(e)}'})

        self._safe_thread('toolsinstall', worker)
