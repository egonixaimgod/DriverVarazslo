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


# WMI/SMBIOS helykitöltő értékek: összerakott (nem márkás) gépeken a gyártó/modell/
# sorozatszám mezőkben ezek állnak - valódi adatként kezelve értelmetlen linket adnának.
OEM_PLACEHOLDER_VALUES = (
    'to be filled by o.e.m.', 'to be filled by oem', 'default string',
    'system manufacturer', 'system product name', 'system serial number',
    'oem', 'o.e.m.', 'none', 'not applicable', 'not specified', 'null', 'x.x.x')

# Alaplap-gyártók: összerakott gépeknél a gyári chipset-/audio-/LAN-driver EGYETLEN
# forrása az alaplap gyártójának oldala (a WU ezeket asztali alaplapokra jellemzően
# nem adja - terepen: ASRock B450M Pro4, ahol a Realtek hang a Microsoft generikus
# hdaudio.inf-jén, a LAN pedig az inbox rtcx21x64.inf-en futott a fix után is).
# Szándékosan a gyártó DOWNLOAD/SUPPORT nyitóoldalára megyünk, nem modell-mélylinkre:
# ezeknél az oldalszerkezet gyakran változik (az MSI/Gigabyte bot-védelem mögött van),
# egy elrohadt mélylink pedig rosszabb, mint egy biztosan élő nyitóoldal + a kiírt
# modellnév, amit a szerelő beír a kereső mezőbe. Ugyanaz a logika, mint a HP ágnál.
BOARD_VENDOR_PAGES = (
    (('asrock',), 'ASRock', 'https://www.asrock.com/support/index.asp'),
    (('asustek', 'asus'), 'ASUS', 'https://www.asus.com/support/download-center/'),
    (('micro-star', 'msi'), 'MSI', 'https://www.msi.com/support/download'),
    (('gigabyte', 'giga-byte'), 'GIGABYTE', 'https://www.gigabyte.com/Support'),
    (('biostar',), 'Biostar', 'https://www.biostar.com.tw/app/en/support/download.php'),
)


def _is_placeholder(value):
    """Igaz, ha a WMI-mező csak SMBIOS-helykitöltő (nem valódi gyártó/modell/serial)."""
    return (value or '').strip().lower() in OEM_PLACEHOLDER_VALUES


class GuiOemDriversMixin:
    """OEM driver-oldal ajánló. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _check_oem_driver_page(self):
        """A hardver-szkennelés végén hívódik. Ismert gyártónál 'oem_driver_info'
        eventtel modell-specifikus support-linket küld. Minden hibát elnyel."""
        try:
            ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                  "$cs = Get-WmiObject Win32_ComputerSystem | Select-Object -First 1 Manufacturer, Model; "
                  "$bios = Get-WmiObject Win32_BIOS | Select-Object -First 1 SerialNumber; "
                  "$bb = Get-WmiObject Win32_BaseBoard | Select-Object -First 1 Manufacturer, Product; "
                  "@{Man=$cs.Manufacturer; Model=$cs.Model; Serial=$bios.SerialNumber; "
                  "Board=$bb.Manufacturer; BoardModel=$bb.Product} | ConvertTo-Json -Compress")
            res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=60)
            data = json.loads(res.stdout) if res and (res.stdout or '').strip() else {}
            man = (data.get('Man') or '').strip()
            model = (data.get('Model') or '').strip()
            serial = (data.get('Serial') or '').strip()
            man_l = man.lower()
            # OEM placeholder sorozatszámok kiszűrése
            if _is_placeholder(serial):
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
            if vendor:
                title = f'{vendor} gép: {model}'
                desc = ('A gyártói oldalon vannak modell-specifikus driverek (hotkey, power, '
                        'dokkoló...), amiket a WU nem ad.')
            else:
                # ÖSSZERAKOTT GÉP: a gépgyártó mezők helykitöltők ("To Be Filled By O.E.M."),
                # de az ALAPLAP gyártója/modellje ilyenkor is valós - és pont ez a hasznos
                # adat: a gyári chipset-, hang- és LAN-driver az alaplap oldalán van.
                # Régen itt egyszerűen feladtuk, így minden nem márkás gépnél (szervizben a
                # gépek jó része ilyen) semmilyen mutató nem került a szerelő elé.
                board = (data.get('Board') or '').strip()
                board_model = (data.get('BoardModel') or '').strip()
                board_l = board.lower()
                for keywords, name, page in BOARD_VENDOR_PAGES:
                    if any(k in board_l for k in keywords):
                        vendor, url = name, page
                        break
                if not vendor:
                    logging.debug(f"[OEM] Nem ismert gépgyártó ({man}) és alaplap-gyártó ({board or '?'}), kártya kihagyva.")
                    return
                model = '' if _is_placeholder(board_model) else board_model
                title = f'{vendor} alaplap{": " + model if model else ""}'
                desc = ('Összerakott gép - a gyári chipset-, hang- (pl. Realtek Audio Console) és '
                        'LAN-driver az alaplap gyártójának oldalán van, ezeket a Windows Update '
                        'jellemzően nem adja fel.')
                if model:
                    note = f'Keresd ezt a modellt az oldalon: {model}'
                serial = ''

            logging.info(f"[OEM] {title}, serial={serial or '?'} -> {url}")
            self.emit('oem_driver_info', {'vendor': vendor, 'model': model, 'serial': serial,
                                          'note': note, 'url': url, 'title': title, 'desc': desc})
        except Exception as e:
            logging.warning(f"[OEM] Gyártói oldal ajánló hiba (nem kritikus): {e}")
