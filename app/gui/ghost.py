"""DriverVarázsló GUI - Szellemeszközök nézet: nem jelenlévő (ghost) eszközök törlése
(a közös PS script + sor-protokoll: app/ghost_core.py)."""

# === AUTO-IMPORTS ===
import subprocess
import logging
from app.ghost_core import build_ghost_ps
from app.ghost_core import parse_ghost_line
# === /AUTO-IMPORTS ===


class GuiGhostMixin:
    """Szellemeszközök nézet: nem jelenlévő (ghost) eszközök törlése. A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # HARDWARE SCAN
    # ================================================================
    def delete_ghost_devices(self):
        logging.info("[API] delete_ghost_devices()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!', 'type': 'error'})
            return
        def worker():
            logging.info("[GHOST] Szellemeszközök törlésének indítása...")
            self.emit('task_start', {'task': 'ghost', 'title': 'Szellemeszközök Törlése'})
            self.emit('task_progress', {'task': 'ghost', 'log': 'Nem csatlakoztatott (fantom) eszközök azonosítása...', 'indeterminate': True})

            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", build_ghost_ps()],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                startupinfo=self._si, creationflags=self._nw)

            success = 0
            total = 0

            for line in process.stdout:
                if self._check_cancel():
                    self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                    process.wait()
                    self.emit('task_progress', {'task': 'ghost', 'log': '\n❗ Megszakítva!'})
                    self.emit('task_complete', {'task': 'ghost', 'status': '❗ Megszakítva!', 'success': success, 'fail': total-success})
                    return
                parsed = parse_ghost_line(line)
                if not parsed:
                    continue
                event, data = parsed
                if event == 'total':
                    total = data
                    self.emit('task_progress', {'task': 'ghost', 'log': f'Összesen {total} db szellemeszköz azonosítva...\n', 'total': total, 'current': 0, 'counter': f'0 / {total}'})
                elif event == 'rm':
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  🗑 Próbálkozás: {data}', 'status': f'Eltávolítás: {data}'})
                elif event == 'ok':
                    success += 1
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  ✅ Sikeresen törölve: {data}', 'current': success, 'counter': f'{success} / {total}'})
                elif event == 'fail':
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  ❌ Sikertelen (valószínűleg védett eszköz): {data}', 'current': success, 'counter': f'{success} / {total}'})
                elif event == 'done':
                    self.emit('task_progress', {'task': 'ghost', 'log': f'\n{data}'})
                else:
                    self.emit('task_progress', {'task': 'ghost', 'log': data})

            process.wait()
            self.emit('task_progress', {'task': 'ghost', 'log': '✅ Szellemeszközök törlése befejeződött.'})
            self.emit('task_complete', {'task': 'ghost', 'status': f'Kész! Törölve: {success} / {total}'})

        self._safe_thread('ghost', worker)
