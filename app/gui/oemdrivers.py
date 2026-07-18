"""DriverVarázsló GUI - OEM (gyártói) driver-oldal ajánló.

Márkás gépeknél (Dell/Lenovo/HP) a gyártó saját oldalán vannak olyan modell-
specifikus driverek (hotkey, power/thermal, dokkoló, ujjlenyomat), amiket se a
WU, se a Microsoft Update Catalog nem ad rendesen. Ez a mixin a szken végén
azonosítja a gyártót + sorozatszámot, és a GÉPRE SZABOTT hivatalos driver-oldal
linkjét adja kártyaként:
  - Dell: Service Tag-es deep-link egyenesen a gép driver-oldalára,
  - Lenovo: sorozatszám-alapú keresőlink (a support-kereső a sorozatszámról a
    termékoldalra visz),
  - HP: driver-oldal + a kártyán kiírt sorozatszám (a HP oldala kéri be).
Telepítőt NEM tölt le és NEM futtat - link-out, mint az AMD/Intel kártyáknál.
Minden lépés hibatűrő: hiba esetén a kártya egyszerűen nem jelenik meg."""

# === AUTO-IMPORTS ===
import json
import logging
import urllib.parse
# === /AUTO-IMPORTS ===


class GuiOemDriversMixin:
    """OEM driver-oldal ajánló. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _check_oem_driver_page(self):
        """A hardver-szkennelés végén hívódik. Ismert gyártónál 'oem_driver_info'
        eventtel modell-specifikus support-linket küld. Minden hibát elnyel."""
        try:
            ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                  "$cs = Get-WmiObject Win32_ComputerSystem | Select-Object Manufacturer, Model; "
                  "$bios = Get-WmiObject Win32_BIOS | Select-Object SerialNumber; "
                  "@{Man=$cs.Manufacturer; Model=$cs.Model; Serial=$bios.SerialNumber} | ConvertTo-Json -Compress")
            res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=60)
            data = json.loads(res.stdout) if res and (res.stdout or '').strip() else {}
            man = (data.get('Man') or '').strip()
            model = (data.get('Model') or '').strip()
            serial = (data.get('Serial') or '').strip()
            man_l = man.lower()
            # OEM placeholder sorozatszámok kiszűrése
            if serial.lower() in ('to be filled by o.e.m.', 'default string', 'system serial number', 'none', ''):
                serial = ''

            vendor = None
            url = None
            note = ''
            if 'dell' in man_l:
                vendor = 'Dell'
                if serial:
                    # Service Tag-es deep-link: egyenesen a GÉP saját driver-oldala.
                    url = f'https://www.dell.com/support/home/hu-hu/product-support/servicetag/{urllib.parse.quote(serial)}/drivers'
                    note = f'Service Tag: {serial}'
                else:
                    url = 'https://www.dell.com/support/home/hu-hu?app=drivers'
            elif 'lenovo' in man_l:
                vendor = 'Lenovo'
                if serial:
                    url = f'https://pcsupport.lenovo.com/hu/hu/search?query={urllib.parse.quote(serial)}'
                    note = f'Sorozatszám: {serial}'
                else:
                    url = 'https://pcsupport.lenovo.com/hu/hu/'
            elif 'hewlett' in man_l or man_l.startswith('hp'):
                vendor = 'HP'
                url = 'https://support.hp.com/hu-hu/drivers'
                if serial:
                    note = f'Sorozatszám (az oldal bekéri): {serial}'
            if not vendor:
                logging.debug(f"[OEM] Nem ismert OEM gyártó ({man}), kártya kihagyva.")
                return

            logging.info(f"[OEM] {vendor} gép: {model}, serial={serial or '?'} -> {url}")
            self.emit('oem_driver_info', {'vendor': vendor, 'model': model,
                                          'serial': serial, 'note': note, 'url': url})
        except Exception as e:
            logging.warning(f"[OEM] Gyártói oldal ajánló hiba (nem kritikus): {e}")
