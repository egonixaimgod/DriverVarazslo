"""DriverVarázsló CLI - CLI: egyszerűsített, egymenetes AutoFix (szándékosan nincs reboot-lánc!)."""

# === AUTO-IMPORTS ===
import socket
import subprocess
import time
import logging
from app import dupdrivers_core
from app.wu_core import AUTOFIX_PRINTER_SKIP_CLASSES
from app.wu_core import WU_MAX_CONSECUTIVE_FAILURES
from app.wu_core import WuProcessAborted
from app.wu_core import _install_abort_reason
from app.wu_core import is_reboot_pending
from app.wu_core import _build_wu_install_ps
from app.wu_core import _collect_printer_protection
from app.wu_core import _is_printer_protected
from app.wu_core import _iter_process_lines
from app.wu_core import _export_net_driver_backup
from app.wu_core import _restore_net_driver_backup
# === /AUTO-IMPORTS ===


class CliAutofixMixin:
    """CLI: egyszerűsített, egymenetes AutoFix (szándékosan nincs reboot-lánc!). A CliApi része (összerakás: app/cli/api.py)."""

    # ================================================================
    # AUTOFIX (1 kattintásos driver fix)
    # ================================================================
    def autofix(self):
        """Teljes automatikus driver fix (mint a GUI-ban)."""
        if self.target_os_path:
            print("\n❌ Hiba: Az 1 Kattintásos Driver Fix (Autofix) csak Élő (Online) rendszeren futtatható!")
            return
            
        print("\n" + "=" * 60)
        print("  ⚡ 1 KATTINTÁSOS AUTOMATIKUS DRIVER FIX")
        print("=" * 60)
        print("""
Lépések:
  0️⃣  Alvó mód és Gyors Rendszerindítás kikapcsolása
  1️⃣  Visszaállítási pont létrehozása
  2️⃣  Windows Update driver keresés LETILTÁSA
  3️⃣  Szellemeszközök törlése
  4️⃣  Összes third-party driver TÖRLÉSE
  5️⃣  Hardver újraszkennelés
  6️⃣  WU driver telepítés (friss driverek)
  7️⃣  Újraindítás

Megjegyzés: ez az egymenetes CLI változat - a GUI verzióval ellentétben nem
iktat be automatikus újraindítás(oka)t a törlés és az újratelepítés közé,
ezért ha egy driver csak egy közbenső reboot után enumerálódik újra, azt
manuálisan kell majd újraszkennelni (Driverek kezelése > Hardver újraszkennelés).
""")

        confirm = input("Biztosan elindítod? (igen/nem): ").strip().lower()
        if confirm not in ['igen', 'i', 'yes', 'y']:
            print("❌ Megszakítva.")
            return

        start_time = time.time()

        # FÁZIS 0: Alvó mód + Fast Startup letiltása
        print("\n" + "=" * 50)
        print("  FÁZIS 0: Alvó mód és Gyors Rendszerindítás kikapcsolása")
        print("=" * 50)
        power_cmds = [
            ['powercfg', '/change', 'monitor-timeout-ac', '0'],
            ['powercfg', '/change', 'monitor-timeout-dc', '0'],
            ['powercfg', '/change', 'standby-timeout-ac', '0'],
            ['powercfg', '/change', 'standby-timeout-dc', '0'],
            ['powercfg', '/change', 'hibernate-timeout-ac', '0'],
            ['powercfg', '/change', 'hibernate-timeout-dc', '0']
        ]
        for cmd in power_cmds:
            self._run(cmd)
        self._run(["powercfg", "/h", "off"])
        print("  ✅ Energiagazdálkodás beállítva, Gyors Rendszerindítás kikapcsolva.")

        # FÁZIS 1: Visszaállítási pont
        print("\n" + "=" * 50)
        print("  FÁZIS 1: Visszaállítási pont létrehozása")
        print("=" * 50)
        self.create_restore_point()

        # FÁZIS 2: WU letiltás
        print("\n" + "=" * 50)
        print("  FÁZIS 2: WU driver letiltás")
        print("=" * 50)
        self.disable_wu_drivers()

        # FÁZIS 3: Szellemeszközök törlése
        print("\n" + "=" * 50)
        print("  FÁZIS 3: Szellemeszközök törlése")
        print("=" * 50)
        self.delete_ghost_devices()

        # FÁZIS 4: Third-party driverek törlése
        print("\n" + "=" * 50)
        print("  FÁZIS 4: Third-party driverek törlése")
        print("=" * 50)
        drivers = self.get_third_party_drivers()
        # Nyomtató-védelem 2.0 (közös mag, mint a GUI AutoFixben): a jelenlévő nyomtatók/
        # szkennerek által használt INF-ek és a nyomtató-gyártók csomagjai nem törlődnek.
        protected_infs, printing_vendors = _collect_printer_protection(self._run)
        protected = [d for d in drivers if _is_printer_protected(d, protected_infs, printing_vendors, AUTOFIX_PRINTER_SKIP_CLASSES)]
        protected_keys = {id(d) for d in protected}
        drivers = [d for d in drivers if id(d) not in protected_keys]
        if protected:
            print(f"🖨️ {len(protected)} db nyomtatóhoz/szkennerhez tartozó driver védve (nem törlődik).")
        if drivers:
            print(f"Talált: {len(drivers)} db third-party driver")
            # 🛟 Hálózati mentőöv: Net-driverek exportja törlés előtt (közös mag).
            backed_up = _export_net_driver_backup(self._run, drivers)
            if backed_up:
                print(f"🛟 {backed_up} db hálózati driver elmentve vész-visszaállításhoz.")
            self.delete_drivers(drivers, reboot=False)
        else:
            print("Nincs third-party driver.")

        # FÁZIS 5: Hardver scan
        print("\n" + "=" * 50)
        print("  FÁZIS 5: Hardver újraszkennelés")
        print("=" * 50)
        print("🔄 pnputil /scan-devices...")
        self._run(['pnputil', '/scan-devices'])
        time.sleep(5)
        print("✅ Kész!")

        # FÁZIS 6: WU driver telepítés
        print("\n" + "=" * 50)
        print("  FÁZIS 6: WU driver telepítés")
        print("=" * 50)
        print("🔄 Driver frissítések keresése és telepítése...")
        print("   (Ez akár 5-10 percig is tarthat)")
        
        # A telepítő script a KÖZÖS _build_wu_install_ps-ből jön - ugyanaz, mint a GUI-s
        # manuális telepítésnél és AutoFixnél; itt a gép összes jelenlévő eszközéhez
        # párosít a scripten belül (a CLI-ben nincs Python-oldali előszűrés).
        ps_script = _build_wu_install_ps(match_system_devices=True)
        logging.debug(f"[CMD] Popen futtatása: {ps_script[:300]}...")
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            startupinfo=self._si, creationflags=self._nw)
        
        install_success = 0
        install_fail = 0
        reboot_needed_drivers = 0
        consecutive_failures = 0
        reboot_pending = False
        check_reboot_after_line = False

        def _abort_check():
            """Megszakítás-döntés a közös magból (_install_abort_reason). A CLI AutoFix
            egylövetű (nincs reboot-lánc), de a "mérgezett session" ugyanúgy sújtja:
            pending-reboot után a WUA minden további csomagra ~2,5 perc várakozással
            hamis hibát ad, ezért ott is megállunk - a fix a végén úgyis újraindít.
            A pending-reboot lekérdezés csak telepítési HIBA után fut (az a jel)."""
            nonlocal reboot_pending, check_reboot_after_line
            if check_reboot_after_line:
                check_reboot_after_line = False
                if is_reboot_pending(self._run):
                    reboot_pending = True
            return _install_abort_reason(consecutive_failures, reboot_pending)

        # A közös script kimeneti protokollja (lásd _build_wu_install_ps docstring).
        # Az olvasás a KÖZÖS _iter_process_lines-on át megy (wu_core): watchdog öli le a
        # folyamatot, ha 30 percig egyetlen sor sem érkezik (beragadt WU szolgáltatás) -
        # a régi közvetlen stdout-olvasás ilyenkor örökre blokkolt.
        try:
            for line in _iter_process_lines(process, self._run, abort_check=_abort_check):
                if line.startswith("FOUND:"):
                    print(f"  📦 {line[6:].strip()}")
                elif line.startswith("TOTAL:"):
                    print(f"\n  Összesen {line[6:].strip()} driver telepítése...")
                elif line.startswith("DLONE:"):
                    print(f"  ⬇ {line[6:].strip()}")
                elif line.startswith("INSTONE:"):
                    print(f"  ⚙ {line[8:].strip()}")
                elif line.startswith("OKRB:"):
                    # Nem szakítunk meg reboot-igényre: amíg sikerülnek a telepítések, megyünk
                    # tovább (lásd a GUI AutoFix azonos ágát) - a megszakítás jele a HIBA.
                    install_success += 1
                    reboot_needed_drivers += 1
                    consecutive_failures = 0
                    print(f"  ✅ {line[5:].strip()} (⚠️ újraindítás után él)")
                elif line.startswith("OK:"):
                    install_success += 1
                    consecutive_failures = 0
                    print(f"  ✅ {line[3:].strip()}")
                elif line.startswith("FAIL:"):
                    install_fail += 1
                    if 'LETÖLTÉS HIBA' not in line:
                        consecutive_failures += 1
                        check_reboot_after_line = True
                    print(f"  ❌ {line[5:].strip()}")
                elif line.startswith("EMPTY:"):
                    print(f"  ℹ️  {line[6:].strip()}")
                elif line.startswith("ERROR:"):
                    print(f"  ❌ HIBA: {line[6:].strip()}")
                elif line.startswith("DONE:"):
                    print(f"\n  Telepítés kész: ✅ {install_success} sikeres, ❌ {install_fail} sikertelen")
                elif line.startswith("INIT:") or line.startswith("SEARCH:") or line.startswith("SKIP:"):
                    pass  # csendes protokoll-sorok
        except WuProcessAborted as ab:
            if ab.reason == 'reboot':
                print("\n  🔄 A rendszer újraindítást igényel - a hátralévő driverek ebben az állapotban")
                print("     nem tudnak rendesen települni (a Windows Update csomagonként ~2,5 perc")
                print("     várakozás után hamis hibát adna). Indítsd újra a gépet, majd futtasd újra a fixet!")
            elif ab.reason == 'failstreak':
                print(f"\n  ⚠️ {WU_MAX_CONSECUTIVE_FAILURES} egymást követő telepítési hiba - a telepítés itt leállt.")
                print("     Indítsd újra a gépet, majd futtasd újra a fixet a maradékhoz!")
            else:
                print("\n  ❌ A WU telepítő 30 percen át nem adott életjelet - a watchdog leállította!")
                print("     (Beragadt Windows Update szolgáltatásra utal - próbáld újra a fixet.)")
        if reboot_needed_drivers:
            print(f"\n  ⚠️ {reboot_needed_drivers} driver csak újraindítás után lép életbe (a fix végén úgyis újraindítunk).")

        if install_success > 0:
            print("\n🔄 Eszközök újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])

        # 🛟 Hálózati mentőöv: ha a törlés+telepítés után nincs internet, a mentett
        # Net-drivereket visszatöltjük (közös mag, mint a GUI AutoFixben).
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3).close()
            net_ok = True
        except OSError:
            net_ok = False
        if not net_ok:
            print("\n🛟 Nincs internet a fix után - mentett hálózati driverek visszaállítása...")
            if _restore_net_driver_backup(self._run):
                self._run(['pnputil', '/scan-devices'])
                print("✅ Hálózati driverek visszatöltve, eszközök újraszkennelve.")
            else:
                print("⚠️ Nincs mentett hálózati driver - ellenőrizd kézzel a hálózatot!")

        # ZÁRÓ DriverStore-TAKARÍTÁS: a telepítések után ottmaradt régi driver-verziók
        # eltakarítása (közös mag a GUI-val: dupdrivers_core.auto_cleanup_duplicates,
        # a kézi takarító panel/menü biztonsági szabályaival - hibája sosem akasztja
        # meg a fixet, a core mindent elnyel).
        if install_success > 0:
            print("\n🧹 DriverStore-takarítás: elavult driver-verziók törlése...")
            dupdrivers_core.auto_cleanup_duplicates(self._run, print, self.get_third_party_drivers)

        # Összegzés
        elapsed = int(time.time() - start_time)
        print("\n" + "=" * 60)
        print(f"  ⚡ AUTOFIX KÉSZ! (Idő: {elapsed // 60} perc {elapsed % 60} mp)")
        print("=" * 60)
        
        # FÁZIS 7: Újraindítás
        if install_success > 0 or len(drivers) > 0:
            print("\n🔄 Újraindítás 30 másodperc múlva...")
            print("   (Ctrl+C a megszakításhoz)")
            try:
                for i in range(30, 0, -1):
                    print(f"\r   {i} másodperc...", end="", flush=True)
                    time.sleep(1)
                print("\n🔄 Újraindítás MOST!")
                self._run(['shutdown', '/r', '/t', '0', '/f'])
            except KeyboardInterrupt:
                print("\n❌ Újraindítás megszakítva.")
        else:
            print("\nNem történt változás - újraindítás nem szükséges.")
