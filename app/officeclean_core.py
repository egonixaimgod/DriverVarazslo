"""Az Office MARADÉKAINAK eltávolítása az eltávolító után: „mintha sose lett volna a gépen”
(2026-11-26, explicit user decision: *„ne maradjon utána semmi se semmilyen registry retek …
se semmilyen mappa”*). A lépések a Microsoft „Manually uninstall Office” leírását követik
(Click-to-Run): folyamatok, ClickToRunSvc, ütemezett feladatok, mappák, registry, parancsikonok.

KÉT, A KÉPERNYŐN MEGHOZOTT DÖNTÉS (jelölőnégyzet, explicit user decision):
  - keep_settings: maradjanak a felhasználók Office-beállításai és fiókjai (Outlook-profilok,
    aláírások, sablonok, HKCU\\Software\\Microsoft\\Office),
  - keep_activation: maradjon az aktiválás (az Office termékkulcsai a Windows licenc-tárában,
    a licencfájlok és a bejelentkezés: `Licenses`, `Licensing`, `Identity`, OSPP).
FELHASZNÁLÓI FÁJLHOZ SOHA NEM NYÚLUNK, CSAK A PROGRAMHOZ (explicit user decision, 2026-11-26:
*„fileokat semmi esetben se töröljön … word fileokat vagy bármit amit csak office al csináltak
csak magát a programot!”*). Három védelem, mindegyik önmagában elég:
  1. A célok között NINCS dokumentum-mappa (Dokumentumok, Letöltések, OneDrive-tartalom…); az
     asztalról és a tálcáról CSAK Office-programra mutató `.lnk` parancsikon törlődik.
  2. A Program Files-on kívül (felhasználói profil, ProgramData) a törlő MINDEN olyan fájlt
     kihagy, ami felhasználói tartalom lehet (`PROTECTED_EXT`: dokumentumok, sablonok, makrók,
     aláírások, Outlook-adatfájlok, helyreállítási fájlok, képek, PDF…), és a `PROTECTED_DIRS`
     mappákba (sablonok, aláírások, nem mentett fájlok, OneDrive-gyorsítótár, indítómappák)
     be sem lép. Ezek a visszaolvasásban sem számítanak maradéknak.
  3. A törlő nem lép át csomóponton/szimbolikus linken (egy junction más mappába vinne).
A Program Files Office-mappái maguk a program: oda az Office soha nem ment felhasználói fájlt.
Ezt a kártya és a megerősítő ablak is kimondja (nevesített, a képernyőn látható kivétel, 1. elv).

A VERDIKT A VISSZAOLVASÁS: `deep_clean` a végén minden célt újra megnéz, és név szerint
visszaadja, ami megmaradt (zárolt fájl, be nem tölthető felhasználói registry is).
"""
import os
import re
import time
import shutil
import logging
import winreg

from app.tempclean_core import _long_path, _disp

OFFICE_LICENSE_APP_ID = '0ff1ce15-a989-479d-af46-f275c6370663'
C2R_PROCESSES = ('OfficeClickToRun.exe', 'OfficeC2RClient.exe', 'AppVShNotify.exe',
                 'IntegratedOffice.exe', 'OfficeSvcMgr.exe')
# ABSZOLÚT TILTÁS (explicit user decision, 2026-11-26: *„docx fileokat meg ppt fileokat ne
# törölhessen … a program ilyenekhez hozzá se érhet”*): ezeket a dokumentum-kiterjesztéseket a
# modul SEHONNAN nem törölheti, a Program Files-ból sem. A kapu `_remove_file`, az egyetlen hely,
# ahol fájl törlődik (forrás-teszt őrzi). Ne szűkítsd.
DOCUMENT_EXT = frozenset((
    '.doc', '.docx', '.docm', '.rtf', '.odt', '.txt', '.wbk', '.asd',
    '.xls', '.xlsx', '.xlsm', '.xlsb', '.csv', '.ods', '.xar',
    '.ppt', '.pptx', '.pptm', '.pps', '.ppsx', '.ppsm', '.odp',
    '.pdf', '.xps', '.pub', '.vsd', '.vsdx', '.vsdm', '.mpp', '.accdb', '.accde', '.mdb', '.mde',
    '.one', '.onepkg', '.pst', '.ost', '.msg', '.eml', '.vcf', '.ics',
))
# A Program Files-on kívül ezek a fájlok SOHA nem törlődnek (felhasználói tartalom lehet).
PROTECTED_EXT = DOCUMENT_EXT | frozenset((
    # Word, Excel, PowerPoint, Access, Publisher, Visio, Project, OneNote, Outlook dokumentumok
    '.doc', '.docx', '.docm', '.dot', '.dotx', '.dotm', '.rtf', '.odt', '.txt', '.wbk', '.asd',
    '.xls', '.xlsx', '.xlsm', '.xlsb', '.xlt', '.xltx', '.xltm', '.xla', '.xlam', '.csv', '.ods', '.xar',
    '.ppt', '.pptx', '.pptm', '.pps', '.ppsx', '.ppsm', '.pot', '.potx', '.potm', '.ppa', '.ppam', '.odp',
    '.accdb', '.accde', '.accdt', '.mdb', '.mde', '.laccdb', '.pub', '.vsd', '.vsdx', '.vsdm', '.vss',
    '.vssx', '.vst', '.vstx', '.mpp', '.mpt', '.one', '.onepkg', '.onetoc2',
    '.pst', '.ost', '.msg', '.eml', '.oft', '.nk2', '.vcf', '.ics',
    # aláírások, képek, PDF, archívum: amit a felhasználó maga tett oda
    '.htm', '.html', '.mht', '.mhtml', '.pdf', '.xps', '.png', '.jpg', '.jpeg', '.gif', '.bmp',
    '.tif', '.tiff', '.emf', '.wmf', '.svg', '.zip', '.7z', '.rar',
    # OneDrive/Office feltöltési gyorsítótár (még fel nem töltött dokumentum)
    '.fsd', '.fsf', '.fsc',
    # amit a felhasználó maga épít: saját szótár, automatikus javítás, Outlook-szabályok, témák
    '.dic', '.exc', '.lex', '.acl', '.rwz', '.thmx', '.xml',
))
# Ezekbe a mappákba (a Program Files-on kívül) be sem lépünk: felhasználói tartalom.
PROTECTED_DIRS = frozenset(('templates', 'signatures', 'stationery', 'document building blocks',
                            'unsavedfiles', 'officefilecache', 'xlstart', 'startup', 'addins',
                            'recent', 'quick parts', 'custom office templates'))
_OFFICE_LNK = re.compile(r'^(microsoft )?(access|excel|onenote|outlook|powerpoint|project|publisher|'
                         r'skype for business|visio|word|lync|infopath)( \d{4}| \(.*\))?( - .*)?\.lnk$', re.I)
_OFFICE_MENU_DIR = re.compile(r'^microsoft office', re.I)
# Az asztalon/tálcán a név mellé a CÉL is kell (egy saját „Word.lnk” ne menjen): az Office
# programfájljai. A .lnk a célútvonalat ASCII-ként és UTF-16-ként is tárolhatja.
_OFFICE_TARGETS = ('winword.exe', 'excel.exe', 'outlook.exe', 'powerpnt.exe', 'msaccess.exe',
                   'mspub.exe', 'onenote.exe', 'winproj.exe', 'visio.exe', 'lync.exe', 'infopath.exe')
# A Windows gyári „Microsoft 365 (Office)” indító-alkalmazása (Store): külön jelölőnégyzet.
OFFICE_HUB_APPX = 'Microsoft.MicrosoftOfficeHub'


def _env(name, fallback):
    return os.environ.get(name) or fallback


def machine_dirs(keep_activation):
    """A gépszintű mappák: [(útvonal, megtartandó alútvonalak)]."""
    pf = _env('ProgramW6432', _env('ProgramFiles', r'C:\Program Files'))
    pf86 = _env('ProgramFiles(x86)', r'C:\Program Files (x86)')
    cpf = _env('CommonProgramW6432', _env('CommonProgramFiles', os.path.join(pf, 'Common Files')))
    cpf86 = _env('CommonProgramFiles(x86)', os.path.join(pf86, 'Common Files'))
    pd = _env('ProgramData', r'C:\ProgramData')
    dirs = []
    for base in (pf, pf86):
        for n in ('Microsoft Office', 'Microsoft Office 15', 'Microsoft Office 16'):
            dirs.append(os.path.join(base, n))
    for base in (cpf, cpf86):
        dirs.append(os.path.join(base, 'Microsoft Shared', 'ClickToRun'))
        for v in ('OFFICE14', 'OFFICE15', 'OFFICE16'):
            dirs.append(os.path.join(base, 'Microsoft Shared', v))
    dirs += [os.path.join(pd, 'Microsoft', 'ClickToRun'), os.path.join(pd, 'Microsoft', 'Office')]
    if not keep_activation:
        dirs.append(os.path.join(pd, 'Microsoft', 'OfficeSoftwareProtectionPlatform'))
    return [(d, ()) for d in dirs]


def user_dirs(profile, keep_settings, keep_activation):
    """Egy felhasználói profil Office-mappái: [(útvonal, megtartandó alútvonalak)]."""
    roam = os.path.join(profile, 'AppData', 'Roaming', 'Microsoft')
    local = os.path.join(profile, 'AppData', 'Local', 'Microsoft', 'Office')
    licensing = ('Licenses', os.path.join('16.0', 'Licensing'), os.path.join('15.0', 'Licensing'))
    out = []
    if not keep_settings:
        # A Templates/Signatures/Document Building Blocks szándékosan NINCS itt: felhasználói tartalom.
        for n in ('Office', 'Word', 'Excel', 'PowerPoint', 'UProof', 'Proof', 'Outlook',
                  'Access', 'Publisher'):
            out.append((os.path.join(roam, n), ()))
        out.append((local, licensing if keep_activation else ()))
    elif not keep_activation:
        out += [(os.path.join(local, p), ()) for p in licensing]
    return out


def machine_keys(keep_activation):
    """A gépszintű registry-ágak (HKLM): [(útvonal, megtartandó alágak)]."""
    keys = []
    lic = tuple(rf'{v}\Common\Licensing' for v in ('15.0', '16.0')) if keep_activation else ()
    for sw in (r'SOFTWARE', r'SOFTWARE\WOW6432Node'):
        keys += [(sw + r'\Microsoft\Office', lic), (sw + r'\Microsoft\AppVISV', ()),
                 (sw + r'\Microsoft\ClickToRun', ())]
        if not keep_activation:
            keys.append((sw + r'\Microsoft\OfficeSoftwareProtectionPlatform', ()))
    return keys


def user_key_rules(keep_settings, keep_activation):
    """A felhasználói registry-ág (HKU\\<SID> alatt): [(útvonal, megtartandó alágak)]."""
    office = r'Software\Microsoft\Office'
    lic = [rf'{v}\Common\{k}' for v in ('15.0', '16.0') for k in ('Licensing', 'Identity')]
    if not keep_settings:
        return [(office, tuple(lic) if keep_activation else ())]
    if not keep_activation:
        return [(office + '\\' + p, ()) for p in lic]
    return []


# ---------------------------------------------------------------- törlés (megtartással)
def _under(rel, keep):
    """`rel` egy megtartandó alútvonal ŐSE-e (akkor csak belemegyünk, nem töröljük)."""
    r = rel.lower().rstrip('\\') + '\\'
    return any(k.lower().startswith(r) for k in keep)


def _is_kept(rel, keep):
    return any(rel.lower() == k.lower() for k in keep)


def _program_area(path):
    """A Program Files (és Common Files) alatt van-e: ott minden a program része."""
    p = os.path.normcase(os.path.abspath(path))
    roots = {_env(n, '') for n in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)',
                                   'CommonProgramW6432', 'CommonProgramFiles', 'CommonProgramFiles(x86)')}
    return any(r and (p + os.sep).startswith(os.path.normcase(os.path.abspath(r)) + os.sep) for r in roots)


def _protected_file(name, program_area):
    """Felhasználói tartalom lehet-e: dokumentum MINDENHOL, a többi a Program Files-on kívül."""
    ext = os.path.splitext(name)[1].lower()
    return ext in DOCUMENT_EXT or (not program_area and ext in PROTECTED_EXT)


def _remove_file(path):
    """A modul EGYETLEN fájltörlő kapuja. Dokumentumot (`DOCUMENT_EXT`) nem töröl, akármi hívja."""
    if os.path.splitext(path)[1].lower() in DOCUMENT_EXT:
        logging.error(f"[OFFICECLEAN] TILTOTT: dokumentum törlését megtagadtam: {path}")
        raise PermissionError(f'dokumentum, a törlése tiltott: {path}')
    os.chmod(path, 0o666)
    os.remove(path)


def _protected_dir(name, program_area):
    return not program_area and name.lower() in PROTECTED_DIRS


def _is_link(p):
    return os.path.islink(p) or (hasattr(os.path, 'isjunction') and os.path.isjunction(p))


def delete_dir(path, keep=(), errors=None):
    """Mappa törlése; a `keep` alútvonalak megmaradnak, és a Program Files-on kívül minden
    felhasználói fájl (`PROTECTED_EXT`) és védett mappa (`PROTECTED_DIRS`) is. Linket nem követ."""
    errors = errors if errors is not None else []
    lp = _long_path(path)
    if not os.path.exists(lp):
        return errors
    prog = _program_area(path)
    logging.warning(f"[OFFICECLEAN] TÖRLÉS: {path}" + (f" (megmarad: {', '.join(keep)})" if keep else '')
                    + ('' if prog else ' (a felhasználói fájlok védve)'))
    if os.path.isfile(lp):  # parancsikon
        if _protected_file(os.path.basename(path), prog):
            logging.info(f"[OFFICECLEAN] VÉDETT, nem törölve: {path}")
            return errors
        try:
            _remove_file(lp)
        except OSError as e:
            errors.append(f'{path}: {e.strerror or e}')
        return errors
    if _is_link(lp):
        logging.info(f"[OFFICECLEAN] Link/csomópont, nem követjük: {path}")
        return errors

    def walk(cur, rel):
        for name in os.listdir(cur):
            full, sub = os.path.join(cur, name), (os.path.join(rel, name) if rel else name)
            if _is_kept(sub, keep):
                continue
            try:
                if _is_link(full):
                    logging.info(f"[OFFICECLEAN] Link/csomópont, nem követjük: {path}\\{sub}")
                elif os.path.isdir(full):
                    if _protected_dir(name, prog):
                        logging.info(f"[OFFICECLEAN] VÉDETT mappa, nem törölve: {path}\\{sub}")
                        continue
                    walk(full, sub)
                    if not os.listdir(full):
                        os.rmdir(full)
                elif _protected_file(name, prog):
                    logging.info(f"[OFFICECLEAN] VÉDETT fájl, nem törölve: {path}\\{sub}")
                else:
                    _remove_file(full)
            except OSError as e:
                errors.append(f'{path}\\{sub}: {e.strerror or e}')
    try:
        walk(lp, '')
        if not os.listdir(lp):
            os.rmdir(lp)
    except OSError as e:
        errors.append(f'{path}: {e.strerror or e}')
    return errors


def delete_key(root, path, keep=(), errors=None):
    """Registry-ág törlése (64 bites nézet); a `keep` alágak megmaradnak. Hibánál gyűjt."""
    errors = errors if errors is not None else []
    acc = winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY
    try:
        winreg.CloseKey(winreg.OpenKey(root, path, 0, acc))
    except FileNotFoundError:
        return errors
    except OSError as e:
        errors.append(f'{path}: {e.strerror or e}')
        return errors
    logging.warning(f"[OFFICECLEAN] REGISTRY TÖRLÉS: {path}" + (f" (megmarad: {', '.join(keep)})" if keep else ''))

    def rm(p, rel):
        try:
            with winreg.OpenKey(root, p, 0, acc) as k:
                subs = []
                while True:
                    try:
                        subs.append(winreg.EnumKey(k, len(subs)))
                    except OSError:
                        break
        except OSError as e:
            errors.append(f'{p}: {e.strerror or e}')
            return
        kept_any = False
        for s in subs:
            srel = f'{rel}\\{s}' if rel else s
            if _is_kept(srel, keep):
                kept_any = True
            elif _under(srel, keep):
                kept_any = True
                rm(f'{p}\\{s}', srel)
            else:
                rm(f'{p}\\{s}', srel)
        try:
            if kept_any:  # az ős megmarad, csak az értékei mennek
                with winreg.OpenKey(root, p, 0, acc) as k:
                    while True:
                        try:
                            winreg.DeleteValue(k, winreg.EnumValue(k, 0)[0])
                        except OSError:
                            break
            else:
                winreg.DeleteKeyEx(root, p, winreg.KEY_WOW64_64KEY, 0)
        except OSError as e:
            errors.append(f'{p}: {e.strerror or e}')
    rm(path, '')
    return errors


def _dir_left(path, keep):
    """Maradt-e a mappából olyasmi, amit törölni kellett volna (a megtartott és a védett
    felhasználói tartalom nem maradék)."""
    lp = _long_path(path)
    if not os.path.exists(lp):
        return False
    prog = _program_area(path)
    if os.path.isfile(lp):
        return not _protected_file(os.path.basename(path), prog)
    if _is_link(lp):
        return False
    for cur, dirs, files in os.walk(lp):
        rel = os.path.relpath(cur, lp)
        rel = '' if rel == '.' else rel
        if rel and (_is_kept(rel, keep) or any(rel.lower().startswith(k.lower() + '\\') for k in keep)):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not _protected_dir(d, prog) and not _is_link(os.path.join(cur, d))]
        if any(not _protected_file(f, prog) for f in files):
            return True
    return False


def _key_left(root, path, keep):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            if not keep:
                return True
            n_vals = winreg.QueryInfoKey(k)[1]
            subs = []
            while True:
                try:
                    subs.append(winreg.EnumKey(k, len(subs)))
                except OSError:
                    break
    except FileNotFoundError:
        return False
    if n_vals:
        return True
    for s in subs:
        if _is_kept(s, keep):
            continue
        if not _under(s, keep) or _key_left(root, f'{path}\\{s}',
                                            tuple(k[len(s) + 1:] for k in keep if _under(s, (k,)))):
            return True
    return False


# ---------------------------------------------------------------- felhasználók
# ---------------------------------------------------------------- dokumentum-mentés
# Az eltávolítás ELŐTT (explicit user decision, 2026-11-26: *„csináljon belőle egy külön mentést
# … c:/drivervarazslóba … backup mappanéven”*): minden, Office-programmal készíthető fájl a
# felhasználói profilokból. Ha nem sikerül, az Office-hoz nem nyúlunk.
# Az .ost kimarad: a levelezőszerver letöltött másolata (újra letölthető), és nagy (mérve a
# fejlesztői gépen: 2,7 GB a 3,3-ból). A takarító úgysem törli. A .pst (helyi levelezés) benne van.
BACKUP_EXT = (DOCUMENT_EXT - {'.txt', '.ost'}) | frozenset((
    '.dot', '.dotx', '.dotm', '.xlt', '.xltx', '.xltm', '.xlam', '.pot', '.potx', '.potm', '.ppam',
    '.thmx', '.vss', '.vssx', '.vst', '.vstx', '.mpt', '.oft', '.accdt', '.onetoc2'))
BACKUP_DIR_NAME = 'office_dokumentum_mentes'
BACKUP_FREE_MARGIN = 2 * 1024 ** 3          # ennyi szabad hely maradjon a mentés után
# Csak a felhőben lévő OneDrive-fájl (Files On-Demand): megnyitása letöltést indítana.
_CLOUD_ONLY_ATTRS = 0x400000 | 0x40000 | 0x1000   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE
# Az AppData-ban csak a Microsoft-mappákban keresünk (ott vannak az Office felhasználói fájljai:
# sablon, aláírás, nem mentett, Outlook); a többi (fejlesztői eszközök, böngésző) csak lassít.
_BACKUP_APPDATA_OK = (os.path.join('appdata', 'roaming', 'microsoft'),
                      os.path.join('appdata', 'local', 'microsoft'))
_BACKUP_SKIP = (os.path.join('appdata', 'local', 'microsoft', 'windows', 'inetcache'),)


def _backup_walk_ok(sub):
    """A profilon belüli (kisbetűs) relatív mappába bemenjünk-e a kereséskor."""
    if any(sub.startswith(s) for s in _BACKUP_SKIP):
        return False
    if sub == 'appdata' or not sub.startswith('appdata' + os.sep):
        return True
    return any(sub.startswith(ok) or ok.startswith(sub + os.sep) for ok in _BACKUP_APPDATA_OK)


def find_documents(roots):
    """[(útvonal, méret)] a `roots` alatt + a csak felhős fájlok száma. Linket nem követ."""
    found, cloud = [], 0
    for root in roots:
        lroot = _long_path(root)
        for cur, dirs, files in os.walk(lroot):
            rel = os.path.relpath(cur, lroot).lower()
            keep = []
            for d in dirs:
                sub = os.path.join(rel, d.lower()) if rel != '.' else d.lower()
                if not _is_link(os.path.join(cur, d)) and _backup_walk_ok(sub):
                    keep.append(d)
            dirs[:] = keep
            for f in files:
                if os.path.splitext(f)[1].lower() not in BACKUP_EXT:
                    continue
                full = os.path.join(cur, f)
                try:
                    st = os.stat(full, follow_symlinks=False)
                except OSError:
                    continue
                if getattr(st, 'st_file_attributes', 0) & _CLOUD_ONLY_ATTRS:
                    cloud += 1
                    continue
                found.append((full, st.st_size))
    return found, cloud


def backup_documents(log, roots, dest_parent):
    """A dokumentumok másolata `dest_parent\\office_dokumentum_mentes\\<idő>` alá, az eredeti
    útvonallal. Visszatérés: (ok, szöveg). Hely-ellenőrzés előtte, méret-visszaolvasás utána."""
    def say(m):
        logging.info(f"[OFFICEBACKUP] {m}")
        log(m)

    say('📦 0/3: a felhasználók dokumentumainak keresése a mentéshez...')
    t0 = time.monotonic()
    found, cloud = find_documents(roots)
    total = sum(s for _, s in found)
    say(f'   {len(found)} dokumentum, {total / 1024 ** 2:.0f} MB ({time.monotonic() - t0:.0f} mp)'
        + (f'; {cloud} csak a OneDrive-felhőben van, azt nem töltöm le (ott biztonságban van)' if cloud else ''))
    if not found:
        return True, 'Nincs mentendő dokumentum.'
    os.makedirs(dest_parent, exist_ok=True)
    free = shutil.disk_usage(dest_parent).free
    if total + BACKUP_FREE_MARGIN > free:
        return False, (f'Nincs elég hely a mentéshez: {total / 1024 ** 3:.1f} GB kellene, '
                       f'{free / 1024 ** 3:.1f} GB szabad (+2 GB tartalék).')
    dest = os.path.join(dest_parent, BACKUP_DIR_NAME, time.strftime('%Y-%m-%d %H-%M-%S'))
    logging.warning(f"[OFFICEBACKUP] MENTÉS IDE: {dest} ({len(found)} fájl)")
    errors, done, step = [], 0, max(1, len(found) // 10)
    for i, (src, size) in enumerate(found, 1):
        disp = _disp(src)
        target = _long_path(os.path.join(dest, os.path.splitdrive(disp)[1].lstrip('\\/')))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
            if os.path.getsize(target) != size:
                raise OSError('a másolat mérete eltér')
            done += size
        except OSError as e:
            errors.append(f'{disp}: {e.strerror or e}')
        if i % step == 0:
            say(f'   mentve: {i} / {len(found)} ({done / 1024 ** 2:.0f} MB)')
    with open(os.path.join(dest, 'INFO.txt'), 'w', encoding='utf-8-sig') as f:
        f.write(f'Office-eltávolítás előtti dokumentum-mentés, {time.strftime("%Y-%m-%d %H:%M:%S")}\n'
                f'{len(found) - len(errors)} / {len(found)} fájl, {done / 1024 ** 2:.0f} MB\n'
                + (f'Csak a OneDrive-felhőben (nem mentve, ott megvan): {cloud}\n' if cloud else '')
                + ('\nNEM SIKERÜLT:\n' + '\n'.join(errors) + '\n' if errors else '')
                + '\nMentett fájlok:\n' + '\n'.join(_disp(p) for p, _ in found) + '\n')
    for e in errors:
        logging.warning(f"[OFFICEBACKUP] {e}")
    if errors:
        return False, (f'{len(errors)} dokumentumot nem sikerült menteni (pl. {errors[0]}). '
                       f'A lista: {dest}\\INFO.txt')
    say(f'   ✅ mentve: {len(found)} dokumentum ide: {dest}')
    return True, dest


def user_profiles():
    """[(SID, profilmappa)] a valódi (S-1-5-21-…) felhasználókra, a ProfileList-ből."""
    out = []
    pl = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList'
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, pl) as k:
        i = 0
        while True:
            try:
                sid = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            if not sid.startswith('S-1-5-21-'):
                continue
            try:
                with winreg.OpenKey(k, sid) as s:
                    path = os.path.expandvars(winreg.QueryValueEx(s, 'ProfileImagePath')[0])
                out.append((sid, path))
            except OSError:
                continue
    return out


def _hive_loaded(sid):
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_USERS, sid))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- a teljes takarítás
def deep_clean(run, log, keep_settings=False, keep_activation=False, remove_hub=False):
    """Az Office maradékainak törlése. Visszatérés: a megmaradt / nem ellenőrizhető tételek
    listája (üres = minden célpont eltűnt)."""
    def say(m):
        logging.info(f"[OFFICECLEAN] {m}")
        log(m)

    say('🧹 3/3: az Office maradékainak törlése (Microsoft kézi eltávolítási lépései)'
        + (' — a fiókok/beállítások MARADNAK' if keep_settings else '')
        + (' — az aktiválás MARAD' if keep_activation else '')
        + ' — felhasználói fájlhoz (dokumentum, sablon, aláírás, .pst) NEM nyúl')
    errors, problems = [], []

    if remove_hub:
        problems += remove_office_hub(run, say)

    for exe in C2R_PROCESSES:
        run(['taskkill', '/F', '/IM', exe], timeout=30, ok_codes=(0, 128))
    run(['sc', 'stop', 'ClickToRunSvc'], timeout=60, ok_codes=(0, 1060, 1062))
    r = run(['sc', 'delete', 'ClickToRunSvc'], timeout=60, ok_codes=(0, 1060, 1072))
    if r.returncode not in (0, 1060, 1072):
        problems.append(f'ClickToRunSvc szolgáltatás: nem törölhető (kód {r.returncode})')

    logging.warning("[OFFICECLEAN] ÜTEMEZETT FELADATOK TÖRLÉSE: \\Microsoft\\Office\\*")
    run(['powershell', '-NoProfile', '-Command',
         "Get-ScheduledTask -TaskPath '\\Microsoft\\Office\\*' -ErrorAction SilentlyContinue | "
         "ForEach-Object { Unregister-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath "
         "-Confirm:$false; 'TOROLVE: ' + $_.TaskPath + $_.TaskName }"], timeout=120)

    if not keep_activation:
        logging.warning("[OFFICECLEAN] AZ OFFICE TERMÉKKULCSAINAK TÖRLÉSE a Windows licenc-tárából")
        run(['powershell', '-NoProfile', '-Command',
             "Get-CimInstance SoftwareLicensingProduct -Filter \"ApplicationID='"
             + OFFICE_LICENSE_APP_ID + "' AND PartialProductKey IS NOT NULL\" | ForEach-Object { "
             "Invoke-CimMethod -InputObject $_ -MethodName UninstallProductKey | Out-Null; "
             "'KULCS TOROLVE: ' + $_.Name }"], timeout=180)

    dirs = machine_dirs(keep_activation)
    keys = [(winreg.HKEY_LOCAL_MACHINE, p, k) for p, k in machine_keys(keep_activation)]

    # A Programok listájának Click-to-Run sorai: a fájljaik ekkorra eltűntek (Microsoft kézi lépés).
    for unroot in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                   r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, unroot) as k:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(k, sub) as s:
                            us = str(winreg.QueryValueEx(s, 'UninstallString')[0])
                    except OSError:
                        continue
                    if 'officeclicktorun.exe' in us.lower():
                        keys.append((winreg.HKEY_LOCAL_MACHINE, f'{unroot}\\{sub}', ()))
        except OSError:
            pass

    # Felhasználók: a betöltött registry-ágak közvetlenül, a többit `reg load`-dal töltjük be.
    loaded = []
    rules = user_key_rules(keep_settings, keep_activation)
    try:
        profiles = user_profiles()
    except OSError as e:
        profiles = []
        problems.append(f'A felhasználók listája nem olvasható: {e}')
    for sid, profile in profiles:
        start = os.path.join(profile, 'AppData', 'Roaming', 'Microsoft', 'Windows', 'Start Menu', 'Programs')
        dirs += [(d, k) for d, k in user_dirs(profile, keep_settings, keep_activation)]
        dirs += _office_shortcuts(start)
        for f in shortcut_folders(profile):
            dirs += _office_lnks_checked(f)
        if not rules:
            continue
        hive = sid
        if not _hive_loaded(sid):
            if not os.path.isfile(os.path.join(profile, 'NTUSER.DAT')):
                # pl. a telepítő `defaultuser0`-ja: nincs registryje, nincs mit takarítani
                logging.info(f"[OFFICECLEAN] {profile}: nincs NTUSER.DAT, a registryje kimarad")
                continue
            hive = f'DV_OFFICE_{len(loaded)}'
            r = run(['reg', 'load', f'HKU\\{hive}', os.path.join(profile, 'NTUSER.DAT')], timeout=60)
            if r.returncode != 0:
                problems.append(f'{profile}: a felhasználó registryje nem tölthető be (kód {r.returncode})')
                continue
            loaded.append(hive)
        keys += [(winreg.HKEY_USERS, f'{hive}\\{p}', k) for p, k in rules]

    pd = _env('ProgramData', r'C:\ProgramData')
    dirs += _office_shortcuts(os.path.join(pd, 'Microsoft', 'Windows', 'Start Menu', 'Programs'))
    dirs += _office_lnks_checked(os.path.join(_env('PUBLIC', r'C:\Users\Public'), 'Desktop'))

    for path, keep in dirs:
        delete_dir(path, keep, errors)
    # A tálca a rögzített ikont az Intéző újraindításáig mutatja; a Windows magától visszaindítja
    # a héjat (AutoRestartShell), a felhasználó jogán. Csak ha tálcaikont töröltünk.
    if any('\\user pinned\\taskbar\\' in p.lower() and not os.path.exists(p) for p, _ in dirs):
        logging.warning("[OFFICECLEAN] Az Intéző újraindítása (törölt tálcaikonok)")
        run(['taskkill', '/F', '/IM', 'explorer.exe'], timeout=30, ok_codes=(0, 128))
    for root, path, keep in keys:
        delete_key(root, path, keep, errors)

    # VISSZAOLVASÁS: a verdikt ez (a törlés közbeni hibák csak a naplóba mennek).
    left = [f'mappa: {p}' for p, k in dirs if _dir_left(p, k)]
    left += [f'registry: {p}' for root, p, k in keys if _key_left(root, p, k)]
    for hive in loaded:
        run(['reg', 'unload', f'HKU\\{hive}'], timeout=60)
    for e in errors:
        logging.warning(f"[OFFICECLEAN] {e}")
    left += problems
    say('Maradék: ' + (f'{len(left)} tétel' if left else 'nincs'))
    return left


def remove_office_hub(run, say):
    """A Windows gyári „Microsoft 365 (Office)” indító-alkalmazásának eltávolítása (minden
    felhasználóról + a kiépítésből, hogy új felhasználó se kapja meg). Külön jelölőnégyzet
    (explicit user decision, 2026-11-26). Visszatérés: a problémák listája (visszaolvasva)."""
    logging.warning(f"[OFFICECLEAN] ALKALMAZÁS ELTÁVOLÍTÁSA: {OFFICE_HUB_APPX} (minden felhasználó + kiépítés)")
    say('🗑️ A „Microsoft 365 (Office)” alkalmazás eltávolítása...')
    run(['powershell', '-NoProfile', '-Command',
         f"Get-AppxPackage -AllUsers -Name '{OFFICE_HUB_APPX}' | ForEach-Object {{ "
         "Remove-AppxPackage -Package $_.PackageFullName -AllUsers -ErrorAction Continue }; "
         "Get-AppxProvisionedPackage -Online | Where-Object { $_.DisplayName -eq '"
         + OFFICE_HUB_APPX + "' } | Remove-AppxProvisionedPackage -Online -ErrorAction Continue | Out-Null"],
        timeout=300)
    r = run(['powershell', '-NoProfile', '-Command',
             f"@(Get-AppxPackage -AllUsers -Name '{OFFICE_HUB_APPX}').Count"], timeout=120)
    n = (r.stdout or '').strip()
    if r.returncode != 0 or not n.isdigit():
        return [f'„Microsoft 365 (Office)” alkalmazás: nem ellenőrizhető (kód {r.returncode})']
    if n != '0':
        return [f'„Microsoft 365 (Office)” alkalmazás: még fent van ({n} felhasználónál)']
    say('   ✅ a „Microsoft 365 (Office)” alkalmazás eltávolítva')
    return []


def _office_shortcuts(start_programs):
    """Az Office parancsikonjai és „Microsoft Office…” mappái egy Start menü Programs mappában."""
    out = []
    try:
        names = os.listdir(start_programs)
    except OSError:
        return out
    for n in names:
        full = os.path.join(start_programs, n)
        if _OFFICE_LNK.match(n) or (os.path.isdir(full) and _OFFICE_MENU_DIR.match(n)):
            out.append((full, ()))
    return out


def _lnk_points_to_office(path):
    try:
        with open(path, 'rb') as f:
            data = f.read(65536).lower()
    except OSError:
        return False
    return any(t.encode('ascii') in data or t.encode('utf-16-le') in data for t in _OFFICE_TARGETS)


def _office_lnks_checked(folder):
    """Asztal / tálca: minden Office-programra mutató .lnk, a nevétől függetlenül (explicit user
    decision, 2026-11-26: *„tálcáról asztalról is töröljön minden ikont ami office volt”*; egy
    átnevezett „Szövegszerkesztő.lnk” is megy). A cél dönt, így egy saját „Word.lnk” marad."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [(os.path.join(folder, n), ()) for n in names
            if n.lower().endswith('.lnk') and _lnk_points_to_office(os.path.join(folder, n))]


def shortcut_folders(profile):
    """Egy felhasználó asztala (a OneDrive-os is) és a tálcára/Startra rögzített ikonjai."""
    pinned = os.path.join(profile, 'AppData', 'Roaming', 'Microsoft', 'Internet Explorer',
                          'Quick Launch', 'User Pinned')
    return [os.path.join(profile, 'Desktop'), os.path.join(profile, 'OneDrive', 'Desktop'),
            os.path.join(pinned, 'TaskBar'), os.path.join(pinned, 'StartMenu')]
