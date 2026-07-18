"""DriverVarázsló CLI - CLI: Windows Update driver-tiltás/engedélyezés/szüneteltetés
(a közös logika: app/wusettings_core.py)."""

# === AUTO-IMPORTS ===
from app import wusettings_core
# === /AUTO-IMPORTS ===


class CliWuMixin:
    """CLI: Windows Update driver-tiltás/engedélyezés/szüneteltetés. A CliApi része (összerakás: app/cli/api.py)."""

    # ================================================================
    # WINDOWS UPDATE
    # ================================================================
    def check_wu_status_cli(self):
        """WU driver frissítés állapota."""
        st = wusettings_core.read_wu_status(self._run)

        drv_status = "✅ ENGEDÉLYEZVE"
        if st['policy_disabled'] and st['search_disabled']:
            drv_status = "⛔ LETILTVA (policy + eszközbeállítások)"
        elif st['policy_disabled']:
            drv_status = "⛔ LETILTVA (policy)"
        elif st['search_disabled']:
            drv_status = "⛔ LETILTVA (eszközbeállítások)"

        if st['service_disabled']:
            return f"⛔ Szolgáltatás LETILTVA (services.msc) | Driverek: {drv_status}"
        if st['paused_until']:
            paused = st['paused_until']
            date_only = paused.split('T')[0] if 'T' in paused else paused
            return f"SZÜNETELTETVE ({date_only}) | Driverek: {drv_status}"
        return drv_status

    def disable_wu_drivers(self):
        """WU driver frissítések letiltása."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return

        print("\n⛔ WU driver frissítések letiltása...")
        print("-" * 50)
        wusettings_core.disable_wu_full(self._run, lambda msg: print(f"  {msg}"))
        print("-" * 50)
        print("✅ WU driver letiltás kész (Cache ürítve)!")

    def enable_wu_drivers(self):
        """WU driver frissítések engedélyezése + teljes reset."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return

        print("\n✅ WU driver frissítések engedélyezése + reset...")
        print("-" * 50)
        wusettings_core.enable_wu_reset(self._run, lambda msg: print(f"  {msg}"))
        print("-" * 50)
        print("✅ WU engedélyezés + reset kész!")

    def restart_wu_services(self):
        """WU szolgáltatások újraindítása."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return

        print("\n🔄 WU szolgáltatások újraindítása...")
        print("-" * 50)
        wusettings_core.restart_wu_services(self._run, lambda msg: print(f"  {msg}"))
        print("-" * 50)
        print("✅ WU szolgáltatások újraindítva!")

    def pause_wu(self, days):
        """Windows Update szüneteltetése N napra (a GUI verzió CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: Offline módban nem elérhető!")
            return

        print(f"\n⏸️  WU szüneteltetése ({days} nap)...")
        print("-" * 50)
        new_date = wusettings_core.pause_wu(self._run, lambda msg: print(f"  {msg}"), days)
        print("-" * 50)
        print(f"✅ Frissítések szüneteltetve idáig: {new_date}")

    def resume_wu(self):
        """Windows Update szüneteltetésének feloldása (a GUI verzió CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: Offline módban nem elérhető!")
            return

        print("\n▶️  WU szüneteltetés feloldása...")
        print("-" * 50)
        wusettings_core.resume_wu(self._run, lambda msg: print(f"  {msg}"))
        print("-" * 50)
        print("✅ Szüneteltetés feloldva!")
