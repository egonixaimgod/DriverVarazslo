"""DriverVarázsló GUI - LAN driver vészcsomag (nicpack.zip) telepítése.

A közös logika az app/nicpack_core.py-ban él (a CLI is azt hívja) - ez a mixin
csak a GUI-s progress/toast réteget adja hozzá."""

# === AUTO-IMPORTS ===
import time
import logging
from app.nicpack_core import _install_nicpack
# === /AUTO-IMPORTS ===


class GuiNicPackMixin:
    """LAN driver vészcsomag telepítése. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def install_nic_pack(self):
        """A nicpack.zip (LAN/Wi-Fi vész-driverek) telepítése - kifejezetten arra az
        esetre, amikor a gépnek nincs hálózati drivere, ezért internet sincs. A csomagot
        ezért ELŐSZÖR helyben keressük (exe mellett / app-mappában), letöltés csak
        tartalék."""
        logging.info("[API] install_nic_pack()")

        def worker():
            self.emit('task_start', {'task': 'nicpack', 'title': '🛟 LAN Driver Mentőcsomag Telepítése'})

            def progress(msg):
                self.emit('task_progress', {'task': 'nicpack', 'log': msg, 'indeterminate': True})

            try:
                installed, total_inf = _install_nicpack(self._run, progress)
                progress(f'✅ Kész: {installed} driver-csomag került a driver store-ba ({total_inf} INF-ből).')
                time.sleep(3)
                if self._check_internet():
                    progress('🌐 Internetkapcsolat ÉL! Most már futtathatod a hardver-szkennelést vagy az AutoFixet.')
                    self.emit('task_complete', {'task': 'nicpack', 'status': '✅ Hálózat rendben - mehet a driver-keresés!'})
                else:
                    progress('⚠️ Internet még mindig nincs - ha a hálózati kártya nem kapott drivert a csomagból, kézi driver kell.')
                    self.emit('task_complete', {'task': 'nicpack', 'status': f'✅ {installed} csomag telepítve (net még nincs)'})
            except Exception as e:
                logging.error(f"[NICPACK] Hiba: {e}", exc_info=True)
                self.emit('task_progress', {'task': 'nicpack', 'log': f'❌ {e}'})
                self.emit('task_progress', {'task': 'nicpack', 'log': 'ℹ️ Tipp: másold a nicpack.zip-et az exe mellé a szerviz-USB-n, akkor internet nélkül is működik.'})
                self.emit('task_complete', {'task': 'nicpack', 'status': f'❌ Hiba: {e}'})

        self._safe_thread('nicpack', worker)
