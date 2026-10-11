"""DriverVarázsló CLI - híd a GUI feature-mixinekhez: emit -> konzol, szálak -> szinkron.

MIÉRT ÍGY (explicit user decision, 2026-08-29 - "a CLI mód is ugyanazokat a funkciókat
tudja, mint a grafikus felület"): a GUI feature-mixinjei (hwscan, autofix, rebind, winact,
display, stress, benchmark...) NEM tartalmaznak semmilyen ablak-specifikus kódot - a
kimenetük kizárólag `self.emit(event, data)` hívásokon át megy ki, a bemenetük pedig
`self._run`. Ezt a CLAUDE.md is rögzíti ("A GUI mixinek meghajthatók: egy mixinnek csak
_run és emit kell"). Ezért a CLI NEM másolja le őket - ugyanazokat az osztályokat kapja
meg, és csak az `emit`-et cseréli konzolos megjelenítésre.

EZ A PROJEKT LEGRÉGEBBI SZABÁLYÁT KÖVETI: a duplikált logika, aminek egy példánya lemarad,
ennek a kódbázisnak a visszatérő hibája (Build 192: "az AutoFix működik, a kézi telepítés
egy egész kiadáson át némán rossz"). Ha a CLI külön driver-kereső motort kapna, a két út
előbb-utóbb eltérne - és pont a régi gépen, ahol senki nem venné észre.

KÉT DOLGOT ÍR FELÜL:
  - `emit`: a GUI-nak JS-eseményt küldene; itt konzolra rajzol (app/cli/console.py).
  - `_safe_thread`: a GUI-ban háttérszál, hogy az ablak ne fagyjon. A CLI-ben SZINKRON
    fut: nincs mit reszponzívan tartani, viszont a menü nem léphet tovább, amíg a
    művelet be nem fejeződött. (A `_task_busy` jelzőt így is állítjuk: a mixinek
    ellenőrzik.)
"""
import time
import logging

from app.cli import console as ui


# Az eseményekhez tartozó megjelenítés. Amit nem ismerünk, az a naplóba megy - a CLI
# sosem nyel el némán egy eseményt.
_QUIET_EVENTS = {
    # Olyan események, amiknek a CLI-ben nincs értelmes megjelenítése (a felület
    # állapotát frissítenék), viszont a naplóban ott a helyük.
    'drivers_loading', 'stress_dl_progress',
    'benchmark_progress', 'nvidia_progress', 'update_progress',
}


class CliBridgeMixin:
    """A GUI feature-mixinek konzolos futtatásához szükséges réteg."""

    # ------------------------------------------------------------------ emit
    def emit(self, event, data=None):
        """A GUI-mixinek egyetlen kimeneti csatornája, konzolra fordítva.

        A GUI-ban ez `window.evaluate_js`-en át JS-eseményt küld; itt kirajzoljuk. A
        naplózás VÁLTOZATLAN (`[EMIT:...]`), hogy egy CLI-futás naplója ugyanúgy
        olvasható legyen, mint egy GUI-futásé."""
        data = data if data is not None else {}
        try:
            logging.debug(f"[EMIT:{event}] {str(data)[:300]}")
        except Exception:
            pass
        try:
            self._render_event(event, data)
        except Exception as e:
            # Egy megjelenítési hiba SOHA nem akaszthatja meg a műveletet.
            logging.warning(f"[CLI-UI] Az esemény megjelenítése hibára futott ({event}): {e}")

    def _render_event(self, event, data):
        if not isinstance(data, dict):
            data = {'value': data}

        if event == 'task_start':
            ui.write('')
            ui.title(data.get('title') or 'Folyamat', color=ui.PURPLE)
            self._cli_task_title = data.get('title') or ''
            return

        if event == 'task_progress':
            log = data.get('log')
            if log:
                for line in str(log).split('\n'):
                    self._render_log_line(line)
            # Százalékos állapot, ha a mixin küldött ilyet.
            pct = data.get('progress')
            if isinstance(pct, (int, float)) and not data.get('indeterminate'):
                ui.progress(int(pct), 100, data.get('status') or '')
            return

        if event == 'task_complete':
            ui.progress_done()
            status = str(data.get('status') or 'Kész')
            ui.write('')
            if status.startswith('❌') or 'hiba' in status.lower() or 'sikertelen' in status.lower():
                ui.err(status)
            else:
                ui.ok(status)
            if data.get('chain_time'):
                ui.write(f"  {ui.YELLOW}{ui.BOLD}Teljes idő: {data['chain_time']}{ui.RESET}")
            return

        if event == 'task_error':
            ui.progress_done()
            ui.err(str(data.get('error') or 'Ismeretlen hiba'))
            return

        if event == 'toast':
            msg = str(data.get('message') or '')
            t = data.get('type')
            (ui.err if t == 'error' else ui.warn if t == 'warning'
             else ui.ok if t == 'success' else ui.info)(msg)
            return

        if event == 'ask_reboot':
            self._cli_reboot_offer = True
            return

        if event == 'hw_scan_progress':
            # A driver-keresés 3-5 percig is tarthat; a CLI-ben ugyanúgy látszania kell,
            # hogy dolgozik a program, mint a felületen. Determinate szakaszban sáv,
            # egyébként egyszerű állapotsor - de MINDIG csak akkor, ha változott a
            # szöveg, különben a katalógus-kör 90+ sort öntene a konzolra.
            status = str(data.get('status') or '')
            if data.get('determinate') and data.get('total'):
                ui.progress(data.get('current') or 0, data['total'],
                            f"{status}  {data.get('detail') or ''}".strip())
            elif status and status != getattr(self, '_cli_last_scan_status', None):
                self._cli_last_scan_status = status
                ui.progress_done()
                ui.info(status)
                if data.get('detail'):
                    ui.dim(data['detail'])
            return

        # A többi esemény adatot szállít a felületnek: eltesszük, a menü olvassa ki.
        self._cli_events[event] = data
        if event not in _QUIET_EVENTS:
            logging.debug(f"[CLI-UI] Esemény eltárolva a menünek: {event}")

    def _render_log_line(self, line):
        """Egy naplósor kirajzolása. A mixinek magyar szövegeket küldenek, elöl gyakran
        egy emoji-val - ezekből olvassuk ki a súlyosságot, hogy a CLI is színezni tudjon."""
        raw = line.rstrip()
        if not raw.strip():
            ui.write('')
            return
        s = raw.strip()
        if s.startswith('❌'):
            ui.err(s[1:].strip())
        elif s.startswith('⚠️') or s.startswith('⚠'):
            ui.warn(s.lstrip('⚠️ ').strip())
        elif s.startswith('✅'):
            ui.ok(s[1:].strip())
        elif s.startswith(('ℹ️', 'ℹ')):
            ui.info(s.lstrip('ℹ️ ').strip())
        elif s.startswith(('   ', '\t')) or raw.startswith('    '):
            ui.dim(s)
        else:
            ui.write('  ' + raw)

    # --------------------------------------------------------------- szálak
    def _safe_thread(self, task, target):
        """A GUI háttérszála helyett SZINKRON futtatás.

        A CLI-ben nincs mit reszponzívan tartani, és a menü nem is léphet tovább, amíg a
        művelet tart. A `_task_busy` jelzőt így is állítjuk: több mixin ellenőrzi, és egy
        beragadt jelző a következő műveletet utasítaná vissza."""
        if self._task_busy:
            ui.warn(f"Már fut egy másik művelet ({self._task_busy}).")
            return
        self._task_busy = task
        self._cancel_flag = False
        logging.info(f"[CLI-TASK] '{task}' indul (szinkron)...")
        try:
            target()
        except KeyboardInterrupt:
            self._cancel_flag = True
            ui.warn('Megszakítva (Ctrl+C).')
            logging.warning(f"[CLI-TASK] '{task}' megszakítva a felhasználó által.")
        except Exception as e:
            logging.error(f"[CLI-TASK] '{task}' kivétellel állt le: {e}", exc_info=True)
            ui.err(f"Hiba: {e}")
        finally:
            self._task_busy = None
            logging.info(f"[CLI-TASK] '{task}' vége.")

    # --------------------------------------------------- fájl/mappa választás
    def select_file(self, *_a, **_k):
        import os
        p = ui.ask('Fájl teljes útvonala (üresen = mégse)')
        if not p:
            return None
        p = p.strip('"')
        if not os.path.isfile(p):
            ui.err(f"Nincs ilyen fájl: {p}")
            return None
        return p

    # ------------------------------------------------------- szinkronizálás
    def _cli_sync(self, fn, timeout=7200):
        """Egy művelet lefuttatása ÉS a közben indított háttérszálak bevárása.

        MIÉRT KELL: a `_safe_thread` felülírása csak azokat a metódusokat teszi szinkronná,
        amik azon keresztül mennek - a GUI-ban viszont több feature SAJÁT `threading.Thread`-et
        indít (load_drivers, load_display_info, a benchmark-metódusok, a BootFixer-letöltés...),
        mert az ablaknak azonnal vissza kell térni. A CLI-ben ez azt jelentené, hogy a menü a
        munka BEFEJEZÉSE ELŐTT rajzolna eredményt - a gyakorlatban üres listát, ami néma hamis
        eredmény: pont az a hibaosztály, amit ez a projekt mindenhol üldöz.

        Ezért a hívás ELŐTT és UTÁN összevetjük a futó szálakat, és az újakat bevárjuk. Így
        egyetlen helyen, a hívási pontok módosítása nélkül lesz szinkron az egész CLI - és a
        GUI viselkedése változatlan marad."""
        import threading
        before = set(threading.enumerate())
        try:
            result = fn()
        finally:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                new = [t for t in threading.enumerate()
                       if t not in before and t.is_alive() and t is not threading.current_thread()]
                if not new:
                    break
                for t in new:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    t.join(timeout=min(left, 1.0))
            else:
                logging.warning(f"[CLI-TASK] A háttérszálak {timeout}s után sem fejeződtek be.")
        return result

    # ------------------------------------------------------------- segédek
    def _cli_take(self, event, default=None):
        """A menü ezzel veszi át a mixinek által küldött adat-eseményt (és törli)."""
        return self._cli_events.pop(event, default)

    def _cli_reset_events(self):
        self._cli_events = {}
        self._cli_reboot_offer = False
