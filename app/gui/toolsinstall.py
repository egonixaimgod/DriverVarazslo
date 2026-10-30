"""DriverVarázsló GUI - Szervíz Programok telepítése: a programonkénti csomagokból
(app/toollib_core.py: GitHub vagy a bolt Drive-ja) kiválasztott programok mappájának bemásolása a Program Files (x86) alá + asztali parancsikon készítése.
A telepítések nyilvántartása a <app_data>/installed_tools.json fájlban él - erről tudja a
UI a "Telepítve" jelvényt, és erről tudja a start_stress_tool, hogy a telepített példányt
indítsa a temp-beli portable helyett."""

# === AUTO-IMPORTS ===
import os
import json
import shutil
import logging
import time
import fnmatch
from app.common import _app_data_dir, _ps_quote
from app.stress_defs import STRESS_TOOLS
from app import toollib_core
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

    def _tools_scan_roots(self):
        """A Program Files (x86) ÉS a sima Program Files: egy kézzel vagy a gyártó
        telepítőjével feltett program bármelyikben lehet."""
        roots = []
        for r in (self._tools_install_root(), os.environ.get('ProgramFiles')):
            if r and os.path.isdir(r) and os.path.normcase(r) not in [os.path.normcase(x) for x in roots]:
                roots.append(r)
        return roots

    def _scan_installed_tools(self, keys):
        """{kulcs: (exe, mappa)} a nyilvántartás ÉS a lemez alapján. A nyilvántartás
        hiányozhat (törölt C:\\DriverVarazslo, kézi másolás), ezért a Program Files
        gyökerek alatt a mappában lévő exe is számít: <gyökér>\\X\\exe vagy
        <gyökér>\\X\\Y\\exe (mélyebbre nem megyünk, a teljes Program Files bejárása
        lassú). A felismert mappa a nyilvántartásnál a telepítéskori célmappa, egyébként
        az exe saját mappája (így pl. CPUID\\CPU-Z törlése nem viszi a CPUID többi
        programját)."""
        records = self._load_tool_install_records()
        out = {}
        for key in keys:
            rec = records.get(key)
            if rec and rec.get('exe') and os.path.exists(rec['exe']):
                out[key] = (rec['exe'], rec.get('dir') or os.path.dirname(rec['exe']))
        missing = [k for k in keys if k not in out]
        if not missing:
            return out
        best = {}  # kulcs -> (minta-prioritás, exe)
        for root in self._tools_scan_roots():
            try:
                level1 = [e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
            except OSError as e:
                logging.warning(f"[TOOLSINSTALL] {root} nem olvasható: {e}")
                continue
            for d1 in level1:
                try:
                    entries = list(os.scandir(d1))
                except OSError:
                    continue  # pl. WindowsApps: hozzáférés megtagadva - nem a mi programunk
                dirs = [d1] + [e.path for e in entries if e.is_dir(follow_symlinks=False)]
                for d in dirs:
                    try:
                        files = entries if d == d1 else list(os.scandir(d))
                    except OSError:
                        continue
                    for f in files:
                        fl = f.name.lower()
                        if not fl.endswith('.exe'):
                            continue
                        for key in missing:
                            for idx, pattern in enumerate(STRESS_TOOLS[key][1]):
                                if fnmatch.fnmatchcase(fl, pattern) and (key not in best or idx < best[key][0]):
                                    best[key] = (idx, f.path)
        for key, (_, exe) in best.items():
            out[key] = (exe, os.path.dirname(exe))
            logging.info(f"[TOOLSINSTALL] {STRESS_TOOLS[key][0]}: nyilvántartás nélkül, a lemezen felismerve: {exe}")
        return out

    def _find_installed_tool_exe(self, key):
        """A telepített program exe-je (nyilvántartás vagy a Program Files alatti mappa),
        különben None, és a hívó visszaesik a portable útra."""
        hit = self._scan_installed_tools([key]).get(key)
        return hit[0] if hit else None

    def get_service_tools_status(self):
        """Szinkron UI-hívás: {kulcs: {'installed': bool, 'path': mappa}} minden
        STRESS_TOOLS-beli programra - a program-kártyák "Telepítve" jelvényéhez."""
        found = self._scan_installed_tools(list(STRESS_TOOLS))
        return {key: {'installed': key in found, 'path': found[key][1] if key in found else None}
                for key in STRESS_TOOLS}

    def toollib_password_needed(self, keys, launch=False):
        """Szinkron UI-hívás: a kijelölt programok közül melyekhez kell MOST jelszó (a
        megjelenített nevük). Csak akkor kérjük (explicit user decision, 2026-10-30), ha
        van köztük Drive-os (licences) program, ami még nincs letöltve, és nincs
        megjegyzett jelszó. `launch=True` (egyenkénti indítás): a telepített program nem
        töltődik le, ahhoz sem kell."""
        if toollib_core.load_password():
            return []
        root = toollib_core.cache_root()
        keys = [k for k in (keys or []) if k in STRESS_TOOLS]
        installed = self._scan_installed_tools(keys) if launch else {}
        out = []
        for k in keys:
            pkg = toollib_core.package_for(k)
            if (pkg and toollib_core.package_source(pkg) == toollib_core.SOURCE_DRIVE
                    and not toollib_core.package_ready(root, pkg) and k not in installed):
                out.append(STRESS_TOOLS[k][0])
        logging.info(f"[TOOLLIB] Jelszó kell ezekhez: {out or 'egyikhez sem'}")
        return out

    def get_toollib_password_saved(self):
        """Szinkron UI-hívás: van-e megjegyzett Drive-jelszó ezen a gépen (a Windows
        Hitelesítőadat-kezelőjében; app/toollib_core.py)."""
        return bool(toollib_core.load_password())

    def forget_toollib_password(self):
        """A megjegyzett Drive-jelszó törlése. True, ha már nincs elmentve."""
        return toollib_core.forget_password()

    def uninstall_service_tools(self, keys):
        """A kiválasztott programok eltávolítása: a mappájuk törlése a Program Files
        alól, az asztali parancsikonjuk törlése, a nyilvántartás frissítése. A verdikt a
        visszaolvasás (az exe eltűnt-e); egy futó program zárolt fájljai miatt a törlés
        félbe is maradhat - azt kiírjuk."""
        logging.info(f"[API] uninstall_service_tools({keys})")
        keys = [k for k in (keys or []) if k in STRESS_TOOLS]
        if not keys:
            self.emit('toast', {'message': '⚠️ Nincs kiválasztott program a törléshez!', 'type': 'warning'})
            return

        def worker():
            try:
                self.emit('task_start', {'task': 'toolsinstall', 'title': 'Szervíz Programok Törlése'})
                found = self._scan_installed_tools(keys)
                records = self._load_tool_install_records()
                ok_count, fail_count, skip_count = self._uninstall_tools_sync(keys, found, records)
                if fail_count == 0:
                    self.emit('task_complete', {'task': 'toolsinstall', 'status': f'✅ {ok_count} program törölve' + (f', {skip_count} nem volt telepítve.' if skip_count else '.')})
                else:
                    self.emit('task_complete', {'task': 'toolsinstall', 'status': f'⚠️ {ok_count} program törölve, {fail_count} törlése nem sikerült (fut még? részletek a fenti naplóban).'})
            except Exception as e:
                logging.error(f"[TOOLSINSTALL] uninstall_service_tools hiba: {e}", exc_info=True)
                self.emit('task_error', {'task': 'toolsinstall', 'error': f'Hiba: {str(e)}'})

        self._safe_thread('toolsinstall', worker)

    def _uninstall_tools_sync(self, keys, found, records):
        """A törlés magja (a worker és a teszt hívja). Visszaad: (ok, hiba, kihagyott)."""
        roots = [os.path.normcase(os.path.abspath(r)) for r in self._tools_scan_roots()]
        desktop = os.path.join(os.environ.get('PUBLIC', r'C:\Users\Public'), 'Desktop')
        ok_count = fail_count = skip_count = 0
        for key in keys:
            display_name = STRESS_TOOLS[key][0]
            hit = found.get(key)
            if not hit:
                skip_count += 1
                self.emit('task_progress', {'task': 'toolsinstall', 'log': f'ℹ️ {display_name}: nincs telepítve, kihagyva.'})
                continue
            exe, folder = hit
            folder_n = os.path.normcase(os.path.abspath(folder))
            # Csak a Program Files gyökerek ALATTI mappát töröljük, magát a gyökeret soha.
            if folder_n in roots or not any(folder_n.startswith(r + os.sep) for r in roots):
                fail_count += 1
                logging.error(f"[TOOLSINSTALL] {display_name}: a mappa nem a Program Files alatt van, nem töröljük: {folder}")
                self.emit('task_progress', {'task': 'toolsinstall', 'log': f'❌ {display_name}: a mappa ({folder}) nem a Program Files alatt van, nem töröltem.'})
                continue
            logging.info(f"[TOOLSINSTALL] {display_name}: törlés: {folder}")
            self.emit('task_progress', {'task': 'toolsinstall', 'log': f'🗑️ {display_name}: {folder} törlése...'})
            errors = []
            shutil.rmtree(folder, onerror=lambda fn, p, ei: errors.append(f"{p}: {ei[1]}"))
            lnks = [(records.get(key) or {}).get('lnk'), os.path.join(desktop, f"{display_name}.lnk")]
            for lnk in {l for l in lnks if l}:
                if os.path.exists(lnk):
                    logging.info(f"[TOOLSINSTALL] {display_name}: parancsikon törlése: {lnk}")
                    try:
                        os.remove(lnk)
                    except OSError as e:
                        errors.append(f"{lnk}: {e}")
            if os.path.exists(exe):
                fail_count += 1
                logging.error(f"[TOOLSINSTALL] {display_name}: az exe a törlés után is megvan ({exe}); hibák: {errors[:5]}")
                self.emit('task_progress', {'task': 'toolsinstall', 'log': f'❌ {display_name}: nem sikerült törölni (fut a program?) - {errors[0] if errors else exe}'})
                continue
            records.pop(key, None)
            self._save_tool_install_records(records)
            ok_count += 1
            if errors:
                logging.warning(f"[TOOLSINSTALL] {display_name}: törölve, de maradt fájl: {errors[:5]}")
                self.emit('task_progress', {'task': 'toolsinstall', 'log': f'⚠️ {display_name}: törölve, de {len(errors)} fájl bent maradt ({errors[0]})'})
            else:
                self.emit('task_progress', {'task': 'toolsinstall', 'log': f'✅ {display_name} törölve (mappa + parancsikon).'})
        return ok_count, fail_count, skip_count

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

    def install_service_tools(self, keys, password=''):
        """A kiválasztott programok telepítése: a programok csomagjának letöltése (ha még
        nincs cache-elve; a Drive-on lévőkhöz `password` kell), a program mappájának másolása a Program Files (x86) alá, asztali
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
                self.emit('task_progress', {'task': 'toolsinstall', 'log': '🌐 Programok előkészítése (első alkalommal letöltés)...', 'indeterminate': True})

                dl_state = {'last': 0.0}

                def dl_progress(phase, done, total):
                    now = time.monotonic()
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

                install_root = self._tools_install_root()
                records = self._load_tool_install_records()
                ok_count = 0
                fail_count = 0

                for key in keys:
                    display_name, _ = STRESS_TOOLS[key]
                    try:
                        # Programonként: egy elbukott (pl. jelszó nélküli Drive-os) letöltés
                        # nem viheti el a többi program telepítését.
                        self.emit('task_progress', {'task': 'toolsinstall', 'log': f'🌐 {display_name}: csomag előkészítése...'})
                        stress_dir = self._download_stresstools(progress=dl_progress, keys=[key], password=password)
                        if not stress_dir:
                            raise Exception(f"a letöltés nem sikerült - {self._toollib_last_error}")
                        src_exe = self._find_stress_tool_exes(stress_dir, [key]).get(key)
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
