"""DriverVarázsló GUI - Naplók nézet: a Drive-ra feltöltött debug-naplók visszatöltése.

MIÉRT VAN EZ A NÉZET (explicit user decision, 2026-08-27): a lánc végén minden gép naplója
felmegy a bolt Drive-jára (lásd app/logupload_core.py). Onnan viszont kézzel visszaszedni
öt lépés (Drive megnyitás -> mappa megkeresés -> letöltés -> kicsomagolás -> odamásolás),
és pont ez az öt lépés marad el, amikor egy hibát elemezni kellene. Ez a gomb egy
kattintásra összeszedi őket, KICSOMAGOLVA - egy olyan fájl, amit előbb ki kell tömöríteni,
a gyakorlatban nem lesz elolvasva.

A JELSZÓ: a végpont címe minden kiadott exe-ben benne van, tehát letöltésre nyitva hagyva
bárki lehúzhatná az ügyfélgépek naplóit. A jelszót a technikus gépeli be, SOSEM ágyazzuk
az exe-be - beégetve pont annyit érne, mint a jelszó nélküli végpont. A magyarázat és a
kiszolgáló oldal az appscript.txt-ben.

A letöltés helye `<app_data>\\letoltott_naplok\\<futás mappája>\\<napló>.log` - szándékosan
NEM az app-data gyökere, mert ott van az ÉLŐ napló, amit a feltöltő beolvas; idegen gépek
naplói közé keveredve előbb-utóbb rossz gép naplója menne fel.
"""

# === AUTO-IMPORTS ===
import os
import logging
import platform

from app import common
from app import benchmark_core, logupload_core
# === /AUTO-IMPORTS ===


class GuiLogsMixin:
    """Naplók nézet: a Drive-ra feltöltött naplók listázása és visszatöltése.
    A DriverToolApi része (összerakás: app/gui/api.py)."""

    def upload_run_log(self, task_id, outcome):
        """A futás naplójának feltöltése a bolt Drive-jára. KÖZÖS belépési pont.

        MIÉRT ITT (2026-09-03, explicit user decision: *"ne csak az autofix töltse fel a
        logokat a felhőbe hanem a manualis scan + telepites is"*): eddig CSAK az 1
        kattintásos fix töltött fel, tehát egy kézi telepítés után a napló a gépen
        maradt - és mire kiderült, hogy valamit elemezni kellene, a gép már az ügyfélnél
        volt. Pontosan az a helyzet, amiért a feltöltés egyáltalán létezik.

        A függvény azért a Naplók-nézet mixinjében él, és nem külön-külön a hívóknál,
        mert ez a HARMADIK hívási hely lett volna ugyanarra a nyolc sorra - és a
        duplikált logika, aminek egy példánya lemarad, ennek a projektnek a legrégebbi
        visszatérő hibája. Mindenki ugyanazt a `self`-et használja (lásd app/gui/api.py).

        SOHA NEM DOB ÉS SOHA NEM AKASZTJA MEG A HÍVÓT: a feltöltés a művelet UTÁN fut, a
        driverek addigra fent vannak, és a napló a gépen is ott marad - ez csak másolat.
        Ezért a hiba `ℹ️`-vel megy ki, nem `❌`-szel: egy hálózati hiba miatt egy
        egyébként tökéletes telepítés nem végződhet hibaüzenettel.

        Visszatérés: True, ha felment (a hívónak jellemzően nem kell)."""
        try:
            self.emit('task_progress', {'task': task_id, 'log': '\n☁️ Napló feltöltése a szerviz Drive-jára...'})
            ok_up, where = logupload_core.upload_logs(
                self._run, benchmark_core.resolve_endpoint(),
                machine_name=platform.node(),
                build=common.BUILD_NUMBER,
                outcome=outcome)
            if ok_up:
                self.emit('task_progress', {'task': task_id,
                                            'log': f'✅ Napló feltöltve{(" - " + where) if where else ""}.'})
            else:
                # Nem hiba a technikus szempontjából: a napló ott van a gépen is.
                self.emit('task_progress', {'task': task_id,
                                            'log': f'ℹ️ A napló feltöltése nem sikerült ({where}) - '
                                                   f'a gépen itt találod: {common._app_data_dir()}'})
            return bool(ok_up)
        except Exception as e:
            logging.warning(f"[LOGUP] A napló-feltöltés hívása hibára futott (nem kritikus): {e}")
            return False

    def get_log_download_dir(self):
        """A letöltési mappa útvonala - a nézet ezt írja ki (szinkron, olcsó hívás)."""
        return logupload_core.download_dir()

    def clear_debug_logs(self):
        """A gép SAJÁT debug-naplóinak törlése (az oldalsáv "napló törlése" gombja).

        SZINKRON, mert gyors (néhány fájl törlése) és a felület a visszatérési értékből
        írja ki, mennyit szabadított fel. A tényleges munka - és a nyitva tartott fájl
        kezelése - a magban: logupload_core.clear_logs()."""
        logging.info("[API] clear_debug_logs()")
        try:
            removed, freed, errors = logupload_core.clear_logs()
            return {'success': not errors, 'removed': removed,
                    'mb': round(freed / 1048576.0, 1), 'errors': errors}
        except Exception as e:
            logging.error(f"[LOGCLEAR] A napló törlése nem sikerült: {e}", exc_info=True)
            return {'success': False, 'removed': 0, 'mb': 0, 'errors': [str(e)]}

    def download_uploaded_logs(self, password, only_new=True, period='all'):
        """A Drive-ra feltöltött naplók letöltése kicsomagolva, háttérszálon.

        `only_new=True` esetén a már meglévő fájlokat átugorja - így a gomb ismételt
        megnyomása csak az újakat hozza le, nem tölt le újra mindent.
        `period`: időszak-szűrő (logupload_core.LOG_PERIODS: today/7/30/365/all)."""
        if period not in logupload_core.LOG_PERIODS:
            logging.warning(f"[LOGDL] Ismeretlen időszak ({period!r}) - az összes napló jön.")
            period = 'all'
        logging.info(f"[LOGDL] Időszak: {logupload_core.LOG_PERIODS[period][0]}")
        password = (password or '').strip()
        if not password:
            self.emit('toast', {'message': '⚠️ Add meg a jelszót!', 'type': 'warning'})
            return

        def worker():
            task = 'logdownload'
            self.emit('task_start', {'task': task, 'title': 'Naplók letöltése a Drive-ról'})
            try:
                url = benchmark_core.resolve_endpoint()
                dest = logupload_core.download_dir()

                # A korábban letöltött mappák átnevezése az új (dátum elöl) alakra. A
                # letöltés ELŐTT kell futnia, különben a `skip_existing` a régi néven
                # keresné a meglévőket, és mindent újra lehúzna.
                mig, _, mig_err = logupload_core.migrate_download_dir(dest)
                if mig:
                    self.emit('task_progress', {'task': task,
                                                'log': f'🔤 {mig} korábbi napló-mappa átnevezve az '
                                                       f'új, dátummal kezdődő névre.'})
                for e in mig_err[:5]:
                    self.emit('task_progress', {'task': task, 'log': f'   ⚠️ {e}'})

                self.emit('task_progress', {'task': task, 'log': '1/3 - Kapcsolódás a szerviz Drive-jához...'})
                rows, err = logupload_core.list_remote_logs(self._run, url, password)
                if err:
                    # A hibás jelszót külön mondjuk ki: ez a leggyakoribb eset, és a
                    # "nem sikerült" önmagában nem mondja meg, hogy elgépelte-e.
                    hiba = ('❌ Hibás jelszó - a helyes jelszót a Google Apps Script '
                            'LOG_READ_PASSWORD sora tartalmazza.') if 'jelsz' in err.lower() \
                        else f'❌ A napló-lista lekérése nem sikerült: {err}'
                    self.emit('task_progress', {'task': task, 'log': hiba})
                    self.emit('task_complete', {'task': task, 'status': '❌ Sikertelen'})
                    return
                if not rows:
                    self.emit('task_progress', {'task': task, 'log': 'ℹ️ Még nincs feltöltött napló a Drive-on.'})
                    self.emit('task_complete', {'task': task, 'status': 'Nincs letölthető napló'})
                    return

                label = logupload_core.LOG_PERIODS[period][0]
                all_count = len(rows)
                rows, no_date = logupload_core.filter_rows_by_period(rows, period)
                logging.info(f"[LOGDL] Időszak-szűrő ({label}): {len(rows)}/{all_count} napló, "
                             f"{no_date} dátum nélküli kimaradt.")
                if period != 'all':
                    self.emit('task_progress', {'task': task, 'log':
                              f'🗓️ Időszak: {label} - {len(rows)} napló a {all_count}-ból.'
                              + (f' ({no_date} napló dátuma nem állapítható meg, ezek kimaradtak - '
                                 f'az „Összes” letölti őket.)' if no_date else '')})
                if not rows:
                    self.emit('task_progress', {'task': task, 'log': f'ℹ️ A kiválasztott időszakban ({label}) nincs feltöltött napló.'})
                    self.emit('task_complete', {'task': task, 'status': f'Nincs napló ({label})'})
                    return
                self.emit('task_progress', {'task': task, 'log': f'2/3 - {len(rows)} napló letöltése ide: {dest}'})

                def progress(i, total, name):
                    self.emit('task_progress', {'task': task, 'log': f'   ⬇️ [{i}/{total}] {name}',
                                                'progress': int(i * 100 / max(1, total))})

                done, skipped, errors = logupload_core.download_logs(
                    self._run, url, password, rows, dest_dir=dest,
                    progress=progress, check_cancel=lambda: self._cancel_flag,
                    skip_existing=bool(only_new))

                self.emit('task_progress', {'task': task, 'log': f'3/3 - Kész: {done} új napló letöltve, {skipped} már megvolt.'})
                if errors:
                    # Nem némítjuk el: egy le nem jött napló pont az lehet, amit keresel.
                    self.emit('task_progress', {'task': task, 'log': f'⚠️ {len(errors)} napló nem jött le:'})
                    for e in errors[:10]:
                        self.emit('task_progress', {'task': task, 'log': f'   • {e}'})
                self.emit('task_progress', {'task': task, 'log': f'\n📁 A naplók helye: {dest}'})
                self.emit('task_complete', {'task': task,
                                            'status': f'{done} napló letöltve ({skipped} már megvolt)'})
                try:
                    if done or skipped:
                        os.startfile(dest)
                except Exception as e:
                    logging.debug(f"[LOGDL] A mappa megnyitása nem sikerült: {e}")
            except Exception as e:
                logging.error(f"[LOGDL] A napló-letöltés hibára futott: {e}", exc_info=True)
                self.emit('task_progress', {'task': task, 'log': f'❌ Hiba: {e}'})
                self.emit('task_complete', {'task': task, 'status': '❌ Hiba'})

        self._safe_thread('logdownload', worker)
