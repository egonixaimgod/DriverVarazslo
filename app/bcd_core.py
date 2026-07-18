"""BCD / bootloader újraépítés - KÖZÖS mag (GUI offline-restore utáni javítás + CLI).

Korábban két külön implementáció élt: a CLI-é (app/cli/bcd.py) diskpart-tal
megkereste a Windows-lemez EFI partícióját és bcdboot /s <EFI> /f UEFI-vel próbált
először, a GUI-é (app/gui/bcd.py _repair_bcd) csak bcdboot /f ALL + bootrec-et
futtatott. A közös mag a TELJESEBB (EFI-kereső) folyamatot használja, aminek a
régi GUI-viselkedés (bcdboot /f ALL, majd bootrec) változatlanul a fallback-lánca:
1) EFI partíció keresése diskpart-tal + bcdboot /s <betű> /f UEFI
2) bcdboot <cél>\\Windows /f ALL
3) bootrec /fixmbr + /fixboot + /rebuildbcd

A GUI "BCD Boot Hiba Javítása" GOMBJA ettől független: az a felhasználó saját
BootFixer.cmd tooljának letöltője (app/gui/bcd.py), és NEM ezt a javítást futtatja.
"""

# === AUTO-IMPORTS ===
import os
import logging
# === /AUTO-IMPORTS ===


def _find_efi_partition(run, target_letter, log):
    """A Windows-meghajtó lemezén lévő EFI (System) partíció megkeresése diskpart-tal,
    és ideiglenes betűjel hozzárendelése. Visszatérés: (efi_letter 'S:' formában vagy
    None, disk_number, efi_partition) - a betűjelet a hívó felelőssége eltávolítani."""
    disk_number = None
    efi_letter = None
    efi_partition = None

    try:
        # Volume-ok listázása
        res = run(['diskpart'], input='list volume\n', timeout=30)

        if res.returncode == 0 and res.stdout:
            lines = res.stdout.splitlines()
            target_volume = None

            # Windows volume keresése
            for line in lines:
                parts = line.split()
                if len(parts) >= 3:
                    for i, p in enumerate(parts):
                        if p.upper() == target_letter and i >= 1:
                            try:
                                target_volume = int(parts[1])
                            except (ValueError, IndexError):
                                pass
                            break

            if target_volume is not None:
                log(f"Windows volume: {target_volume}")

                # Disk azonosítása
                res2 = run(['diskpart'], input=f'select volume {target_volume}\ndetail volume\n', timeout=30)

                if res2.returncode == 0 and res2.stdout:
                    for line in res2.stdout.splitlines():
                        if 'Disk' in line and '#' not in line:
                            parts = line.split()
                            for p in parts:
                                if p.isdigit():
                                    disk_number = int(p)
                                    break
                            if disk_number is not None:
                                break

                if disk_number is not None:
                    log(f"Lemez: Disk {disk_number}")

                    # EFI partíció keresése ezen a lemezen
                    res3 = run(['diskpart'], input=f'select disk {disk_number}\nlist partition\n', timeout=30)

                    if res3.returncode == 0 and res3.stdout:
                        for line in res3.stdout.splitlines():
                            line_upper = line.upper()
                            if 'SYSTEM' in line_upper or 'EFI' in line_upper:
                                parts = line.split()
                                for i, p in enumerate(parts):
                                    if p.isdigit() and i >= 1:
                                        efi_partition = int(p)
                                        break
                                if efi_partition:
                                    break

                    if efi_partition:
                        log(f"EFI partíció: Partition {efi_partition}")

                        # Szabad betűjel keresése
                        used_letters = set()
                        for line in lines:
                            parts = line.split()
                            for p in parts:
                                if len(p) == 1 and p.isalpha():
                                    used_letters.add(p.upper())

                        free_letter = None
                        for c in 'STUVWXYZ':
                            if c not in used_letters:
                                free_letter = c
                                break

                        if free_letter:
                            res4 = run(['diskpart'],
                                       input=f'select disk {disk_number}\nselect partition {efi_partition}\nassign letter={free_letter}\n',
                                       timeout=30)
                            if res4.returncode == 0:
                                efi_letter = free_letter + ':'
                                log(f"EFI betűjel: {efi_letter}")
    except Exception as e:
        log(f"⚠️  Lemez azonosítási hiba: {e}")

    return efi_letter, disk_number, efi_partition


def repair_bcd(run, log, target_drive):
    """BCD újraépítése a target_drive-on lévő Windowshoz (EFI-kereséssel, több
    fallback-lépcsővel - lásd a modul docstringjét). Visszatérés: True, ha a
    Windows mappa létezett és a javítási lánc lefutott."""
    target_drive = target_drive.rstrip('\\') + '\\'
    target_letter = target_drive[0].upper()
    windows_path = os.path.join(target_drive, 'Windows')

    if not os.path.exists(windows_path):
        log(f"⚠️  Windows mappa nem található: {windows_path}")
        return False

    log(f"Cél Windows meghajtó: {target_drive}")
    log("A Windows meghajtó lemezének azonosítása...")

    efi_letter, disk_number, efi_partition = _find_efi_partition(run, target_letter, log)

    # bcdboot futtatása
    success = False

    if efi_letter:
        log(f"bcdboot {target_drive}Windows /s {efi_letter} /f UEFI")
        res = run(['bcdboot', f'{target_drive}Windows', '/s', efi_letter, '/f', 'UEFI'])
        if res.returncode == 0:
            success = True
            log("✅ BCD sikeresen újraépítve (UEFI)!")
        else:
            log("⚠️  UEFI bcdboot hiba, fallback...")

        # EFI betűjel eltávolítása
        try:
            run(['diskpart'],
                input=f'select disk {disk_number}\nselect partition {efi_partition}\nremove letter={efi_letter[0]}\n',
                timeout=30)
        except Exception as e:
            logging.debug(e)

    if not success:
        # Fallback: /s nélkül
        log(f"bcdboot {target_drive}Windows /f ALL")
        res = run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
        if res.returncode == 0:
            success = True
            log("✅ BCD sikeresen újraépítve (ALL)!")
        else:
            err_msg = (res.stderr or '').strip() or (res.stdout or '').strip() or f'Exit code: {res.returncode}'
            log(f"⚠️  bcdboot hiba (0x{res.returncode:X}): {err_msg[:300]}")

    if not success:
        log("bootrec parancsok futtatása...")
        for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
            res = run(['bootrec', cmd])
            log(f"  bootrec {cmd}: {'✅' if res.returncode == 0 else '⚠️ (nem elérhető)'}")

    logging.info(f"[BCD] Javítás befejezve, success={success}")
    return True
