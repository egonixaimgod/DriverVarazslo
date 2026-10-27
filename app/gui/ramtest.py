"""DriverVarázsló GUI - RAM TEST: TestMem5 -> y-cruncher -> HCI MemTest egymás után, felügyelet
nélkül, közben és a végén a WHEA-napló figyelésével, záró verdikttel a szervizesnek.
(Az értelmezők és a verdikt: app/ramtest_core.py; a csomag és a letöltés közös a stressz-tesztével.)"""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import datetime
import logging
import os
import subprocess
import time
from app.ramtest_core import HCI_MAX_HOURS_PER_100
from app.ramtest_core import RAMTEST_MODES
from app.ramtest_core import RAMTEST_TOOLS
from app.ramtest_core import STEP_NAMES
from app.ramtest_core import TM5_CONFIG_NAME
from app.ramtest_core import TM5_CONFIG_SOURCE
from app.ramtest_core import TM5_MAX_HOURS_PER_CYCLE
from app.ramtest_core import YC_GRACE_SECONDS
from app.ramtest_core import describe_exit_code
from app.ramtest_core import hci_alloc_failed
from app.ramtest_core import hci_grid
from app.ramtest_core import hci_plan
from app.ramtest_core import parse_hci_status
from app.ramtest_core import parse_hci_title_errors
from app.ramtest_core import parse_tm5_log
from app.ramtest_core import parse_whea_output
from app.ramtest_core import parse_ycruncher_output
from app.ramtest_core import ramtest_verdict
from app.ramtest_core import tm5_config_text
from app.ramtest_core import whea_query_ps
from app.ramtest_core import whea_summary
from app.ramtest_core import ycruncher_args
from app.win32 import BM_CLICK
# === /AUTO-IMPORTS ===

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
SMTO_ABORTIFHUNG = 0x0002

# A HCI MemTest vezérlőinek azonosítói (élőben kiolvasva, 7.0 ingyenes): a MB-mező és az állapotsor.
HCI_EDIT_ID = 1000
HCI_STATUS_ID = 1999

# Milyen gyakran nézzük a WHEA-naplót teszt KÖZBEN (a végén mindig van egy záró lekérdezés).
RAMTEST_WHEA_POLL_SECONDS = 120
# A teszt előtti napok, amikre egy tájékoztató WHEA-áttekintést adunk (a gép előélete).
RAMTEST_WHEA_HISTORY_DAYS = 7


class GuiRamTestMixin:
    """RAM TEST: három memóriateszt egymás után + WHEA-figyelés. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def get_ramtest_modes(self):
        """A felület és a CLI ebből írja ki a két módot (egy forrás: RAMTEST_MODES)."""
        return {k: {'label': v['label'], 'desc': v['desc']} for k, v in RAMTEST_MODES.items()}

    # ------------------------------------------------------------------
    # segédek
    # ------------------------------------------------------------------
    def _ramtest_log(self, text, **extra):
        self.emit('task_progress', dict({'task': 'ramtest', 'log': text}, **extra))

    def _ramtest_whea(self, since):
        """WHEA-események since óta: lista, vagy None (a lekérdezés elbukott)."""
        res = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', whea_query_ps(since)],
                        timeout=90)
        events = parse_whea_output(res.stdout)
        if events is None:
            logging.warning(f"[RAMTEST] WHEA-lekérdezés elbukott (rc={res.returncode}): {(res.stdout or '')[-300:]} {(res.stderr or '')[-300:]}")
        else:
            logging.info(f"[RAMTEST] WHEA {since:%Y-%m-%d %H:%M} óta: {len(events)} esemény "
                         f"{[(e['id'], e['time']) for e in events[:20]]}")
        return events

    def _ramtest_kill(self, pids=(), images=()):
        for pid in pids:
            if pid and pid > 0:
                self._run(['taskkill', '/PID', str(pid), '/T', '/F'], ok_codes=(0, 128))
        if images:
            args = []
            for image in images:
                args += ['/IM', image]
            self._run(['taskkill', '/F', '/T'] + args, ok_codes=(0, 128))

    def _ramtest_image_running(self, image):
        res = self._run(['tasklist', '/FI', f'IMAGENAME eq {image}', '/NH'])
        return image.lower() in (res.stdout or '').lower()

    def _ramtest_ctrl_text(self, hwnd):
        """Egy MÁSIK folyamat vezérlőjének szövege WM_GETTEXT-tel (a GetWindowText ott nem
        mindig küld üzenetet), időkorláttal, hogy egy lefagyott program ne akassza meg a szálat."""
        user32 = self._stress_user32()
        n = ctypes.c_size_t(0)
        if not user32.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 3000, ctypes.byref(n)):
            return None
        buf = ctypes.create_unicode_buffer(n.value + 1)
        out = ctypes.c_size_t(0)
        if not user32.SendMessageTimeoutW(hwnd, WM_GETTEXT, n.value + 1, ctypes.addressof(buf),
                                          SMTO_ABORTIFHUNG, 3000, ctypes.byref(out)):
            return None
        return buf.value

    def _ramtest_dlg_item(self, hwnd, ctrl_id):
        user32 = self._stress_user32()
        user32.GetDlgItem.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        user32.GetDlgItem.restype = ctypes.wintypes.HWND
        return user32.GetDlgItem(hwnd, ctrl_id)

    def _ramtest_wait(self, check, deadline, since, whea_seen, progress, box_end=None, box_result=None):
        """Közös várakozó ciklus: 2 mp-enként check() -> None (még fut) vagy eredmény-dict;
        közben megszakítás, határidő és WHEA-figyelés (az új bejegyzéseket azonnal kiírja,
        de a tesztet nem állítja le - a verdikt a végén dönt). box_end: a Gyors mód időkerete -
        ha addig a check() nem talált hibát, a box_result() az eredmény (nem időtúllépés)."""
        last_whea = time.monotonic()
        last_progress = 0.0
        while True:
            if self._check_cancel():
                return {'state': 'cancelled', 'detail': 'megszakítva'}
            result = check()
            if result is not None:
                return result
            now = time.monotonic()
            if box_end is not None and now > box_end:
                return box_result()
            if now > deadline:
                return {'state': 'tool_error', 'detail': 'időtúllépés - a program nem fejezte be a tesztet a felső korláton belül'}
            if now - last_progress >= 30:
                last_progress = now
                progress()
            if now - last_whea >= RAMTEST_WHEA_POLL_SECONDS:
                last_whea = now
                events = self._ramtest_whea(since)
                if events and len(events) > whea_seen['n']:
                    for ev in events[whea_seen['n']:]:
                        self._ramtest_log(f"  ⚠️ WHEA {ev['id']} ({ev['time']}): {ev['message']}")
                    whea_seen['n'] = len(events)
            time.sleep(2)

    # ------------------------------------------------------------------
    # 1. TestMem5
    # ------------------------------------------------------------------
    def _ramtest_tm5(self, exe, mode, since, whea_seen):
        exe = self._ascii_tool_copy(exe)
        folder = os.path.dirname(exe)
        src = os.path.join(folder, 'bin', TM5_CONFIG_SOURCE)
        if not os.path.exists(src):
            return {'state': 'tool_error', 'detail': f'hiányzik a konfig: bin\\{TM5_CONFIG_SOURCE}'}
        # newline='': a CRLF sorvégeknek meg kell maradniuk - LF-es konfigra a TM5 azt mondja,
        # hogy "The configuration file specified on the command line was not found" (Build 396, terepen).
        with open(src, 'r', encoding='latin-1', newline='') as f:
            cfg = tm5_config_text(f.read(), mode['tm5_cycles'])
        cfg_path = os.path.join(folder, 'bin', TM5_CONFIG_NAME)
        logging.info(f"[RAMTEST] TM5 konfig írása: {cfg_path} (Cycles={mode['tm5_cycles']})")
        with open(cfg_path, 'w', encoding='latin-1', newline='') as f:
            f.write(cfg)
        log_path = os.path.join(folder, 'Log.txt')
        crash_paths = [os.path.join(folder, n) for n in ('Crash.log', 'crash.log')]
        for p in [log_path] + crash_paths:
            if os.path.exists(p):
                logging.info(f"[RAMTEST] Előző TM5-napló törlése: {p}")
                os.remove(p)
        # A TM5 a parancssort nyersen olvassa ('Config File="…"'), ezért sztringként indul.
        cmd = f'"{exe}" Config File="{TM5_CONFIG_NAME}"'
        proc = subprocess.Popen(cmd, cwd=folder)
        self._stress_pids['tm5'] = proc.pid
        logging.info(f"[RAMTEST] TM5 elindítva: {cmd}, pid={proc.pid}")
        t0 = time.monotonic()
        state = {'last': {}}

        def check():
            text = ''
            if os.path.exists(log_path):
                with open(log_path, 'r', encoding='latin-1', errors='replace') as f:
                    text = f.read()
            parsed = parse_tm5_log(text)
            if parsed != state['last']:
                logging.info(f"[RAMTEST] TM5 napló: {parsed}")
                state['last'] = parsed
            if any(os.path.exists(p) for p in crash_paths):
                return {'state': 'errors', 'detail': 'a TM5 egy tesztszála összeomlott (Crash.log) - a TM5 szerint '
                                                      'ez legtöbbször a memória hibája'}
            if parsed['state'] == 'errors':
                return {'state': 'errors', 'detail': f"{parsed['errors']} hiba - {parsed['detail']}"}
            if parsed['state'] == 'ok':
                return {'state': 'ok', 'detail': parsed['detail']}
            if parsed['state'] in ('tool_error', 'stopped'):
                return {'state': 'tool_error', 'detail': parsed['detail']}
            # Nem emelt indításnál a TM5 magát emelt jogon újraindítja (élőben mérve), ezért a
            # kilépett PID önmagában nem jelent semmit - a képnév dönt.
            if proc.poll() is not None and not self._ramtest_image_running('TM5.exe'):
                logging.warning(f"[RAMTEST] TM5 kilépett eredmény nélkül, kód: {describe_exit_code(proc.returncode)}, "
                                f"napló: {text[-500:]!r}")
                return {'state': 'tool_error', 'detail': f'a TM5 eredmény nélkül kilépett ({parsed["detail"] or "üres napló"}, '
                                                         f'kód: {describe_exit_code(proc.returncode)})'}
            return None

        def progress():
            mins = int((time.monotonic() - t0) / 60)
            self.emit('task_progress', {'task': 'ramtest', 'indeterminate': True,
                                        'status': f"🧪 TM5 fut - {mins} perc ({state['last'].get('detail') or 'indul…'})"})

        deadline = t0 + mode['tm5_cycles'] * TM5_MAX_HOURS_PER_CYCLE * 3600
        box = mode.get('box_seconds')
        result = self._ramtest_wait(
            check, deadline, since, whea_seen, progress,
            box_end=t0 + box if box else None,
            box_result=lambda: {'state': 'ok', 'detail': f"{box // 60} perc hibamentesen (időkeretes gyorsteszt, "
                                                          f"nem teljes kör; {state['last'].get('detail') or '-'})"})
        self._ramtest_kill([proc.pid], ['TM5.exe'])
        self._stress_pids.pop('tm5', None)
        result['minutes'] = int((time.monotonic() - t0) / 60)
        return result

    # ------------------------------------------------------------------
    # 2. y-cruncher
    # ------------------------------------------------------------------
    def _ramtest_ycruncher(self, exe, mode, since, whea_seen):
        folder = os.path.dirname(exe)
        _, avail_gb = self._get_ram_gb()
        # Látható konzolban fut (a szervizes lássa, explicit user decision, 2026-10-26: "most is fut
        # vmi ram teszt de nem latom sehol"), az eredményt a konzol KÉPERNYŐJÉRŐL olvassuk, mint a
        # Linpacknél. A pause:1 nyitva tartja az ablakot a végén, hogy a záró sorok olvashatók
        # legyenek; utána mi zárjuk be. (Átirányított kimenetnél a szöveg csak kilépéskor jött meg.)
        args = ['pause:1' if a == 'pause:-2' else a for a in ycruncher_args(mode['yc_seconds'], avail_gb)]
        logging.info(f"[RAMTEST] y-cruncher indul (látható konzol): {exe} {' '.join(args)}")
        proc = subprocess.Popen([exe] + args, cwd=folder, creationflags=subprocess.CREATE_NEW_CONSOLE)
        self._stress_pids['ycruncher'] = proc.pid
        t0 = time.monotonic()
        seen = {'screen': '', 'errors': []}

        def check():
            screen = self._read_console_screen(proc.pid, max_rows=60)
            if screen:
                seen['screen'] = screen
                parsed = parse_ycruncher_output(screen)
                # A hibasorok kigördülhetnek a képernyőről: amit egyszer láttunk, megtartjuk.
                seen['errors'] += [e for e in parsed['errors'] if e not in seen['errors']]
                if seen['errors']:
                    return {'state': 'errors', 'detail': seen['errors'][-1]}
                if parsed['finished']:
                    return {'state': 'ok', 'detail': 'a teszt végigfutott, hiba nélkül'}
            rc = proc.poll()
            if rc is None:
                return None
            logging.warning(f"[RAMTEST] y-cruncher kilépett befejezés nélkül, kód: {describe_exit_code(rc)}")
            return {'state': 'tool_error', 'detail': f"kilépett befejezés nélkül, kód: {describe_exit_code(rc)}"}

        def progress():
            el = int(time.monotonic() - t0)
            tail = '\n'.join(seen['screen'].splitlines()[-6:])
            logging.info(f"[RAMTEST] y-cruncher {el} mp, képernyő vége:\n{tail}")
            self.emit('task_progress', {'task': 'ramtest', 'current': min(el, mode['yc_seconds']), 'total': mode['yc_seconds'],
                                        'status': f"🧪 y-cruncher fut - {el // 60} / {mode['yc_seconds'] // 60} perc"})

        try:
            result = self._ramtest_wait(check, t0 + mode['yc_seconds'] + YC_GRACE_SECONDS,
                                        since, whea_seen, progress)
        finally:
            logging.info(f"[RAMTEST] y-cruncher képernyő a végén:\n{seen['screen'][-1500:]}")
            self._ramtest_kill([proc.pid])
            self._stress_pids.pop('ycruncher', None)
        return result

    # ------------------------------------------------------------------
    # 3. HCI MemTest (több példány, a MemTestHelper mintájára)
    # ------------------------------------------------------------------
    def _ramtest_hci_start_instance(self, exe, mb):
        """Egy HCI-példány: indítás, az üdvözlő dialógus elnyomása, MB beírása, Start.
        Visszaad: (pid, főablak hwnd) vagy (pid, None), ha nem sikerült elindítani a tesztet."""
        user32 = self._stress_user32()
        proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
        # Az üdvözlő ablak OK-ja ("Welcome, New MemTest User") - minden indításkor jön.
        hwnd, ok_btn = self._find_pid_window_with_child_text(proc.pid, 'OK', timeout=30, exact=True)
        if ok_btn:
            user32.PostMessageW(ok_btn, BM_CLICK, 0, 0)
        # exact=True: az üdvözlő ablak szövege is tartalmazza, hogy "click [Start Testing]" (élőben ez
        # a Static került elő a gomb helyett, és a MB-mezőt a rossz ablakban kerestük).
        main, start_btn = self._find_pid_window_with_child_text(proc.pid, 'Start Testing', timeout=30, exact=True)
        if not start_btn:
            logging.warning(f"[RAMTEST] HCI pid={proc.pid}: nincs 'Start Testing' gomb")
            self._debug_dump_pid_windows(proc.pid, 'HCI start')
            return proc.pid, None
        edit = self._ramtest_dlg_item(main, HCI_EDIT_ID)
        buf = ctypes.create_unicode_buffer(str(mb))
        res = ctypes.c_size_t(0)
        user32.SendMessageTimeoutW(edit, WM_SETTEXT, 0, ctypes.addressof(buf), SMTO_ABORTIFHUNG, 3000, ctypes.byref(res))
        typed = self._ramtest_ctrl_text(edit)
        if typed != str(mb):
            logging.warning(f"[RAMTEST] HCI pid={proc.pid}: a MB-mező visszaolvasva '{typed}', nem '{mb}'")
            return proc.pid, None
        user32.PostMessageW(start_btn, BM_CLICK, 0, 0)
        logging.info(f"[RAMTEST] HCI pid={proc.pid}: {mb} MB beírva (visszaolvasva), Start Testing megnyomva")
        return proc.pid, main

    def _ramtest_hci_dismiss_popups(self, pid, main):
        """Az indítás utáni "Message for first-time users" és hasonló OK-s ablakok bezárása
        (a főablakot kivéve). Hibánál a HCI szintén dialógust dob - azt a hibaszám már
        megmutatta, mire ide érünk, ezért zárható. Forró ciklusban fut (2 mp, példányonként),
        ezért csak a TALÁLATOT naplózza - a _find_pid_window_with_child_text minden hívást naplózna."""
        user32 = self._stress_user32()
        found = []

        def _cb(hwnd, _lparam):
            owner = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and hwnd != main and user32.IsWindowVisible(hwnd):
                btn = self._find_child_by_text(hwnd, 'OK', exact=True)
                if btn:
                    found.append((hwnd, btn))
            return True

        user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)(_cb), 0)
        titles = []
        for hwnd, btn in found:
            title = self._window_title(hwnd)
            logging.info(f"[RAMTEST] HCI pid={pid}: felugró ablak bezárva: '{title}'")
            user32.PostMessageW(btn, BM_CLICK, 0, 0)
            titles.append(title)
        return titles

    def _ramtest_hci_layout(self, mains):
        """A HCI-ablakok rácsba rendezése a fő monitoron, eredeti méretben (hci_grid). Ha a sorok
        egymásra csúsznak, a FELSŐ sor kerül felülre: így minden ablak alja - az állapotsor a
        "% Coverage, N Errors" szöveggel - látszik. A verdikt a visszaolvasott hely."""
        HWND_TOP, SWP_NOSIZE, SWP_NOACTIVATE = 0, 0x0001, 0x0010
        user32 = self._stress_user32()
        prev_ctx = self._enter_per_monitor_dpi(user32)
        try:
            area = self._primary_work_area(user32)
            win, vis = self._frame_rects(user32, mains[0]) if mains else (None, None)
            if not area or not vis:
                logging.warning(f"[RAMTEST] HCI-ablakok elrendezése kihagyva (munkaterület={area}, ablak={vis})")
                return
            w, h = vis[2] - vis[0], vis[3] - vis[1]
            grid = hci_grid(len(mains), w, h, area)
            logging.info(f"[RAMTEST] HCI-ablakok elrendezése: {len(mains)} ablak {w}x{h}, munkaterület {area}, "
                         f"{max(g[2] for g in grid) + 1} sor x {max(g[3] for g in grid) + 1} oszlop")
            # Alulról felfelé, balról jobbra helyezünk el, mindig HWND_TOP-pal: a végén a 0. sor van legfelül.
            order = sorted(range(len(mains)), key=lambda i: (-grid[i][2], grid[i][3]))
            missed = []
            for i in order:
                hwnd, (x, y) = mains[i], grid[i][:2]
                win, vis = self._frame_rects(user32, hwnd)
                if not win:
                    missed.append(i)
                    continue
                user32.SetWindowPos(hwnd, HWND_TOP, x - (vis[0] - win[0]), y - (vis[1] - win[1]), 0, 0,
                                    SWP_NOSIZE | SWP_NOACTIVATE)
            time.sleep(0.5)
            missed += [i for i in range(len(mains)) if i not in missed
                       and not self._window_in_place(user32, mains[i], grid[i][0], grid[i][1])[0]]
            if missed:
                logging.warning(f"[RAMTEST] {len(missed)} HCI-ablak nem került a helyére: {sorted(missed)}")
            else:
                logging.info("[RAMTEST] Mind a HCI-ablakok a helyükön (visszaolvasva).")
        finally:
            if prev_ctx:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(prev_ctx))

    def _ramtest_hci(self, exe, mode, since, whea_seen):
        total_gb, avail_gb = self._get_ram_gb()
        plan = hci_plan((avail_gb or 0) * 1024, os.cpu_count())
        if not plan:
            return {'state': 'tool_error', 'detail': f'nincs elég szabad memória ({avail_gb} GB)'}
        n, per = plan
        target = mode['hci_coverage']
        logging.info(f"[RAMTEST] HCI terv: {n} példány x {per} MB (szabad {avail_gb:.1f} GB, {os.cpu_count()} szál), cél {target}%")
        self._ramtest_log(f'  ▶ {n} példány × {per} MB = {n * per / 1024:.1f} GB, cél: {target}% lefedettség')
        instances = []
        try:
            for i in range(n):
                if self._check_cancel():
                    return {'state': 'cancelled', 'detail': 'megszakítva'}
                pid, main = self._ramtest_hci_start_instance(exe, per)
                instances.append((pid, main))
                self._stress_pids[f'hci{i}'] = pid
                if not main:
                    return {'state': 'tool_error', 'detail': f'a(z) {i + 1}. HCI-példány tesztje nem indult el'}
            t0 = time.monotonic()
            last = {'cov': 0.0, 'rows': [], 'laid_out': False}

            def check():
                covs = []
                rows = []
                verdict = None
                for pid, main in instances:
                    popups = self._ramtest_hci_dismiss_popups(pid, main)
                    title = self._ramtest_ctrl_text(main) or ''
                    raw = self._ramtest_ctrl_text(self._ramtest_dlg_item(main, HCI_STATUS_ID)) or ''
                    rows.append(f'pid={pid}: {raw.strip() or "-"}')
                    if verdict:
                        continue
                    # Foglalási hiba: a teszt el sem indul, a példány örökre "üres" maradna (Build 396).
                    if hci_alloc_failed(raw) or any(hci_alloc_failed(t) for t in popups):
                        verdict = {'state': 'tool_error', 'detail': f'a pid={pid} HCI-példány nem kapott memóriát '
                                                                    f'({raw.strip() or popups[0]}) - nem a RAM hibája'}
                        continue
                    status = parse_hci_status(raw)
                    errs = max(parse_hci_title_errors(title) or 0, status[1] if status else 0)
                    if errs:
                        verdict = {'state': 'errors', 'detail': f'{errs} hiba a(z) pid={pid} példányban '
                                                                f'({status[0] if status else "?"}% lefedettségnél)'}
                    elif status is None:
                        if not self._stress_user32().IsWindow(main):
                            verdict = {'state': 'tool_error', 'detail': f'a pid={pid} HCI-példány bezárult'}
                        else:
                            covs.append(0.0)  # még nem írt állapotot
                    else:
                        covs.append(status[0])
                last['rows'] = rows
                if verdict:
                    logging.info(f"[RAMTEST] HCI állapot a döntéskor: {' | '.join(rows)}")
                    return verdict
                # Az elrendezés csak akkor, ha MINDEN példány már írja a lefedettséget: addig a
                # "Message for first-time users" bezárása a főablakát előre hozza, és felborítja
                # a z-sorrendet (élőben: az alsó sorok takarták a felsők állapotsorát).
                if not last['laid_out'] and all('coverage' in r.lower() for r in rows):
                    last['laid_out'] = True
                    try:
                        self._ramtest_hci_layout([main for _, main in instances])
                    except Exception:
                        logging.exception('[RAMTEST] HCI-ablakok elrendezése elszállt (a teszt fut tovább)')
                last['cov'] = min(covs)
                return {'state': 'ok', 'detail': f'{n} példány, mind ≥ {target}%, 0 hiba'} if last['cov'] >= target else None

            def progress():
                mins = int((time.monotonic() - t0) / 60)
                logging.info(f"[RAMTEST] HCI állapot ({mins} perc): {' | '.join(last['rows'])}")
                self.emit('task_progress', {'task': 'ramtest', 'current': min(last['cov'], target), 'total': target,
                                            'status': f"🧪 HCI MemTest - {last['cov']:.1f}% / {target}% ({mins} perc)"})

            deadline = t0 + HCI_MAX_HOURS_PER_100 * 3600 * target / 100
            box = mode.get('box_seconds')
            return self._ramtest_wait(
                check, deadline, since, whea_seen, progress,
                box_end=t0 + box if box else None,
                box_result=lambda: {'state': 'ok', 'detail': f"{box // 60} perc hibamentesen, {n} példány, "
                                                              f"{last['cov']:.1f}% lefedettség (időkeretes gyorsteszt)"})
        finally:
            self._ramtest_kill([pid for pid, _ in instances])
            for i in range(len(instances)):
                self._stress_pids.pop(f'hci{i}', None)

    # ------------------------------------------------------------------
    # a lánc
    # ------------------------------------------------------------------
    def start_ram_test(self, mode='gyors'):
        logging.info(f"[API] start_ram_test({mode})")
        if mode not in RAMTEST_MODES:
            self.emit('toast', {'message': f'❌ Ismeretlen RAM TEST mód: {mode}', 'type': 'error'})
            return
        cfg = RAMTEST_MODES[mode]

        def worker():
            self.emit('task_start', {'task': 'ramtest', 'title': f"RAM TEST ({cfg['label']})"})
            started = datetime.datetime.now()
            t0 = time.monotonic()
            self._lock_power_for_stress()
            try:
                self._ramtest_log(f"🧪 RAM TEST - {cfg['label']} mód: {cfg['desc']}")
                history = self._ramtest_whea(started - datetime.timedelta(days=RAMTEST_WHEA_HISTORY_DAYS))
                if history is None:
                    self._ramtest_log(f'⚠️ A WHEA-napló nem olvasható (előzmény, {RAMTEST_WHEA_HISTORY_DAYS} nap).')
                elif history:
                    self._ramtest_log(f'ℹ️ A teszt ELŐTT, az elmúlt {RAMTEST_WHEA_HISTORY_DAYS} napban {len(history)} WHEA-bejegyzés volt:')
                    for i, n, why in whea_summary(history):
                        self._ramtest_log(f'    WHEA {i} × {n}: {why}')
                else:
                    self._ramtest_log(f'✅ Az elmúlt {RAMTEST_WHEA_HISTORY_DAYS} napban nincs WHEA-bejegyzés.')

                self.emit('task_progress', {'task': 'ramtest', 'indeterminate': True, 'status': '🌐 Tesztprogramok (csomag) letöltése...'})
                stress_dir = self._download_stresstools(progress=self._task_dl_progress('ramtest'))
                if not stress_dir:
                    raise Exception('Hiba a programcsomag letöltésekor vagy kicsomagolásakor (nincs net / hibás ZIP).')
                found = self._find_stress_tool_exes(stress_dir, list(RAMTEST_TOOLS))

                whea_seen = {'n': 0}
                results = {}
                steps = [('tm5', self._ramtest_tm5), ('ycruncher', self._ramtest_ycruncher), ('hcimemtest', self._ramtest_hci)]
                for idx, (key, fn) in enumerate(steps, 1):
                    name = STEP_NAMES[key]
                    if self._check_cancel():
                        results[key] = {'state': 'cancelled', 'detail': 'megszakítva'}
                        continue
                    if any(r['state'] == 'errors' for r in results.values()):
                        results[key] = {'state': 'skipped', 'detail': 'kihagyva - egy korábbi teszt már hibát talált'}
                        logging.info(f"[RAMTEST] {name} kihagyva (korábbi hiba)")
                        continue
                    exe = found.get(key)
                    if not exe:
                        results[key] = {'state': 'tool_error', 'detail': 'nincs a programcsomagban (stresstools.zip)'}
                        self._ramtest_log(f'⚠️ {idx}/3 {name}: nincs a programcsomagban - kihagyva.')
                        continue
                    self.emit('task_progress', {'task': 'ramtest', 'phase': f'{idx}/3 · {name}'})
                    self._ramtest_log(f'\n▶ {idx}/3 {name} indul...')
                    try:
                        res = fn(exe, cfg, started, whea_seen)
                    except Exception as e:
                        logging.exception(f"[RAMTEST] {name} hiba")
                        res = {'state': 'tool_error', 'detail': f'kivétel: {e}'}
                    results[key] = res
                    icon = {'ok': '✅', 'errors': '❌', 'cancelled': '🛑'}.get(res['state'], '⚠️')
                    self._ramtest_log(f"{icon} {name}: {res.get('detail', '')}")
                    logging.info(f"[RAMTEST] {key} eredmény: {res}")

                whea = self._ramtest_whea(started)
                verdict = ramtest_verdict(results, whea)
                if verdict['ok'] and cfg.get('box_seconds'):
                    # Egy időkeretes teszt nem mondhatja ugyanazt, mint egy teljes kör (4. elv).
                    verdict['headline'] = ('✅ A gyorsteszt hibátlan, a WHEA-napló tiszta - ritkán jelentkező '
                                           'hibákra a Normál vagy az Egész éjszakás mód')
                mins = int((time.monotonic() - t0) / 60)
                self._ramtest_log('\n━━━━━━━━ ÖSSZEGZÉS A SZERVIZESNEK ━━━━━━━━')
                for key in STEP_NAMES:
                    r = results.get(key, {'state': 'skipped', 'detail': ''})
                    self._ramtest_log(f"  {STEP_NAMES[key]}: {r['state']} - {r.get('detail', '')}")
                if whea is None:
                    self._ramtest_log('  WHEA-napló: NEM OLVASHATÓ (nem ellenőrizhető)')
                elif not whea:
                    self._ramtest_log('  WHEA-napló a teszt alatt: tiszta')
                else:
                    for i, n, why in whea_summary(whea):
                        self._ramtest_log(f'  WHEA {i} × {n}: {why}')
                for a in verdict['advice']:
                    self._ramtest_log(f'  👉 {a}')
                self._ramtest_log(f"\n{verdict['headline']} (futásidő: {mins // 60} óra {mins % 60} perc)")
                logging.info(f"[RAMTEST] VERDIKT: {verdict['headline']} | {results} | whea={whea}")
                # Minden RAM TEST végén felmegy a napló (explicit user decision, 2026-10-27).
                self.upload_run_log('ramtest', f"RAM TEST ({cfg['label']}) - {verdict['headline']}, "
                                               f"{mins // 60} óra {mins % 60} perc")
                self.emit('task_complete', {'task': 'ramtest', 'status': verdict['headline']})
            except Exception as e:
                logging.exception('[RAMTEST] hiba')
                self.upload_run_log('ramtest', f"RAM TEST ({cfg['label']}) - HIBA: {e}")
                self.emit('task_error', {'task': 'ramtest', 'error': f'Hiba: {e}'})
            finally:
                self._ramtest_kill(images=[t[1][0] for t in RAMTEST_TOOLS.values()])
                self._restore_power_after_stress()

        self._safe_thread('ramtest', worker)
