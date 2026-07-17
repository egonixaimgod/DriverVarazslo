"""Temp Fájlok Törlése - közös mag (a GUI és a CLI clean_temp_files is ezt hívja,
hogy a kettő ne driftelhessen szét): kategória-definíciók, mappatartalom-törlés,
Lomtár-ürítés, méret-formázás."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import os
import logging
import shutil
from app.win32 import _SHQUERYRBINFO
# === /AUTO-IMPORTS ===



# Temp Törlés funkció - modul-szintű (nem osztálymetódus) segédfüggvények, mert a
# DriverToolApi (GUI) és a CliApi (szöveges menü) egymástól független osztályok, de
# mindkettőnek ugyanez kell (ld. clean_temp_files mindkét osztályban) - így egy helyen
# módosítva nem tud a kettő szétdriftelni.
def _fmt_bytes(n):
    """Bájt -> emberi olvasható méret (KB/MB/GB) - a Temp Törlés progress-logjához/kiírásához."""
    n = float(max(n, 0))
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f"{int(n)} B" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _clean_folder_contents(folder, cancel_check=None):
    """Egy mappa TARTALMÁNAK (nem magának a mappának) törlése, elemenként próbálkozva -
    így egy zárolt (épp használatban lévő) fájl/almappa nem akasztja meg a többi elem
    törlését, csak kimarad a számlálásból. cancel_check: opcionális () -> bool (a GUI
    megszakítás-jelzőjéhez) - CLI hívásnál nincs ilyen, ott sosem szakad meg félúton.
    Visszaadja: (felszabadított_bájt, törölt_elemek, kihagyott_elemek)."""
    freed = 0
    removed = 0
    failed = 0
    if not folder or not os.path.isdir(folder):
        return freed, removed, failed
    try:
        entries = os.listdir(folder)
    except Exception as e:
        logging.warning(f"[TEMPCLEAN] Mappa nem olvasható ({folder}): {e}")
        return freed, removed, failed
    for name in entries:
        if cancel_check and cancel_check():
            break
        full = os.path.join(folder, name)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                size = 0
                for root, _dirs, files in os.walk(full):
                    for f in files:
                        try:
                            size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                shutil.rmtree(full)
            else:
                size = os.path.getsize(full)
                os.remove(full)
            freed += size
            removed += 1
        except Exception as e:
            failed += 1
            logging.debug(f"[TEMPCLEAN] Nem törölhető ({full}): {e}")
    return freed, removed, failed


def _empty_recycle_bin():
    """Lomtár ürítése SHEmptyRecycleBinW-vel. Előtte SHQueryRecycleBinW-vel lekérdezi a
    méretét (bájtban), mert az ürítő hívás magától nem adja vissza, mennyi hely szabadult
    fel - ez csak a progress-logban/kiírásban megjelenő becsléshez kell, az ürítés akkor
    is lefut, ha a lekérdezés valamiért hibázna."""
    freed = 0
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
        if hr == 0:
            freed = info.i64Size
    except Exception as e:
        logging.debug(f"[TEMPCLEAN] SHQueryRecycleBinW hiba: {e}")
    try:
        SHERB_NOCONFIRMATION = 0x00000001
        SHERB_NOPROGRESSUI = 0x00000002
        SHERB_NOSOUND = 0x00000004
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    except Exception as e:
        logging.warning(f"[TEMPCLEAN] SHEmptyRecycleBinW hiba: {e}")
    return freed


def _temp_clean_category_defs(sys_drive):
    """Temp Törlés kategóriák definíciója - EGY helyen (a GUI clean_temp_files és a CLI
    clean_temp_files is ezt hívja), hogy a kettő ne driftelhessen szét. Elemek:
    (kulcs, címke, [törlendő mappák], [leállítandó szolgáltatások], alapból_bepipálva).
    A 3 "alapból_bepipálva" kategória (user_temp/windows_temp/wu_cache) a törzs-tartalom,
    a többi opcionális extra - felhasználói kérésre bővítve, de tudatosan kikapcsolva
    alapból, mert vagy specifikusabb (pl. csak DirectX-es játékosoknak van D3DSCache-e),
    vagy csak diagnosztikai adat (CBS log, crash dump), amit nem mindenki akar automatikusan
    elveszíteni."""
    import tempfile
    local = os.environ.get('LOCALAPPDATA', '')
    wer_base = os.path.join(sys_drive, 'ProgramData', 'Microsoft', 'Windows', 'WER')
    return [
        ('user_temp', '👤 Felhasználói TEMP mappa (%TEMP%)',
         [tempfile.gettempdir()], [], True),
        ('windows_temp', '🖥️ Rendszer TEMP mappa (Windows\\Temp)',
         [os.path.join(sys_drive, 'Windows', 'Temp')], [], True),
        ('wu_cache', '🔄 Windows Update letöltési gyorsítótár',
         [os.path.join(sys_drive, 'Windows', 'SoftwareDistribution', 'Download')], ['wuauserv', 'bits'], True),
        ('delivery_opt', '📦 Delivery Optimization gyorsítótár',
         [os.path.join(sys_drive, 'Windows', 'SoftwareDistribution', 'DeliveryOptimization')], ['DoSvc'], False),
        ('wer', '⚠️ Hibajelentések (Windows Error Reporting)',
         [os.path.join(wer_base, 'ReportQueue'), os.path.join(wer_base, 'ReportArchive')], [], False),
        ('shader_cache', '🎮 DirectX Shader Cache',
         [os.path.join(local, 'D3DSCache')] if local else [], [], False),
        ('cbs_logs', '📜 Windows telepítési naplók (CBS logok)',
         [os.path.join(sys_drive, 'Windows', 'Logs', 'CBS')], [], False),
        ('crash_dumps', '💥 Programösszeomlás-dumpok (Crash Dumps)',
         [os.path.join(local, 'CrashDumps')] if local else [], [], False),
        ('inet_cache', '🌐 Internet Explorer/Edge (legacy) gyorsítótár',
         [os.path.join(local, 'Microsoft', 'Windows', 'INetCache')] if local else [], [], False),
        # Színprofil-mappa: főleg nyomtatódriverek (tipikusan HP) szemetelik tele akár
        # több GB ICC-profillal. A Spoolert leállítjuk törlés előtt, mert a nyomtató-
        # driverek profiljait zárolhatja. A Windows beépített profiljai (pl. sRGB) is
        # törlődnek, ezért csak opt-in extra - a driverek újratelepítése visszateszi őket.
        ('color_profiles', '🎨 Színprofilok (spool\\drivers\\color)',
         [os.path.join(sys_drive, 'Windows', 'System32', 'spool', 'drivers', 'color')], ['Spooler'], False),
    ]
