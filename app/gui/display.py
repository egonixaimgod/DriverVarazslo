"""DriverVarázsló GUI - Kijelző & Színkezelés nézet.

2026-09-22-ÉN TELJESEN ÚJRAÍRVA (explicit user decision: *"a kijelző & színkezelés tabot
TELJESEN alakítsd át, legyen professzionális de egyszerű és átlátható"*). Amit a nézet tud:
  - HDR ki/be és ACM ("Automatically manage color for apps") ki/be kijelzőnként;
  - az ACM TARTÓS kikapcsolása (zár: minden bejelentkezéskor ellenőriz és kikapcsol);
  - a profil-betöltés tiltása - tiltáskor minden profil lekerül a kijelzőkről, a gamma
    azonnal lineáris;
  - ICC-profil aktiválása SDR- és HDR-módra, VALÓS IDŐBEN (a gamma azonnal betöltődik),
    és kiírja, melyik profilon fut épp a kijelző - a Windows saját válaszából;
  - profilok telepítése, törlése, és a teljes gyári visszaállítás előnézettel.

A logika az app/colormgmt_core.py-ban (profil/ACM/gyári) és az app/display_core.py-ban
(monitor/HDR/EDID/gamma-olvasás) van; ez a mixin csak szálat kezel és megjelenít.
MINDEN művelet után a teljes állapotot FRISSEN olvassuk vissza és küldjük a nézetnek - a
felület soha nem "feltételezi" egy kapcsolás eredményét.
"""

# === AUTO-IMPORTS ===
import logging
import threading
import subprocess
import time
from app import display_core
from app import colormgmt_core
# === /AUTO-IMPORTS ===


# A Windows saját színkezelő/HDR felületei (link-out). Csak NÉVVEL felsorolt parancs indul,
# a nézetből érkező kulcs soha nem lesz parancs.
WINDOWS_COLOR_TOOLS = {
    'colorcpl': ('Színkezelés (vezérlőpult)', ['colorcpl.exe']),
    'hdr_settings': ('Windows HDR-beállítások', ['cmd', '/c', 'start', '', 'ms-settings:display-hdr']),
    'display_settings': ('Kijelző-beállítások', ['cmd', '/c', 'start', '', 'ms-settings:display']),
    'dccw': ('Kijelzőkalibráló varázsló', ['dccw.exe']),
    'hdr_calibration': ('Windows HDR Calibration (Store)',
                        ['cmd', '/c', 'start', '', 'ms-windows-store://pdp/?productid=9N7F2SM5D1LR']),
}


class GuiDisplayMixin:
    """Kijelző & Színkezelés nézet. A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ------------------------------------------------------------------
    # Állapot
    # ------------------------------------------------------------------
    def _collect_display_state(self):
        """A nézet TELJES állapota egy dict-ben. Minden alrész külön try-ban: egy hibás
        EDID vagy olvashatatlan profil nem viheti el az egész nézetet."""
        st = {'displays': [], 'library': [], 'profile_dir': '', 'calibration': None,
              'registered': [], 'broken': 0, 'orphans': [], 'acm_guard': False,
              'acm_guard_last': '', 'modern_api': colormgmt_core._API_ADD is not None, 'errors': []}
        try:
            st['displays'] = display_core.enumerate_displays()
        except Exception as e:
            logging.error(f"[DISPLAY] Kijelzők felderítése sikertelen: {e}", exc_info=True)
            st['errors'].append(f'Kijelzők felderítése: {e}')
        assocs = []
        try:
            assocs = colormgmt_core.all_associations()
        except Exception as e:
            logging.error(f"[DISPLAY] Társítások olvasása sikertelen: {e}", exc_info=True)
            st['errors'].append(f'Társítások: {e}')
        for d in st['displays']:
            try:
                d['profiles'] = colormgmt_core.display_profiles(d, assocs)
            except Exception as e:
                logging.error(f"[DISPLAY] {d.get('name')} profiljai nem olvashatók: {e}", exc_info=True)
                d['profiles'] = {'sdr': [], 'hdr': [], 'active_sdr': None, 'active_hdr': None}
        try:
            st['orphans'] = [{'root': a['root'], 'instance': a['instance'],
                              'slot': 'HDR' if a['hdr'] else 'SDR', 'profiles': a['profiles']}
                             for a in colormgmt_core.orphan_associations(st['displays'], assocs)]
        except Exception as e:
            logging.warning(f"[DISPLAY] Árva társítások felmérése sikertelen: {e}")
        try:
            st['profile_dir'], st['library'] = colormgmt_core.list_library()
        except Exception as e:
            logging.error(f"[DISPLAY] Profilkönyvtár olvasása sikertelen: {e}", exc_info=True)
            st['errors'].append(f'Profilkönyvtár: {e}')
        try:
            st['calibration'] = display_core.calibration_management()
        except Exception as e:
            logging.warning(f"[DISPLAY] Profil-betöltés állapota nem olvasható: {e}")
        try:
            st['registered'] = display_core.registered_profiles_report()
            st['broken'] = sum(1 for r in st['registered'] if r['missing'])
        except Exception as e:
            logging.warning(f"[DISPLAY] Regisztrált profilok nem olvashatók: {e}")
        try:
            st['acm_guard'] = colormgmt_core.acm_guard_installed(self._run)
            st['acm_guard_last'] = colormgmt_core.acm_guard_last_line()
        except Exception as e:
            logging.warning(f"[DISPLAY] ACM-zár állapota nem olvasható: {e}")
        logging.info(f"[DISPLAY] Állapot: {len(st['displays'])} kijelző, "
                     + '; '.join(f"{d.get('name')}: SDR={d['profiles'].get('active_sdr') or '-'} "
                                 f"HDR={d['profiles'].get('active_hdr') or '-'} "
                                 f"ACM={'BE' if (d.get('hdr') or {}).get('wcg_enabled') else 'KI'}"
                                 for d in st['displays'])
                     + f" | profil-betöltés={st['calibration']}, ACM-zár={st['acm_guard']}, "
                       f"árva={len(st['orphans'])}, törött regisztráció={st['broken']}")
        return st

    def _emit_display_state(self):
        try:
            st = self._collect_display_state()
            # Az ACM-zár a nézet megnyitásakor is érvényesül: ha a Windows közben
            # visszakapcsolta (új monitor, frissítés), itt és most kikapcsoljuk - névvel.
            if st.get('acm_guard'):
                fixed = colormgmt_core.enforce_acm_off(st['displays'], 'ACM-zár')
                if fixed:
                    self.emit('toast', {'message': f"🔒 Az automatikus színkezelés visszakapcsolt ezen: "
                                                   f"{', '.join(fixed)} - a zár kikapcsolta.", 'type': 'warning'})
                    st = self._collect_display_state()
            self.emit('display_info', st)
        except Exception as e:
            logging.error(f"[DISPLAY] Állapot-frissítés hiba: {e}", exc_info=True)
            self.emit('display_info', {'displays': [], 'library': [], 'errors': [str(e)]})

    def load_display_info(self):
        """A nézet betöltése háttérszálon -> 'display_info'. Szándékosan nem megy át a
        _task_busy kapun: csak olvas (ugyanaz az elv, mint a load_drivers-nél)."""
        logging.info("[API] load_display_info()")
        threading.Thread(target=self._emit_display_state, daemon=True, name="display-load").start()

    def _display_action(self, name, fn):
        """Közös keret minden műveletnek: szál, hibakezelés, a végén FRISS állapot."""
        def worker():
            try:
                fn()
            except Exception as e:
                logging.error(f"[DISPLAY] {name} hiba: {e}", exc_info=True)
                self.emit('toast', {'message': f'❌ Hiba ({name}): {e}', 'type': 'error'})
            finally:
                self._emit_display_state()
        threading.Thread(target=worker, daemon=True, name=f"display-{name}").start()

    def _fresh_display(self, index):
        """A nézet sorszámához tartozó FRISS kijelző-rekord (a monitor közben lecsatlakozhatott)."""
        for d in display_core.enumerate_displays():
            if d.get('index') == int(index):
                return d
        self.emit('toast', {'message': '❌ Ez a kijelző már nem elérhető - frissítsd a nézetet!',
                            'type': 'error'})
        return None

    # ------------------------------------------------------------------
    # HDR és ACM
    # ------------------------------------------------------------------
    def set_display_hdr(self, index, enable):
        logging.info(f"[API] set_display_hdr(index={index}, enable={enable})")

        def run():
            d = self._fresh_display(index)
            if not d:
                return
            aid, tid = display_core.find_display_target(d['adapter_low'], d['adapter_high'], d['target_id'])
            if aid is None:
                return
            ok, st = display_core.set_hdr(aid, tid, bool(enable))
            if ok:
                self.emit('toast', {'message': f"✅ HDR {'bekapcsolva' if enable else 'kikapcsolva'}: "
                                               f"{d['name']} ({st.get('bits') or '?'} bit)", 'type': 'success'})
            else:
                self.emit('toast', {'message': '❌ A HDR átkapcsolása nem sikerült - egyes kijelzők csak a '
                                               'Windows beállításaiból engedik.', 'type': 'error'})
            time.sleep(1.5)     # a kijelző újra-egyeztet, a kép 1-2 mp-re elsötétülhet
        self._display_action('hdr', run)

    def set_display_acm(self, index, enable):
        """Az "Automatically manage color for apps" (ACM) ki/be egy kijelzőn."""
        logging.info(f"[API] set_display_acm(index={index}, enable={enable})")

        def run():
            d = self._fresh_display(index)
            if not d:
                return
            aid, tid = display_core.find_display_target(d['adapter_low'], d['adapter_high'], d['target_id'])
            if aid is None:
                return
            ok, _st = display_core.set_acm(aid, tid, bool(enable))
            if ok:
                extra = ''
                if enable and colormgmt_core.acm_guard_installed(self._run):
                    extra = ' ⚠️ Az ACM-zár él: a következő bejelentkezéskor újra kikapcsolja!'
                self.emit('toast', {'message': f"✅ Automatikus színkezelés {'BE' if enable else 'KI'}: "
                                               f"{d['name']}.{extra}", 'type': 'warning' if extra else 'success'})
            else:
                self.emit('toast', {'message': '❌ Az automatikus színkezelés nem kapcsolható ezen a kijelzőn '
                                               '(Windows 11 24H2+ és támogató kijelző kell).', 'type': 'error'})
        self._display_action('acm', run)

    def set_acm_lock(self, enabled):
        """Az ACM TARTÓS kikapcsolása (zár) vagy a zár feloldása."""
        logging.info(f"[API] set_acm_lock(enabled={enabled})")

        def run():
            if enabled:
                ok, msg = colormgmt_core.install_acm_guard(self._run)
                if not ok:
                    self.emit('toast', {'message': f'❌ A zár nem telepíthető: {msg}', 'type': 'error'})
                    return
                fixed = colormgmt_core.enforce_acm_off(display_core.enumerate_displays(), 'ACM-zár bekapcsolása')
                self.emit('toast', {'message': '🔒 Automatikus színkezelés TARTÓSAN kikapcsolva'
                                               + (f" (most kikapcsolva: {', '.join(fixed)})" if fixed else '')
                                               + ' - minden bejelentkezéskor ellenőrizzük.', 'type': 'success'})
            else:
                ok = colormgmt_core.remove_acm_guard(self._run)
                self.emit('toast', {'message': '🔓 ACM-zár feloldva - a kapcsoló újra szabadon használható.'
                                    if ok else '❌ A zár nem távolítható el (részletek a naplóban).',
                                    'type': 'success' if ok else 'error'})
        self._display_action('acm-lock', run)

    # ------------------------------------------------------------------
    # Profil-betöltés és profilok
    # ------------------------------------------------------------------
    def set_profile_loading(self, enabled):
        logging.info(f"[API] set_profile_loading(enabled={enabled})")

        def run():
            res = colormgmt_core.set_profile_loading(bool(enabled), display_core.enumerate_displays())
            if not res['ok']:
                self.emit('toast', {'message': '❌ A profil-betöltés nem állítható át (részletek a naplóban).',
                                    'type': 'error'})
            elif enabled:
                self.emit('toast', {'message': '✅ Profil-betöltés ENGEDÉLYEZVE - most már aktiválhatsz profilt.',
                                    'type': 'success'})
            else:
                bad = [g[0] for g in res['gamma'] if not g[1]]
                self.emit('toast', {'message': f"🔒 Profil-betöltés LETILTVA - {len(res['removed'])} profil-társítás "
                                               f"leszedve, a gamma lineáris"
                                               + (f" (NEM sikerült: {', '.join(bad)})" if bad else '') + '.',
                                    'type': 'warning' if bad else 'success'})
        self._display_action('profile-loading', run)

    def activate_color_profile(self, index, slot, profile_file):
        """Profil aktiválása egy kijelzőn ('sdr' vagy 'hdr' módra). Üres profilnév =
        a mód profiljának levétele (Windows alapértelmezés)."""
        hdr = str(slot).lower() == 'hdr'
        logging.info(f"[API] activate_color_profile(index={index}, slot={slot}, profil={profile_file!r})")

        def run():
            d = self._fresh_display(index)
            if not d:
                return
            label = 'HDR' if hdr else 'SDR'
            if not profile_file:
                ok, msg = colormgmt_core.clear_slot(d, hdr)
                self.emit('toast', {'message': (f"✅ {label}-profil levéve: {d['name']} - Windows alapértelmezés"
                                                + (f" ({msg})" if msg else '') + '.') if ok
                                    else f'❌ A levétel nem sikerült: {msg}', 'type': 'success' if ok else 'error'})
                return
            if display_core.calibration_management() == 0:
                self.emit('toast', {'message': '🔒 A profil-betöltés le van tiltva - előbb engedélyezd!',
                                    'type': 'warning'})
                return
            r = colormgmt_core.activate_profile(d, profile_file, hdr)
            if not r['ok']:
                self.emit('toast', {'message': f"❌ Az aktiválás nem sikerült: {r.get('error') or 'ismeretlen hiba'}",
                                    'type': 'error'})
                return
            tail = ''
            if hdr:
                tail = (' A HDR-profil HDR módban érvényes.' if (d.get('hdr') or {}).get('enabled')
                        else ' A HDR-profil a HDR bekapcsolásakor lép életbe.')
            elif r.get('gamma'):
                tail = f" Gamma: {r['gamma']}."
            self.emit('toast', {'message': f"✅ „{profile_file}” aktív ({label}): {d['name']}.{tail}"
                                           + ('' if r['verified'] else ' ⚠️ A Windows visszaigazolása hiányzik.'),
                                'type': 'success' if r['verified'] and r.get('gamma_ok', True) else 'warning'})
        self._display_action('profile-activate', run)

    def reset_display_gamma(self, index):
        """A kijelző gamma-táblája azonnal lineárisra (a betöltött korrekció eldobása)."""
        logging.info(f"[API] reset_display_gamma(index={index})")

        def run():
            d = self._fresh_display(index)
            if not d:
                return
            ok, msg = colormgmt_core.reset_gamma(d.get('gdi_name', ''), 'kézi visszaállítás')
            self.emit('toast', {'message': f"✅ Gamma lineáris: {d['name']}." if ok else f'❌ {msg}',
                                'type': 'success' if ok else 'error'})
        self._display_action('gamma-reset', run)

    def install_icc_profile(self):
        logging.info("[API] install_icc_profile()")

        def run():
            path = self.select_file('Válassz ICC/ICM színprofilt', 'Színprofil (*.icc;*.icm)')
            if not path:
                return
            ok, res = colormgmt_core.install_profile(path)
            self.emit('toast', {'message': f'✅ Profil telepítve: {res} - most már aktiválható.' if ok
                                else f'❌ {res}', 'type': 'success' if ok else 'error'})
        self._display_action('icc-install', run)

    def uninstall_icc_profile(self, profile_file):
        logging.info(f"[API] uninstall_icc_profile({profile_file!r})")

        def run():
            ok, msg = colormgmt_core.delete_profile(profile_file)
            self.emit('toast', {'message': f'✅ „{profile_file}” törölve (minden kijelzőről is leszedve).'
                                if ok else f'❌ Nem sikerült törölni: {msg}', 'type': 'success' if ok else 'error'})
        self._display_action('icc-delete', run)

    def repair_color_registration(self):
        """Törött regisztrációk + árva társítások eltávolítása."""
        logging.info("[API] repair_color_registration()")

        def run():
            removed, errors = display_core.repair_registered_profiles()
            orphans = colormgmt_core.remove_orphans(display_core.enumerate_displays())
            n = len(removed) + len(orphans)
            if errors:
                self.emit('toast', {'message': f'❌ Nem minden javítható: {errors[0]}', 'type': 'error'})
            else:
                self.emit('toast', {'message': f'✅ {n} hibás bejegyzés eltávolítva.' if n
                                    else 'ℹ️ Nem volt javítanivaló.', 'type': 'success' if n else 'info'})
        self._display_action('repair', run)

    # ------------------------------------------------------------------
    # Gyári visszaállítás
    # ------------------------------------------------------------------
    def get_color_reset_preview(self):
        """SZINKRON: a gyári visszaállítás pontos előnézete a megerősítő ablakhoz."""
        logging.info("[API] get_color_reset_preview()")
        try:
            return colormgmt_core.factory_reset_plan(self._run, display_core.enumerate_displays())
        except Exception as e:
            logging.error(f"[DISPLAY] Előnézet hiba: {e}", exc_info=True)
            return {'error': str(e)}

    def factory_reset_colors(self, delete_printer_profiles=True):
        logging.info(f"[API] factory_reset_colors(delete_printer_profiles={delete_printer_profiles})")

        def run():
            res = colormgmt_core.factory_reset(self._run, display_core.enumerate_displays(),
                                               bool(delete_printer_profiles))
            self.emit('color_reset_result', res)
            if res['errors']:
                self.emit('toast', {'message': f"⚠️ Gyári visszaállítás kész, {len(res['errors'])} hibával "
                                               f"(részletek az ablakban).", 'type': 'warning'})
            else:
                self.emit('toast', {'message': '✅ Minden színbeállítás gyári állapotban. A teljes hatáshoz '
                                               'jelentkezz ki és be (vagy indítsd újra a gépet).', 'type': 'success'})
        self._display_action('factory-reset', run)

    # ------------------------------------------------------------------
    # Kiugrás a Windows saját felületeire
    # ------------------------------------------------------------------
    def open_windows_color_tool(self, key):
        info = WINDOWS_COLOR_TOOLS.get(key)
        if not info:
            logging.warning(f"[DISPLAY] Ismeretlen Windows-eszköz kulcs: {key!r}")
            self.emit('toast', {'message': '❌ Ismeretlen beállító felület.', 'type': 'error'})
            return
        label, cmd = info
        logging.info(f"[DISPLAY] [CMD] Popen futtatása ({label}): {subprocess.list2cmdline(cmd)}")
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            self.emit('toast', {'message': f'✅ {label} megnyitva.', 'type': 'success'})
        except Exception as e:
            logging.error(f"[DISPLAY] {label} indítása sikertelen: {e}")
            self.emit('toast', {'message': f'❌ Nem sikerült megnyitni ({label}): {e}', 'type': 'error'})

    def open_color_profile_folder(self):
        try:
            path = display_core.color_directory()
            logging.info(f"[DISPLAY] Színprofil-mappa megnyitása: {path}")
            subprocess.Popen(['explorer.exe', path], stdin=subprocess.DEVNULL)
        except Exception as e:
            logging.error(f"[DISPLAY] A színprofil-mappa megnyitása sikertelen: {e}")
            self.emit('toast', {'message': f'❌ Nem sikerült megnyitni a mappát: {e}', 'type': 'error'})
