"""DriverVarázsló GUI - Mentés és Visszaállítás nézet (2026-11-04-én újraírva).

Driver-mentés (third-party / a Windows saját driverei / mindkettő), visszaállítás egy
mentésből erre a gépre vagy egy nem induló Windowsra, a Windows saját drivereinek
kinyerése egy telepítő-lemezképből, visszaállítási pont. A logika EGY példányban az
`app/backup_core.py`-ban van, ez a fájl csak a felület (és a CLI) kapuja: mappát kér,
szálat indít, és a haladást a folyamatablakba küldi."""

# === AUTO-IMPORTS ===
import os
import re
import logging
from app import backup_core
# === /AUTO-IMPORTS ===

_COUNT_RE = re.compile(r'(\d+)\s*(?:/|of)\s*(\d+)', re.IGNORECASE)


class GuiBackupMixin:
    """Mentés és Visszaállítás nézet. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _bk_logger(self, task):
        """A mag `log` callbackja: a sor a folyamatablakba megy, a DISM folyamatjelzője
        (`[===  42.0%  ]`) és a `12/58` / `12 of 58` számláló pedig a sávra."""
        def log(msg):
            msg = str(msg)
            pct = backup_core.progress_percent(msg)
            if pct is not None:
                self.emit('task_progress', {'task': task, 'current': int(pct), 'total': 100,
                                            'counter': f'{pct:.0f}%'})
                return
            ev = {'task': task, 'log': msg}
            m = _COUNT_RE.search(msg)
            if m and int(m.group(2)) > 0:
                ev.update({'current': int(m.group(1)), 'total': int(m.group(2)),
                           'counter': f'{m.group(1)} / {m.group(2)}'})
            self.emit('task_progress', ev)
        return log

    # ------------------------------------------------------------------ áttekintés
    def get_backup_overview(self):
        """A nézet induló adatai: honnan ment a program, mekkora a két rész, és milyen
        automatikus mentések vannak ezen a lemezen."""
        target = self.target_os_path
        logging.info(f"[API] get_backup_overview() - cél: {target or 'élő rendszer'}")
        try:
            sizes = backup_core.store_overview(target)
        except Exception as e:
            logging.error(f"[BACKUP] A driver-tár áttekintése elbukott: {e}", exc_info=True)
            sizes = None
        try:
            auto = backup_core.list_auto_backups(target)
        except Exception as e:
            logging.error(f"[BACKUP] Az automatikus mentések listája elbukott: {e}", exc_info=True)
            auto = []
        return {'source': backup_core.describe_source(target), 'offline': bool(target),
                'target_root': backup_core.target_root(target) if target else '',
                'is_pe': os.environ.get('SystemDrive', 'C:').upper() == 'X:',
                'sizes': sizes, 'auto_backups': auto}

    # ---------------------------------------------------------------------- mentés
    def backup_drivers(self, mode, dest=None):
        """Mentés indítása. `mode`: 'thirdparty' | 'windows' | 'all'; `dest`: a célmappa
        (None = mappaválasztó). A mentés ezen belül egy új, dátumozott mappába kerül."""
        logging.info(f"[API] backup_drivers(mode={mode!r}, dest={dest!r})")
        if mode not in backup_core.BACKUP_MODES:
            self.emit('toast', {'message': f'Ismeretlen mentés-fajta: {mode}', 'type': 'error'})
            return
        dest = dest or self.select_directory('Hova mentsem a drivereket?')
        if not dest:
            logging.info("[BACKUP] Mégse - nincs célmappa")
            return
        target = self.target_os_path

        def worker():
            what = backup_core.BACKUP_MODES[mode][1]
            self.emit('task_start', {'task': 'backup', 'title': f'Driver-mentés: {what}'})
            r = backup_core.backup_drivers(self._run, self._bk_logger('backup'), self._check_cancel,
                                           dest, target, mode, self._si, self._nw)
            st = r['status']
            if st == 'ok':
                status = f'✅ Mentés kész ({r["size_mb"]:.0f} MB): {r["folder"]}'
            elif st == 'partial':
                status = f'⚠️ A mentés hiányos ({r["size_mb"]:.0f} MB): {r["folder"]}'
            elif st == 'cancelled':
                status = '❗ Megszakítva'
            elif st == 'no_space':
                status = '❌ Nincs elég hely a célmeghajtón'
            else:
                status = '❌ A mentés nem sikerült'
            log = '\n' + ('\n'.join('⚠️ ' + p for p in r['problems']) if r['problems'] else '')
            self.emit('task_complete', {'task': 'backup', 'status': status, 'log': log,
                                        'folder': r.get('folder')})
        self._safe_thread('backup', worker)

    # ---------------------------------------------------------------- visszaállítás
    def inspect_backup_folder(self, path=None):
        """Egy mentés tartalma a felületnek (melyik csomagok, van-e Windows-rész).
        `path` None = mappaválasztó. Mégse esetén None."""
        path = path or self.select_directory('Válaszd ki a mentés mappáját')
        if not path:
            return None
        logging.info(f"[API] inspect_backup_folder({path!r})")
        info = backup_core.inspect_backup(path)
        logging.info(f"[RESTORE] {path}: ok={info['ok']}, {info['kind'] or info['error']}, "
                     f"{len(info['packages'])} csomag, windows={bool(info['windows'])}")
        return info

    def check_restore_target(self, path=None):
        """Egy nem induló Windows meghajtójának ellenőrzése. `path` None = mappaválasztó."""
        path = path or self.select_directory('Válaszd ki a nem induló Windows meghajtóját')
        if not path:
            return None
        root = backup_core.target_root(path)
        problem = backup_core.offline_target_problem(root)
        logging.info(f"[API] check_restore_target({path!r}) -> {root}: {problem or 'rendben'}")
        return {'ok': not problem, 'root': root, 'error': problem,
                'label': '' if problem else backup_core.describe_windows(root)}

    def restore_drivers(self, source, target=None, packages=None, include_windows=True, fix_boot=True):
        """Visszaállítás a `source` mentésből. `target` None = erre a gépre, különben egy nem
        induló Windows meghajtója; `packages` = a kiválasztott csomag-mappák (None = mind)."""
        logging.info(f"[API] restore_drivers(source={source!r}, target={target!r}, "
                     f"packages={'mind' if packages is None else len(packages)}, "
                     f"windows={include_windows}, boot={fix_boot})")
        if target is None and os.environ.get('SystemDrive', 'C:').upper() == 'X:':
            self.emit('toast', {'message': 'WinPE alatt nincs „erre a gépre” visszaállítás - válaszd ki a nem induló Windows meghajtóját.', 'type': 'error'})
            return

        def worker():
            where = f'→ {target}' if target else '→ erre a gépre'
            self.emit('task_start', {'task': 'restore', 'title': f'Driver-visszaállítás {where}'})
            r = backup_core.restore_drivers(self._run, self._bk_logger('restore'), self._check_cancel,
                                            self._si, self._nw, source, target, packages,
                                            include_windows, fix_boot)
            # A pnputil/DISM itt Popen-nel fut, nem a `_run`-on: a csomaglista gyorsítótárát
            # nekünk kell eldobni, különben a Driverek nézet a régi listát mutatná.
            invalidate = getattr(self, 'invalidate_driver_cache', None)
            if invalidate:
                invalidate()
            st = r['status']
            if st == 'ok':
                status = f'✅ Visszaállítás kész: {r["tp_done"]} third-party csomag' + (
                    ', a Windows hiányzó fájljai visszamásolva' if r['windows'] == 'copied' else '')
                if target:
                    status += ' - indítsd el a gépet a javított Windowsról'
            elif st == 'errors':
                status = f'⚠️ Visszaállítás hibákkal: {r["tp_done"]} sikerült, {len(r["tp_failed"])} nem'
            elif st == 'cancelled':
                status = '❗ Megszakítva'
            else:
                status = '❌ A visszaállítás nem indult el (lásd a naplót)'
            log = ('\nNem sikerült: ' + ', '.join(r['tp_failed'])) if r['tp_failed'] else ''
            self.emit('task_complete', {'task': 'restore', 'status': status, 'log': log})
        self._safe_thread('restore', worker)

    # ------------------------------------------------------- telepítő-lemezképből
    def extract_wim(self, image=None, dest=None):
        """A Windows saját drivereinek kinyerése egy `install.wim` / `install.esd` fájlból
        (a neve a régi, mert a CLI és a felület így hívja). None = fájl-/mappaválasztó."""
        logging.info(f"[API] extract_wim(image={image!r}, dest={dest!r})")
        image = image or self.select_file('Válaszd ki az install.wim vagy install.esd fájlt',
                                          'Windows lemezkép (*.wim;*.esd)')
        if not image:
            return
        dest = dest or self.select_directory('Hova mentsem a kinyert drivereket?')
        if not dest:
            return
        target = self.target_os_path

        def worker():
            self.emit('task_start', {'task': 'wim', 'title': 'Windows-driverek kinyerése lemezképből'})
            r = backup_core.extract_windows_drivers(self._run, self._bk_logger('wim'), self._check_cancel,
                                                    image, dest, target)
            st = r['status']
            if st == 'ok':
                status = f'✅ {r["count"]} Windows-csomag kinyerve ({r["size_mb"]:.0f} MB): {r["folder"]}'
            elif st == 'partial':
                status = f'⚠️ Hiányos kinyerés ({r["count"]} csomag): {r["folder"]}'
            elif st == 'cancelled':
                status = '❗ Megszakítva'
            else:
                status = f'❌ Nem sikerült: {r["error"]}'
            self.emit('task_complete', {'task': 'wim', 'status': status,
                                        'log': f'\n{r["error"]}' if r.get('error') and st != 'fail' else ''})
        self._safe_thread('wim', worker)

    # --------------------------------------------------------- visszaállítási pont
    def create_restore_point(self):
        logging.info("[API] create_restore_point()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Visszaállítási pont csak a most futó Windowsról készíthető (offline módban nem).', 'type': 'error'})
            return

        def worker():
            self.emit('task_start', {'task': 'rp', 'title': 'Visszaállítási pont'})
            status, desc = backup_core.create_restore_point(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'rp', 'log': msg, 'indeterminate': True}))
            text = {'ok': f'✅ Visszaállítási pont létrehozva: {desc}',
                    'ok_unverified': '⚠️ A visszaállítási pont létrehozása elindult, de még nem látszik a listában',
                    'enable_failed': '❌ A rendszervédelem nem kapcsolható be ezen a gépen'}.get(
                status, '❌ A visszaállítási pont nem jött létre')
            self.emit('task_complete', {'task': 'rp', 'status': text})
        self._safe_thread('rp', worker)
