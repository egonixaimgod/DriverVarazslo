r"""A GÉP FELÉPÍTÉSE: alkatrészek (videókártya, processzor, alaplap -> hang/hálózat/chipset,
csatlakoztatott eszközök) és a hozzájuk tartozó driver-csomagok.

MIÉRT LÉTEZIK (explicit user decision, 2026-09-17): a Driverek nézet Class oszlopa
alapján a technikus nem tudta megmondani, melyik driver MINEK a drivere - és ezt
mérés igazolta: a Class a driver GYÁRTÓJÁNAK besorolása arról, milyen TÍPUSÚ
Windows-eszközt telepít, nem arról, melyik alkatrészhez való. A fejlesztői gép 144
csomagján:
  - `Camera`  = egy Samsung TELEFON kameramódja, nem webkamera;
  - `Display` = egy virtuális monitor-szoftver (Amyuni), nem videókártya;
  - `MEDIA`   = az alaplapi Realtek hang ÉS a processzor-grafika HDMI-hangja egyaránt;
  - `Modem`/`Ports`/`Net`/`USB` = zömében Samsung telefon-driverek;
  - a `System` osztályú "High Definition Audio hangvezérlő" valójában a HANG vezérlője.
A Class tehát pontosan KI VAN OLVASVA, csak mást jelent, mint amit a felületen sugall.

=============================================================================
MIBŐL DOLGOZIK - A BIZONYÍTÉKOK ERŐSORRENDJÉBEN
=============================================================================

1. A KÖTÉS: melyik eszköz-példány használja az INF-et (a Windows eszközfája,
   `DEVPKEY_Device_DriverInfPath`). Ez nem következtetés, hanem a Windows saját
   nyilvántartása - ugyanaz, amit az Eszközkezelő mutat.
2. A CONTAINER ID: a Windows ugyanazzal a GUID-dal jelöl minden eszközt, ami a
   SZÁMÍTÓGÉP RÉSZE, és külön container-t ad minden külső eszköznek (egér, nyomtató,
   monitor, telefon). Ez az "Eszközök és nyomtatók" ablak saját csoportosítása.
3. A HARDVER OSZTÁLYKÓDJA: `PCI\CC_0403` (hang), `PCI\CC_0200` (Ethernet), `USB\Class_0E`
   (videó) - ezt az ESZKÖZ SZILÍCIUMA jelenti a busznak, nem a driver gyártója írja be.
4. A TOPOLÓGIA: egy PCIe-porton ülő, csupa azonos gyártójú funkcióból álló kártya
   DEDIKÁLT videókártya; a rendszerbuszon közvetlenül ülő (vagy vegyes gyártójú hídon
   osztozó) grafika a processzorba/chipsetbe ÉPÍTETT.
5. Csak ezek híján: a Windows-osztály, végül a név.

Eszközhöz nem kötött csomagnál (pl. egy sosem csatlakoztatott telefon drivere) az
INF-ből dolgozunk: a gyártó adta modell-leírásból ("SAMSUNG Mobile USB Modem"), a
hardver-azonosítók buszából és gyártókódjából (USB `VID_04E8` = Samsung).

=============================================================================
EZ BESOROLÁS, NEM SZŰRŐ
=============================================================================

A CLAUDE.md "MINDEN DRIVERT LEHESSEN TÖRÖLNI" szabálya érvényes: a modul egyetlen
csomagot sem rejt el, nem tilt le és nem véd meg - csak megmondja, hova tartozik, és
miért. A törlés változatlanul mindenre megy, amit a technikus kipipál.

A modul nem hív subprocesst és nem importál ctypes-t: a nyers adatokat (eszközfa,
INF-tények, használat, gépazonosító) a hívó adja át, így minden döntés offline,
valódi terepi adaton tesztelhető.
"""

# === AUTO-IMPORTS ===
import re
import logging
import collections
# === /AUTO-IMPORTS ===


PC_CONTAINER_ID = '00000000-0000-0000-ffff-ffffffffffff'

# --- FUNKCIÓK ---------------------------------------------------------------
F_GPU, F_CPU, F_AUDIO, F_NET, F_BT = 'gpu', 'cpu', 'audio', 'network', 'bluetooth'
F_STORAGE, F_USB, F_CHIPSET, F_SECURITY, F_FIRMWARE = 'storage', 'usb', 'chipset', 'security', 'firmware'
F_INPUT, F_CAMERA, F_BIO, F_CARD, F_SENSOR = 'input', 'camera', 'biometric', 'cardreader', 'sensor'
F_BATTERY, F_PRINTER, F_PHONE, F_MONITOR = 'battery', 'printer', 'phone', 'monitor'
F_SOFTWARE, F_OTHER, F_INHERIT = 'software', 'other', 'inherit'

# (ikon, címke az alaplap-/beépített szekcióhoz, címke a "nincs hozzá eszköz" csoporthoz)
FUNC_META = {
    F_GPU:      ('🎮', 'Grafika', 'Videókártyához / grafikához'),
    F_CPU:      ('🧠', 'Processzor', 'Processzorhoz'),
    F_AUDIO:    ('🔊', 'Hang', 'Hanghoz'),
    F_NET:      ('🌐', 'Hálózat (LAN / Wi-Fi)', 'Hálózathoz (LAN / Wi-Fi)'),
    F_BT:       ('📶', 'Bluetooth', 'Bluetooth-hoz'),
    F_STORAGE:  ('💽', 'Tároló-vezérlő és meghajtók', 'Tárolóhoz (SATA / NVMe / RAID)'),
    F_USB:      ('🔌', 'USB-vezérlők', 'USB-vezérlőhöz'),
    F_CHIPSET:  ('⚙️', 'Chipset és rendszervezérlők', 'Chipsethez / alaplaphoz'),
    F_SECURITY: ('🛡️', 'Biztonság (TPM)', 'Biztonsági eszközhöz'),
    F_FIRMWARE: ('🧬', 'BIOS / UEFI firmware', 'Firmware / BIOS'),
    F_INPUT:    ('⌨️', 'Beépített billentyűzet, touchpad', 'Beviteli eszközhöz (egér, billentyűzet, touchpad)'),
    F_CAMERA:   ('📷', 'Kamera', 'Kamerához / szkennerhez'),
    F_BIO:      ('🔒', 'Ujjlenyomat-olvasó', 'Ujjlenyomat-olvasóhoz'),
    F_CARD:     ('💳', 'Kártyaolvasó', 'Kártyaolvasóhoz'),
    F_SENSOR:   ('📡', 'Szenzorok', 'Szenzorhoz'),
    F_BATTERY:  ('🔋', 'Akkumulátor', 'Akkumulátorhoz'),
    F_PRINTER:  ('🖨️', 'Nyomtató', 'Nyomtatóhoz / szkennerhez'),
    F_PHONE:    ('📱', 'Telefon / mobileszköz', 'Telefonhoz / mobileszközhöz'),
    F_MONITOR:  ('🖥️', 'Monitor', 'Monitorhoz'),
    F_SOFTWARE: ('🧰', 'Szoftveres driver', 'Szoftveres / virtuális driver'),
    F_OTHER:    ('🧩', 'Egyéb', 'Nem azonosítható'),
}

# A külső eszközök kártyacímei. KÜLÖN tábla, mert a FUNC_META beépített címkéje
# ("Beépített billentyűzet, touchpad") egy USB-egérre hamis lenne - az első élő
# futás pontosan ezt írta ki a HP USB Optical Mouse-ra.
EXT_LABEL = {
    F_MONITOR: 'Monitor', F_PRINTER: 'Nyomtató / szkenner', F_INPUT: 'Beviteli eszköz',
    F_AUDIO: 'Hangeszköz (fejhallgató, hangszóró)', F_CAMERA: 'Kamera / webkamera',
    F_STORAGE: 'Külső tároló', F_NET: 'Hálózati adapter', F_BT: 'Bluetooth-eszköz',
    F_PHONE: 'Telefon / mobileszköz', F_OTHER: 'Külső eszköz',
}

# Az alaplap-alcsoportok sorrendje - a technikus így gondolkodik a gépről.
BOARD_ORDER = [F_MONITOR, F_AUDIO, F_NET, F_BT, F_STORAGE, F_USB, F_CHIPSET, F_SECURITY, F_FIRMWARE,
               F_INPUT, F_CAMERA, F_BIO, F_CARD, F_SENSOR, F_BATTERY, F_OTHER]
# A külső eszközök sorrendje.
EXT_ORDER = [F_MONITOR, F_PRINTER, F_INPUT, F_AUDIO, F_CAMERA, F_STORAGE, F_NET, F_BT, F_PHONE, F_OTHER]
# A "nincs hozzá eszköz" csoportok sorrendje.
NODEV_ORDER = [F_GPU, F_AUDIO, F_NET, F_BT, F_CHIPSET, F_STORAGE, F_USB, F_INPUT, F_CAMERA, F_BIO,
               F_CARD, F_SECURITY, F_FIRMWARE, F_MONITOR, F_PRINTER, F_PHONE, F_OTHER]

# Egy alaplap-alcsoport kártyája akkor is megjelenik, ha nincs hozzá gyári csomag -
# ezekkel a funkciókkal a technikus/ügyfél a gép felépítését látja ("a hang a Windows
# beépített driverén fut"). A többi (pl. szenzor) csak gyári csomaggal kerül ki, különben
# zaj lenne. Laptopon a beépített perifériák is a gép részei.
BOARD_HEADLINE = {F_AUDIO, F_NET, F_BT, F_STORAGE, F_USB, F_CHIPSET, F_SECURITY, F_FIRMWARE}
BOARD_HEADLINE_LAPTOP = BOARD_HEADLINE | {F_MONITOR, F_INPUT, F_CAMERA, F_BIO, F_CARD, F_BATTERY}

# --- HARDVER OSZTÁLYKÓDOK -----------------------------------------------------
_PCI_CC_RE = re.compile(r'^PCI\\CC_([0-9A-F]{2})([0-9A-F]{2})?', re.IGNORECASE)
_USB_CLASS_RE = re.compile(r'^USB\\CLASS_([0-9A-F]{2})(?:&SUBCLASS_([0-9A-F]{2}))?(?:&PROT_([0-9A-F]{2}))?',
                           re.IGNORECASE)
_PCI_VEN_RE = re.compile(r'^PCI\\VEN_([0-9A-F]{4})', re.IGNORECASE)
_HDA_VEN_RE = re.compile(r'^HDAUDIO\\FUNC_(\d\d).*?VEN_([0-9A-F]{4})', re.IGNORECASE)
_USB_VID_RE = re.compile(r'\bVID_([0-9A-F]{4})', re.IGNORECASE)


def _pci_class_function(base, sub):
    """PCI-SIG osztálykód -> funkció. A kódot a hardver jelenti, ez a legerősebb jel."""
    base, sub = (base or '').upper(), (sub or '').upper()
    if base == '01':
        return F_STORAGE
    if base == '02':
        return F_NET
    if base == '03':
        return F_GPU
    if base == '04':
        return F_CAMERA if sub == '00' else F_AUDIO
    if base == '08':
        return F_CARD if sub == '05' else F_CHIPSET
    if base == '09':
        return F_INPUT
    if base in ('0B', '12'):
        # 0B = koprocesszor, 12 = feldolgozó-gyorsító (NPU): a processzor része.
        return F_CPU
    if base == '0C':
        if sub in ('03', '0A', '00'):
            return F_USB           # USB / Thunderbolt / FireWire
        if sub == '04':
            return F_STORAGE       # Fibre Channel
        return F_CHIPSET           # SMBus stb.
    if base == '0D':
        return F_BT if sub == '11' else F_NET
    if base == '0F':
        return F_NET
    if base == '10':
        return F_SECURITY
    if base in ('05', '06', '07', '0A', '0E', '11', '13'):
        return F_CHIPSET
    return None


def _usb_class_function(cls, sub, prot):
    cls, sub, prot = (cls or '').upper(), (sub or '').upper(), (prot or '').upper()
    return {
        '01': F_AUDIO, '02': F_NET, '03': F_INPUT, '05': F_INPUT, '06': F_CAMERA,
        '07': F_PRINTER, '08': F_STORAGE, '09': F_USB, '0A': F_NET, '0B': F_SECURITY,
        '0D': F_SECURITY, '0E': F_CAMERA, '10': F_CAMERA,
    }.get(cls) or (
        (F_BT if (sub == '01' and prot in ('01', '04')) else F_NET) if cls == 'E0' else None)


# --- WINDOWS-OSZTÁLYOK (csak a hardver-jelek híján) ------------------------------
SETUP_CLASS_FUNC = {
    'display': F_GPU, 'media': F_AUDIO, 'audioprocessingobject': F_AUDIO,
    'audioendpoint': F_INHERIT, 'net': F_NET, 'netservice': F_SOFTWARE,
    'nettrans': F_SOFTWARE, 'netclient': F_SOFTWARE, 'bluetooth': F_BT, 'infrared': F_NET,
    'hdc': F_STORAGE, 'scsiadapter': F_STORAGE, 'diskdrive': F_STORAGE, 'cdrom': F_STORAGE,
    'floppydisk': F_STORAGE, 'fdc': F_STORAGE, 'mediumchanger': F_STORAGE, 'tapedrive': F_STORAGE,
    'volume': F_INHERIT, 'volumesnapshot': F_INHERIT, 'mtd': F_STORAGE,
    'usb': F_USB, 'ucm': F_USB, 'usbfunctioncontroller': F_USB,
    'system': F_CHIPSET, 'computer': F_CHIPSET, 'processor': F_CPU, 'ports': F_CHIPSET,
    'multiportserial': F_CHIPSET, 'firmware': F_FIRMWARE, 'securitydevices': F_SECURITY,
    'smartcardreader': F_SECURITY, 'smartcard': F_SECURITY, 'smartcardfilter': F_SECURITY,
    'biometric': F_BIO, 'keyboard': F_INPUT, 'mouse': F_INPUT, 'hidclass': F_INPUT,
    'camera': F_CAMERA, 'image': F_CAMERA,
    'printer': F_PRINTER, 'printqueue': F_PRINTER, 'wsdprintdevice': F_PRINTER, 'dot4': F_PRINTER,
    'dot4print': F_PRINTER, 'printerupgrade': F_PRINTER, 'pnpprinters': F_PRINTER,
    'monitor': F_MONITOR, 'modem': F_NET, 'battery': F_BATTERY, 'sensor': F_SENSOR,
    'sdhost': F_CARD, 'wpd': F_PHONE, 'androidusbdeviceclass': F_PHONE,
    'eclairandroidusbdeviceclass': F_PHONE, 'androidaoadeviceclass': F_PHONE,
    'wireless communication devices': F_PHONE, 'wceusbs': F_PHONE,
    'softwarecomponent': F_INHERIT, 'extension': F_INHERIT, 'softwaredevice': F_SOFTWARE,
    'antivirus': F_SOFTWARE, 'securityfilter': F_SOFTWARE, 'legacydriver': F_SOFTWARE,
    'usbdevice': F_OTHER,
}

# Telefon- és mobilgyártók USB gyártókódjai. SZÁNDÉKOSAN NINCS BENNE a Lenovo (17EF):
# annak dokkolói és billentyűzetei is ezzel a kóddal jönnek.
PHONE_USB_VIDS = {
    '04E8': 'Samsung', '05AC': 'Apple', '18D1': 'Google', '2717': 'Xiaomi', '12D1': 'Huawei',
    '1004': 'LG', '22B8': 'Motorola', '0FCE': 'Sony', '0BB4': 'HTC', '05C6': 'Qualcomm',
    '0E8D': 'MediaTek', '2A70': 'OnePlus', '19D2': 'ZTE', '1BBB': 'TCL/Alcatel', '2D95': 'Vivo',
    '22D9': 'Oppo', '0421': 'Nokia', '2A45': 'Meizu',
}
# A GPU-gyártók PCI-kódjai (a HDMI/DP-hang besorolásához).
GPU_PCI_VENDORS = {'10DE': 'NVIDIA', '1002': 'AMD', '8086': 'Intel'}

_PHONE_NAME_RE = re.compile(r'\b(android|iphone|ipad|ipod|mobile|phone|adb|mtp|galaxy|symbian|'
                            r'bootloader|fastboot|kies)\b', re.IGNORECASE)
_IGPU_NAME_RE = re.compile(r'\b(u?hd graphics|iris|radeon\(tm\) graphics|radeon graphics|'
                           r'vega \d+ graphics|arc\(tm\) graphics)\b', re.IGNORECASE)

# Név-kulcsszavak - CSAK a gyenge osztályokra (System, Extension, SoftwareComponent,
# USBDevice...) és az INF-alapú rendeltetéshez. A sorrend számít: a "graphics" előbb,
# mint az "audio" (az "Intel Display Audio" a grafika része).
_NAME_KEYWORDS = [
    (re.compile(r'display audio|hdmi audio|graphics|display|geforce|radeon|quadro|\bgpu\b', re.I), F_GPU),
    (re.compile(r'bluetooth', re.I), F_BT),
    (re.compile(r'audio|sound|speaker|microphone|\bhda\b', re.I), F_AUDIO),
    (re.compile(r'ethernet|wi-?fi|wireless|wlan|\blan\b|network|gbe|killer', re.I), F_NET),
    (re.compile(r'fingerprint|biometric', re.I), F_BIO),
    (re.compile(r'card reader|sd host|cardreader|memory card', re.I), F_CARD),
    (re.compile(r'camera|webcam|\buvc\b|scanner', re.I), F_CAMERA),
    (re.compile(r'sata|ahci|nvme|raid|rapid storage|storage|optane|\bdisk\b', re.I), F_STORAGE),
    (re.compile(r'xhci|usb 3|host controller|thunderbolt|usb4', re.I), F_USB),
    (re.compile(r'touchpad|touch pad|pointing|trackpad|keyboard|mouse|\bhid\b|precision touch|ultranav', re.I), F_INPUT),
    (re.compile(r'firmware|\bbios\b|uefi', re.I), F_FIRMWARE),
    (re.compile(r'\btpm\b|trusted platform|smart ?card', re.I), F_SECURITY),
    (re.compile(r'management engine|chipset|smbus|serial io|thermal|dynamic platform|lpc|'
                r'pci express root|host bridge|watchdog|gpio|i2c|power engine|\bmei\b|'
                r'active management|\bsol\b|dptf|\bgna\b', re.I), F_CHIPSET),
    (re.compile(r'battery', re.I), F_BATTERY),
    (re.compile(r'sensor|accelerometer', re.I), F_SENSOR),
]

_SOFTWARE_ID_PREFIXES = ('ROOT\\', 'SWD\\', 'SW\\', 'HTREE\\', 'UMB\\')
# Az "örökölt" (szülőjükhöz tartozó) szoftveres csomópontok buszai: egy videókártya
# vezérlőpult-komponense (SWD\DRIVERENUM) a videókártyáé, egy hangszóró-végpont
# (SWD\MMDEVAPI) a hangkártyáé.
_INHERIT_ID_PREFIXES = ('SWD\\DRIVERENUM\\', 'SWD\\MMDEVAPI\\', 'SWC\\')
_GENERIC_NAME_RE = re.compile(
    r'^(usb (kompozit|composite|beviteli|input)|hid[- ]|általános|generic|usb (eszköz|device)$|'
    r'usb-hub|usb hub|szoftveres eszköz|software device)', re.IGNORECASE)


def _upper_list(v):
    if not v:
        return []
    if isinstance(v, str):
        v = [v]
    return [str(x).upper() for x in v if x]


def node_name(node):
    return (node.get('friendly') or node.get('desc') or node.get('id') or '').strip()


def _pci_vendor(node):
    m = _PCI_VEN_RE.match(node.get('id') or '')
    return m.group(1).upper() if m else ''


def node_function(node):
    """Egy eszköz-csomópont funkciója és a döntés FORRÁSA -> (funkció, indok). TISZTA.

    Erősorrend: a hardver által jelentett osztálykód (PCI/USB/HDAUDIO) -> a busz
    (MONITOR, PRINTENUM) -> a Windows-osztály -> telefon-gyártókód -> a név."""
    dev_id = (node.get('id') or '').upper()
    compat = _upper_list(node.get('compat'))
    cls = (node.get('cls') or '').strip().lower()

    if dev_id.startswith('PCI\\'):
        for c in compat:
            m = _PCI_CC_RE.match(c)
            if m:
                f = _pci_class_function(m.group(1), m.group(2))
                if f:
                    return f, f"a hardver PCI-osztálykódja: {m.group(1)}{m.group(2) or ''}"
    if dev_id.startswith('HDAUDIO\\FUNC_01'):
        return F_AUDIO, 'HD Audio busz, hang-funkció (FUNC_01)'
    if dev_id.startswith('HDAUDIO\\FUNC_02'):
        return F_NET, 'HD Audio busz, modem-funkció (FUNC_02)'
    usb_func = None
    usb_why = ''
    if dev_id.startswith('USB\\') or dev_id.startswith('USBPRINT\\'):
        best = None
        for c in compat:
            m = _USB_CLASS_RE.match(c)
            if m and (best is None or len(c) > len(best[0])):
                best = (c, m)
        if best:
            m = best[1]
            usb_func = _usb_class_function(m.group(1), m.group(2), m.group(3))
            usb_why = f"a hardver USB-osztálykódja: {m.group(1)}"
    if dev_id.startswith(('DISPLAY\\', 'MONITOR\\')) or cls == 'monitor':
        return F_MONITOR, 'monitor-busz (EDID)'
    if dev_id.startswith(('SWD\\PRINTENUM\\', 'USBPRINT\\', 'WSDPRINT\\', 'LPTENUM\\', 'DOT4\\')):
        return F_PRINTER, 'nyomtató-busz'
    if dev_id.startswith(_INHERIT_ID_PREFIXES):
        return F_INHERIT, 'a szülő eszközéhez tartozó szoftver-komponens'
    if dev_id.startswith('ACPI_HAL\\'):
        # A Windows ACPI/UEFI-gyökere ("Microsoft UEFI-kompatibilis rendszer"): a neve
        # miatt a firmware-kártyára került volna a valódi BIOS-csomópont helyett.
        return F_CHIPSET, 'a Windows ACPI/UEFI rendszergyökere'

    # Telefon: a Windows-osztály vagy a gyártókód mondja meg (egy telefon USB-n sokféle
    # osztályú interfészt hoz: modem, hálózat, MTP, ADB).
    if SETUP_CLASS_FUNC.get(cls) == F_PHONE:
        return F_PHONE, f"Windows-osztály: {node.get('cls')}"
    m = _USB_VID_RE.search(dev_id)
    if (m and m.group(1) in PHONE_USB_VIDS and dev_id.startswith('USB\\')
            and usb_func not in (F_PRINTER, F_STORAGE, F_INPUT, F_AUDIO)
            and SETUP_CLASS_FUNC.get(cls) not in (F_PRINTER, F_MONITOR, F_INPUT, F_STORAGE)):
        return F_PHONE, f"USB-gyártókód VID_{m.group(1)} ({PHONE_USB_VIDS[m.group(1)]})"

    if usb_func:
        return usb_func, usb_why
    f = SETUP_CLASS_FUNC.get(cls)
    if f and f not in (F_OTHER,) and cls not in ('system', 'usbdevice', 'hidclass'):
        return f, f"Windows-osztály: {node.get('cls')}"
    name = node_name(node)
    for rx, kf in _NAME_KEYWORDS:
        if rx.search(name):
            return kf, f"eszköznév: '{name}'"
    if f:
        return f, f"Windows-osztály: {node.get('cls')}"
    return F_OTHER, 'nincs azonosító jel'


# =============================================================================
# A GÉP ALKATRÉSZEKRE BONTÁSA
# =============================================================================

def _index(nodes):
    by_id = {}
    for n in nodes or []:
        if n.get('id'):
            by_id[n['id'].lower()] = n
    kids = collections.defaultdict(list)
    for n in nodes or []:
        p = (n.get('parent') or '').lower()
        if p and p in by_id:
            kids[p].append(n)
    return by_id, kids


def _descendants(root_id, kids):
    out, stack, seen = [], [root_id.lower()], set()
    while stack:
        cur = stack.pop()
        for k in kids.get(cur, []):
            kid = k['id'].lower()
            if kid in seen:
                continue
            seen.add(kid)
            out.append(k)
            stack.append(kid)
    return out


def find_gpus(nodes, by_id=None, kids=None):
    """A jelenlévő PCI-videókártyák és típusuk (dedikált / beépített). TISZTA.

    Visszatérés: [{'node', 'vendor', 'integrated': bool, 'card_root': id|None, 'why'}].

    A TOPOLÓGIA DÖNT, NEM A NÉV:
      - ha a GPU szülője nem PCI-híd (a rendszerbuszon ül közvetlenül, pl. Intel iGPU a
        00:02.0-n), akkor BEÉPÍTETT;
      - ha PCI-hídon ül, és a híd alatt MINDEN PCI-funkció a GPU gyártójától van
        (GPU + HDMI-hang + USB-C a kártyán), akkor DEDIKÁLT kártya - a kártya határa a
        legfelső ilyen híd;
      - ha a híd alatt más gyártó funkciói is vannak (AMD APU: a Radeon mellett AMD
        PSP, USB, ACP egy belső hídon), akkor BEÉPÍTETT.
    A név csak holtversenyben számít (egygyártós híd + tipikus iGPU-név)."""
    if by_id is None or kids is None:
        by_id, kids = _index(nodes)
    out = []
    for n in nodes or []:
        if not n.get('present') or not (n.get('id') or '').upper().startswith('PCI\\'):
            continue
        if node_function(n)[0] != F_GPU:
            continue
        vendor = _pci_vendor(n)
        parent = by_id.get((n.get('parent') or '').lower())
        if not parent or not (parent.get('id') or '').upper().startswith('PCI\\'):
            out.append({'node': n, 'vendor': vendor, 'integrated': True, 'card_root': None,
                        'why': 'közvetlenül a rendszerbuszon ül (a processzorba/chipsetbe épített grafika)'})
            continue

        def _single_vendor(root):
            vs = {_pci_vendor(d) for d in _descendants(root['id'], kids)
                  if (d.get('id') or '').upper().startswith('PCI\\')}
            vs.discard('')
            return vs == {vendor}

        if not _single_vendor(parent):
            out.append({'node': n, 'vendor': vendor, 'integrated': True, 'card_root': None,
                        'why': 'más gyártók vezérlőivel osztozik egy belső hídon (processzorba épített grafika)'})
            continue
        root = parent
        while True:
            gp = by_id.get((root.get('parent') or '').lower())
            if not gp or not (gp.get('id') or '').upper().startswith('PCI\\') or not _single_vendor(gp):
                break
            root = gp
        if _IGPU_NAME_RE.search(node_name(n)) and vendor != '10DE':
            out.append({'node': n, 'vendor': vendor, 'integrated': True, 'card_root': None,
                        'why': 'egygyártós hídon ül, de a neve beépített grafikára vall'})
            continue
        out.append({'node': n, 'vendor': vendor, 'integrated': False, 'card_root': root['id'],
                    'why': 'saját PCIe-porton ül, és minden funkciója ugyanattól a gyártótól van (bővítőkártya)'})
    # Dedikált előre, azon belül név szerint - így a felület sorrendje stabil.
    out.sort(key=lambda g: (g['integrated'], node_name(g['node']).lower()))
    return out


def assign_node_slots(nodes, form='desktop'):
    """Minden eszköz-csomópont -> alkatrész-hely (slot kulcs) + indok. TISZTA.

    Slot-kulcsok: `gpu:<id>`, `cpu`, `board:<funkció>`, `ext:<container>`, `software`,
    `absent:<funkció>` (a gép részeként nyilvántartott, de NEM jelenlévő eszköz - pl.
    egy korábbi videókártya a gépbe áttett lemezen).

    Visszatérés: ({id_kisbetű: (slot, indok)}, gpus)"""
    by_id, kids = _index(nodes)
    gpus = find_gpus(nodes, by_id, kids)
    slots = {}

    gpu_member = {}
    for g in gpus:
        key = 'gpu:' + g['node']['id'].lower()
        gpu_member[g['node']['id'].lower()] = (key, 'maga a grafikus vezérlő - ' + g['why'])
        if g['integrated']:
            for d in _descendants(g['node']['id'], kids):
                gpu_member.setdefault(d['id'].lower(), (key, 'a grafikus vezérlőhöz tartozik (eszközfa)'))
        else:
            for d in _descendants(g['card_root'], kids):
                gpu_member.setdefault(d['id'].lower(), (key, 'a videókártyán ül (ugyanaz a PCIe-port)'))
    igpu_by_vendor = {}
    for g in gpus:
        if g['integrated']:
            igpu_by_vendor.setdefault(g['vendor'], 'gpu:' + g['node']['id'].lower())
    gpu_by_vendor = {}
    for g in gpus:
        gpu_by_vendor.setdefault(g['vendor'], 'gpu:' + g['node']['id'].lower())

    def _slot(n, depth=0):
        nid = n['id'].lower()
        if nid in slots:
            return slots[nid]
        if depth > 64:
            return ('board:' + F_OTHER, 'túl mély eszközlánc')
        container = (n.get('container') or '').lower()
        dev_id = n['id'].upper()
        parent = by_id.get((n.get('parent') or '').lower())
        res = None
        if container and container != PC_CONTAINER_ID:
            res = ('ext:' + container, 'külső eszköz (saját Windows-container)')
        elif nid in gpu_member:
            res = gpu_member[nid]
        else:
            func, why = node_function(n)
            m = _HDA_VEN_RE.match(dev_id)
            if m and m.group(2).upper() in ('8086', '1002', '10DE') and m.group(1) == '01':
                # HDMI/DisplayPort-hang: az Intel/AMD/NVIDIA HD Audio kodek MINDIG a
                # grafika hangja (analóg hangkodeket ezek a gyártók nem gyártanak).
                target = gpu_by_vendor.get(m.group(2).upper())
                if target:
                    res = (target, f"a grafika HDMI/DP-hangja (HD Audio kodek, gyártókód {m.group(2)})")
            if res is None and func == F_AUDIO and dev_id.startswith('PCI\\'):
                ven = _pci_vendor(n)
                # AMD APU: a HDMI-hang saját PCI-funkció a belső hídon. Csak GPU-gyártónál
                # (1002/10DE) - az Intel 8086 a chipset HANG-vezérlője is lehet.
                if ven in ('1002', '10DE') and ven in igpu_by_vendor:
                    res = (igpu_by_vendor[ven], 'a beépített grafika HDMI/DP-hangvezérlője')
            if res is None:
                if func == F_INHERIT:
                    if parent is not None:
                        ps, _pw = _slot(parent, depth + 1)
                        res = (ps, 'a szülő eszközéhez tartozó komponens')
                    else:
                        res = ('software', 'szoftveres komponens, szülő nélkül')
                elif dev_id.startswith(_SOFTWARE_ID_PREFIXES):
                    res = ('software', 'szoftveres / virtuális eszköz (nem fizikai buszon ül)')
                elif func == F_CPU:
                    res = ('cpu', why)
                elif func == F_SOFTWARE:
                    res = ('software', why)
                elif func in (F_PRINTER, F_PHONE):
                    res = ('board:' + F_OTHER, why)
                else:
                    res = ('board:' + func, why)
        # NEM JELENLÉVŐ BELSŐ ESZKÖZ: nem tehetjük egy alkatrész alá, ami a gépben VAN -
        # a gépbe áttett lemezen egy korábbi gép videókártyája különben "a gép
        # videókártyájaként" jelenne meg. Ha a szülője jelen van (egy leállított
        # szoftver-komponens), marad a szülőnél.
        if not n.get('present') and not res[0].startswith('ext:') and res[0] != 'software':
            if parent is None or not parent.get('present'):
                func = node_function(n)[0]
                if func == F_INHERIT:
                    func = F_OTHER
                res = ('absent:' + func, 'a gép részeként van nyilvántartva, de most nincs jelen')
        slots[nid] = res
        return res

    for n in nodes or []:
        if n.get('id'):
            _slot(n)
    return slots, gpus


def container_category(members):
    """Egy külső eszköz (container) kategóriája a tagjai funkciójából. TISZTA.

    Elsőbbség: nyomtató > telefon > monitor > kamera > hang > tároló > hálózat >
    Bluetooth > beviteli. (Egy fejhallgató hang + HID -> hang; egy telefon MTP +
    hálózat + modem -> telefon; egy webkamera kép + mikrofon -> kamera.)"""
    funcs = {node_function(m)[0] for m in members}
    for f in (F_PRINTER, F_PHONE, F_MONITOR, F_CAMERA, F_AUDIO, F_STORAGE, F_NET, F_BT, F_INPUT):
        if f in funcs:
            return f
    return F_OTHER


def container_name(members):
    """A külső eszköz emberi neve: a legfelső tag hardver által jelentett neve
    ('HP USB Optical Mouse'), ennek híján a nem-generikus Windows-név."""
    ids = {m['id'].lower() for m in members}
    tops = [m for m in members if (m.get('parent') or '').lower() not in ids] or list(members)
    tops.sort(key=lambda m: (not m.get('present'), len(m.get('id') or '')))
    cands = []
    for m in tops + [x for x in members if x not in tops]:
        bd = (m.get('busdesc') or '').strip()
        if bd and '\n' not in bd and not bd.startswith('#') and len(bd) <= 80 and not _GENERIC_NAME_RE.match(bd):
            cands.append(bd)
        nm = node_name(m)
        if nm and not _GENERIC_NAME_RE.match(nm) and '\n' not in nm:
            cands.append(nm)
    return cands[0] if cands else (node_name(tops[0]) if tops else 'Külső eszköz')


# A laptop beépített eszközeinek címe. A kijelző a gép RÉSZE (a Windows a számítógép
# container-ébe teszi), tehát nem "Monitor", hanem "Beépített kijelző".
_BOARD_TITLE = {F_INPUT: 'Beépített billentyűzet, touchpad', F_CAMERA: 'Beépített kamera',
                F_MONITOR: 'Beépített kijelző'}


def _headline_nodes(func, members):
    """Egy alkatrész-kártyán megnevezett eszközök: a lényeg, a zaj nélkül (ACPI
    rendszer-csomópontok, USB-hubok, hangszóró-végpontok)."""
    pres = [m for m in members if m.get('present') and node_function(m)[0] != F_INHERIT]
    ids = {m['id'].lower() for m in pres}

    def _vendor_bound(m):
        return (m.get('inf') or '').lower().startswith('oem')
    if func == F_AUDIO:
        pick = [m for m in pres if m['id'].upper().startswith(('HDAUDIO\\', 'USB\\'))]
        return pick or [m for m in pres if m['id'].upper().startswith('PCI\\')]
    if func == F_CHIPSET:
        return [m for m in pres if m['id'].upper().startswith('PCI\\') or _vendor_bound(m)]
    if func == F_USB:
        return [m for m in pres if m['id'].upper().startswith('PCI\\') or _vendor_bound(m)]
    if func == F_STORAGE:
        return [m for m in pres if not m['id'].upper().startswith(('STORAGE\\', 'ROOT\\'))]
    if func == F_NET:
        return [m for m in pres if not m['id'].upper().startswith(_SOFTWARE_ID_PREFIXES)]
    if func == F_FIRMWARE:
        fw = [m for m in pres if (m.get('cls') or '').lower() == 'firmware']
        return fw or pres
    return [m for m in pres if (m.get('parent') or '').lower() not in ids]


def _device_entry(m):
    inf = (m.get('inf') or '').lower()
    return {'name': node_name(m), 'id': m.get('id') or '', 'present': bool(m.get('present')),
            'inf': inf, 'vendor_driver': inf.startswith('oem'), 'problem': m.get('problem') or 0}


# =============================================================================
# CSOMAG-RENDELTETÉS ESZKÖZ NÉLKÜL (az INF alapján)
# =============================================================================

def package_purpose(facts, pkg=None, printer=False):
    """Egy eszközhöz NEM kötött csomag rendeltetése az INF alapján -> (funkció, indok).
    TISZTA függvény.

    `facts`: a `driverusage_core.parse_inf_facts` kimenete; `pkg`: a dism-sor (osztály,
    gyártó) tartaléknak; `printer`: a közös nyomtató-felismerés
    (`wu_core.identify_printer_packages`) verdiktje - UGYANAZ, amit a nyomtató-szűrő és
    az AutoFix törlés-védelme használ."""
    facts = facts or {}
    pkg = pkg or {}
    cls = (facts.get('inf_class') or pkg.get('class') or '').strip().lower()
    descs = facts.get('descriptions') or []
    desc_txt = ' | '.join(descs)
    buses = {b.upper() for b in (facts.get('buses') or [])}
    usb_vids = [v.upper() for v in (facts.get('usb_vids') or [])]
    hda_vens = {v.upper() for v in (facts.get('hdaudio_vens') or [])}
    provider = (facts.get('provider') or pkg.get('provider') or '')
    shown = f"'{descs[0]}'" if descs else 'nincs modell-leírás'

    if printer:
        return F_PRINTER, f"nyomtató-driver (a közös nyomtató-felismerés szerint), INF: {shown}"
    phone_vids = [v for v in usb_vids if v in PHONE_USB_VIDS]
    if SETUP_CLASS_FUNC.get(cls) == F_PHONE:
        return F_PHONE, f"Windows-osztály: {facts.get('inf_class') or pkg.get('class')}, INF: {shown}"
    if phone_vids and cls not in ('display', 'monitor', 'printer', 'image', 'hdc', 'scsiadapter'):
        return F_PHONE, (f"USB-gyártókód VID_{phone_vids[0]} ({PHONE_USB_VIDS[phone_vids[0]]}, "
                         f"telefon-gyártó), INF: {shown}")
    if re.search(r'\bapple\b', provider, re.I) and 'PCI' not in buses:
        return F_PHONE, f"Apple mobileszköz-támogatás, INF: {shown}"
    if _PHONE_NAME_RE.search(desc_txt) and 'PCI' not in buses and cls not in ('display', 'monitor'):
        return F_PHONE, f"a modell-leírás mobileszközre utal: {shown}"
    if not facts.get('hwid_count'):
        return F_SOFTWARE, f"az INF nem nevez meg hardvert (szoftveres driver), osztály: {facts.get('inf_class') or pkg.get('class') or '?'}"

    if cls == 'display':
        if 'PCI' in buses:
            return F_GPU, f"PCI-s grafikus driver, INF: {shown}"
        return F_SOFTWARE, f"virtuális kijelző (nem PCI-hardver), INF: {shown}"
    if cls in ('media', 'audioprocessingobject'):
        if hda_vens and hda_vens <= {'8086', '1002', '10DE'}:
            return F_GPU, f"a grafika HDMI/DP-hangja (HD Audio gyártókód {', '.join(sorted(hda_vens))}), INF: {shown}"
        if 'INTELAUDIO' in buses and not (hda_vens - {'8086'}):
            return F_GPU, f"Intel kijelző-hang, INF: {shown}"
        return F_AUDIO, f"Windows-osztály: {facts.get('inf_class')}, INF: {shown}"
    strong = {
        'net': F_NET, 'bluetooth': F_BT, 'hdc': F_STORAGE, 'scsiadapter': F_STORAGE,
        'keyboard': F_INPUT, 'mouse': F_INPUT, 'monitor': F_MONITOR, 'camera': F_CAMERA,
        'image': F_CAMERA, 'biometric': F_BIO, 'firmware': F_FIRMWARE,
        'securitydevices': F_SECURITY, 'smartcardreader': F_SECURITY, 'battery': F_BATTERY,
        'sensor': F_SENSOR, 'sdhost': F_CARD, 'processor': F_CPU,
    }
    if cls in strong:
        return strong[cls], f"Windows-osztály: {facts.get('inf_class')}, INF: {shown}"
    if cls == 'usb' and 'PCI' in buses:
        return F_USB, f"PCI-s USB-vezérlő driver, INF: {shown}"
    # LEÍRÁSONKÉNT, SORRENDBEN: az első leírás a csomag fő eszköze. Az összefűzött
    # szövegen keresve az Intel DPTF (hőkezelő) csomag a grafikához került, mert a
    # sokadik résztvevője "Display"-t említ (mérve, oem43.inf).
    for d in descs:
        for rx, f in _NAME_KEYWORDS:
            if rx.search(d):
                return f, f"a modell-leírás alapján: '{d}'"
    if 'PCI' in buses or 'ACPI' in buses:
        return F_CHIPSET, f"alaplapi (PCI/ACPI) eszköz driverje, INF: {shown}"
    if cls in ('system', 'extension', 'softwarecomponent'):
        return F_CHIPSET, f"rendszer-komponens, INF: {shown}"
    return F_OTHER, f"Windows-osztály: {facts.get('inf_class') or pkg.get('class') or '?'}, INF: {shown}"


# =============================================================================
# A TELJES TÉRKÉP
# =============================================================================

def detect_form_factor(role, nodes):
    """Laptop vagy asztali gép -> (form, forrás).

    Elsődleges: a firmware saját bejelentése (`PowerDeterminePlatformRoleEx`, az ACPI
    FADT "Preferred PM Profile" mezője: 2 = mobil, 8 = tablet). Ha az nem mond semmit,
    a gépbe épített ACPI-akkumulátor (`ACPI\\PNP0C0A`) dönt - egy asztali gép
    szünetmentese HID-akkumulátorként jelentkezik, nem ACPI-ként, tehát nem téveszt meg."""
    battery = any((n.get('id') or '').upper().startswith('ACPI\\PNP0C0A') and n.get('present')
                  for n in nodes or [])
    if role in (2, 8):
        return 'laptop', f"firmware: mobil gép (platform-szerep {role})"
    if role in (1, 3, 4, 5, 6, 7):
        if battery:
            return 'laptop', f"firmware: {role}, de beépített akkumulátor van"
        return 'desktop', f"firmware: asztali gép (platform-szerep {role})"
    if battery:
        return 'laptop', 'beépített akkumulátor (a firmware nem jelentett géptípust)'
    return 'desktop', 'nincs akkumulátor, a firmware nem jelentett géptípust'


def _clean_cpu(name):
    name = re.sub(r'\((R|TM)\)', '', name or '', flags=re.I)
    name = re.sub(r'\s+CPU\s+@.*$', '', name)
    return re.sub(r'\s{2,}', ' ', name).strip()


def machine_title(identity):
    """A gép emberi neve: gyári gépnél a típus, összerakott gépnél az alaplap."""
    from app.machine_core import is_placeholder
    identity = identity or {}
    mfg = identity.get('SystemManufacturer', '')
    prod = identity.get('SystemProductName', '')
    board = ' '.join(x for x in (identity.get('BaseBoardManufacturer', ''),
                                 identity.get('BaseBoardProduct', '')) if x and not is_placeholder(x))
    if prod and not is_placeholder(prod):
        title = prod if (not mfg or is_placeholder(mfg) or mfg.lower().split()[0] in prod.lower()) \
            else f"{mfg} {prod}"
        return title, board
    return (board or 'Ismeretlen gép'), board


def build_machine_map(nodes, packages, facts, usage=None, identity=None, role=None,
                      printer_pubs=None):
    """A Driverek nézet gép-térképe. TISZTA függvény.

    `nodes`: az eszközfa (`app/win32.py: enumerate_device_nodes`); `packages`: a dism-sorok;
    `facts`: {published: parse_inf_facts}; `usage`: a használat-besorolás;
    `identity`: `read_hardware_identity`; `role`: `platform_role`; `printer_pubs`: a
    közös nyomtató-felismerés halmaza.

    Visszatérés: {'form', 'form_source', 'title', 'board', 'cpu', 'slots': [...],
    'packages': {published: {'slot', 'what', 'what_kind', 'why'}}}"""
    nodes = nodes or []
    usage = usage or {}
    facts = facts or {}
    printer_pubs = {p.lower() for p in (printer_pubs or [])}
    identity = identity or {}
    form, form_src = detect_form_factor(role, nodes)
    title, board = machine_title(identity)
    cpu_name = _clean_cpu(identity.get('ProcessorNameString', ''))

    node_slots, gpus = assign_node_slots(nodes, form)
    by_slot = collections.defaultdict(list)
    for n in nodes:
        s = node_slots.get((n.get('id') or '').lower())
        if s:
            by_slot[s[0]].append(n)

    # --- csomagok -> slot ---
    pkg_nodes = collections.defaultdict(list)
    for n in nodes:
        inf = (n.get('inf') or '').lower()
        if inf.startswith('oem'):
            pkg_nodes[inf].append(n)
    pkg_info = {}
    slot_pkgs = collections.defaultdict(list)
    for p in packages or []:
        pub = (p.get('published') or '').strip().lower()
        if not pub:
            continue
        bound = pkg_nodes.get(pub, [])
        present = [n for n in bound if n.get('present')]
        cand = present or bound
        u = usage.get(pub) or {}
        if cand:
            counter = collections.Counter(node_slots[n['id'].lower()][0] for n in cand)
            slot = counter.most_common(1)[0][0]
            names = []
            for n in cand:
                nm = node_name(n)
                if nm not in names:
                    names.append(nm)
            first = next(n for n in cand if node_slots[n['id'].lower()][0] == slot)
            why = (f"A Windows eszközfája szerint ezt {'a JELENLÉVŐ' if present else 'a most NEM csatlakoztatott'} "
                   f"eszközt hajtja: {', '.join(names[:3])}{' (+' + str(len(names) - 3) + ')' if len(names) > 3 else ''}. "
                   f"Hely: {node_slots[first['id'].lower()][1]}.")
            kind = 'device' if present else 'absent-device'
            what = ', '.join(names[:2]) + (f' (+{len(names) - 2})' if len(names) > 2 else '')
            # SZÁNDÉKOSAN NINCS felülbírálás: egy szoftveres eszközön ülő nyomtató-segéd
            # (pl. egy szkenner ROOT\IMAGE csomópontja) ott marad, ahol az eszközfa
            # mutatja. Az első változat áttette a nyomtatók közé "nincs hozzá eszköz"
            # címmel - miközben a Windows szerint VAN eszköz, ami használja.
        else:
            f, fwhy = package_purpose(facts.get(pub), p, pub in printer_pubs)
            pf = facts.get(pub) or {}
            descs = pf.get('descriptions') or []
            running = u.get('running_services') or []
            if descs:
                what = descs[0] + (f' (+{len(descs) - 1} típus)' if len(descs) > 1 else '')
            else:
                # Leírás nélküli (szoftveres) csomagnál a fájlnév semmit nem mond - a
                # szolgáltatás neve és a gyártó igen ("ESET · ehdrv (fut)").
                svc = [s.get('name') for s in (u.get('services') or []) if s.get('name')]
                bits = [x for x in (pf.get('provider') or p.get('provider'),) if x]
                if svc:
                    bits.append('szolgáltatás: ' + ', '.join(
                        f"{s} ({'fut' if s in running else 'leállítva'})" for s in svc[:3]))
                what = ' · '.join(bits) or (p.get('original') or pub)
            if f == F_SOFTWARE or (running and not (facts.get(pub) or {}).get('hwid_count')):
                slot = 'software'
                kind = 'service' if (u.get('services')) else 'inf'
                why = (f"Nincs hozzá eszköz; szoftveres driver - {fwhy}"
                       + (f". Fut: {', '.join(running)}" if running else ''))
            else:
                slot = 'nodev:' + f
                kind = 'inf'
                why = (f"A gépben nincs hozzá tartozó eszköz (sem csatlakoztatva, sem "
                       f"nyilvántartva). Rendeltetés az INF alapján: {fwhy}.")
        pkg_info[pub] = {'slot': slot, 'what': what, 'what_kind': kind, 'why': why}
        slot_pkgs[slot].append(pub)

    # --- slot-kártyák ---
    slots = []

    def _add(key, section, icon, label, name, hot, members, badge='', extra=None):
        heads = members
        devs = [_device_entry(m) for m in heads][:8]
        pk = slot_pkgs.get(key, [])
        entry = {
            'key': key, 'section': section, 'icon': icon, 'title': label, 'name': name,
            'badge': badge, 'hot': hot, 'devices': devs, 'more_devices': max(0, len(heads) - 8),
            'packages': pk,
            'inbox_only': bool(devs) and not pk and not any(d['vendor_driver'] for d in devs),
            'problems': sum(1 for d in devs if d['problem']),
            'present': any(m.get('present') for m in members) if members else False,
        }
        if extra:
            entry.update(extra)
        slots.append(entry)

    # Grafika
    for g in gpus:
        key = 'gpu:' + g['node']['id'].lower()
        members = [m for m in by_slot.get(key, []) if m.get('present')]
        heads = [g['node']] + [m for m in members if m is not g['node']
                               and (m.get('inf') or '').lower().startswith('oem')
                               and node_function(m)[0] != F_INHERIT]
        _add(key, 'parts', '🎮' if not g['integrated'] else '🖥️',
             'Videókártya' if not g['integrated'] else 'Grafika',
             node_name(g['node']), 'gpu' if not g['integrated'] else 'cpu', heads,
             badge='dedikált' if not g['integrated'] else 'a processzorba épített',
             extra={'why': g['why']})
    # Processzor
    cpu_members = [m for m in by_slot.get('cpu', []) if m.get('present')]
    threads = sum(1 for m in cpu_members if (m.get('cls') or '').lower() == 'processor')
    _add('cpu', 'parts', '🧠', 'Processzor', cpu_name or (node_name(cpu_members[0]) if cpu_members else 'Processzor'),
         'cpu', cpu_members[:1] + [m for m in cpu_members[1:] if (m.get('inf') or '').lower().startswith('oem')],
         badge=f'{threads} szál' if threads else '')
    # Alaplap / beépített
    headline = BOARD_HEADLINE_LAPTOP if form == 'laptop' else BOARD_HEADLINE
    for f in BOARD_ORDER:
        key = 'board:' + f
        members = by_slot.get(key, [])
        heads = _headline_nodes(f, members)
        if not heads and not slot_pkgs.get(key):
            continue
        if f not in headline and not slot_pkgs.get(key):
            continue
        icon, label, _n = FUNC_META[f]
        if form == 'laptop' or f == F_MONITOR:
            label = _BOARD_TITLE.get(f, label)
        name = ''
        if f == F_CHIPSET:
            lpc = [m for m in heads if any(c.upper().startswith('PCI\\CC_0601') for c in _upper_list(m.get('compat')))]
            name = node_name(lpc[0]) if lpc else (node_name(heads[0]) if heads else '')
        elif heads:
            names = []
            for h in heads:
                if node_name(h) not in names:
                    names.append(node_name(h))
            name = ', '.join(names[:2]) + (f' (+{len(names) - 2})' if len(names) > 2 else '')
        _add(key, 'parts', icon, label, name, f, heads)
    # Külső eszközök
    ext_keys = [k for k in by_slot if k.startswith('ext:')] + [k for k in slot_pkgs if k.startswith('ext:') and k not in by_slot]
    ext_entries = []
    for key in ext_keys:
        members = by_slot.get(key, [])
        pk = slot_pkgs.get(key, [])
        present = [m for m in members if m.get('present')]
        hardware = [m for m in present if not m['id'].upper().startswith(_SOFTWARE_ID_PREFIXES)]
        cat = container_category(members) if members else F_OTHER
        # Hálózaton látott eszközök (TV-k, más gépek) és csak-nyomtatósor "nyomtatók":
        # gyári csomag nélkül zaj, a gép felépítéséhez semmit nem adnak.
        if not pk and not (hardware and cat in (F_MONITOR, F_INPUT, F_AUDIO, F_CAMERA, F_STORAGE,
                                                  F_PHONE, F_NET, F_BT, F_PRINTER)):
            continue
        icon = FUNC_META.get(cat, FUNC_META[F_OTHER])[0]
        label = EXT_LABEL.get(cat, EXT_LABEL[F_OTHER])
        heads = [m for m in (present or members) if node_function(m)[0] != F_INHERIT]
        tops_ids = {m['id'].lower() for m in heads}
        heads = [m for m in heads if (m.get('parent') or '').lower() not in tops_ids] or heads
        ext_entries.append((EXT_ORDER.index(cat) if cat in EXT_ORDER else 99, container_name(members) if members else key,
                            key, icon, label, heads, bool(present), cat))
    ext_entries.sort(key=lambda e: (e[0], not e[6], e[1].lower()))
    for _o, name, key, icon, label, heads, is_present, cat in ext_entries:
        # A külső monitor/beviteli eszköz KÜLÖN pont az ábrán, mint a laptop beépített
        # kijelzője és billentyűzete ('monitor'/'input').
        _add(key, 'external', icon, label, name, {F_MONITOR: 'ext-monitor', F_PRINTER: 'printer',
                                                  F_PHONE: 'phone', F_INPUT: 'ext-input'}.get(cat, 'peripheral'),
             heads, badge='' if is_present else 'nincs csatlakoztatva', extra={'category': cat})
    # Szoftveres
    soft_members = [m for m in by_slot.get('software', [])
                    if m.get('present') and (m.get('inf') or '').lower().startswith('oem')]
    if slot_pkgs.get('software') or soft_members:
        _add('software', 'software', '🧰', 'Szoftveres driverek',
             'vírusirtó, VPN, virtuális eszközök', 'software', soft_members)
    # Nincs jelen / nincs hozzá eszköz
    # A kártya címe a rendeltetés RÖVID címkéje (a szekció címe már kimondja, hogy nincs
    # hozzá eszköz - kártyánként megismételve zaj volt), a `name` a táblázat
    # csoport-fejlécének teljes mondata ("Telefonhoz / mobileszközhöz").
    for f in NODEV_ORDER + [F_CPU, F_SOFTWARE]:
        key = 'absent:' + f
        if slot_pkgs.get(key):
            icon, short, nl = FUNC_META.get(f, FUNC_META[F_OTHER])
            _add(key, 'absent', icon, short, nl, None,
                 [m for m in by_slot.get(key, [])])
    for f in NODEV_ORDER:
        key = 'nodev:' + f
        if slot_pkgs.get(key):
            icon, short, nl = FUNC_META.get(f, FUNC_META[F_OTHER])
            if f == F_INPUT:
                short = EXT_LABEL[F_INPUT]   # nem "beépített": bármilyen egér/billentyűzet lehet
            _add(key, 'nodev', icon, short, nl, None, [])

    # Minden csomagnak legyen kártyája (egy váratlan slot-kulcs se tűnjön el a képről).
    known = {s['key'] for s in slots}
    for key, pubs in slot_pkgs.items():
        if key not in known:
            logging.warning(f"[MACHINEMAP] Váratlan hely-kulcs '{key}' ({len(pubs)} csomag) - "
                            f"az 'Egyéb' kártyára kerül.")
            _add(key, 'nodev', '🧩', 'Egyéb', key, None, [])

    order = {k: i for i, k in enumerate(s['key'] for s in slots)}
    for pub, info in pkg_info.items():
        info['order'] = order.get(info['slot'], 999)
    return {'form': form, 'form_source': form_src, 'title': title, 'board': board,
            'cpu': cpu_name, 'slots': slots, 'packages': pkg_info}


def log_machine_map(mm):
    """A térkép összefoglalója a naplóba (Rule 0): minden csomag helye INDOKKAL, DEBUG-on;
    a kártyák és a darabszámok INFO-n. Egy "rossz helyre tette a drivert" bejelentés
    így a naplóból megválaszolható."""
    if not mm:
        return
    sections = collections.Counter(s['section'] for s in mm['slots'])
    logging.info(f"[MACHINEMAP] {mm['title']} ({mm['form']}: {mm['form_source']}) - "
                 f"{len(mm['slots'])} kártya {dict(sections)}, {len(mm['packages'])} csomag")
    for s in mm['slots']:
        logging.info(f"[MACHINEMAP] {s['icon']} {s['title']}: {s['name']} "
                     f"[{s['key']}] - {len(s['packages'])} csomag, {len(s['devices'])} eszköz"
                     f"{' (csak Windows beépített driver)' if s['inbox_only'] else ''}")
    for pub, info in sorted(mm['packages'].items()):
        logging.debug(f"[MACHINEMAP] {pub} -> {info['slot']}: {info['what']} | {info['why']}")
