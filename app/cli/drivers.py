"""DriverVarázsló CLI - CLI: driver listázás (online/offline) és törlés
(a közös listázó/parzoló/törlő logika: app/drivers_core.py)."""

# === AUTO-IMPORTS ===
import time
import logging
from app import drivers_core
from app.common import CMD_TIMEOUT_RETURNCODE
from app.common import spawn_failed
from app.drivers_core import DELETE_DRIVER_TIMEOUT
# === /AUTO-IMPORTS ===


class CliDriversMixin:
    """CLI: driver listázás (online/offline) és törlés. A CliApi része (összerakás: app/cli/api.py)."""

    # ================================================================
    # DRIVER KEZELÉS
    # ================================================================
    def get_third_party_drivers(self):
        """Third-party driverek listája."""
        self._print_progress("📋 Third-party driverek lekérdezése...")
        return drivers_core.get_third_party_drivers(self._run)

    def get_all_drivers(self):
        """Összes driver listája (veszélyes mód)."""
        self._print_progress("📋 Összes driver lekérdezése (PowerShell)...")
        try:
            return drivers_core.get_all_drivers(self._run)
        except Exception as e:
            logging.warning(f"[DRIVERS_CLI] Összes-driver lista értelmezése sikertelen (üres listával folytatunk): {e}")
            return []

    def get_offline_drivers(self, all_drivers=False):
        """Offline OS driverek listája."""
        self._print_progress(f"📋 Offline driverek lekérdezése: {self.target_os_path}...")
        return drivers_core.get_offline_drivers(self._run, self.target_os_path, all_drivers)

    def list_drivers(self, all_drivers=False):
        """Driver lista megjelenítése."""
        if self.target_os_path:
            drivers = self.get_offline_drivers(all_drivers)
        elif all_drivers:
            drivers = self.get_all_drivers()
        else:
            drivers = self.get_third_party_drivers()

        if not drivers:
            print("❌ Nincs találat vagy hiba történt.")
            return []

        mode = "ÖSSZES" if all_drivers else "Third-party"
        loc = f" ({self.target_os_path})" if self.target_os_path else ""
        print(f"\n{'='*60}")
        print(f"  {mode} driverek{loc}: {len(drivers)} db")
        print(f"{'='*60}")
        print(f"{'#':>4}  {'Published':<18} {'Provider':<25} {'Class':<15}")
        print("-" * 70)
        for i, d in enumerate(drivers, 1):
            pub = d.get('published', '?')[:17]
            prov = d.get('provider', '?')[:24]
            cls = d.get('class', '?')[:14]
            print(f"{i:4}  {pub:<18} {prov:<25} {cls:<15}")
        print("-" * 70)
        return drivers

    def delete_drivers(self, drivers, list_all=False, reboot=False):
        """Driverek törlése."""
        total = len(drivers)
        print(f"\n🗑️  {total} driver törlése indul...")
        print("-" * 50)

        success = 0
        fail = 0
        is_offline = bool(self.target_os_path)

        for i, drv in enumerate(drivers, 1):
            pub = drv.get('published', '?')
            print(f"  [{i}/{total}] {pub}... ", end="", flush=True)

            is_oem = pub.lower().startswith("oem")
            # timeout: a pnputil a PnP query-remove-ra vár, és egy nem válaszoló eszköz
            # (terepen: Intel RST tárolóvezérlő) percekig lógatja - lásd DELETE_DRIVER_TIMEOUT.
            res = drivers_core.delete_driver_package(self._run, pub, self.target_os_path,
                                                     timeout=DELETE_DRIVER_TIMEOUT)

            if spawn_failed(res):
                # A folyamat el sem indult (0xC0000142): a session szétesett, minden további
                # törlés no-op lenne. Megállunk - a néma "sikeres törlés" sokkal rosszabb.
                print("❌")
                print("\n" + "!" * 50)
                print("A Windows nem tud több folyamatot indítani (0xC0000142).")
                print("A driver-törlés FÉLBEMARADT - indítsd újra a gépet, majd próbáld újra!")
                print("!" * 50)
                fail += 1
                break
            if getattr(res, 'returncode', 0) == CMD_TIMEOUT_RETURNCODE:
                print(f"⏱️ (időtúllépés {DELETE_DRIVER_TIMEOUT}s - az eszköz nem válaszol)")
                fail += 1
                continue

            if drivers_core.delete_succeeded(res):
                print("✅")
                success += 1
            else:
                # A GUI verzióval egyezően az agresszív force-delete fallback (takeown/
                # icacls/rmtree) csak "ÖSSZES driver" módban fut le - third-party
                # (list_all=False) nézetben egy sikertelen törlés egyszerűen sikertelen
                # marad (lásd drivers_core.force_delete_driver_files).
                if list_all and not is_oem and drivers_core.force_delete_driver_files(self._run, pub, self.target_os_path):
                    print("✅ (force)")
                    success += 1
                else:
                    print("❌")
                    fail += 1

        print("-" * 50)
        print(f"✅ Sikeres: {success}  |  ❌ Sikertelen: {fail}")

        # Post-delete scan
        if not is_offline and success > 0:
            print("\n🔄 Hardverek újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
            time.sleep(2)
            print("✅ Kész!")

            if reboot:
                print("\n🔄 Újraindítás 5 másodperc múlva...")
                time.sleep(5)
                self._run(['shutdown', '/r', '/t', '0', '/f'])

        return success, fail
