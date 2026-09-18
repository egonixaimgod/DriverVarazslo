"""DriverVarázsló GUI - Driverek kezelése nézet: listázás (online/offline) és törlés
(a közös listázó/parzoló/törlő logika: app/drivers_core.py)."""

# === AUTO-IMPORTS ===
import os
import threading
import time
import logging
import traceback
from app import drivers_core
# === /AUTO-IMPORTS ===


class GuiDriversMixin:
    """Driverek kezelése nézet: listázás (online/offline) és törlés. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def get_printer_driver_infs(self, known_drivers=None):
        """Melyik listázott csomagok számítanak NYOMTATÓ-drivernek (a lista szűréséhez).

        MIÉRT UGYANAZ A FÜGGVÉNY, AMIT AZ AUTOFIX HASZNÁL (`wu_core._collect_printer_protection`
        + `_is_printer_protected`): ha ez a szűrő saját logikát kapna, a kézi lista és az
        AutoFix védelme előbb-utóbb eltérne, és a technikus itt kitörölne valamit, amit a
        fix megvédett volna. Az OSZTÁLY-alapú egyeztetés önmagában KEVÉS - terepen bizonyított,
        hogy a multifunkciós nyomtatók csomagjai USB/Ports/SYSTEM osztályban szóródnak szét
        (`mvusbews.inf`, `hppscnd.inf`, `hpbuio70l.inf`), ezért kell a jelenlévő nyomtatási
        komponensek tényleges INF-jei + a gyártó-kulcsszavak is.

        `known_drivers`: a felület MÁR BETÖLTÖTT listája - ugyanaz a fogás, mint az AutoFix
        törlés-előnézeténél: egy friss `dism /Get-Drivers` 15-50 mp, és a felületnek pont
        ugyanaz az adat már a kezében van.

        A FELISMERÉS MAGÁRÓL A CSOMAGRÓL DÖNT, NEM ARRÓL, HOGY BE VAN-E DUGVA A NYOMTATÓ
        (2026-09-17, explicit user decision). A szervizben lévő laptop mellett soha nincs
        ott az ügyfél nyomtatója, tehát a "melyik INF-et használja egy jelenlévő nyomtató"
        jel alapesetben hiányzik - erre építeni azt jelentené, hogy a technikus kitörli a
        behozott gép nyomtató-driverét CSAK AZÉRT, mert a nyomtató otthon maradt.
        `wu_core.identify_printer_packages` ezért az INF-ből dolgozik (osztály, kulcsszó,
        azonos fizikai eszköz) - a részletes indoklás és a mért számok ott.

        MIÉRT NEM A `_is_printer_protected` FUT ITT (ami a fix törlés-védelme): annak van
        egy negyedik ága is, a puszta GYÁRTÓNÉV-egyezés, és a gépen mérve abból 70 SAMSUNG
        TELEFON-driver esett a "nyomtató" halmazba (ADB, modem, COM-port, hálókártya) -
        csak mert van egy Samsung nyomtató is a gépen. A TÖRLÉSNÉL ez a túlvédés indokolt
        és marad (egy elvesztett nyomtató-driver a nyomtató nélkül nem telepíthető vissza),
        a LISTÁBAN viszont nem véd semmit, csak elveszi a technikustól a látványt."""
        logging.info(f"[API] get_printer_driver_infs({len(known_drivers or [])} csomag)")
        try:
            from app.wu_core import collect_printer_packages
            drivers = known_drivers or []
            if not drivers:
                logging.info("[PRINTER-FILTER] Nincs betöltött driver-lista, nincs mit szűrni.")
                return {'published': [], 'count': 0}
            # UGYANAZ AZ EGY FÜGGVÉNY, amit az AutoFix törlés-védelme is hív - így a két
            # képernyő szerkezetileg nem mondhat mást ugyanarról a csomagról.
            found = collect_printer_packages(self._run, drivers)
            hits = [d for d in drivers if (d.get('published') or '').lower() in found]
            logging.info(f"[PRINTER-FILTER] {len(hits)} nyomtató-driver a(z) {len(drivers)} "
                         f"csomagból elrejtve.")
            logging.debug(f"[PRINTER-FILTER] Tételek: "
                          f"{[d.get('original') or d.get('published') for d in hits]}")
            return {'published': [d.get('published', '') for d in hits], 'count': len(hits)}
        except Exception as e:
            # Fail-safe: hiba esetén NEM rejtünk el semmit. Egy néma szűrő, ami többet rejt
            # el a kelleténél, rosszabb, mint a szűretlen lista.
            logging.warning(f"[PRINTER-FILTER] A nyomtató-driverek felderítése sikertelen: {e}")
            return {'published': [], 'count': 0, 'error': str(e)}

    def get_driver_usage(self, known_drivers=None):
        """MELYIK CSOMAGOT HASZNÁLJA MOST A GÉP - a Driverek nézet csoportosításához.

        MIÉRT (explicit user decision, 2026-09-17): a technikus eddig VAKON törölt. A
        terepi eset, amiből ez lett: egy távoli asztali program ("ninja...") drivere
        ment el, és ezzel a távoli elérés is - a listából semmi nem árulta el, hogy azt
        a drivert a gép épp használja.

        UGYANAZT A MAGOT hívja, amit az 1 kattintásos fix törlési előnézete
        (`app/driverusage_core.py`) - ez a lényeg benne. Ha a két képernyő külön logikán
        futna, a technikus itt kitörölne valamit, amit ott védettnek látott.

        EZ NEM SZŰRŐ: semmit nem rejt el és nem tilt le, csak besorol és megindokol
        (lásd a CLAUDE.md "MINDEN DRIVERT LEHESSEN TÖRÖLNI" szabályát). A törlés
        változatlanul mindenre megy, amit a technikus kipipál.

        OFFLINE MÓDBAN NEM FUT: a futó gép eszközei és szolgáltatásai semmit nem
        mondanak egy MÁSIK lemezen lévő Windows csomagjairól - az ottani állapotot ide
        kiírni néma hazugság lenne. Ilyenkor 'unknown' marad minden sor."""
        logging.info(f"[API] get_driver_usage({len(known_drivers or [])} csomag)")
        if self.target_os_path:
            logging.info("[USAGE] Offline mód - a használat-felderítés kihagyva "
                         f"(célpont: {self.target_os_path}).")
            return {'usage': {}, 'counts': {}, 'offline': True}
        try:
            from app import driverusage_core as duc
            # A csomaglistát a felülettől vesszük át, ha már betöltötte: a dism
            # önmagában 15-77 mp, és pontosan ugyanazt adná, ami a képernyőn van
            # (ugyanaz a fogás, mint a törlési előnézetnél).
            pkgs = [d for d in (known_drivers or []) if isinstance(d, dict) and d.get('published')]
            ctx = duc.collect_usage_context(self._run, pkgs or None)
            usage = ctx['usage'] if ctx else {}
            counts = duc.summarize_counts(usage)
            if not usage:
                # Megkülönböztetjük az "elbukott felderítést" a "minden használatlan"
                # eredménytől: az előbbi alapján TILOS törlési döntést hozni.
                logging.warning("[USAGE] A felderítés nem adott eredményt - a felület "
                                "'ismeretlen' állapotot mutat, nem 'nem használt'-at.")
                return {'usage': {}, 'counts': {}, 'error': 'A használat-felderítés nem futott le.'}
            out = {'usage': usage, 'counts': counts}
            out.update(self._build_machine_map(ctx, pkgs))
            return out
        except Exception as e:
            logging.warning(f"[USAGE] A használat-felderítés sikertelen: {e}", exc_info=True)
            return {'usage': {}, 'counts': {}, 'error': str(e)}

    def _build_machine_map(self, ctx, pkgs):
        """A GÉP FELÉPÍTÉSE: alkatrészek és a hozzájuk tartozó driverek
        (`app/machinemap_core.py`). UGYANABBÓL az eszközfából és INF-tényekből dolgozik,
        amiből a használat-besorolás készült - egy felderítés, két nézet.

        A térkép hibája SOHA nem viheti el a használat-besorolást: ha itt valami elszáll,
        a táblázat a régi (használat szerinti) csoportosítással működik tovább, és a
        felület kimondja, hogy a térkép nem készült el."""
        nodes = (ctx or {}).get('raw', {}).get('nodes')
        if not nodes:
            logging.warning("[MACHINEMAP] Nincs eszközfa (a használat-felderítés a "
                            "PowerShell-tartalékon futott) - a gép-térkép kimarad.")
            return {'machine': None, 'machine_error': 'Az eszközfa nem olvasható, a gép felépítése nem rajzolható ki.'}
        try:
            from app import machinemap_core as mmc
            from app import win32
            from app.wu_core import identify_printer_packages
            printers = set(identify_printer_packages(pkgs)) if pkgs else set()
            identity = win32.read_hardware_identity()
            role = win32.platform_role()
            mm = mmc.build_machine_map(nodes, pkgs, ctx.get('facts') or {}, ctx.get('usage') or {},
                                       identity, role, printers)
            mmc.log_machine_map(mm)
            return {'machine': mm}
        except Exception as e:
            logging.warning(f"[MACHINEMAP] A gép-térkép nem készült el: {e}", exc_info=True)
            return {'machine': None, 'machine_error': f'A gép felépítése nem rajzolható ki: {e}'}

    # ================================================================
    # DRIVER LISTING
    # ================================================================
    def load_drivers(self, all_drivers=False):
        logging.info(f"[API] load_drivers(all_drivers={all_drivers})")
        def worker():
            self.emit('drivers_loading')
            start = time.monotonic()
            # A DISM felismert hibái ide gyűlnek. MIÉRT KELL (2026-09-07, terepi naplóból):
            # egy foglalt DISM ("another DISM operation") üres listát ad, amit a felület
            # eddig ugyanúgy `0 driver`-ként mutatott, mint a "tényleg nincs csomag" esetet.
            # A kettő gyökeresen mást jelent a technikusnak, ezért a hibát ki kell mondani.
            problems = []
            try:
                if self.target_os_path:
                    logging.info(f"[DRIVERS] Offline mód: {self.target_os_path}")
                    drivers = self._get_offline_drivers(all_drivers, problems)
                elif all_drivers:
                    logging.info("[DRIVERS] Összes driver lekérdezés (élő rendszer)")
                    drivers = self._get_all_drivers()
                else:
                    logging.info("[DRIVERS] Third-party driverek lekérdezés")
                    drivers = self._get_third_party_drivers(problems)
                elapsed = time.monotonic() - start
                logging.info(f"[DRIVERS] Betöltve: {len(drivers)} driver ({elapsed:.1f}s)")
                payload = {'drivers': drivers, 'elapsed': round(elapsed, 1)}
                if problems and not drivers:
                    payload['error'] = (f"A driver-lista lekérdezése nem sikerült: {problems[0]}. "
                                        f"Ez NEM azt jelenti, hogy nincs driver a gépen - "
                                        f"próbáld újra a Frissítés gombbal.")
                self.emit('drivers_loaded', payload)
            except Exception as e:
                logging.error(f"[DRIVERS] Betöltési hiba: {e}")
                logging.error(traceback.format_exc())
                self.emit('drivers_loaded', {'drivers': [], 'elapsed': 0, 'error': str(e)})
        threading.Thread(target=worker, daemon=True, name="drivers-load").start()

    def _get_third_party_drivers(self, problems=None):
        """A third-party csomagok listája - RÖVID ÉLETŰ GYORSÍTÓTÁRRAL.

        `problems`: opcionális lista a DISM felismert hibájához (lásd drivers_core).

        MIÉRT (terepi mérés, 2026-08-31, ThinkPad T14 Gen 1): egy AutoFix lánc **19-szer**
        futtatta a `dism /Get-Drivers`-t, hívásonként átlag **90 másodpercig** - összesen
        **28 perc**, a teljes lánc idejének negyede. A hívás azért ilyen lassú, mert a
        katalógus-telepítések felduzzasztják a DriverStore-t (ezt a CLAUDE.md már méri:
        0,6 mp -> 77 mp), és a lánc több lépése is ugyanazt a listát kéri el egymás után,
        közben változatlan rendszerállapot mellett.

        A GYORSÍTÓTÁR HELYESSÉGE AZON ÁLL, HOGY NEM IDŐALAPÚ: minden DriverStore-módosító
        parancs (`pnputil /add-driver`, `/delete-driver`, `dism /Add-Driver` stb.) MAGÁTÓL
        eldobja a `_run`-ban (lásd `drivers_core.mutates_driver_store`). Ez azért fontos,
        mert a lánc több döntése ELŐTTE/UTÁNA összehasonlításon alapul (pl.
        `verify_failed_installs`): ha egy telepítés utáni lekérdezés régi listát kapna, a
        chain néma hamis eredményt hozna - pontosan az a hibaosztály, amit ez a projekt
        mindenhol üldöz. A rövid TTL csak másodlagos védőháló arra az esetre, ha valami
        rajtunk kívül (Windows Update, egy másik program) írná a DriverStore-t."""
        now = time.monotonic()
        cached = getattr(self, '_dv_cache', None)
        if cached and (now - cached[0]) < drivers_core.DRIVER_LIST_TTL:
            logging.debug(f"[DRIVERS] A csomaglista a gyorsítótárból ({len(cached[1])} db, "
                          f"{now - cached[0]:.0f} mp-e olvasva) - dism megspórolva.")
            return cached[1]
        logging.debug("[DRIVERS] dism /English /Online /Get-Drivers futtatása...")
        drivers = drivers_core.get_third_party_drivers(self._run, problems)
        # A lekérdezés ALATT is történhetett módosítás (a lánc több szálon dolgozik):
        # olyankor nem tesszük el, inkább a következő hívó kérdezze le újra.
        if getattr(self, '_dv_cache_dirty_at', 0) <= now:
            self._dv_cache = (time.monotonic(), drivers)
        else:
            logging.debug("[DRIVERS] A lekérdezés alatt módosult a DriverStore - nem gyorsítótárazunk.")
        return drivers

    def invalidate_driver_cache(self):
        """A csomaglista-gyorsítótár eldobása. A `_run` hívja minden DriverStore-módosító
        parancs után - lásd `_get_third_party_drivers` docstringjét."""
        if getattr(self, '_dv_cache', None) is not None:
            logging.debug("[DRIVERS] A csomaglista-gyorsítótár eldobva (DriverStore módosult).")
        self._dv_cache = None
        self._dv_cache_dirty_at = time.monotonic()

    def _get_all_drivers(self):
        logging.debug("[DRIVERS] _get_all_drivers() indult")
        drivers = drivers_core.get_all_drivers(self._run)
        logging.debug(f"[DRIVERS] _get_all_drivers: {len(drivers)} valid driver")
        return drivers

    def _get_offline_drivers(self, all_drivers=False, problems=None):
        logging.debug(f"[DRIVERS] _get_offline_drivers(all_drivers={all_drivers})")
        drivers = drivers_core.get_offline_drivers(self._run, self.target_os_path,
                                                   all_drivers, problems)
        logging.debug(f"[DRIVERS] _get_offline_drivers: {len(drivers)} valid driver")
        return drivers

    # ================================================================
    # DRIVER DELETION
    # ================================================================
    def delete_drivers(self, published_names, list_all=False, reboot=False):
        logging.info(f"[API] delete_drivers() - {len(published_names)} driver, list_all={list_all}, reboot={reboot}")
        logging.info(f"[DELETE] Törlendő driverek: {published_names}")
        def worker():
            total = len(published_names)
            success = 0
            fail = 0
            # "EGY TELEPÍTETT ESZKÖZ MÉG HASZNÁLJA" (0xE000023D): nem végleges bukás, hanem
            # megszüntethető akadály - a köteg VÉGÉN egyszer leállítjuk a nyomtatósort és
            # újrapróbáljuk (ugyanaz a mag, amit az AutoFix használ). Terepi eset, amiből
            # ez lett (2026-09-18): a technikus 14 nyomtató-csomagot jelölt ki, mind a 14
            # ezzel a hibával bukott, és a képernyőn csak annyi állt, hogy "❌ sikertelen"
            # - se ok, se teendő. A Spooler leállítása CSOMAGONKÉNT pazarlás és fölösleges
            # kockázat lenne, ezért gyűjtjük őket.
            in_use = []
            logging.info(f"[DELETE] Törlés indulása: {total} db driver")
            self.emit('task_start', {'task': 'delete', 'title': f'Törlés folyamatban... ({total} driver)'})
            self.emit('task_progress', {'task': 'delete', 'log': f'Kijelölt driverek törlése indult ({total} db)'})

            cancelled = False
            for i, pub in enumerate(published_names):
                if self._cancel_flag:
                    self.emit('task_progress', {'task': 'delete', 'log': '❗ Törlés megszakítva a felhasználó által!'})
                    self.emit('task_progress', {'status': '❗ Megszakítva!', 'counter': f'{i} / {total}'})
                    cancelled = True
                    break

                self.emit('task_progress', {
                    'task': 'delete', 'current': i, 'total': total,
                    'status': f'Törlés: {pub}', 'counter': f'{i+1} / {total}',
                    'log': f'🗑 Törlés: {pub}'
                })
                try:
                    is_oem = pub.lower().startswith("oem")
                    res = drivers_core.delete_driver_package(self._run, pub, self.target_os_path)

                    if drivers_core.delete_succeeded(res):
                        success += 1
                        self.emit('task_progress', {'task': 'delete', 'log': f'  ✅ {pub} törölve'})
                    elif drivers_core.delete_blocked_in_use(res):
                        # A köteg végén, a nyomtatósor leállítása után újrapróbáljuk.
                        in_use.append({'published': pub})
                        logging.warning(f"[DELETE] HASZNÁLATBAN: {pub} (returncode={res.returncode}) "
                                        f"- a köteg végén újrapróbáljuk.")
                        self.emit('task_progress', {'task': 'delete', 'log':
                                  f'  ⏸️ {pub}: egy telepített eszköz még használja - a végén újrapróbáljuk'})
                    else:
                        # Az agresszív force-fallback csak "ÖSSZES driver" módban, nem-oem
                        # csomagra fut (lásd drivers_core.force_delete_driver_files).
                        if list_all and not is_oem:
                            if drivers_core.force_delete_driver_files(self._run, pub, self.target_os_path):
                                success += 1
                                self.emit('task_progress', {'task': 'delete', 'log': f'  ✅ {pub} törölve (force)'})
                            else:
                                fail += 1
                                self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} sikertelen (nem található)'})
                        else:
                            fail += 1
                            self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} sikertelen'})
                except Exception as e:
                    fail += 1
                    self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} hiba: {e}'})

            # MÁSODIK KÖR: amit "egy telepített eszköz használ" - a nyomtatósor átmeneti
            # leállításával. UGYANAZ A MAG, amit az AutoFix törlési fázisa hív.
            if in_use and not cancelled:
                r = drivers_core.retry_in_use_deletes(
                    self._run, in_use,
                    log=lambda m: self.emit('task_progress', {'task': 'delete', 'log': m}),
                    check_cancel=lambda: bool(self._cancel_flag),
                    timeout=drivers_core.DELETE_DRIVER_TIMEOUT,
                    target_os_path=self.target_os_path)
                success += len(r['deleted'])
                fail += len(r['failed'])
                if r['failed']:
                    self.emit('task_progress', {'task': 'delete', 'log':
                              f'\n❌ {len(r["failed"])} csomagot így sem lehetett eltávolítani:'})
                    for f in r['failed']:
                        self.emit('task_progress', {'task': 'delete', 'log': f'   • {f}'})
                if r['still_in_use']:
                    # A KONKRÉT ok és a valódi teendő - nem elég annyi, hogy "sikertelen".
                    self.emit('task_progress', {'task': 'delete',
                              'log': drivers_core.in_use_explanation(len(r['still_in_use']))})

            # Post-delete scan
            is_offline = bool(self.target_os_path)
            is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
            if not is_offline and not is_pe and success > 0:
                self.emit('task_progress', {'task': 'delete', 'log': 'Hardverek újraszkennelése...', 'status': 'Hardverek újraszkennelése...'})
                self._run(['pnputil', '/scan-devices'])
                time.sleep(10)
                self.emit('task_progress', {'task': 'delete', 'log': '✅ Hardverek frissítve!'})

            if cancelled:
                self.emit('task_progress', {'task': 'delete', 'log': f'\n--- MEGSZAKÍTVA! Sikeres: {success}, Sikertelen: {fail} ---', 'current': i, 'total': total})
                self.emit('task_complete', {'task': 'delete', 'success': success, 'fail': fail,
                                            'counter': '❗ Megszakítva',
                                            'status': f'❗ Megszakítva! Sikeres: {success}, Sikertelen: {fail}'})
            else:
                self.emit('task_progress', {'task': 'delete', 'log': f'\n--- Sikeres: {success}, Sikertelen: {fail} ---', 'current': total, 'total': total})
                self.emit('task_complete', {'task': 'delete', 'success': success, 'fail': fail,
                                            'counter': f'✅ {success} / ❌ {fail}',
                                            'status': f'Kész! Sikeres: {success}, Sikertelen: {fail}'})

                # Újraindítás ha kérték
                if reboot and success > 0:
                    self.emit('task_progress', {'task': 'delete', 'log': '\n🔄 Újraindítás 5 másodperc múlva...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])

        self._safe_thread('delete', worker)
