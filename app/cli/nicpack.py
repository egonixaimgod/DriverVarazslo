"""DriverVarázsló CLI - LAN driver vészcsomag (nicpack.zip) telepítése.

A közös logika az app/nicpack_core.py-ban él (a GUI is azt hívja)."""

# === AUTO-IMPORTS ===
import logging
from app.nicpack_core import _install_nicpack
# === /AUTO-IMPORTS ===


class CliNicPackMixin:
    """LAN driver vészcsomag telepítése. A CliApi része (összerakás: app/cli/api.py)."""

    def install_nic_pack(self):
        print("\n🛟 LAN DRIVER MENTŐCSOMAG (nicpack.zip) TELEPÍTÉSE")
        print("-" * 50)
        try:
            installed, total_inf = _install_nicpack(self._run, lambda msg: print(f"  {msg}"))
            print(f"\n✅ Kész: {installed} driver-csomag telepítve ({total_inf} INF-ből).")
            print("   Futtass hardver-újraszkennelést, majd ellenőrizd a hálózatot!")
        except Exception as e:
            logging.error(f"[NICPACK] CLI hiba: {e}")
            print(f"\n❌ Hiba: {e}")
            print("ℹ️ Tipp: másold a nicpack.zip-et az exe mellé (szerviz-USB), akkor internet nélkül is működik.")
