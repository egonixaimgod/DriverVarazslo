"""DriverVarázsló GUI - Rendszer Riport (PDF) nézet: HTML hardver-riport generálás
S.M.A.R.T. adatokkal (a teljes adatgyűjtés + HTML-generálás: app/report_core.py).

NAPLÓZÁS (2026-08-26, explicit user decision: "naplózzon mindent"): ez a nézet korábban
EGYETLEN sort írt a naplóba a belépéskor, aztán semmit a sikerig/hibáig. Terepen két gépen
"lefagyott" - és a logból nem lehetett megmondani, HOL, mert nem volt mit megmondani.

A fagyás oka pedig itt van, a második sorban: a S.M.A.R.T. adatokhoz a `smartctl` kell, az
pedig a stresstools.zip-ben van - amit a program SZÜKSÉG ESETÉN LETÖLT. Az a csomag
~621 MB, és egy frissen újratelepített gépen SOHA nincs meg. Vagyis a "Riport generálása"
gomb megnyomása után a program percekig tölt, miközben a felületen csak annyi látszik,
hogy a gomb felirata "⏳ Adatok letapogatása..." - ez pontosan úgy néz ki, mint egy fagyás.

Ezért minden fázis naplózódik ÉS a felületre is kimegy, a letöltés pedig MB-os haladást
mutat. A visszatérési érték alakja változatlan ({'success','path'}), hogy a ui.html
szerződése ne törjön."""

# === AUTO-IMPORTS ===
import os
import time
import logging
import traceback
from app import report_core
# === /AUTO-IMPORTS ===


class GuiReportMixin:
    """Rendszer Riport (PDF) nézet: HTML hardver-riport generálás S.M.A.R.T. adatokkal. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _report_progress_cb(self, task_id):
        """A stresstools.zip letöltésének haladása a riport-folyamat naplójába/képernyőjére.

        MIÉRT KELL: e nélkül a ~621 MB néma percekben telik el, és a technikus jogosan hiszi
        azt, hogy a program lefagyott (terepen két gépen pontosan ez történt)."""
        state = {'last': 0.0}

        def cb(phase, done, total):
            now = time.monotonic()
            is_final = bool(total) and done >= total
            if now - state['last'] < 2.0 and not is_final:
                return
            state['last'] = now
            if phase == 'download':
                if total:
                    pct = int(done * 100 / total)
                    txt = f'   ⬇️ Diagnosztikai eszközök letöltése: {done / 1048576:.0f}/{total / 1048576:.0f} MB ({pct}%)'
                else:
                    txt = f'   ⬇️ Diagnosztikai eszközök letöltése: {done / 1048576:.0f} MB'
            else:
                txt = f'   📦 Kicsomagolás: {done}/{total} fájl' if total else '   📦 Kicsomagolás...'
            logging.info(f"[REPORT] {txt.strip()}")
            self.emit('task_progress', {'task': task_id, 'log': txt})

        return cb

    def get_report_defaults(self):
        """A riport-kártya alapállapota: USB-s lemezről fut-e a Windows. Ha igen, a
        "teszt-SSD" pipa alapból bepipálva jelenik meg (2026-09-25, explicit user decision).

        Csak a DETEKTÁLT USB kapcsolja be: ha a rendszerlemez nem azonosítható, a pipa KI
        marad - egy tévesen bepipált kapcsoló az ügyfél saját lemezét venné ki a riportból."""
        disk = report_core.find_system_disk(self._run)
        usb = report_core.system_disk_is_usb(disk)
        logging.info(f"[REPORT] Alapállapot: a futó Windows lemeze "
                     f"{'USB-s -> a teszt-SSD pipa BEPIPÁLVA' if usb else ('nem USB-s' if usb is False else 'nem azonosítható')}"
                     f"{(' (' + disk.get('model', '') + ', busz=' + disk.get('bus', '') + ')') if disk else ''}")
        return {'system_disk_usb': bool(usb), 'system_disk_known': usb is not None,
                'model': (disk or {}).get('model') or '', 'bus': (disk or {}).get('bus') or ''}

    def generate_system_report(self, note=None, skip_system_disk=False):
        """Rendszer Riport generálása. Visszatérés: {'success': True, 'path': <html>}.

        Szinkron marad (a ui.html a visszatérési értékből veszi az útvonalat), de minden
        fázist naplóz és a felületre is kiír - lásd a modul fejlécét.

        skip_system_disk: a futó Windows lemeze (a szerviz USB-s teszt-SSD-je) nem kerül
        a riport háttértárai közé (2026-09-24, explicit user decision)."""
        task = 'report'
        skip_system_disk = bool(skip_system_disk)
        logging.info(f"[REPORT] === Rendszer Riport generálás INDUL (megjegyzés: "
                     f"{'igen' if note else 'nem'}, rendszerlemez kihagyása: "
                     f"{'IGEN' if skip_system_disk else 'nem'}) ===")
        t_all = time.monotonic()
        self.emit('task_start', {'task': task, 'title': 'Rendszer Riport készítése'})
        try:
            # 1) S.M.A.R.T. eszköz (smartctl) - ehhez kell a stresstools csomag.
            self.emit('task_progress', {'task': task, 'log': '🔧 Diagnosztikai eszközök ellenőrzése (S.M.A.R.T. olvasáshoz)...', 'indeterminate': True})
            logging.info("[REPORT] 1/4 - stresstools csomag ellenőrzése/letöltése (smartctl miatt)...")
            t0 = time.monotonic()
            stress_dir = self._download_stresstools(progress=self._report_progress_cb(task))
            logging.info(f"[REPORT] 1/4 kész: stresstools mappa={stress_dir!r} ({time.monotonic() - t0:.1f}s)")

            t0 = time.monotonic()
            smartctl_exe = report_core.find_smartctl(stress_dir)
            if smartctl_exe:
                logging.info(f"[REPORT] 2/4 - smartctl megvan: {smartctl_exe} ({time.monotonic() - t0:.1f}s)")
                self.emit('task_progress', {'task': task, 'log': '✅ S.M.A.R.T. olvasó megvan.'})
            else:
                # NEM végzetes: a riport S.M.A.R.T. adatok nélkül is elkészül. De ki KELL
                # mondani, különben a hiányzó lemez-adatok némán tűnnek el a riportból.
                logging.warning(f"[REPORT] 2/4 - a smartctl NEM található a(z) {stress_dir!r} mappában - "
                                f"a riport S.M.A.R.T. adatok NÉLKÜL készül el.")
                self.emit('task_progress', {'task': task, 'log': '⚠️ A S.M.A.R.T. olvasó nem található - a riport a lemez-egészség adatai nélkül készül.'})

            # 2) Adatgyűjtés + HTML. Ez a leghosszabb szakasz (WMI-lekérdezések, smartctl
            # futtatás lemezenként), gyenge gépen 1-2 perc is lehet.
            self.emit('task_progress', {'task': task, 'log': '🔎 Hardver-adatok begyűjtése és a riport összeállítása (gyengébb gépen 1-2 perc)...', 'indeterminate': True})
            logging.info("[REPORT] 3/4 - adatgyűjtés + HTML-generálás indul...")
            t0 = time.monotonic()
            info = {}
            final_path = report_core.generate_system_report(self._run, smartctl_exe, note,
                                                            skip_system_disk=skip_system_disk, report=info)
            gen_s = time.monotonic() - t0
            if skip_system_disk:
                # Ha nem tudtuk azonosítani, NEM hagytunk ki semmit - ezt ki kell mondani,
                # különben a technikus azt hinné, a riportban nincs benne a teszt-SSD.
                if info.get('system_disk_unknown'):
                    self.emit('task_progress', {'task': task, 'log': '⚠️ A futó rendszer lemezét nem sikerült azonosítani - a riport minden háttértárat tartalmaz.'})
                elif info.get('excluded_disks'):
                    for d in info['excluded_disks']:
                        self.emit('task_progress', {'task': task, 'log': f'🧪 Kihagyva a riportból (a futó rendszer lemeze): {d}'})
                elif smartctl_exe:
                    self.emit('task_progress', {'task': task, 'log': '⚠️ A futó rendszer lemeze nem szerepelt a S.M.A.R.T. listában - nem kellett kihagyni semmit.'})
            try:
                size_kb = os.path.getsize(final_path) / 1024.0
            except Exception:
                size_kb = -1
            logging.info(f"[REPORT] 3/4 kész: {final_path} ({size_kb:.0f} KB, {gen_s:.1f}s)")

            # A "Bolti nyomtatóval nyomtatás" gomb (print_via_store_printer) ebből
            # tudja, melyik fájlt kell kinyomtatnia - nem kér újra útvonalat a UI-tól.
            self._last_report_path = final_path
            logging.info(f"[REPORT] 4/4 - kész. Teljes idő: {time.monotonic() - t_all:.1f}s")
            self.emit('task_progress', {'task': task, 'log': f'✅ Riport elkészült: {final_path}'})
            self.emit('task_complete', {'task': task, 'status': '✅ Riport elkészült'})
            return {'success': True, 'path': final_path}
        except Exception as e:
            logging.error(f"[REPORT] HIBA a riport generálásánál ({time.monotonic() - t_all:.1f}s után): {e}")
            logging.error(traceback.format_exc())
            self.emit('task_error', {'task': task, 'error': str(e)})
            raise Exception(str(e))
