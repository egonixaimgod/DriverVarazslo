"""DriverVarázsló GUI - Windows & Office nézet: aktiválási állapot, gyári kulcs, aktiválás.

MIT OLD MEG A SZERVIZBEN: újratelepítés után a leggyakoribb kérdés, hogy "miért nem
aktivált magától" - a válasz szinte mindig a licenc CSATORNÁJÁBAN van (OEM/Retail/Volume),
és OEM gépnél a kulcs ott van a UEFI MSDM táblájában, csak ki kell olvasni. Ez a nézet
ezeket egy képernyőn megmutatja, és egy gombbal el is végzi az aktiválást.

NINCS BEÍRANDÓ MEZŐ - EGY GOMB, ÉS VÉGIGMEGY (explicit user decision, 2026-08-28:
"ne legyen ures mezo, ne kelljen mezokbe irkalni, ha ranyomok menjen vegig"). A program
magától eldönti, mit használ:
  - VAN gyári kulcs a BIOS-ban -> AZT (és KMS-kiszolgáló nem kell hozzá),
  - NINCS -> a kiadáshoz tartozó nyilvános GVLK + a `winact_core.KMS_HOST` konstans,
  - VAN gyári kulcs, DE a Windows ELUTASÍTJA (más kiadáshoz való) -> tartalékként a
    GVLK + KMS, kiírt figyelmeztetéssel (2026-09-07, lásd a tartalék-ág kommentjét).
A KMS-kiszolgáló címe tehát a FORRÁSBAN van (app/winact_core.py tetején), nem a felületen.

A SIKERT MINDIG A WMI MONDJA MEG, nem az slmgr kimenete - lásd app/winact_core.py fejléc.

OFFICE-AKTIVÁLÁS (2026-09-14): külön gomb, ami egy SAJÁT GitHub release-ből tölt le egy
ZIP-et, kicsomagolja, és elindítja benne a megnevezett .bat fájlt - a letöltés ugyanazon
a bevált úton, amit a stresstools.zip használ. A csomag URL-je és a .bat neve az
`app/winact_core.py` tetején lévő két konstansban van (OFFICE_ACTIVATOR_URL /
OFFICE_ACTIVATOR_BAT); ha a .bat neve nincs beállítva, a gomb letölt, kicsomagol és
KIÍRJA, milyen .bat/.cmd fájlok vannak a csomagban - futtatni olyankor semmit nem futtat.
"""

# === AUTO-IMPORTS ===
import os
import logging

from app import winact_core
# === /AUTO-IMPORTS ===


class GuiWinActMixin:
    """Windows & Office nézet: aktiválási állapot és aktiválás.
    A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ---------------------------------------------------------------- állapot
    def get_activation_status(self):
        """A teljes aktiválási kép (Windows + Office). Szinkron: két WMI-lekérdezés."""
        logging.info("[API] get_activation_status()")
        if self.target_os_path:
            return {'offline': True}
        try:
            win = winact_core.collect_windows_activation(self._run)
            office = winact_core.collect_office_activation(self._run)
            return {'offline': False, 'windows': win, 'office': office,
                    'plan': self._winact_plan(win),
                    # Az Office-aktiváló csomag beállítottsága: ebből dönti el a felület,
                    # hogy a gomb engedélyezett-e, és mit írjon mellé (lásd
                    # winact_core.office_activator_plan).
                    'office_plan': winact_core.office_activator_plan(),
                    'kms_note': winact_core.KMS_RENEWAL_NOTE}
        except Exception as e:
            logging.error(f"[WINACT] Az állapot lekérdezése hibára futott: {e}", exc_info=True)
            return {'offline': False, 'error': str(e)}

    def _winact_plan(self, win):
        """MIT FOG CSINÁLNI a gomb. A felület ezt kiírja, hogy a technikus a kattintás
        ELŐTT lássa - egy gomb, aminek a hatása nem látszik előre, a legrosszabb fajta."""
        kms = (winact_core.KMS_HOST or '').strip()
        if win.get('oem_key'):
            return {'source': 'oem', 'key': win['oem_key'], 'kms': '', 'ready': True,
                    'text': 'A gépbe égetett GYÁRI kulccsal aktivál (KMS-kiszolgáló nem kell).'}
        if not kms:
            return {'source': 'gvlk', 'key': win.get('gvlk', ''), 'kms': '', 'ready': False,
                    'text': ('Ezen a gépen NINCS gyári kulcs, KMS-kiszolgáló pedig nincs beállítva - '
                            'így nem lehet aktiválni. A címet az app/winact_core.py fájl tetején, '
                            'a KMS_HOST sorba kell beírni.')}
        return {'source': 'gvlk', 'key': win.get('gvlk', ''), 'kms': kms, 'ready': True,
                'text': f"Nincs gyári kulcs, ezért a(z) {win.get('gvlk_name')} kulccsal és a "
                        f"{kms} KMS-kiszolgálóval aktivál."}

    # ------------------------------------------------------------- aktiválás
    def activate_windows(self):
        """EGY KATTINTÁS: kulcs telepítése -> KMS-kiszolgáló (csak ha kell) -> aktiválás,
        majd ELLENŐRZÉS a WMI-ből. Nincs paramétere: a program magától dönt (lásd
        `_winact_plan` és a modul fejléce)."""
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Offline módban nem elérhető!', 'type': 'error'})
            return

        def worker():
            task = 'winact'
            self.emit('task_start', {'task': task, 'title': 'Windows aktiválása'})
            try:
                before = winact_core.collect_windows_activation(self._run)
                self.emit('task_progress', {'task': task, 'log':
                          f"Kiadás: {before.get('edition') or 'ismeretlen'}\n"
                          f"Jelenlegi állapot: {before.get('status_text')}"})

                # KULCSVÁLASZTÁS. Ha a gépnek VAN gyári kulcsa a BIOS-ban, AZT használjuk:
                # a gépnek már van érvényes licence, és a GVLK ráhúzása lecserélné egy
                # KMS-kliens kulcsra, ami KMS-kiszolgáló nélkül nem is aktiválna - vagyis
                # egy működő licencből csinálnánk nem működőt. A GVLK a tartalék.
                plan = self._winact_plan(before)
                use_key, use_kms = plan['key'], plan['kms']

                if plan['source'] == 'oem':
                    # "Ez a kulcs mindig működni fog" ÁLLT ITT 2026-09-07-ig - és nem
                    # igaz: a terepi Dell-en a Windows elutasította, mert a BIOS-kulcs
                    # nem a telepített kiadáshoz való. Egy magabiztos, hamis mondat a
                    # bukás ELŐTT rosszabb, mint a semmi.
                    self.emit('task_progress', {'task': task, 'log':
                              '🔑 Ezen a gépen GYÁRI kulcs van a BIOS-ban, ezért ELŐSZÖR azt '
                              'próbáljuk (KMS-kiszolgáló nem kell hozzá).'})
                elif not plan['ready']:
                    # Nem kezdünk bele: a GVLK felrakása KMS nélkül csak elrontaná a
                    # jelenlegi állapotot, aktiválni pedig úgysem tudna.
                    self.emit('task_progress', {'task': task, 'log': '❌ ' + plan['text']})
                    self.emit('task_complete', {'task': task, 'status': '❌ Nincs beállítva KMS-kiszolgáló'})
                    return
                else:
                    self.emit('task_progress', {'task': task, 'log':
                              f"ℹ️ Nincs gyári kulcs a BIOS-ban, ezért a kiadáshoz tartozó nyilvános "
                              f"KMS-kulcs (GVLK) megy fel: {before.get('gvlk_name')}"})

                verdict, out, after = self._winact_attempt(task, use_key, use_kms)
                tartalek_futott = False

                # ============================================================
                # TARTALÉK: a GYÁRI kulcs elbukott -> KMS/GVLK (2026-09-07)
                # ============================================================
                # Ez a 2026-08-28-i "a gyári kulcs mindig nyer" szabály SZŰKÍTÉSE, nem a
                # visszavonása, és a feltételek pontosan azt a rést fedik le, amit a
                # terepi Dell-eset megmutatott: a BIOS-kulcs Home, a gépen viszont Pro
                # van, tehát a gyári kulcs ezen a rendszeren HASZNÁLHATATLAN - a program
                # eddig itt megállt, és a technikusnak kézzel kellett kulcsot cserélnie.
                #
                # CSAK az `ipk` bukására lép be, és ez a legfontosabb feltétel:
                #   - `ipk_failed` = a gyári kulcsot maga a Windows utasította el (rossz
                #     kiadás), tehát FEL SEM KERÜLT: nincs mit elrontani rajta;
                #   - `not_activated` (az /ipk sikerült, csak az /ato nem) esetén NEM
                #     lépünk be, mert ott a gyári kulcs BIZONYÍTOTTAN illik a kiadáshoz
                #     (az /ipk validálja), a bukás oka jellemzően hálózat - a GVLK
                #     ráhúzása egy JÓ gyári kulcsot cserélne le KMS-kliens kulcsra, ami
                #     pontosan a 2026-08-28-i szabály tiltása.
                # Plusz: csak nem aktivált gépen (working licencet soha nem bántunk).
                if (verdict == 'ipk_failed' and plan['source'] == 'oem'
                        and not before.get('activated')
                        and before.get('gvlk') and (winact_core.KMS_HOST or '').strip()):
                    use_kms = (winact_core.KMS_HOST or '').strip()
                    self.emit('task_progress', {'task': task, 'log':
                              '\n⚠️ FIGYELEM - TARTALÉK AKTIVÁLÁS INDUL\n'
                              '   Ezen a gépen VAN gyári, alaplapba égetett kulcs, de azt a Windows '
                              'elutasította (a fenti hibakód szerint nem ehhez a kiadáshoz való). '
                              'Mivel a gép jelenleg NINCS aktiválva, a gyári kulcs pedig fel sem került, '
                              f'a program most a szerviz KMS-kulcsával próbálja meg: {before.get("gvlk_name")}.\n'
                              '   AMIT EZ JELENT: a Windows ezután KMS-licenccel lesz aktiválva, NEM gyárival.\n'
                              '   A gyári kulcs a BIOS-ban MEGMARAD (onnan nem törölhető) - ha később a '
                              'hozzá való kiadás kerül a gépre, az magától gyári aktiválást kap.'})
                    logging.warning("[WINACT] A gyári kulcs elbukott az /ipk lépésnél - "
                                    "tartalék aktiválás a GVLK + KMS ággal.")
                    tartalek_futott = True
                    verdict, out, after = self._winact_attempt(
                        task, before.get('gvlk'), use_kms, cimke='TARTALÉK: ')

                if verdict == 'ok':
                    msg = '\n✅ KÉSZ - a Windows AKTIVÁLVA van.'
                    if use_kms:
                        msg += ('\n⚠️ Ez KMS-licenc, nem a gyári: a gépben lévő gyári kulcs nem volt '
                                'használható ehhez a kiadáshoz.' if before.get('oem_key') else '')
                        msg += f"\nℹ️ {winact_core.KMS_RENEWAL_NOTE}"
                    self.emit('task_progress', {'task': task, 'log': msg})
                    self.emit('task_complete', {'task': task, 'status': '✅ A Windows aktiválva'})
                else:
                    # EGY helyen a magyarázat, hogy a bukási ágak ne mondhassanak mást
                    # ugyanarról a gépről (ez volt a 2026-09-07 előtti állapot hibája).
                    self._winact_fail_report(task, after or before, use_kms, out,
                                             verdict=verdict, fallback_used=tartalek_futott)
                    oem = (after or before).get('oem_key')
                    self.emit('task_complete', {'task': task, 'status': (
                        '❌ A gyári kulcs és a KMS-kulcs sem működött' if tartalek_futott else
                        '❌ A gyári kulcs nem megy ehhez a kiadáshoz' if (oem and verdict == 'ipk_failed') else
                        '❌ A gyári kulccsal sem sikerült' if oem else
                        '❌ KMS-beállítás sikertelen' if verdict == 'kms_failed' else
                        '❌ Az aktiválás nem sikerült')})
            except Exception as e:
                logging.error(f"[WINACT] Az aktiválás hibára futott: {e}", exc_info=True)
                self.emit('task_progress', {'task': task, 'log': f'❌ Hiba: {e}'})
                self.emit('task_complete', {'task': task, 'status': '❌ Hiba'})

        self._safe_thread('winact', worker)

    # --------------------------------------------------------- Office aktiválás
    def activate_office(self):
        """EGY KATTINTÁS: az Office-aktiváló csomag letöltése a GitHub release-ből,
        kicsomagolása, és a benne lévő .bat elindítása külön, látható ablakban.

        A LETÖLTÉS UGYANAZ A BEVÁLT ÚT, AMIT A stresstools.zip HASZNÁL (explicit user
        decision, 2026-09-14) - a mag a `winact_core.download_office_activator`, ott van
        leírva, mi az azonos és mi tér el. A csomag URL-jét és az elindítandó .bat nevét a
        felhasználó írja be az `app/winact_core.py` tetején lévő két konstansba.

        MIÉRT NEM FUT LE "MAGÁTÓL" A VÉGÉN AZ ELLENŐRZÉS, mint a Windows-aktiválásnál: az
        aktiváló script INTERAKTÍV (menüs, a technikus válaszol neki), és külön, önálló
        folyamatban fut - nem tudjuk, mikor végzett, és megvárni sem szabad (az ember
        nélkül futó lánccal ellentétben itt épp ember ül a gép előtt).

        SEMMILYEN FELUGRÓ ABLAK NINCS - SE MEGERŐSÍTŐ, SE FOLYAMAT-MODÁL (explicit user
        decision, 2026-09-14: *"nem kell ilyen felugro faszom ablak amikor ranyomok h
        office aktivalasa es le kell okezni, ranyomok töltse le es inditsa el a bat filet a
        zipbe ennyi"*). Az első változat a szokásos `task_start`/`task_complete` modált
        használta, amit a végén le kellett OK-zni - egyetlen gombnyomásból így három lett.
        Ez pontosan az a minta, amit az EGYENKÉNTI stresstool-indítás (`start_stress_tool`)
        már 2026-07 óta követ, szintén explicit kérésre: a letöltés a NÉZETBE ÁGYAZOTT
        sávon látszik (`stress_dl_progress`), az eredmény egy rövid toast, a részletes
        szöveg pedig a gomb alatti dobozba kerül (`officeact_result`) - ott elolvasható,
        de semmit nem kell lezárni. Ne tedd vissza a modált."""
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Offline módban nem elérhető!', 'type': 'error'})
            return

        def worker():
            plan = winact_core.office_activator_plan()
            if not plan['ready']:
                # Nincs beállítva a letöltési link - a teendőt EGY helyen írjuk le
                # (a plan szövege), hogy a felület és a napló ne mondhasson mást.
                self._officeact_msg(False, plan['text'])
                return
            try:
                # A LETÖLTÉSI SÁV A STRESSTOOLS THROTTLINGOLT EMITTERÉT HASZNÁLJA: a két
                # API-osztály ugyanazon a `self`-en osztozik, tehát ez nem duplikáció,
                # hanem újrahasznosítás - és a callback alakja (fázis, kész, összes)
                # pontosan egyezik azzal, amit a `download_office_activator` vár.
                # A `finally`-ben lévő {'active': False} a hívó felelőssége (lásd a
                # `_stress_dl_progress_emitter` docstringjét), enélkül a sáv ottragadna.
                try:
                    ext_dir, batches = winact_core.download_office_activator(
                        self._run,
                        progress=self._stress_dl_progress_emitter('Office aktiváló'))
                finally:
                    self.emit('stress_dl_progress', {'active': False})

                # --- A .bat neve nincs beállítva: KIÍRJUK, mi van a csomagban.
                # Ez nem hibaág, hanem a beállítás elvégzésének a módja: a felhasználó
                # innen tudja meg, mit kell a konstansba írnia. Futtatni nem futtatunk.
                if plan['mode'] == 'list':
                    self._officeact_msg(False, plan['text'] + '\n\n'
                                        + self._batch_choices_text(ext_dir, batches))
                    return

                bat = winact_core.find_activator_bat(ext_dir, plan['bat'])
                if not bat:
                    # NEM elég azt mondani, hogy "nem található": abból nem derül ki, hogy
                    # elgépelt nevet írt-e be, vagy a csomag tartalma más. A találtakat
                    # tehát ki KELL írni (ugyanaz az elv, mint a nyomtató-hibánál: a gép
                    # saját listáját naplózzuk, nem csak a hiányt).
                    logging.warning(f"[OFFICEACT] A megadott script nem található a csomagban: "
                                    f"'{plan['bat']}' (mappa: {ext_dir})")
                    self._officeact_msg(False, f"A csomagban NINCS '{plan['bat']}' nevű fájl.\n\n"
                                        + self._batch_choices_text(ext_dir, batches))
                    return

                winact_core.launch_activator_bat(bat)
                self._officeact_msg(True,
                    f'Elindult: {os.path.basename(bat)} — egy külön, fekete parancssori '
                    'ablakban, rendszergazdaként. Ott folytasd, kövesd a script utasításait. '
                    'Az ablak a végén nyitva marad, hogy lásd az eredményt (X-szel bezárható). '
                    'Ha kész, az „Állapot Frissítése” gombbal ellenőrizheted, aktiválva '
                    'lett-e az Office.')
            except Exception as e:
                logging.error(f"[OFFICEACT] Az Office-aktiválás hibára futott: {e}", exc_info=True)
                self._officeact_msg(False, str(e))

        self._safe_thread('officeact', worker)

    def _officeact_msg(self, ok, text):
        """Az Office-aktiválás eredménye: RÖVID toast + a részletes szöveg a nézetbe.

        MIÉRT KETTŐ: a toast 3-4 másodpercig látszik és nem fér bele egy több soros
        magyarázat (a hibák itt hosszúak: mit hova kell beírni, vírusirtó-kizárás,
        a csomagban talált fájlok listája). A toast tehát csak azt mondja meg, hogy
        SIKERÜLT-E, és hogy hol a részlet; a szöveg maga a gomb alatti dobozba megy, ahol
        ott is marad, amíg el nem olvassák - lezárni viszont nem kell semmit."""
        self.emit('officeact_result', {'ok': bool(ok), 'text': str(text or '')})
        self.emit('toast', {
            'message': ('✅ Az aktiváló elindult — folytasd a fekete ablakban!' if ok
                        else '❌ Nem indult el — a részletek a gomb alatt olvashatók.'),
            'type': 'success' if ok else 'error'})

    def _batch_choices_text(self, ext_dir, batches):
        """A csomagban talált .bat/.cmd fájlok felsorolása - ez a válasz arra, hogy "mit
        írjak be a konstansba?". Két helyről hívjuk (nincs beállított név; a beállított
        név nem található), mert mindkét esetben pontosan ugyanez a teendő - két külön
        szöveg előbb-utóbb eltérne egymástól."""
        if batches:
            return ('📋 A csomagban ezek az indítható script-fájlok vannak:\n'
                    + '\n'.join(f'   • {b}' for b in batches)
                    + '\n\n👉 A megfelelő fájl NEVÉT (az esetleges almappa nélkül) írd be az '
                      'app/winact_core.py fájl OFFICE_ACTIVATOR_BAT sorába, pl.:\n'
                      f"   OFFICE_ACTIVATOR_BAT = '{os.path.basename(batches[0])}'")
        return ('⚠️ A csomagban EGYETLEN .bat/.cmd fájl sincs. Lehet, hogy .exe-t vagy .ps1-et '
                f'tartalmaz - azt kézzel kell elindítani ebből a mappából:\n   {ext_dir}')

    def clear_kms_server(self):
        """A beállított KMS-kiszolgáló törlése (vissza a Microsoft alapértelmezettre).

        A GOMBJA 2026-09-14-én KIKERÜLT A FELÜLETRŐL (explicit user decision), a metódus
        viszont ÉL: a CLI aktiválás-menüje továbbra is kínálja. Ne töröld holt kódként."""
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Offline módban nem elérhető!', 'type': 'error'})
            return

        def worker():
            ok, out = winact_core.clear_kms_host(self._run)
            self.emit('toast', {'message': ('✅ A KMS-kiszolgáló törölve.' if ok
                                            else f'❌ Nem sikerült: {out}'),
                                'type': 'success' if ok else 'error'})
            self.emit('activation_status', self.get_activation_status())

        self._safe_thread('winact-ckms', worker)

    def open_activation_tool(self, which):
        """A Windows saját aktiválási felületei. A kulcsból LOOKUP van, nem futtatás -
        így a frontendről nem lehet tetszőleges programot elindíttatni."""
        tools = {
            'settings': ['cmd', '/c', 'start', '', 'ms-settings:activation'],
            'changekey': ['cmd', '/c', 'start', '', 'ms-settings:activation'],
            'phone': ['cmd', '/c', 'start', '', 'slui.exe', '4'],
            'troubleshoot': ['cmd', '/c', 'start', '', 'ms-settings:activation'],
        }
        cmd = tools.get(str(which or ''))
        if not cmd:
            logging.warning(f"[WINACT] Ismeretlen aktiválási eszköz: {which}")
            return {'success': False}
        try:
            self._run(cmd, timeout=30)
            return {'success': True}
        except Exception as e:
            logging.warning(f"[WINACT] Az eszköz indítása nem sikerült ({which}): {e}")
            return {'success': False, 'error': str(e)}

    # ------------------------------------------------------------ segédek
    def _winact_attempt(self, task, use_key, use_kms, cimke=''):
        """EGY teljes próbálkozás: kulcs -> KMS (ha kell) -> aktiválás -> ELLENŐRZÉS.

        Visszatérés: (verdikt, utolsó_slmgr_kimenet, záró_állapot_vagy_None), ahol a
        verdikt: 'ok' | 'ipk_failed' | 'kms_failed' | 'not_activated'.

        MIÉRT KÜLÖN METÓDUS (2026-09-07): mert a gyári kulcs bukása után UGYANEZT a
        három lépést kell megismételni a GVLK-val, és két külön példány előbb-utóbb
        eltérne egymástól - ez a projekt legrégebbi visszatérő hibája. Itt csak a
        PRÓBÁLKOZÁS tényei mennek ki (melyik lépés, mit felelt, milyen hibakóddal); a
        magyarázat és a teendő a hívó `_winact_fail_report`-jának a dolga, mert az már a
        TELJES folyamatot ismeri (pl. hogy jön-e még tartalék próbálkozás)."""
        def kod_sor(out):
            kod, magyarazat = winact_core.slmgr_error_text(out)
            if kod:
                self.emit('task_progress', {'task': task, 'log':
                          f"🔎 Hibakód: {kod}" + (f" - {magyarazat}" if magyarazat else
                                                  " (ismeretlen kód, de kereshető)")})

        # --- 1/3 kulcs telepítése
        self.emit('task_progress', {'task': task, 'log': f'\n{cimke}1/3 - Termékkulcs telepítése: {use_key}'})
        ok, out = winact_core.install_product_key(self._run, use_key)
        self.emit('task_progress', {'task': task, 'log': f'   {out}'})
        if not ok:
            self.emit('task_progress', {'task': task, 'log':
                      '❌ A kulcs telepítése nem sikerült - ezzel a kulccsal nem folytatható.'})
            kod_sor(out)
            self.emit('activation_status', self.get_activation_status())
            return 'ipk_failed', out, None

        # --- 2/3 KMS-kiszolgáló (opcionális)
        if use_kms:
            self.emit('task_progress', {'task': task, 'log': f'\n{cimke}2/3 - KMS-kiszolgáló beállítása: {use_kms}'})
            ok, out = winact_core.set_kms_host(self._run, use_kms)
            self.emit('task_progress', {'task': task, 'log': f'   {out}'})
            if not ok:
                self.emit('task_progress', {'task': task, 'log':
                          '❌ A KMS-kiszolgáló beállítása nem sikerült.'})
                kod_sor(out)
                self.emit('activation_status', self.get_activation_status())
                return 'kms_failed', out, None
        else:
            self.emit('task_progress', {'task': task, 'log':
                      f'\n{cimke}2/3 - KMS-kiszolgáló nem kell, ez a lépés kimarad '
                      '(az aktiválás a Microsoft felé megy).'})

        # --- 3/3 aktiválás
        self.emit('task_progress', {'task': task, 'log': f'\n{cimke}3/3 - Aktiválás...', 'indeterminate': True})
        ok, out = winact_core.activate(self._run)
        self.emit('task_progress', {'task': task, 'log': f'   {out}'})

        # --- ELLENŐRZÉS. Az slmgr kimenete lokalizált, ezért a WMI dönt.
        self.emit('task_progress', {'task': task, 'log': '\nEllenőrzés (a rendszer licenc-állapota)...'})
        after = winact_core.collect_windows_activation(self._run)
        self.emit('activation_status', self.get_activation_status())
        if after.get('activated'):
            return 'ok', out, after
        self.emit('task_progress', {'task': task, 'log':
                  # A RÉSZLETES állapot, nem a jelvényé: az 5-ös kódnál a jelvény már
                  # "Aktiválva", és ez a sor ellentmondana neki.
                  f"\n❌ NEM sikerült - a rendszer licenc-állapota: "
                  f"{after.get('status_detail') or after.get('status_text')}"})
        kod_sor(out)
        return 'not_activated', out, after

    def _winact_fail_report(self, task, state, use_kms, out, verdict='', fallback_used=False):
        """MIÉRT nem sikerült - a kód lefordítva, a gyári kulcs esetén NÉVEN NEVEZVE.

        MINDEN bukási ágból ezt kell hívni, nem csak a végéről (2026-09-07, terepi eset,
        Dell laptop). Korábban a `_winact_hint` KIZÁRÓLAG a záró "nem aktivált" ágban
        futott le, a kulcs- és a KMS-telepítés bukásánál viszont nem - így egy gyári
        kulcsos gépen, ahol az `slmgr /ipk` hasalt el, a technikus ennyit látott:

            ❌ A kulcs telepítése nem sikerült - az aktiválás nem folytatható.
            Error: 0xC004F050 On a computer running Microsoft Windows non-core
            edition, run 'slui.exe 0x2a 0xC004F050' to display the error text.

        Vagyis a nyers, angol slmgr-szöveget, magyarázat és teendő nélkül - a technikus
        szavaival "valami sumák error szöveget". Pedig épp ez az a pillanat, amikor a
        legfontosabb kimondani, hogy a gépen GYÁRI kulcs van, és hogy a program mit
        kezd vele.

        A HIBAKÓD SORÁT NEM ITT ÍRJUK KI (2026-09-07, a tartalék-ág bevezetésekor): az a
        PRÓBÁLKOZÁS ténye, ezért a `_winact_attempt`-ben megy ki, minden próbálkozásnál
        külön. Enélkül a tartalék-ágon az ELSŐ (gyári kulcsos) hiba kódja elveszne, mert
        ez a jelentés már csak a második próbálkozás kimenetét látja. A kódot itt is
        kiolvassuk, de csak a szóhasználat eldöntéséhez (biztos ok vs. valószínű ok)."""
        sorok = []
        kod, _magyarazat = winact_core.slmgr_error_text(out)
        if state.get('oem_key'):
            # EZ A LÉNYEG, és a felhasználó kifejezetten ezt kérte: ne egy hibakód
            # álljon ott, hanem az, hogy mi a helyzet a gyári kulccsal.
            sorok.append(
                '🔑 EZEN A GÉPEN GYÁRI, AZ ALAPLAPBA ÉGETETT (BIOS/UEFI) KULCS VAN, ezért a '
                'program ELŐSZÖR mindig azzal próbál aktiválni. Saját (KMS/GVLK) kulcsot csak '
                'akkor tesz rá, ha a gyári kulcsot maga a Windows utasította el - egy MŰKÖDŐ '
                'gyári licencet soha nem cserélünk le, mert a gyári kulcs a BIOS-ból nem '
                'törölhető, és kár lenne érte.')
            kiadas = (state.get('edition') or '').strip()
            # A VERDIKT MONDJA MEG, MI AZ OK - ne a hibakódból találgassunk:
            #   fallback_used -> a GYÁRI kulcsot elutasította a Windows (ezért futott
            #                    tartalék), a verdikt viszont már a TARTALÉK próbálkozásé;
            #                    a kettőt összemosni itt egyenesen hazugság lenne (a saját
            #                    tesztem kapta el: "a gyári kulcsot ELFOGADTA" ment ki egy
            #                    olyan gépen, ahol épp az bukott el);
            #   ipk_failed    -> a Windows a KULCSOT utasította el = kiadás-eltérés;
            #   bármi más     -> a kulcsot ELFOGADTA (az /ipk validálja a kiadás ellen),
            #                    tehát a baj az aktiválás oldalán van (hálózat/kiszolgáló),
            #                    és kiadás-eltérést állítani itt egyenesen félrevezetne.
            if fallback_used:
                sorok.append(
                    'A gyári kulcsot a Windows ELUTASÍTOTTA (nem ehhez a kiadáshoz való'
                    + (f', most ez van fenn: {kiadas}' if kiadas else '')
                    + '), ezért próbáltuk meg a szerviz KMS-kulcsával - de az sem ment végig.')
            elif verdict == 'ipk_failed':
                biztos = kod in ('0xC004F050', '0xC004E016')
                sorok.append(
                    ('A gyári kulcs itt AZÉRT nem jó, mert nem ahhoz a KIADÁSHOZ való, ami fel van '
                     if biztos else
                     'A gyári kulcsot a Windows elutasította; ennek a leggyakoribb oka, hogy nem '
                     'ahhoz a KIADÁSHOZ való, ami fel van ')
                    + f'telepítve{f" (most ez van fenn: {kiadas})" if kiadas else ""}: a BIOS-kulcs '
                    'jellemzően Home, a gépre viszont Pro van telepítve. Ilyenkor a gyári kulcs '
                    'ezen a rendszeren nem használható.')
            else:
                sorok.append(
                    'A gyári kulcsot a Windows ELFOGADTA (tehát a kiadáshoz való), csak maga az '
                    'aktiválás nem ment végig - ez jellemzően hálózati vagy kiszolgáló-oldali hiba.')

            if fallback_used:
                sorok.append(
                    '👉 TEENDŐ: a program a gyári kulcs után a szerviz KMS-kulcsával is '
                    'megpróbálta, és az sem sikerült. Ellenőrizd, hogy a KMS-kiszolgáló elérhető-e '
                    'a gépről (tűzfal, VPN, hálózat). Ha igen, akkor a gyári kulcshoz való kiadást '
                    'érdemes feltenni - az utána magától aktivál a BIOS-ból.')
            elif verdict == 'ipk_failed':
                # Nem futott tartalék (nincs beállított KMS-kiszolgáló vagy GVLK).
                sorok.append(
                    '👉 TEENDŐ: vagy a gyári kulcsnak megfelelő kiadást kell feltenni (az utána '
                    'magától aktivál a BIOS-ból), vagy tudatosan MÁS kulccsal kell aktiválni - azt '
                    'a Gépház > Aktiválás > Termékkulcs módosítása alatt, kézzel. (A szerviz '
                    'KMS-kulcsával a program magától megpróbálta volna, de ahhoz a '
                    'winact_core.KMS_HOST nincs beállítva.)')
            else:
                sorok.append(
                    '👉 TEENDŐ: ellenőrizd az internetkapcsolatot, majd próbáld újra. A gyári '
                    'kulcs jó, tehát ha a hálózat rendben van, az aktiválásnak működnie kell.')
        else:
            sorok.append(self._winact_hint(state, use_kms))
        for s in sorok:
            self.emit('task_progress', {'task': task, 'log': s})

    def _winact_hint(self, state, kms_host):
        """Miért nem sikerült - a leggyakoribb okok, konkrét teendővel. Egy puszta
        hibakód a technikusnak semmit nem mond."""
        if kms_host:
            return ('Leggyakoribb ok: a KMS-kiszolgáló nem érhető el a hálózatról (tűzfal, '
                    'VPN, elgépelt név), vagy nem ad ki licencet erre a kiadásra.')
        if state.get('oem_key'):
            return ('Ezen a gépen VAN gyári kulcs a BIOS-ban, és azzal próbáltunk aktiválni. '
                    'Ha így sem megy, jellemzően nem a kulcshoz való kiadás van fenn '
                    '(pl. Home kulcs, de Pro telepítve), vagy nincs internet.')
        if 'retail' in (state.get('channel') or '').lower():
            return ('Retail licenc: ha a kulcs jó, de nem aktivál, jellemzően a gép '
                    'hardvere változott - a Beállítások > Aktiválás alatti hibaelhárító, '
                    'vagy a Microsoft-fiókhoz kötött digitális licenc a megoldás.')
        return ('Ellenőrizd az internetkapcsolatot és azt, hogy a kulcs a telepített '
                'kiadáshoz való (Home kulcs nem megy Pro-ra és fordítva).')
