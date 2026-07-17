"""DriverVarázsló GUI - Temp Fájlok Törlése nézet (a közös logika: app/tempclean_core.py)."""

# === AUTO-IMPORTS ===
import os
import logging
from app.tempclean_core import _clean_folder_contents
from app.tempclean_core import _empty_recycle_bin
from app.tempclean_core import _fmt_bytes
from app.tempclean_core import _temp_clean_category_defs
# === /AUTO-IMPORTS ===


class GuiTempCleanMixin:
    """Temp Fájlok Törlése nézet (a közös logika: app/tempclean_core.py). A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # TEMP FÁJLOK TÖRLÉSE (lemez felszabadítás)
    # ================================================================
    def clean_temp_files(self, options=None):
        """Windows ideiglenes fájlok törlése a bejelölt kategóriák szerint - lásd
        _temp_clean_category_defs a teljes listáért (felhasználói/rendszer TEMP, WU/
        Delivery Optimization cache, hibajelentések, Shader Cache, CBS logok, Crash
        Dumpok, IE/Edge cache), plusz a két speciális kategória (miniatűr-gyorsítótár,
        Lomtár). Csak élő (online) rendszeren értelmezhető - ua. mint a
        szellemeszköz-törlésnél: egy célzott offline OS TEMP mappáinak törlése minden
        felhasználói profilra kiterjedne (nem tudnánk, melyik "aktuális" felhasználóé a
        %TEMP%), ezért nem támogatott."""
        logging.info(f"[API] clean_temp_files(options={options})")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!', 'type': 'error'})
            return

        opts = options or {}
        folder_categories = []
        for key, label, paths, services, default_checked in _temp_clean_category_defs(self.sys_drive):
            if opts.get(key, default_checked) and paths:
                folder_categories.append((label, paths, services))
        do_thumbnails = opts.get('thumbnail_cache', False)
        do_recycle_bin = opts.get('recycle_bin', False)

        if not folder_categories and not do_thumbnails and not do_recycle_bin:
            self.emit('toast', {'message': '⚠️ Nincs kiválasztva egyetlen törlendő kategória sem!', 'type': 'warning'})
            return

        def worker():
            self.emit('task_start', {'task': 'tempclean', 'title': 'Temp Fájlok Törlése'})
            total_freed = 0
            total_removed = 0
            total_failed = 0

            # Néhány kategória (WU cache, Delivery Optimization) mappáját egy szolgáltatás
            # tartja zárolva - ezeket egyszerre, egy körben állítjuk le/indítjuk újra (nem
            # kategóriánként), hogy egy szolgáltatást ne kelljen kétszer le-/felkapcsolni,
            # ha véletlenül több kategória is hivatkozna rá.
            services_to_stop = sorted({s for _, _, services in folder_categories for s in services})
            if services_to_stop:
                self.emit('task_progress', {'task': 'tempclean', 'log': f'⏸️ Szolgáltatások leállítása a cache törléséhez ({", ".join(services_to_stop)})...', 'indeterminate': True})
                self._run(['powershell', '-NoProfile', '-Command', f'Stop-Service {",".join(services_to_stop)} -Force -ErrorAction SilentlyContinue'])

            for label, paths, _services in folder_categories:
                if self._check_cancel():
                    break
                cat_freed = cat_removed = cat_failed = 0
                for path in paths:
                    self.emit('task_progress', {'task': 'tempclean', 'log': f'{label} törlése ({path})...', 'indeterminate': True})
                    freed, removed, failed = _clean_folder_contents(path, self._check_cancel)
                    cat_freed += freed
                    cat_removed += removed
                    cat_failed += failed
                self.emit('task_progress', {'task': 'tempclean', 'log': f'  ✅ {cat_removed} elem törölve, {cat_failed} zárolt/hozzáférhetetlen elem kihagyva ({_fmt_bytes(cat_freed)} felszabadítva).'})
                total_freed += cat_freed
                total_removed += cat_removed
                total_failed += cat_failed

            if services_to_stop:
                self.emit('task_progress', {'task': 'tempclean', 'log': '▶️ Szolgáltatások újraindítása...'})
                self._run(['powershell', '-NoProfile', '-Command', f'Start-Service {",".join(services_to_stop)} -ErrorAction SilentlyContinue'])

            if do_thumbnails and not self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'log': '🖼️ Miniatűr (thumbnail) gyorsítótár törlése...', 'indeterminate': True})
                freed = removed = failed = 0
                local = os.environ.get('LOCALAPPDATA')
                explorer_dir = os.path.join(local, 'Microsoft', 'Windows', 'Explorer') if local else None
                if explorer_dir and os.path.isdir(explorer_dir):
                    for name in os.listdir(explorer_dir):
                        if not (name.startswith('thumbcache_') or name.startswith('iconcache_')):
                            continue
                        full = os.path.join(explorer_dir, name)
                        try:
                            size = os.path.getsize(full)
                            os.remove(full)
                            freed += size
                            removed += 1
                        except Exception as e:
                            failed += 1
                            logging.debug(f"[TEMPCLEAN] Nem törölhető ({full}): {e}")
                self.emit('task_progress', {'task': 'tempclean', 'log': f'  ✅ {removed} fájl törölve, {failed} kihagyva ({_fmt_bytes(freed)} felszabadítva).'})
                total_freed += freed
                total_removed += removed
                total_failed += failed

            if do_recycle_bin and not self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'log': '🗑️ Lomtár ürítése...', 'indeterminate': True})
                rb_freed = _empty_recycle_bin()
                self.emit('task_progress', {'task': 'tempclean', 'log': f'  ✅ Lomtár kiürítve ({_fmt_bytes(rb_freed)} felszabadítva).'})
                total_freed += rb_freed
                total_removed += 1

            if self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'log': '\n❗ Megszakítva!'})
                self.emit('task_complete', {'task': 'tempclean', 'status': f'❗ Megszakítva! Eddig felszabadítva: {_fmt_bytes(total_freed)}'})
                return

            self.emit('task_progress', {'task': 'tempclean', 'log': f'\n✅ Kész! Összesen {total_removed} elem törölve, {total_failed} kihagyva (zárolt/hozzáférhetetlen fájlok - ez normális, ha épp használatban vannak).'})
            self.emit('task_complete', {'task': 'tempclean', 'status': f'🧹 Felszabadított hely: {_fmt_bytes(total_freed)}'})

        self._safe_thread('tempclean', worker)
