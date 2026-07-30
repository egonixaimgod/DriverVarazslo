"""DriverVarázsló GUI - Kijelző nézet: a Windows színkezelésének és HDR-jének EGY képernyős
kezelése (explicit felhasználói kérés, 2026-07-30).

MIÉRT: a Windows 11 ezeket a beállításokat három külön helyre szórta szét (Beállítások >
Kijelző > HDR, a régi Színkezelés vezérlőpult, és a Store-ból telepíthető HDR-kalibráló),
ráadásul a legfontosabb információkat egyik sem mutatja meg: mennyi a panel tényleges
csúcsfényereje, milyen profil van RÁTÉVE a monitorra, ép-e egyáltalán a színkezelés.
Ez a nézet mindezt egy helyen mutatja, és a felfedezett hibát javítani is tudja.

A nem-UI logika az app/display_core.py-ban van (a `_core` konvenció szerint), a mixin
csak megjelenít és szálat kezel.

AMI SZÁNDÉKOSAN NINCS BENNE EBBEN A KÖRBEN: a profil TÁRSÍTÁSA monitorhoz, ICC-profil
generálás és a vezetett nit-teszt. A társításhoz használt mscms API a fejlesztői gépen
TRUE-t adott vissza úgy, hogy közben semmit nem írt a registrybe (emelt joggal sem) -
amíg ez nincs tisztázva, nem kerül ide gomb: egy néma sikerre épülő gomb rosszabb, mint
a hiánya. A nézet ezért ebben a körben OLVAS, HDR-t kapcsol és a törött színkezelést
javítja - mindezt élőben ellenőrzött API-kkal.
"""

# === AUTO-IMPORTS ===
import os
import logging
import threading
import subprocess
from app import display_core
from app import colorprofile_core
from app.common import _app_data_dir
# === /AUTO-IMPORTS ===


# A Windows saját színkezelő/HDR felületei, amikre a nézet ki tud ugrani. Azért maradnak
# elérhetők, mert amit MI nem csinálunk meg (pl. hivatalos HDR-kalibráció), azt a szerviz
# innen egy kattintással eléri - link-out, ugyanaz az elv, mint a gyártói driver-kártyáknál.
WINDOWS_COLOR_TOOLS = {
    'colorcpl': ('Színkezelés (vezérlőpult)', ['colorcpl.exe']),
    'hdr_settings': ('Windows HDR-beállítások', ['cmd', '/c', 'start', '', 'ms-settings:display-hdr']),
    'display_settings': ('Kijelző-beállítások', ['cmd', '/c', 'start', '', 'ms-settings:display']),
    'dccw': ('Kijelzőkalibráló varázsló', ['dccw.exe']),
    'hdr_calibration': ('Windows HDR Calibration (Store)',
                        ['cmd', '/c', 'start', '', 'ms-windows-store://pdp/?productid=9N7F2SM5D1LR']),
}


class GuiDisplayMixin:
    """Kijelző nézet: monitor-áttekintés, HDR-kezelés, színprofilok állapota.
    A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ------------------------------------------------------------------
    # Adatgyűjtés
    # ------------------------------------------------------------------
    def _collect_display_state(self):
        """A nézet teljes állapota egyetlen dict-ben. Minden alrész külön try-ban fut:
        egy hibás EDID vagy egy olvashatatlan profil nem viheti el az egész nézetet -
        a szerviznek a maradék információ is többet ér, mint egy üres képernyő."""
        state = {'displays': [], 'profiles': [], 'profile_dir': '', 'assoc': {'system': [], 'user': []},
                 'calibration': None, 'registered': [], 'broken': 0, 'errors': []}
        try:
            state['displays'] = display_core.enumerate_displays()
        except Exception as e:
            logging.error(f"[DISPLAY] Kijelzők felderítése sikertelen: {e}", exc_info=True)
            state['errors'].append(f'Kijelzők felderítése: {e}')
        try:
            state['profile_dir'], state['profiles'] = display_core.list_color_profiles()
        except Exception as e:
            logging.error(f"[DISPLAY] Profilok listázása sikertelen: {e}", exc_info=True)
            state['errors'].append(f'Profilok listázása: {e}')
        try:
            state['assoc'] = display_core.profile_associations()
        except Exception as e:
            logging.error(f"[DISPLAY] Társítások olvasása sikertelen: {e}", exc_info=True)
            state['errors'].append(f'Társítások: {e}')
        try:
            state['calibration'] = display_core.calibration_management()
        except Exception as e:
            logging.warning(f"[DISPLAY] Kalibrációkezelés olvasása sikertelen: {e}")
        try:
            state['registered'] = display_core.registered_profiles_report()
            state['broken'] = sum(1 for r in state['registered'] if r['missing'])
        except Exception as e:
            logging.error(f"[DISPLAY] Regisztrált profilok olvasása sikertelen: {e}", exc_info=True)
            state['errors'].append(f'Regisztrált profilok: {e}')
        state['assoc_count'] = len(state['assoc']['system']) + len(state['assoc']['user'])
        return state

    def load_display_info(self):
        """A Kijelző nézet betöltése/frissítése háttérszálon -> 'display_info' esemény.
        SZÁNDÉKOSAN nem megy át a _task_busy kapun: csak olvas, és a nézet váltásakor
        mindig le kell futnia (ugyanaz az elv, mint a load_drivers-nél)."""
        logging.info("[API] load_display_info()")

        def worker():
            try:
                state = self._collect_display_state()
                self.emit('display_info', state)
                if state['broken']:
                    self.emit('toast', {'message': f"⚠️ {state['broken']} törött színkezelés-bejegyzés "
                                                   f"a rendszerben - a nézetben javítható!", 'type': 'warning'})
            except Exception as e:
                logging.error(f"[DISPLAY] load_display_info hiba: {e}", exc_info=True)
                self.emit('display_info', {'displays': [], 'profiles': [], 'errors': [str(e)]})
                self.emit('toast', {'message': f'❌ Hiba a kijelző-adatok lekérésekor: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="display-load").start()

    # ------------------------------------------------------------------
    # HDR
    # ------------------------------------------------------------------
    def set_display_hdr(self, adapter_low, adapter_high, target_id, enable):
        """HDR be-/kikapcsolása a megadott kijelzőn, majd a nézet frissítése.

        A kijelzőt MINDIG frissen keressük ki az aktív utak közül (find_display_target),
        nem a nézet által korábban kapott azonosítót használjuk közvetlenül: a monitor
        azóta lecsatlakozhatott vagy az elrendezés megváltozhatott, és egy nem létező
        target-re küldött kapcsolás csendben elszállna."""
        logging.info(f"[API] set_display_hdr(adapter={adapter_low}:{adapter_high}, "
                     f"target={target_id}, enable={enable})")

        def worker():
            try:
                aid, tid = display_core.find_display_target(int(adapter_low), int(adapter_high),
                                                            int(target_id))
                if aid is None:
                    self.emit('toast', {'message': '❌ Ez a kijelző már nem elérhető - frissítsd a nézetet!',
                                        'type': 'error'})
                    self.load_display_info()
                    return
                ok, state = display_core.set_hdr(aid, tid, bool(enable))
                if ok:
                    txt = 'bekapcsolva' if enable else 'kikapcsolva'
                    extra = f" · {state['bits']} bit · {state['mode'] or ''}".rstrip(' ·')
                    self.emit('toast', {'message': f'✅ HDR {txt}{extra}', 'type': 'success'})
                else:
                    self.emit('toast', {'message': '❌ A HDR átkapcsolása nem sikerült (részletek a debug logban). '
                                                   'Egyes kijelzők csak a Windows beállításaiból engedik.',
                                        'type': 'error'})
            except Exception as e:
                logging.error(f"[DISPLAY] set_display_hdr hiba: {e}", exc_info=True)
                self.emit('toast', {'message': f'❌ Hiba a HDR kapcsolásakor: {e}', 'type': 'error'})
            finally:
                # A kapcsolás után a kijelző újra-egyeztet (a kép 1-2 mp-re elsötétülhet),
                # ezért a friss állapotot csak utána olvassuk vissza.
                import time
                time.sleep(2.0)
                try:
                    self.emit('display_info', self._collect_display_state())
                except Exception as e:
                    logging.error(f"[DISPLAY] Az állapot frissítése a HDR-kapcsolás után hibára futott: {e}")

        threading.Thread(target=worker, daemon=True, name="display-hdr").start()

    # ------------------------------------------------------------------
    # Színkezelés javítása / visszaállítása
    # ------------------------------------------------------------------
    def repair_color_registration(self):
        """A törött (nem létező fájlra mutató) profil-regisztrációk eltávolítása.
        Ez a fejlesztői gépen talált valódi hiba javítása: egy külső HDR-eszköz ott hagyta
        a saját profilját sRGB munkatérként regisztrálva, a fájlt viszont elvitte - ettől a
        Windows színkezelő hívásai hibára futnak, és semmilyen Windows-felület nem szól róla."""
        logging.info("[API] repair_color_registration()")

        def worker():
            try:
                removed, errors = display_core.repair_registered_profiles()
                if removed:
                    self.emit('toast', {'message': f'✅ {len(removed)} törött színkezelés-bejegyzés eltávolítva. '
                                                   f'A Windows a saját alapértelmezését használja tovább.',
                                        'type': 'success'})
                elif errors:
                    self.emit('toast', {'message': f'❌ Nem sikerült eltávolítani: {errors[0]}', 'type': 'error'})
                else:
                    self.emit('toast', {'message': 'ℹ️ Nincs törött bejegyzés - a színkezelés rendben van.',
                                        'type': 'info'})
                self.emit('display_info', self._collect_display_state())
            except Exception as e:
                logging.error(f"[DISPLAY] repair_color_registration hiba: {e}", exc_info=True)
                self.emit('toast', {'message': f'❌ Hiba a javításkor: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="display-repair").start()

    def set_calibration_management(self, enabled):
        """A "Windows-kijelzőkalibráció használata" kapcsoló át-/visszakapcsolása.

        Mindkét irány legitim (explicit user decision, 2026-07-31), ezért kapcsoló és nem
        csak javítás-gomb:
          - KI: szélesgamutos OLED-en, ahol a natív, telített kép a cél, és a felhasználó
            biztosra akar menni, hogy semmi ne tudjon a háttérben gamma-görbét betölteni;
          - BE: minden más gépen - és kötelezően ott, ahol egy korábbi kiadásunk
            (Build 235-237) AutoFixe kikapcsolta, mert a 0 magától soha nem áll vissza.
        A magyarázatot a display_core.set_calibration_management docstringje viszi."""
        logging.info(f"[API] set_calibration_management(enabled={enabled})")

        def worker():
            try:
                ok, prev = display_core.set_calibration_management(bool(enabled))
                prev_txt = prev if prev is not None else 'nincs beállítva'
                if ok and enabled:
                    self.emit('toast', {'message': f'✅ Kalibráció-kezelés BEkapcsolva (előző: {prev_txt}). '
                                                   f'A társított profilok gamma-görbéje újra betöltődhet.',
                                        'type': 'success'})
                elif ok:
                    self.emit('toast', {'message': f'✅ Kalibráció-kezelés KIkapcsolva (előző: {prev_txt}). '
                                                   f'Mostantól semmilyen profil gamma-görbéje nem tölt be - '
                                                   f'a panel natívan megy. Teljes hatáshoz újraindítás.',
                                        'type': 'success'})
                else:
                    self.emit('toast', {'message': '❌ Nem sikerült átállítani (részletek a debug logban).',
                                        'type': 'error'})
                self.emit('display_info', self._collect_display_state())
            except Exception as e:
                logging.error(f"[DISPLAY] set_calibration_management hiba: {e}", exc_info=True)
                self.emit('toast', {'message': f'❌ Hiba: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="display-calib").start()

    def reset_display_colors(self):
        """Színprofilok visszaállítása gyári alapállapotra - UGYANAZ a mag, amit az AutoFix
        is futtat (app/colorprofile_core.py), csak itt külön, kézzel indítva. A művelet
        minden törölt hozzárendelést névvel naplóz, és NEM nyúl sem a nyomtató-profilokhoz,
        sem a Windows kalibrációkezelő kapcsolójához (lásd a mag dokumentációját)."""
        logging.info("[API] reset_display_colors()")

        def worker():
            try:
                ok, deleted = colorprofile_core.reset_color_profiles(
                    self._run, lambda m: self.emit('toast', {'message': m, 'type': 'info'}))
                if ok:
                    self.emit('toast', {'message': f'✅ Színprofilok visszaállítva ({deleted} hozzárendelés törölve). '
                                                   f'A teljes hatáshoz újraindítás javasolt.', 'type': 'success'})
                else:
                    self.emit('toast', {'message': '⚠️ A visszaállítás nem futott le hibátlanul '
                                                   '(részletek a debug logban).', 'type': 'warning'})
                self.emit('display_info', self._collect_display_state())
            except Exception as e:
                logging.error(f"[DISPLAY] reset_display_colors hiba: {e}", exc_info=True)
                self.emit('toast', {'message': f'❌ Hiba a visszaállításkor: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="display-reset").start()

    # ------------------------------------------------------------------
    # Kiugrás a Windows saját felületeire
    # ------------------------------------------------------------------
    def open_windows_color_tool(self, key):
        """A Windows saját színkezelő/HDR felületének megnyitása (link-out).
        Csak a WINDOWS_COLOR_TOOLS-ban NÉVVEL felsorolt parancsokat indítjuk - a nézetből
        érkező kulcsot soha nem használjuk parancsként, így a JS oldalról nem lehet
        tetszőleges programot elindíttatni."""
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
        """A színprofilok mappájának megnyitása az Intézőben."""
        try:
            path = display_core.color_directory()
            logging.info(f"[DISPLAY] Színprofil-mappa megnyitása: {path}")
            subprocess.Popen(['explorer.exe', path], stdin=subprocess.DEVNULL)
            self.emit('toast', {'message': '✅ Színprofil-mappa megnyitva.', 'type': 'success'})
        except Exception as e:
            logging.error(f"[DISPLAY] A színprofil-mappa megnyitása sikertelen: {e}")
            self.emit('toast', {'message': f'❌ Nem sikerült megnyitni a mappát: {e}', 'type': 'error'})
