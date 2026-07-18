"""Driver mentés/visszaállítás, WIM-kinyerés, visszaállítási pont - KÖZÖS mag (GUI + CLI).

Korábban a robocopy-s kényszermásolás, a visszaállítási folyamat és a WIM-kinyerés
két külön (és lassan széttartó) példányban élt az app/gui/backup.py-ban és az
app/cli/backup.py-ban - most EGY példányban itt. A kanonikus viselkedés a (gazdagabb)
GUI-verzióé: hely-ellenőrzések, ESD->WIM konvertálás, régi backup-formátum INF-kinyerés,
OEM-mappánkénti DISM regisztrálás, első-bejelentkezési rescan script - a CLI mindezt
innen ugyanúgy megkapja.

Minden hosszú folyamat log(msg) callbackkal ír (GUI: task_progress emit, CLI: print),
és opcionális check_cancel() callbackkel szakítható meg (CLI: None).
"""

# === AUTO-IMPORTS ===
import os
import subprocess
import time
import logging
import shutil
from datetime import datetime
from app import bcd_core
# === /AUTO-IMPORTS ===


class RestoreCancelled(Exception):
    """A felhasználó megszakította a folyamatot (GUI Cancel gomb)."""


def export_drivers_cmd(target_os_path, folder):
    """A dism /export-driver parancs (online vagy offline cél szerint)."""
    if target_os_path:
        return ['dism', f'/Image:{target_os_path}', '/export-driver', f'/destination:{folder}']
    return ['dism', '/online', '/export-driver', f'/destination:{folder}']


def _stream_cmd(cmd, on_line, check_cancel, si, nw):
    """Parancs futtatása soronkénti kimenet-továbbítással. Megszakításkor NEM lőjük ki
    a folyamatot erőszakosan (egy megszakadt DISM/pnputil korrupt Windowst hagyhatna) -
    csak abbahagyjuk az olvasást és megvárjuk a biztonságos leállást.
    Visszatérés: (returncode, cancelled)."""
    logging.debug(f"[CMD] Popen futtatása: {' '.join(str(c) for c in cmd)}")
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               startupinfo=si, creationflags=nw, errors='replace')
    cancelled = False
    for line in process.stdout:
        if check_cancel and check_cancel():
            cancelled = True
            on_line('⚠️ Megszakítás kérve, várakozás a biztonságos leállásra...')
            break
        stripped = line.strip()
        if stripped:
            on_line(stripped)
    process.wait()
    return process.returncode, cancelled


def force_copy(run, log, src, dst, check_cancel=None):
    """Robocopy-alapú kényszerített másolás jogosultság-megkerüléssel (inbox/system
    driverekhez), robocopy-hiba esetén mappánkénti jogszerzéses fallback.
    Visszatérési érték: True, ha a másolás közben bármilyen hiba történt - a hívónak
    ezt a végső "sikeres" összegzésbe be KELL számítania, különben egy ténylegesen
    hiányos másolás is sikeresnek tűnik."""
    logging.debug(f"[RESTORE] force_copy: {src} -> {dst}")
    if not os.path.exists(src):
        logging.warning(f"[RESTORE] Forrás nem létezik: {src}")
        log(f'  ❌ Forrás nem létezik: {src}')
        return True
    os.makedirs(dst, exist_ok=True)

    free_bytes = shutil.disk_usage(dst).free
    needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(src) for f in fs if os.path.exists(os.path.join(r, f)))
    if needed_bytes > free_bytes:
        log(f'  ❌ Nincs elég szabad hely a célmeghajtón! Szükséges: {needed_bytes // (1024*1024)} MB, '
            f'elérhető: {free_bytes // (1024*1024)} MB.')
        return True

    log(f'\n  Robocopy indul: {os.path.basename(src)} -> {os.path.basename(dst)}\n  (Backup mód - Windows jogosultságok megkerülése)')
    cmd = ['robocopy', src, dst, '/E', '/ZB', '/R:1', '/W:1', '/COPY:DAT', '/NC', '/NS', '/NFL', '/NDL', '/NP']
    res = run(cmd)

    if res.returncode < 8:
        logging.info(f"[RESTORE] Robocopy sikeres, returncode={res.returncode}")
        log(f'  ✅ Sikeres robocopy kényszerítés ({res.returncode})')
        return False

    log(f'  ⚠️ Robocopy hiba ({res.returncode}), végső tartalék: mappánkénti jogszerzés (lassabb)...')
    had_error = False
    for root, _, files in os.walk(src):
        if check_cancel and check_cancel():
            return had_error
        rel = os.path.relpath(root, src)
        target_dir = os.path.join(dst, rel) if rel != '.' else dst
        os.makedirs(target_dir, exist_ok=True)

        for f in files:
            if check_cancel and check_cancel():
                return had_error
            sfile = os.path.join(root, f)
            dfile = os.path.join(target_dir, f)
            if os.path.exists(dfile):
                run(f'takeown /f "{dfile}" /A', shell=True)
                run(f'icacls "{dfile}" /grant *S-1-5-32-544:F', shell=True)
                run(f'attrib -R "{dfile}"', shell=True)
            try:
                shutil.copy2(sfile, dfile)
            except Exception as e:
                log(f'❌ Hiba ({f}): {e}')
                had_error = True
    log('  ⚠️ Fallback másolás hibákkal fejeződött be.' if had_error else '  ✅ Fallback másolás befejeződött.')
    return had_error


def backup_all_drivers(run, log, check_cancel, dest, target_os_path):
    """ÖSSZES driver mentése: DISM export (OEM) + FileRepository + Windows\\INF fizikai
    másolása egy időbélyeges célmappába. Visszatérés: dict(status, folder, size_mb,
    dism_ok) - status: 'ok' | 'no_space' | 'cancelled'."""
    folder = os.path.join(dest, f"DriverVarázsló_FullExport_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(folder, exist_ok=True)

    log('DISM driver exportálás indítása... (Ez eltarthat egy ideig)')
    res = run(export_drivers_cmd(target_os_path, folder))
    dism_ok = res.returncode == 0
    if dism_ok:
        log('✅ DISM exportálás sikeres!')
    else:
        log(f'❌ Hiba az exportálásnál: {(res.stderr or "")[:300]}')

    if check_cancel and check_cancel():
        return {'status': 'cancelled', 'folder': folder, 'size_mb': 0, 'dism_ok': dism_ok}

    # Inbox driverek (FileRepository + INF) másolása
    log('Windows inbox driverek másolása (FileRepository)...')
    windows_dir = os.path.join(target_os_path, 'Windows') if target_os_path else os.environ.get('SYSTEMROOT', r'C:\Windows')
    driverstore = os.path.join(windows_dir, 'System32', 'DriverStore', 'FileRepository')
    inbox_folder = os.path.join(folder, '_Windows_Inbox_Drivers')
    os.makedirs(inbox_folder, exist_ok=True)

    needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(driverstore) for f in fs if os.path.exists(os.path.join(r, f))) if os.path.exists(driverstore) else 0
    free_bytes = shutil.disk_usage(dest).free
    if needed_bytes > free_bytes:
        log(f'❌ Nincs elég szabad hely a célmeghajtón! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB.')
        return {'status': 'no_space', 'folder': folder, 'size_mb': 0, 'dism_ok': dism_ok}

    run(['robocopy', driverstore, inbox_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])

    if check_cancel and check_cancel():
        return {'status': 'cancelled', 'folder': folder, 'size_mb': 0, 'dism_ok': dism_ok}

    log('Windows INF mappa másolása...')
    inf_src = os.path.join(windows_dir, 'INF')
    inbox_inf_folder = os.path.join(folder, '_Windows_Inbox_INF')
    os.makedirs(inbox_inf_folder, exist_ok=True)
    run(['robocopy', inf_src, inbox_inf_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])

    total_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(folder) for f in fns
                     if os.path.exists(os.path.join(dp, f)))
    size_mb = total_size / (1024 * 1024)
    return {'status': 'ok', 'folder': folder, 'size_mb': size_mb, 'dism_ok': dism_ok}


def run_restore(run, log, check_cancel, si, nw, online, source, target):
    """Driver-visszaállítás (élő rendszerre pnputil-lal, halott Windowsra fizikai
    másolás + DISM regisztrálás + BCD javítás + első-bejelentkezési rescan script).
    Visszatérés: 'ok' | 'errors' | 'cancelled'."""
    restore_had_errors = False
    norm_source = os.path.normpath(source)
    norm_target = os.path.normpath(target) if target else None
    logging.debug(f"[RESTORE] norm_source={norm_source}, norm_target={norm_target}")

    # Forrás-típus detektálás
    is_wim_extract = False
    if not online:
        repo_check = os.path.join(norm_source, "FileRepository")
        inf_check = os.path.join(norm_source, "INF")
        if os.path.isdir(repo_check) or os.path.isdir(inf_check):
            is_wim_extract = True
    inbox_subfolder = os.path.join(norm_source, "_Windows_Inbox_Drivers") if not online else None
    has_inbox_subfolder = inbox_subfolder and os.path.isdir(inbox_subfolder)
    logging.info(f"[RESTORE] Típus detektálás: is_wim_extract={is_wim_extract}, has_inbox_subfolder={has_inbox_subfolder}")

    def run_dism_add_driver(driver_path, label=""):
        """DISM /Add-Driver egy mappára /Recurse-szel. Visszatérés: (returncode, cancelled)."""
        scratch = os.path.join(norm_target, "Scratch")
        os.makedirs(scratch, exist_ok=True)
        cmd = ['dism', f'/Image:{norm_target}', '/Add-Driver', f'/Driver:{driver_path}', '/Recurse', '/ForceUnsigned', f'/ScratchDir:{scratch}']
        log(f'{label}Parancs: {" ".join(cmd)}')
        returncode, cancelled = _stream_cmd(cmd, log, check_cancel, si, nw)
        if not cancelled:
            log(f'Return code: {returncode}')
        return returncode, cancelled

    if online:
        cmd = ['pnputil', '/add-driver', f"{norm_source}\\*.inf", '/subdirs', '/install']
        log(f'Parancs: {" ".join(cmd)}')
        returncode, cancelled = _stream_cmd(cmd, log, check_cancel, si, nw)
        if cancelled:
            return 'cancelled'
        log(f'\nReturn code: {returncode}')
    elif is_wim_extract:
        # WIM-ből kimentett driverek (Windows_Gyari_Alap_Driverek_*) - FileRepository + INF formátum
        log('WIM-ből kimentett gyári driverek visszaállítása...')
        new_format_repo = os.path.join(norm_source, "FileRepository")
        new_format_inf = os.path.join(norm_source, "INF")
        target_repo = os.path.join(norm_target, "Windows", "System32", "DriverStore", "FileRepository")
        target_inf = os.path.join(norm_target, "Windows", "INF")

        try:
            if os.path.exists(new_format_repo):
                log('1/2 FileRepository és INF fizikai másolása...')
                restore_had_errors = force_copy(run, log, new_format_repo, target_repo, check_cancel) or restore_had_errors
                if check_cancel and check_cancel():
                    return 'cancelled'
                if os.path.exists(new_format_inf):
                    restore_had_errors = force_copy(run, log, new_format_inf, target_inf, check_cancel) or restore_had_errors
                    if check_cancel and check_cancel():
                        return 'cancelled'
            else:
                log('1/2 DriverStore fizikai másolása...')
                restore_had_errors = force_copy(run, log, norm_source, target_repo, check_cancel) or restore_had_errors
                if check_cancel and check_cancel():
                    return 'cancelled'

            if restore_had_errors:
                log('⚠️ Fizikai másolás hibákkal fejeződött be!')
            else:
                log('✅ Fizikai másolás kész!')
        except Exception as e:
            err_msg = str(e)
            if len(err_msg) > 300:
                err_msg = err_msg[:300] + "..."
            log(f'❌ Másolási hiba: {err_msg}')
            restore_had_errors = True

        # DISM regisztrálás a fizikai másolás után
        log('\n2/2 DISM driver regisztrálás (inbox drivereknél sok hiba normális)...')
        _, dism_cancelled = run_dism_add_driver(norm_source, "")
        if dism_cancelled:
            return 'cancelled'
        log('✅ A fizikai másolás + DISM regisztrálás kész. Az inbox driverek a másolásnak köszönhetően elérhetőek.')

    elif has_inbox_subfolder:
        # DriverVarázsló_FullExport / ALL_Driver_Backup formátum: _Windows_Inbox_Drivers + oem almappák
        log('Teljes export formátum észlelve (DriverVarázsló_FullExport / ALL_Driver_Backup).\n'
            'Az inbox drivereket fizikailag másoljuk (DISM nem tudja telepíteni őket),\n'
            'az OEM drivereket DISM-mel regisztráljuk.\n')

        # 1) Inbox driverek fizikai másolása (FileRepository + INF)
        target_repo = os.path.join(norm_target, "Windows", "System32", "DriverStore", "FileRepository")
        target_inf = os.path.join(norm_target, "Windows", "INF")
        inbox_inf_subfolder = os.path.join(norm_source, "_Windows_Inbox_INF")
        log('--- 1. LÉPÉS: Inbox driverek fizikai másolása a DriverStore-ba ---')
        try:
            restore_had_errors = force_copy(run, log, inbox_subfolder, target_repo, check_cancel) or restore_had_errors
            if check_cancel and check_cancel():
                return 'cancelled'
            if os.path.isdir(inbox_inf_subfolder):
                log('Windows INF mappa visszamásolása (új formátumú backup)...')
                restore_had_errors = force_copy(run, log, inbox_inf_subfolder, target_inf, check_cancel) or restore_had_errors
                if check_cancel and check_cancel():
                    return 'cancelled'
            else:
                # Régi backup: nincs _Windows_Inbox_INF, ezért a FileRepository almappáiból
                # kiszedjük az .inf fájlokat és bemásoljuk a Windows\INF-be
                log('Régi backup formátum: _Windows_Inbox_INF nem található.\n'
                    'INF fájlok kinyerése a FileRepository almappáiból...')
                os.makedirs(target_inf, exist_ok=True)
                inf_count = 0
                for repo_dir in os.listdir(inbox_subfolder):
                    repo_path = os.path.join(inbox_subfolder, repo_dir)
                    if not os.path.isdir(repo_path):
                        continue
                    for fname in os.listdir(repo_path):
                        if fname.lower().endswith('.inf'):
                            src_inf = os.path.join(repo_path, fname)
                            dst_inf = os.path.join(target_inf, fname)
                            try:
                                shutil.copy2(src_inf, dst_inf)
                                inf_count += 1
                            except Exception as e:
                                logging.debug(e)
                log(f'✅ {inf_count} db .inf fájl kinyerve a Windows\\INF mappába (.pnf-eket a Windows legenerálja bootoláskor).')
            if restore_had_errors:
                log('⚠️ Inbox driverek fizikai másolása hibákkal fejeződött be!')
            else:
                log('✅ Inbox driverek fizikai másolása kész!')
        except Exception as e:
            err_msg = str(e)
            if len(err_msg) > 300:
                err_msg = err_msg[:300] + "..."
            log(f'❌ Inbox másolási hiba: {err_msg}')
            restore_had_errors = True

        # 2) OEM driverek DISM-mel (almappák, amik nem _Windows_Inbox_*)
        oem_folders = []
        for item in os.listdir(norm_source):
            item_path = os.path.join(norm_source, item)
            if os.path.isdir(item_path) and item not in ("_Windows_Inbox_Drivers", "_Windows_Inbox_INF"):
                has_inf = any(f.lower().endswith('.inf') for _, _, fns in os.walk(item_path) for f in fns)
                if has_inf:
                    oem_folders.append(item_path)

        if oem_folders:
            log(f'\n--- 2. LÉPÉS: {len(oem_folders)} db OEM driver mappa DISM regisztrálása ---')
            for i, oem_path in enumerate(oem_folders):
                if check_cancel and check_cancel():
                    return 'cancelled'
                log(f'\n[{i+1}/{len(oem_folders)}] {os.path.basename(oem_path)}:')
                _, dism_cancelled = run_dism_add_driver(oem_path, "  ")
                if dism_cancelled:
                    return 'cancelled'
            log('\n✅ OEM driverek DISM regisztrálása kész!')
        else:
            log('\nNincs OEM driver mappa a backup-ban.')

    else:
        # Egyéb mappa (pl. DriverVarázsló_Export / Driver_Backup third-party export) - tisztán DISM
        _, dism_cancelled = run_dism_add_driver(norm_source, "")
        if dism_cancelled:
            return 'cancelled'

    # Post-install
    if online:
        is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
        if not is_pe:
            log('Hardverváltozások keresése...')
            time.sleep(1.5)
            run(['pnputil', '/scan-devices'])
            time.sleep(10)
            log('✅ Scan kész!')
    else:
        # === BCD JAVÍTÁS (boot loader) ===
        log('\n--- BOOT LOADER (BCD) JAVÍTÁS ---')
        bcd_core.repair_bcd(run, log, norm_target)

        # Automata PnP rescan beállítása az asztal betöltésére
        log('\nElső bejelentkezési rescan script beállítása...')
        startup_dir = os.path.join(target, "ProgramData", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        os.makedirs(startup_dir, exist_ok=True)
        bat_path = os.path.join(startup_dir, "auto_pnputil_scan.bat")
        bat_content = (
            '@echo off\n'
            'set LOGFILE="%SystemDrive%\\Users\\Public\\driver_startup_log.txt"\n'
            'echo [%DATE% %TIME%] Boot rescan indult... >> %LOGFILE%\n'
            'pnputil /scan-devices >> %LOGFILE% 2>&1\n'
            'echo [%DATE% %TIME%] Kesz! >> %LOGFILE%\n'
            'ping 127.0.0.1 -n 3 > nul\n'
            '(goto) 2>nul & del "%~f0"\n'
        )
        try:
            with open(bat_path, 'w', encoding='utf-8') as f:
                f.write(bat_content)
            log('✅ Startup script elhelyezve.')
        except Exception as e:
            log(f'⚠ Script írási hiba: {e}')

    log('\n==== BEFEJEZVE ====')
    if restore_had_errors:
        log('⚠️ A visszaállítás hibákkal fejeződött be - egyes driverek fizikai másolása sikertelen volt, a napló tartalmazza a részleteket!')
        return 'errors'
    return 'ok'


def extract_wim(run, log, check_cancel, wim_path, dest, target_os_path=None):
    """WIM/ESD-ből gyári driverek kinyerése (mount + FileRepository/INF másolás).
    ESD-t először WIM-mé konvertál. Hiba esetén kivételt dob (a mount-ot ilyenkor is
    leválasztja), megszakításkor RestoreCancelled-et. Visszatérés: a célmappa."""
    wim = os.path.abspath(wim_path).replace("/", "\\")
    # A WIM csatolási mappának NTFS-en kell lennie (a cserélhető USB-t a DISM visszautasítja).
    # WinPE-ben a SystemDrive az X: RAM-disk - sosem szabad oda írni nagy fájlokat,
    # attól függetlenül, hogy van-e kiválasztott offline cél-OS.
    is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
    if is_pe:
        sys_temp = os.path.join(target_os_path, 'DV_Temp') if target_os_path else r'C:\DV_Temp'
    else:
        sys_temp = os.environ.get('SystemDrive', 'C:') + '\\DV_Temp'
    mount_dir = os.path.join(sys_temp, f"WIM_{int(time.time())}")
    target_folder = os.path.join(dest, f"Windows_Gyari_Alap_Driverek_{datetime.now().strftime('%Y%m%d_%H%M')}")
    logging.info(f"[WIM] Mount dir: {mount_dir}")
    logging.info(f"[WIM] Target folder: {target_folder}")

    if os.path.exists(mount_dir):
        logging.debug("[WIM] Régi mount dir törlése...")
        shutil.rmtree(mount_dir, ignore_errors=True)
    os.makedirs(mount_dir, exist_ok=True)
    os.makedirs(target_folder, exist_ok=True)

    wim_to_mount = wim
    try:
        # Cancel check mount előtt
        if check_cancel and check_cancel():
            raise RestoreCancelled()

        if wim.lower().endswith('.esd'):
            needed_bytes = os.path.getsize(wim) * 2  # biztonsági ráhagyás a konvertált WIM méretére
            free_bytes = shutil.disk_usage(sys_temp).free
            if needed_bytes > free_bytes:
                raise Exception(f"Nincs elég szabad hely a konvertáláshoz! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB ({sys_temp}).")
            log('ESD -> WIM konvertálás (ez 10-15 percet is igénybe vehet!)...')
            temp_wim = os.path.join(sys_temp, f"converted_{int(time.time())}.wim")
            res_esd = run(["dism", "/Export-Image", f"/SourceImageFile:{wim}", "/SourceIndex:1", f"/DestinationImageFile:{temp_wim}", "/Compress:max", "/CheckIntegrity"])
            if res_esd.returncode != 0:
                raise Exception(f"ESD Konvertálási hiba: {res_esd.stderr}")
            wim_to_mount = temp_wim
        else:
            log('WIM csatolás (ez 4-5 perc)...')

        logging.info("[WIM] DISM Mount-Image futtatása...")
        res = run(["dism", "/Mount-Image", f"/ImageFile:{wim_to_mount}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])
        if res.returncode != 0:
            logging.error(f"[WIM] DISM Mount hiba: {res.stdout} {res.stderr}")
            raise Exception(f"DISM Mount hiba: {res.stdout} {res.stderr}")

        # Cancel check mount után (az except ág leválaszt)
        if check_cancel and check_cancel():
            raise RestoreCancelled()

        logging.info("[WIM] WIM csatolva, fájlok másolása...")
        log('Fájlok másolása...')

        driverstore = os.path.join(mount_dir, "Windows", "System32", "DriverStore", "FileRepository")
        target_repo = os.path.join(target_folder, "FileRepository")
        if os.path.exists(driverstore):
            needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(driverstore) for f in fs if os.path.exists(os.path.join(r, f)))
            free_bytes = shutil.disk_usage(target_folder).free
            if needed_bytes > free_bytes:
                raise Exception(f"Nincs elég szabad hely a célmappában! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB.")
            logging.info(f"[WIM] FileRepository másolása: {driverstore} -> {target_repo}")
            shutil.copytree(driverstore, target_repo, dirs_exist_ok=True)
        else:
            logging.error("[WIM] FileRepository nem található!")
            raise Exception("FileRepository nem található a WIM-ben!")

        inf_dir = os.path.join(mount_dir, "Windows", "INF")
        target_inf = os.path.join(target_folder, "INF")
        if os.path.exists(inf_dir):
            logging.info(f"[WIM] INF mappa másolása: {inf_dir} -> {target_inf}")
            shutil.copytree(inf_dir, target_inf, dirs_exist_ok=True)

        logging.info("[WIM] WIM leválasztása...")
        log('WIM leválasztása...')
        run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
        run(["dism", "/Cleanup-Wim"])
        for _ in range(3):
            try:
                shutil.rmtree(mount_dir, ignore_errors=False)
                break
            except Exception:
                time.sleep(2)
        shutil.rmtree(mount_dir, ignore_errors=True)
        if wim.lower().endswith('.esd') and wim_to_mount != wim and os.path.exists(wim_to_mount):
            try:
                os.remove(wim_to_mount)
            except Exception as e:
                logging.debug(f"[WIM] Ideiglenes konvertált WIM törlése sikertelen: {e}")

        logging.info(f"[WIM] Kész! Kimenet: {target_folder}")
        return target_folder
    except Exception:
        run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
        run(["dism", "/Cleanup-Wim"])
        shutil.rmtree(mount_dir, ignore_errors=True)
        # Az ESD->WIM konvertált ideiglenes fájlt hiba esetén is töröljük, különben egy
        # több GB-os "converted_*.wim" örökre ott marad a DV_Temp mappában. Fontos: sosem
        # a wim_to_mount == wim (eredeti forrásfájl) esetet töröljük.
        try:
            if wim.lower().endswith('.esd') and wim_to_mount != wim and os.path.exists(wim_to_mount):
                os.remove(wim_to_mount)
        except Exception as e2:
            logging.debug(f"[WIM] Ideiglenes konvertált WIM törlése sikertelen (hibaágban): {e2}")
        raise


def create_restore_point(run, log, desc=None):
    """Visszaállítási pont létrehozása (rendszervédelem bekapcsolása fallbackkal,
    24 órás limit feloldása, Checkpoint-Computer + utólagos ellenőrzés).
    Visszatérés: (status, desc) - status: 'ok' | 'ok_unverified' | 'enable_failed' | 'fail'."""
    desc = desc or f"DriverVarázsló_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 1) Rendszervédelem bekapcsolása (akkor is, ha ki volt kapcsolva)
    logging.info("[RESTORE_POINT] Rendszervédelem engedélyezése...")
    log('Rendszervédelem engedélyezése...')
    enable_ps = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try { Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction Stop; Write-Output "OK" } catch { Write-Output "FAIL: $($_.Exception.Message)" }'
    enable_res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
    enable_out = (enable_res.stdout or '').strip()
    if 'FAIL' in enable_out:
        logging.warning(f"[RESTORE_POINT] Enable-ComputerRestore hiba: {enable_out}")
        log(f'⚠ Enable-ComputerRestore hiba: {enable_out}\nRegistry + vssadmin fallback...')
        run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'DisableSR', '/t', 'REG_DWORD', '/d', '0', '/f'])
        run(['vssadmin', 'resize', 'shadowstorage', f'/for={os.environ.get("SystemDrive", "C:")}', f'/on={os.environ.get("SystemDrive", "C:")}', '/maxsize=5%'])
        enable_res2 = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
        enable_out2 = (enable_res2.stdout or '').strip()
        if 'FAIL' in enable_out2:
            logging.error(f"[RESTORE_POINT] Rendszervédelem nem kapcsolható be: {enable_out2}")
            log(f'❌ Rendszervédelem nem kapcsolható be: {enable_out2}')
            return 'enable_failed', desc
        logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva (fallback)")
        log('✅ Rendszervédelem bekapcsolva (fallback)')
    else:
        logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva")
        log('✅ Rendszervédelem bekapcsolva')

    # 2) 24 órás gyakoriság-limit feloldása
    logging.info("[RESTORE_POINT] 24 órás limit feloldása...")
    run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore',
         '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])

    # 3) Visszaállítási pont létrehozása
    logging.info("[RESTORE_POINT] Checkpoint-Computer futtatása...")
    log(f'Visszaállítási pont: {desc}')
    create_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try {{ Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop; Write-Output "OK" }} catch {{ Write-Output "FAIL: $($_.Exception.Message)" }}'
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", create_ps], encoding='utf-8')
    create_out = (res.stdout or '').strip()
    logging.debug(f"[RESTORE_POINT] Checkpoint result: {create_out}")

    # 4) Ellenőrzés
    logging.info("[RESTORE_POINT] Ellenőrzés...")
    verify_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; (Get-ComputerRestorePoint | Where-Object {{ $_.Description -eq "{desc}" }}).Description'
    verify_res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", verify_ps], encoding='utf-8')
    verified = desc in (verify_res.stdout or '')
    logging.debug(f"[RESTORE_POINT] Verified: {verified}")

    if 'OK' in create_out and verified:
        logging.info(f"[RESTORE_POINT] Sikeresen létrehozva: {desc}")
        return 'ok', desc
    if 'OK' in create_out:
        logging.warning("[RESTORE_POINT] Lefutott de nem ellenőrizhető (késleltetett létrehozás?)")
        return 'ok_unverified', desc
    logging.error(f"[RESTORE_POINT] Hiba: {create_out}")
    log(f'❌ Hiba: {create_out}')
    return 'fail', desc


def create_restore_point_quick(run, desc):
    """Gyors (nem ellenőrzött) visszaállítási pont - az AutoFix használja, ahol egy
    elutasított pont nem állítja meg a folyamatot. Visszatérés: sikerült-e a parancs."""
    run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
    run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
    ps_cmd = f'Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction SilentlyContinue'
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
    return res.returncode == 0
