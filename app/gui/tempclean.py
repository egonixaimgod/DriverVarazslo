"""DriverVarázsló GUI - Rendszer karbantartás (Temp Fájlok Törlése) nézet.
A közös logika: app/tempclean_core.py. 2026-09-22-én átdolgozva (lásd a mag fejlécét)."""

# === AUTO-IMPORTS ===
import shutil
import logging
import threading
from app import tempclean_core as tc
# === /AUTO-IMPORTS ===


class GuiTempCleanMixin:
    """Rendszer karbantartás nézet. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def scan_temp_sizes(self):
        """A méret-előnézet háttérszálon -> 'tempclean_sizes' esemény. Csak olvas, ezért nem
        megy át a _task_busy kapun (ugyanaz az elv, mint a load_drivers-nél)."""
        logging.info("[API] scan_temp_sizes()")

        def worker():
            try:
                self.emit('tempclean_sizes', tc.measure_categories(self.sys_drive))
            except Exception as e:
                logging.error(f"[TEMPCLEAN] Méret-felmérés hiba: {e}", exc_info=True)
                self.emit('tempclean_sizes', {'sizes': {}, 'disk': {}, 'error': str(e)})
        threading.Thread(target=worker, daemon=True, name="tempclean-scan").start()

    def _disk_free(self):
        try:
            return shutil.disk_usage(self.sys_drive).free
        except OSError:
            return None

    def clean_temp_files(self, options=None):
        """A bejelölt kategóriák takarítása. Csak élő (online) rendszeren: egy offline OS
        %TEMP%-je nem egyértelmű (melyik felhasználóé?), ezért nem támogatott."""
        logging.info(f"[API] clean_temp_files(options={options})")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!', 'type': 'error'})
            return

        opts = options or {}
        folder_categories = []
        for key, label, paths, services, default_checked in tc._temp_clean_category_defs(self.sys_drive):
            if opts.get(key, default_checked):
                folder_categories.append((key, label, paths, services))
        specials = {k: bool(opts.get(k, d)) for k, _l, d in tc.SPECIAL_CATEGORIES}

        if not folder_categories and not any(specials.values()):
            self.emit('toast', {'message': '⚠️ Nincs kiválasztva egyetlen törlendő kategória sem!', 'type': 'warning'})
            return

        def say(msg):
            self.emit('task_progress', {'task': 'tempclean', 'log': msg})

        def worker():
            self.emit('task_start', {'task': 'tempclean', 'title': 'Rendszer karbantartás'})
            free_before = self._disk_free()
            total_freed = total_removed = total_failed = 0
            warnings = []

            # A szolgáltatás-zárolt kategóriák (WU, Delivery Optimization) előtt egyszer
            # állítjuk le, és a visszaindítás `finally`-ben van: egy hiba után sem maradhat
            # leállítva semmi. Csak az indul vissza, ami ELŐTTE is futott.
            services = sorted({s for _k, _l, _p, svcs in folder_categories for s in svcs})
            running = []
            try:
                if services:
                    self.emit('task_progress', {'task': 'tempclean', 'indeterminate': True,
                                                'log': f'⏸️ Szolgáltatások leállítása a gyorsítótár törléséhez ({", ".join(services)})...'})
                    running = tc.stop_services(self._run, services)

                for key, label, paths, _svc in folder_categories:
                    if self._check_cancel():
                        break
                    if not paths:
                        say(f'{label}: ezen a gépen nincs ilyen mappa.')
                        continue
                    cf = cr = cx = 0
                    for path in paths:
                        self.emit('task_progress', {'task': 'tempclean', 'indeterminate': True,
                                                    'log': f'{label} törlése ({path})...'})
                        f, r, x = tc._clean_folder_contents(path, self._check_cancel)
                        cf += f
                        cr += r
                        cx += x
                    say(f'  ✅ {cr} fájl törölve ({tc._fmt_bytes(cf)})'
                        + (f', {cx} zárolt fájl kihagyva (épp használatban van)' if cx else '') + '.')
                    total_freed += cf
                    total_removed += cr
                    total_failed += cx
            finally:
                if running:
                    say('▶️ Szolgáltatások visszaindítása...')
                    bad = tc.start_services(self._run, running)
                    if bad:
                        warnings.append(f'nem indult vissza: {", ".join(bad)}')
                        say(f'  ⚠️ NEM indult vissza: {", ".join(bad)} - indítsd el kézzel (services.msc) vagy indítsd újra a gépet!')

            if specials['thumbnail_cache'] and not self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'indeterminate': True, 'log': '🖼️ Miniatűr-gyorsítótár törlése...'})
                f, r, x = tc.clean_thumbnails()
                say(f'  ✅ {r} fájl törölve ({tc._fmt_bytes(f)})'
                    + (f', {x} zárolt - ezeket a Windows Intéző használja, kijelentkezés után törölhetők' if x else '') + '.')
                total_freed += f
                total_removed += r
                total_failed += x

            if specials['recycle_bin'] and not self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'indeterminate': True, 'log': '🗑️ Lomtár ürítése...'})
                f, items, ok = tc._empty_recycle_bin()
                if not items:
                    say('  ✅ A Lomtár már üres volt.')
                elif ok:
                    say(f'  ✅ Lomtár kiürítve: {items} elem ({tc._fmt_bytes(f)}).')
                    total_freed += f
                    total_removed += items
                else:
                    warnings.append('a Lomtár nem ürült ki teljesen')
                    say('  ⚠️ A Lomtár ürítés után sem üres - lehet, hogy egy fájl épp használatban van.')

            if specials['component_cleanup'] and not self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'indeterminate': True,
                                            'log': '🧩 Régi Windows-frissítések takarítása (DISM) - ez 5-20 percig is tarthat, a gép nem fagyott le...'})
                ok, msg = tc.run_component_cleanup(self._run)
                if ok:
                    say('  ✅ Komponenstár kitakarítva.')
                else:
                    warnings.append('a komponenstár-takarítás nem sikerült')
                    say(f'  ⚠️ A komponenstár-takarítás nem sikerült ({msg}).')

            free_after = self._disk_free()
            measured = (free_after - free_before) if (free_before is not None and free_after is not None) else None
            if self._check_cancel():
                say('\n❗ Megszakítva!')
                self.emit('task_complete', {'task': 'tempclean', 'status': f'❗ Megszakítva! Eddig: {tc._fmt_bytes(total_freed)}'})
            else:
                say(f'\n✅ Kész! {total_removed} fájl törölve'
                    + (f', {total_failed} zárolt fájl kihagyva (épp használatban voltak - ez normális)' if total_failed else '') + '.')
                if measured is not None:
                    say(f'💽 Szabad hely: {tc._fmt_bytes(free_before)} → {tc._fmt_bytes(free_after)} '
                        f'(mérve: +{tc._fmt_bytes(max(measured, 0))}).')
                self.emit('task_complete', {'task': 'tempclean',
                                            'status': f'🧹 Felszabadítva: {tc._fmt_bytes(max(measured or 0, total_freed))}'
                                                      + (f' · ⚠️ {"; ".join(warnings)}' if warnings else '')})
            logging.info(f"[TEMPCLEAN] Kész: {total_removed} törölve, {total_failed} kihagyva, számolt "
                         f"{tc._fmt_bytes(total_freed)}, mért szabadhely-változás "
                         f"{tc._fmt_bytes(measured) if measured is not None else '?'}, figyelmeztetések: {warnings}")
            try:
                self.emit('tempclean_sizes', tc.measure_categories(self.sys_drive))
            except Exception as e:
                logging.warning(f"[TEMPCLEAN] A takarítás utáni méret-felmérés hibára futott: {e}")

        self._safe_thread('tempclean', worker)
