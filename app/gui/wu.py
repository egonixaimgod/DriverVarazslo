"""DriverVarázsló GUI - Windows Update (Operációs rendszer nézet): driver-push tiltás/
engedélyezés, szüneteltetés (1 hét / N nap / 10 év), feloldás, szolgáltatás-javítás.
A közös logika: app/wusettings_core.py.

2026-11-03 (explicit user decision): MINDEN gomb ugyanazon a folyamaton megy át
(`_wu_action`): a kért beállítás beírása + visszaolvasása -> a közös WU-javítás
(szolgáltatások le, beragadt szemét törlése, szolgáltatások vissza; keresést SOHA nem
indít) -> a Windows TÉNYLEGES állapotának visszaolvasása. Ha a Windows még nem vette át
(mérve: csak újraindítás után veszi át), vagy zárolt szemét maradt: a program kiírja,
hogy 10 mp múlva újraindítja a gépet, bejelentkezés után egy ütemezett feladat elindítja,
és a `finish_wu_after_reboot` befejezi + ellenőrzi. „✅ Kész” csak visszaolvasott,
egyező állapotra jön."""

# === AUTO-IMPORTS ===
import os
import sys
import shutil
import time
import logging
from app import wusettings_core
from app.common import _app_exe_path, logon_task_ps
# === /AUTO-IMPORTS ===

WU_FINISH_TASK = 'DriverVarazsloWuFinish'
WU_FINISH_FLAG = '--wu-finish'
WU_REBOOT_COUNTDOWN = 10          # mp, a Mégse gomb addig megfogja
WU_FINISH_WAIT = 90               # mp: újraindítás után eddig várunk, hogy a Windows átvegye


class GuiWuMixin:
    """Windows Update nézet. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _set_wu_pause(self, pause=True):
        wusettings_core.set_wu_pause(self._run, pause)

    # ================================================================
    # ÁLLAPOT
    # ================================================================
    def check_wu_status(self):
        """A kiírás a Windows TÉNYLEGES állapotából (PolicyState) jön; ha az eltér a
        beállítottól, azt kimondja. Visszatérés: {status, color, lines[]}."""
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown', 'lines': []}
        try:
            st = wusettings_core.read_wu_status(self._run)
            desired = {'paused_until': st['paused_until'], 'exclude_drivers': st['policy_disabled']}
            eff = st['effective']
            mism = wusettings_core.state_mismatches(desired, eff)
            pending = wusettings_core.load_pending()
            lines = []

            src = eff if eff is not None else desired
            pu = (src.get('paused_until') or '')[:10]
            lines.append(f"⏸️ Szüneteltetés: {'szüneteltetve ' + pu + '-ig' if pu else 'nincs szüneteltetve'}"
                         + ('' if eff is not None else ' (beállítás szerint; a Windows állapota nem olvasható)'))
            if st['policy_disabled'] and st['search_disabled']:
                drv = 'LETILTVA (WU-ajánlat és eszköztelepítés is)'
            elif st['policy_disabled']:
                drv = 'részben letiltva (csak a WU-ajánlat; az eszköztelepítés a WU-ról keres)'
            elif st['search_disabled']:
                drv = 'részben letiltva (csak az eszköztelepítés; a WU ajánlhat drivert)'
            else:
                drv = 'engedélyezve'
            lines.append(f'🧩 Driver-frissítés: {drv}')
            for m in mism:
                lines.append(f'⚠️ {m}')
            if mism:
                lines.append('➡️ Újraindítás után lép életbe - vagy nyomd meg a „WU Javítás” gombot, az elvégzi.')
            for r in st['reboot_flags']:
                lines.append(f'🔁 {r}')
            if pending:
                lines.append(f"⏳ Újraindításra vár: {pending.get('action', 'WU-művelet')} "
                             f"(bejelentkezés után a program magától befejezi)")

            if st['service_disabled']:
                result = {'status': 'Windows Update szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif mism:
                result = {'status': '⚠️ A Windows még NEM vette át a beállítást - újraindítás kell', 'color': 'warning'}
            elif pu:
                result = {'status': f'SZÜNETELTETVE {pu}-ig · Driverek: {"LETILTVA" if st["policy_disabled"] else "engedélyezve"}',
                          'color': 'warning'}
            else:
                color = 'disabled' if st['policy_disabled'] else 'enabled'
                result = {'status': f'Fut · Driver-frissítés: {"LETILTVA" if st["policy_disabled"] else "engedélyezve"}',
                          'color': color}
            result['lines'] = lines
            # A CLI régi kulcsa.
            result['pause_status'] = lines[0]
            logging.info(f"[WU_STATUS] {result['status']} | {' | '.join(lines)}")
            return result
        except Exception as e:
            logging.error(f"[WU_STATUS] Hiba: {e}", exc_info=True)
            return {'status': 'Ismeretlen (az állapot nem olvasható)', 'color': 'unknown', 'lines': []}

    # ================================================================
    # A KÖZÖS GOMB-FOLYAMAT
    # ================================================================
    def _wu_offline_refused(self):
        if self.target_os_path:
            self.emit('toast', {'message': '❌ A Windows Update beállítások csak élő rendszeren módosíthatók!',
                                'type': 'error'})
            return True
        return False

    def _wu_action(self, task, title, apply_fn, full_reset=False):
        """apply_fn(log) -> (ok, szöveg): a kért beállítás beírása, visszaolvasva."""
        def worker():
            log = lambda m: self.emit('task_progress', {'task': task, 'log': m})
            self.emit('task_start', {'task': task, 'title': title})
            self.emit('task_progress', {'task': task, 'log': f'{title}...', 'indeterminate': True})
            logging.info(f"[WU] {title}: indul")
            ok, text = apply_fn(log)
            if not ok:
                logging.error(f"[WU] {title}: a beállítás NEM íródott be: {text}")
                self.emit('task_complete', {'task': task, 'status': f'❌ {text}'})
                return
            log(text)
            rep = wusettings_core.repair_wu(self._run, log, full_reset=full_reset)
            self._wu_conclude(task, title, rep, finishing=False)
        self._safe_thread(task, worker)

    def _wu_conclude(self, task, title, rep, finishing):
        """A verdikt: a Windows tényleges állapota + a javítás eredménye. Újraindítás kell
        -> (első körben) 10 mp-es visszaszámlálás + befejező feladat; újraindítás után nincs
        újabb újraindítás, csak őszinte jelentés."""
        log = lambda m: self.emit('task_progress', {'task': task, 'log': m})
        desired = wusettings_core.desired_state()
        eff = wusettings_core.read_effective_state()
        mism = wusettings_core.state_mismatches(desired, eff)
        reasons = mism + rep['reboot_reasons'] + wusettings_core.wu_reboot_flags()
        state_txt = wusettings_core.describe_state(desired, eff)
        logging.info(f"[WU] {title}: beállítva={desired}, Windows szerint={eff}, újraindítás oka={reasons}, "
                     f"problémák={rep['problems']}")

        if not reasons or finishing:
            if finishing:
                self._wu_unregister_finish()
                wusettings_core.clear_pending()
            if reasons:
                for r in reasons:
                    log(f'⚠️ {r}')
                log('⚠️ Az újraindítás után sem állt helyre minden - a fenti tételeket nézd meg (a napló is tartalmazza).')
                status = f'⚠️ Nem minden lépett életbe · Windows szerint: {state_txt}'
            elif rep['problems']:
                status = f"⚠️ A beállítás életben ({state_txt}), de: {'; '.join(rep['problems'])}"
            else:
                status = f'✅ Kész · Windows szerint: {state_txt}'
            log(status)
            self.emit('task_complete', {'task': task, 'status': status})
            return

        for r in reasons:
            log(f'🔁 Újraindítás kell: {r}')
        try:
            wusettings_core.save_pending(title, desired)
            registered = self._wu_register_finish()
        except Exception as e:
            logging.error(f"[WU] A befejezés nem ütemezhető: {e}", exc_info=True)
            registered = False
        if not registered:
            status = ('⚠️ Újraindítás kell, de az automatikus befejezés nem ütemezhető - indítsd újra a gépet '
                      'kézzel, utána nyisd meg a programot: indításkor befejezi.')
            log(status)
            self.emit('task_complete', {'task': task, 'status': status})
            return
        log(f'🔁 A gép {WU_REBOOT_COUNTDOWN} másodperc múlva ÚJRAINDUL. Bejelentkezés után a program magától '
            f'elindul, befejezi és ellenőrzi. (Mégse: „Folyamat Leállítása”, CLI-ben Ctrl+C.)')
        for i in range(WU_REBOOT_COUNTDOWN * 2):
            if getattr(self, '_cancel_flag', False):
                log('❗ Újraindítás megszakítva. A beállítás a következő újraindításkor lép életbe; '
                    'bejelentkezés után a program magától befejezi.')
                self.emit('task_complete', {'task': task, 'status': '⏸️ Újraindítás elhalasztva - a következő újraindítás után fejeződik be'})
                return
            if i % 2 == 0:
                self.emit('task_progress', {'task': task, 'status': f'🔁 Újraindítás {WU_REBOOT_COUNTDOWN - i // 2} mp múlva...'})
            time.sleep(0.5)
        logging.warning(f"[WU] {title}: újraindítás (shutdown /r /t 0 /f), ok: {reasons}")
        self.emit('task_complete', {'task': task, 'status': '🔁 Újraindítás...'})
        self._run(['shutdown', '/r', '/t', '0', '/f'])

    def _wu_register_finish(self):
        """A bejelentkezéskori befejező feladat (a fix láncáéval azonos beállításokkal),
        visszaolvasva. Ideiglenes mappából futó exét a Public mappába másol (mint a fix)."""
        flag = WU_FINISH_FLAG + (' --cli' if '--cli' in sys.argv else '')
        exe_path = _app_exe_path()
        if os.environ.get('TEMP', '!!').lower() in exe_path.lower():
            try:
                safe = os.path.join(os.environ.get('PUBLIC', r'C:\Users\Public'),
                                    'DriverVarazslo_Resume.exe' if getattr(sys, 'frozen', False) else 'DriverVarazslo_Resume.py')
                shutil.copy2(exe_path, safe)
                exe_path = safe
            except Exception as e:
                logging.error(f"[WU] Biztonsági másolat a Public mappába sikertelen: {e}")
        if getattr(sys, 'frozen', False):
            exec_path, args = exe_path, flag
        else:
            exec_path, args = sys.executable, f'"{exe_path}" {flag}'
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                         logon_task_ps(WU_FINISH_TASK, exec_path, args)], ok_codes=(0, 1))
        out = getattr(res, 'stdout', '') or ''
        ok = any(ln.strip().startswith('ARGS=') and flag in ln for ln in out.splitlines())
        if ok:
            logging.info(f"[WU] A befejező feladat ({WU_FINISH_TASK}) regisztrálva: {args}")
        else:
            logging.error(f"[WU] A befejező feladat NEM regisztrálható (kód={getattr(res, 'returncode', None)}): {out.strip()[:400]}")
        return ok

    def _wu_unregister_finish(self):
        self._run(["powershell", "-NoProfile", "-Command",
                   f'Unregister-ScheduledTask -TaskName "{WU_FINISH_TASK}" -Confirm:$false -ErrorAction SilentlyContinue'],
                  ok_codes=(0, 1))

    def wu_finish_pending(self):
        """Indításkor: van-e újraindítás után befejezendő WU-művelet (és tényleg volt-e
        azóta újraindítás). Nem eldönthető uptime-nál is befejezzük (a befejezés nem árt)."""
        p = wusettings_core.load_pending()
        if not p:
            return False
        rb = wusettings_core.rebooted_since(p.get('saved_at'))
        logging.info(f"[WU_PENDING] Befejezésre váró WU-művelet: {p.get('action')} | újraindult azóta: {rb}")
        return rb is not False

    def finish_wu_after_reboot(self):
        """Újraindítás után: a beállítás újra-ellenőrzése (ha elveszett, újra beírjuk), a
        megmaradt szemét takarítása, majd a Windows tényleges állapotának visszaolvasása
        (legfeljebb WU_FINISH_WAIT mp-ig várva, hogy átvegye). Újraindítást NEM kér."""
        logging.info("[API] finish_wu_after_reboot()")
        pending = wusettings_core.load_pending()
        if not pending:
            self._wu_unregister_finish()
            return False
        task = 'wu_finish'
        title = f"WU befejezése újraindítás után - {pending.get('action', '')}"

        def worker():
            log = lambda m: self.emit('task_progress', {'task': task, 'log': m})
            self.emit('task_start', {'task': task, 'title': title})
            self.emit('task_progress', {'task': task, 'log': 'A kért beállítás ellenőrzése...', 'indeterminate': True})
            if not wusettings_core.reassert_pending(self._run, pending, log):
                log('⚠️ A kért beállítás nem írható vissza (lásd a naplót).')
            rep = wusettings_core.repair_wu(self._run, log)
            log('⏳ Várakozás, hogy a Windows átvegye a beállítást...')
            t0 = time.monotonic()
            while time.monotonic() - t0 < WU_FINISH_WAIT:
                if not wusettings_core.state_mismatches(wusettings_core.desired_state(),
                                                        wusettings_core.read_effective_state()):
                    break
                time.sleep(3)
            logging.info(f"[WU_PENDING] Várakozás vége {time.monotonic() - t0:.0f} mp után.")
            self._wu_conclude(task, title, rep, finishing=True)
        self._safe_thread(task, worker)
        return True

    # ================================================================
    # GOMBOK
    # ================================================================
    def disable_wu(self):
        logging.info("[API] disable_wu()")
        if self._wu_offline_refused():
            return

        def apply(log):
            ok = wusettings_core.set_wu_driver_policy(self._run, disabled=True)
            return ok, ('✅ Driver-frissítés letiltása beírva (ExcludeWUDriversInQualityUpdate=1, SearchOrderConfig=0).'
                        if ok else 'A driver-letiltás NEM íródott be (visszaolvasva eltér) - fut a program rendszergazdaként?')
        self._wu_action('disable_wu', 'WU driver-frissítés letiltása', apply)

    def enable_wu(self):
        logging.info("[API] enable_wu()")
        if self._wu_offline_refused():
            return

        def apply(log):
            unpaused = wusettings_core.remove_pause()
            drv = wusettings_core.set_wu_driver_policy(self._run, disabled=False)
            ok = unpaused and drv
            return ok, ('✅ Szünet feloldva, driver-frissítés engedélyezve.' if ok
                        else 'A szünet feloldása / a driver-engedélyezés NEM íródott be (lásd a naplót).')
        self._wu_action('enable_wu', 'WU driver-engedélyezés + teljes reset', apply, full_reset=True)

    def restart_wu(self):
        logging.info("[API] restart_wu()")
        if self._wu_offline_refused():
            return

        def apply(log):
            st = wusettings_core.describe_state(wusettings_core.desired_state(), None).replace(
                ' (a Windows saját állapota nem olvasható)', '')
            return True, f'ℹ️ A beállítások változatlanok maradnak ({st}); frissítés-keresés NEM indul.'
        self._wu_action('restart_wu', 'WU javítás (szolgáltatások újraindítása + takarítás)', apply)

    def pause_wu(self, days, extend=None):
        """days: 1..3650. extend=None: a 7 napos gomb a meglévő szünethez ad (halmozható),
        minden más mostantól számol."""
        logging.info(f"[API] pause_wu(days={days}, extend={extend})")
        if self._wu_offline_refused():
            return
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= wusettings_core.WU_PAUSE_MAX_DAYS:
            self.emit('toast', {'message': f'❌ A napok száma 1 és {wusettings_core.WU_PAUSE_MAX_DAYS} között legyen!',
                                'type': 'error'})
            return
        if extend is None:
            extend = days == 7

        def apply(log):
            end = wusettings_core.write_pause_until(wusettings_core.pause_end_for(days, extend))
            return (end is not None,
                    f'✅ Szünet beírva: {end[:10]}-ig' if end else 'A szünet NEM íródott be (lásd a naplót).')
        self._wu_action('pause_wu', f'WU szüneteltetés ({days} nap)', apply)

    def resume_wu(self):
        logging.info("[API] resume_wu()")
        if self._wu_offline_refused():
            return

        def apply(log):
            ok = wusettings_core.remove_pause()
            return ok, ('✅ Szünet törölve (a driver-beállítás változatlan).' if ok
                        else 'A szünet NEM törölhető (lásd a naplót).')
        self._wu_action('resume_wu', 'WU szüneteltetés feloldása', apply)
