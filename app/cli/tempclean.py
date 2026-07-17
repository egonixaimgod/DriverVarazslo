"""DriverVarázsló CLI - CLI: temp fájlok törlése (a közös logika: app/tempclean_core.py)."""

# === AUTO-IMPORTS ===
import os
from app.tempclean_core import _clean_folder_contents
from app.tempclean_core import _empty_recycle_bin
from app.tempclean_core import _fmt_bytes
from app.tempclean_core import _temp_clean_category_defs
# === /AUTO-IMPORTS ===


class CliTempCleanMixin:
    """CLI: temp fájlok törlése (a közös logika: app/tempclean_core.py). A CliApi része (összerakás: app/cli/api.py)."""

    # ================================================================
    # TEMP FÁJLOK TÖRLÉSE (lemez felszabadítás) - a GUI clean_temp_files
    # megfelelője, szinkron kiírással, a modul-szintű _clean_folder_contents /
    # _fmt_bytes / _empty_recycle_bin segédfüggvényeket a GUI-verzióval megosztva.
    # ================================================================
    def clean_temp_files(self, thumbnail_cache=False, recycle_bin=False, **overrides):
        """Windows ideiglenes fájlok törlése (mint a GUI Temp Törlés funkciója). Csak élő
        (online) rendszeren fut - ua. az indoklás, mint a szellemeszköz-törlésnél.
        overrides: a _temp_clean_category_defs kulcsai szerint felülírható, hogy melyik
        kategória fusson (alapértelmezésben a defs-ben megjelölt 3 "alapból bepipálva"
        kategória fut - user_temp/windows_temp/wu_cache)."""
        if self.target_os_path:
            print("\n❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!")
            return

        folder_categories = []
        for key, label, paths, services, default_checked in _temp_clean_category_defs(self.sys_drive):
            if overrides.get(key, default_checked) and paths:
                folder_categories.append((label, paths, services))

        if not folder_categories and not thumbnail_cache and not recycle_bin:
            print("\n⚠️ Nincs kiválasztva egyetlen törlendő kategória sem!")
            return

        print("\n🧹 Temp fájlok törlése...")
        print("-" * 50)
        total_freed = 0
        total_removed = 0
        total_failed = 0

        services_to_stop = sorted({s for _, _, services in folder_categories for s in services})
        if services_to_stop:
            print(f"⏸️ Szolgáltatások leállítása a cache törléséhez ({', '.join(services_to_stop)})...")
            self._run(['powershell', '-NoProfile', '-Command', f'Stop-Service {",".join(services_to_stop)} -Force -ErrorAction SilentlyContinue'])

        for label, paths, _services in folder_categories:
            cat_freed = cat_removed = cat_failed = 0
            for path in paths:
                print(f"{label} törlése ({path})...", end=" ", flush=True)
                freed, removed, failed = _clean_folder_contents(path)
                print(f"✅ {removed} elem törölve, {failed} kihagyva ({_fmt_bytes(freed)} felszabadítva).")
                cat_freed += freed
                cat_removed += removed
                cat_failed += failed
            total_freed += cat_freed
            total_removed += cat_removed
            total_failed += cat_failed

        if services_to_stop:
            print("▶️ Szolgáltatások újraindítása...")
            self._run(['powershell', '-NoProfile', '-Command', f'Start-Service {",".join(services_to_stop)} -ErrorAction SilentlyContinue'])

        if thumbnail_cache:
            print("🖼️ Miniatűr (thumbnail) gyorsítótár törlése...", end=" ", flush=True)
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
                    except Exception:
                        failed += 1
            print(f"✅ {removed} fájl törölve, {failed} kihagyva ({_fmt_bytes(freed)} felszabadítva).")
            total_freed += freed
            total_removed += removed
            total_failed += failed

        if recycle_bin:
            print("🗑️ Lomtár ürítése...", end=" ", flush=True)
            rb_freed = _empty_recycle_bin()
            print(f"✅ Kiürítve ({_fmt_bytes(rb_freed)} felszabadítva).")
            total_freed += rb_freed
            total_removed += 1

        print("-" * 50)
        print(f"🧹 Kész! Összesen {total_removed} elem törölve, {total_failed} kihagyva. Felszabadított hely: {_fmt_bytes(total_freed)}")
        return total_freed, total_removed, total_failed
