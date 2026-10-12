"""DriverVarázsló GUI - Szellemeszközök nézet: nem jelenlévő (ghost) eszközök törlése
(a közös PS script + sor-protokoll: app/ghost_core.py)."""

# === AUTO-IMPORTS ===
import subprocess
import logging
from app.ghost_core import build_ghost_ps
from app.ghost_core import parse_ghost_line
from app.ghost_core import GHOST_REMOVE_TIMEOUT
# === /AUTO-IMPORTS ===


class GuiGhostMixin:
    """Szellemeszközök nézet: nem jelenlévő (ghost) eszközök törlése. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def remove_ghost_device(self, pnp_id, name=''):
        """EGYETLEN szellemeszköz eltávolítása - a hibás eszközök sorában lévő mini gombhoz.

        MIÉRT (2026-09-03, explicit user decision: *"a hibás drivereknél ha code 24 esek
        akkor ott is lehetne egy mini szellemeszközök törlése gomb"*): a Code 24 azt
        jelenti, hogy a készülék nincs a gépben, tehát a sor egy kihúzott eszköz
        maradványa. A teendő-szöveg eddig a Szellemeszközök menübe küldött - az viszont
        egy külön nézet, egy külön futás, és a technikus onnan nem látja, hogy pont ez a
        sor tűnt-e el. Egy sorban lévő gomb ugyanazt a mozdulatot egy kattintásra teszi.

        SZÁNDÉKOSAN CSAK EZT AZ EGY ESZKÖZT VISZI EL, nem a teljes szellemeszköz-kört.
        A menüpont változatlanul ott van, ha valaki mindet akarja; egy sor melletti gomb
        viszont a SORRA kell hasson, különben a technikus egy kattintással törölne 130
        eszközt anélkül, hogy kérte volna. Ugyanaz a `pnputil /remove-device`, amit a
        közös PS-script is futtat eszközönként.

        SZINKRON, mint a `fix_problem_device`: egyetlen gyors parancs, a `_task_busy`
        kaput szándékosan megkerüli (a felület a gombot tiltja le a hívás idejére)."""
        logging.info(f"[API] remove_ghost_device({pnp_id!r})")
        if self.target_os_path:
            return {'ok': False, 'error': 'Ez a funkció csak élő (online) rendszeren működik.'}
        if not pnp_id:
            return {'ok': False, 'error': 'Hiányzó eszköz-azonosító.'}
        try:
            # ok_codes: az 1 és a 3010 is VÁRT kimenet (nincs ilyen csomópont / újraindítás
            # kell) - WARNING nélkül. A verdikt a returncode, nem a lokalizált szöveg.
            res = self._run(['pnputil', '/remove-device', pnp_id],
                            timeout=GHOST_REMOVE_TIMEOUT, ok_codes=(0, 1, 3010))
            rc = getattr(res, 'returncode', 1)
            if rc in (0, 3010):
                logging.info(f"[GHOST] Egyedi szellemeszköz törölve: {name or pnp_id} (rc={rc})")
                return {'ok': True, 'reboot': rc == 3010}
            out = ((getattr(res, 'stdout', '') or '') + (getattr(res, 'stderr', '') or '')).strip()
            logging.warning(f"[GHOST] Az egyedi törlés nem sikerült ({name or pnp_id}): "
                            f"rc={rc} {out[:200]}")
            return {'ok': False, 'error': out[:200] or f'a pnputil {rc} kóddal tért vissza'}
        except Exception as e:
            logging.warning(f"[GHOST] Az egyedi törlés hibára futott ({name or pnp_id}): {e}")
            return {'ok': False, 'error': str(e)}

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
            finished = False

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
                elif event == 'timeout':
                    logging.warning(f"[GHOST] Törlés IDŐTÚLLÉPÉS ({GHOST_REMOVE_TIMEOUT}s), kilőve: {data}")
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  ⏱️ Beragadt, kihagyva {GHOST_REMOVE_TIMEOUT} mp után: {data}', 'current': success, 'counter': f'{success} / {total}'})
                elif event == 'done':
                    finished = True
                    self.emit('task_progress', {'task': 'ghost', 'log': f'\n{data}'})
                else:
                    self.emit('task_progress', {'task': 'ghost', 'log': data})

            process.wait()
            logging.info(f"[GHOST] Szellemeszköz-törlő script vége (returncode={process.returncode}, "
                         f"végigért={finished}, törölve {success}/{total}).")
            if not finished:
                # A szkript a DONE sor előtt leállt (PowerShell-hiba) - ez nem "befejeződött".
                logging.error("[GHOST] A szellemeszköz-törlő szkript nem ért végig (nincs DONE sor).")
                self.emit('task_progress', {'task': 'ghost', 'log': f'⚠️ A szellemeszköz-törlés NEM ért végig (a PowerShell-szkript leállt, kód {process.returncode}) - részletek a naplóban.'})
                self.emit('task_complete', {'task': 'ghost', 'status': f'⚠️ Félbemaradt! Törölve: {success} / {total}'})
                return
            self.emit('task_progress', {'task': 'ghost', 'log': '✅ Szellemeszközök törlése befejeződött.'})
            self.emit('task_complete', {'task': 'ghost', 'status': f'Kész! Törölve: {success} / {total}'})

        self._safe_thread('ghost', worker)
