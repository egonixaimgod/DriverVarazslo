"""DriverVarázsló GUI - Bluetooth tiszta lap: minden párosítás és BT-eszközcsomópont
törlése (a közös mag és az indoklás: app/bluetooth_core.py)."""

# === AUTO-IMPORTS ===
import logging
from app.bluetooth_core import clear_bluetooth
from app.win32 import enumerate_device_nodes
# === /AUTO-IMPORTS ===


class GuiBluetoothMixin:
    """Bluetooth tiszta lap. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def clear_bluetooth_devices(self):
        logging.info("[API] clear_bluetooth_devices()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!', 'type': 'error'})
            return

        def worker():
            self.emit('task_start', {'task': 'bluetooth', 'title': 'Bluetooth tiszta lap'})
            self.emit('task_progress', {'task': 'bluetooth', 'indeterminate': True,
                                        'log': 'Bluetooth-eszközök és párosítások felderítése...'})

            def log(line):
                self.emit('task_progress', {'task': 'bluetooth', 'log': line, 'status': line.strip()})

            rep = clear_bluetooth(self._run, log, enumerate_device_nodes, self._check_cancel)
            if rep['cancelled']:
                log('\n❗ Megszakítva - az adapter és a szolgáltatások visszaálltak, a törlés félbemaradt.')
                self.emit('task_complete', {'task': 'bluetooth', 'status': '❗ Megszakítva!'})
                return
            n_pair = sum(1 for p in rep['pairings'] if p.startswith('Devices'))
            log(f"\nPárosítás törölve: {n_pair}, eszközcsomópont eltávolítva: {rep['removed']} / {rep['total']}")
            if rep['pairings_left'] is None:
                log('❌ A párosítás-adatbázis törlése NEM ellenőrizhető (a SYSTEM-lépés nem ért végig).')
            elif rep['pairings_left']:
                log(f"❌ Bent maradt {rep['pairings_left']} párosítás-bejegyzés.")
            if rep['left_nodes'] is None:
                log('❌ A maradék csomópontok nem olvashatók vissza.')
            elif rep['left_nodes']:
                log(f"❌ Bent maradt {len(rep['left_nodes'])} csomópont: {', '.join(rep['left_nodes'][:10])}")
            if rep['stuck']:
                log('⏱️ A törlés beragadt: indítsd újra a gépet, és futtasd újra (a párosítások már törölve, '
                    'másodszorra a csomópontok gyorsan mennek).')
            for e in rep['errors']:
                log(f'⚠️ {e}')
            for name in rep['radio_problems']:
                log(f'❌ Az adapter NEM indult vissza: {name} - Eszközkezelőben engedélyezd, vagy indítsd újra a gépet.')
            if rep['services_down']:
                log(f"❌ Nem indult vissza: {', '.join(rep['services_down'])} - indítsd újra a gépet.")
            if rep['ok']:
                log('✅ A Bluetooth tiszta lappal indul. Ha a Beállításokban még látszik valami, indítsd újra a gépet.')
                self.emit('task_complete', {'task': 'bluetooth', 'status': f'Kész! {n_pair} párosítás törölve'})
            else:
                self.emit('task_complete', {'task': 'bluetooth', 'status': '⚠️ Nem minden sikerült - részletek fent és a naplóban'})

        self._safe_thread('bluetooth', worker)
