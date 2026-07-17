"""DriverVarázsló GUI - Bolti nyomtató: a Rendszer Riport 1 kattintásos nyomtatása a Microstore hálózati nyomtatójára."""

# === AUTO-IMPORTS ===
import os
import re
import time
import logging
import socket
from app.common import _ps_quote
# === /AUTO-IMPORTS ===


# Microstore bolti hálózati nyomtató - "1 kattintás" nyomtatás a Rendszer Riporthoz
# (print_via_store_printer). SUMATRA_PDF_FILENAMES a stresstools.zip-ben keresett néma
# (dialógus nélküli) PDF-nyomtató segédprogram, HP_DRIVER_INF_FILENAMES a szintén a
# ZIP-be csomagolt HP LaserJet 1320 PCL6 driver (pnputil /export-driver-rel exporttal
# kinyerve egy már működő gépről) INF fájlja - mindkettő ugyanabból a ZIP-ből, mint a
# stabilitás-teszt eszközök, hogy ne kelljen külön letöltési URL egy-egy apró fájlért.
STORE_PRINTER_IP = "192.168.35.12"
STORE_PRINTER_PORT_NAME = "IP_192.168.35.12"
STORE_PRINTER_NAME = "Microstore Bolti Nyomtató"
# STORE_PRINTER_REFERENCE_NAME csak ott segít, ahol ÉPPEN ez a nyomtató már fel van véve
# - lásd _resolve_store_printer_driver. Terepen bizonyítva (egy random gépen tesztelve):
# sem ez a nyomtató, sem a hozzá tartozó driver NEM garantált egyetlen más gépen sem, és
# az `Add-PrinterDriver -Name` MAGÁBAN NEM tölt le semmit a Windows Update-ről (az
# interaktív "Nyomtató hozzáadása" varázsló automatikus driver-felismerése egy MÁSIK,
# PowerShell-ből el nem érhető mechanizmust használ) - csak akkor sikerül, ha a driver
# MÁR a driver store-ban van. Emiatt a becsomagolt INF-et `pnputil /add-driver`-rel kell
# előbb odastageelni, utána sikerül csak az Add-PrinterDriver/Add-Printer.
STORE_PRINTER_REFERENCE_NAME = "BOLT hp LaserJet 1320 PCL 6"
STORE_PRINTER_HP_DRIVER_NAME = "hp LaserJet 1320 PCL 6"
SUMATRA_PDF_FILENAMES = ['sumatrapdf.exe']
HP_DRIVER_INF_FILENAMES = ['hpc1320u.inf']


class GuiStorePrintMixin:
    """Bolti nyomtató: a Rendszer Riport 1 kattintásos nyomtatása a Microstore hálózati nyomtatójára. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _cleanup_store_printer(self, printer_name, staged_driver_published_name):
        """Eltávolítja a bolti nyomtatót (és ha mi stageeltük, a drivert is) erről a
        gépről, miután a nyomtatás megtörtént - ez a program tipikusan idegen (ügyfél-)
        gépeken fut szervizelés közben, nem szabad rajta hagyni a bolti nyomtatót/drivert.
        Csak print_via_store_printer hívja, és csak akkor, ha MI adtuk hozzá ezt a
        nyomtatót ebben a futásban (we_added_printer) - a bolt saját, állandó gépén már
        eleve meglévő nyomtatóhoz ez sosem nyúl. Előbb megvárja, amíg a nyomtatási sor
        kiürül (a SumatraPDF `-exit-on-print`-je csak a nyomtatás API-hívás visszatéréséig
        vár, nem addig, amíg a spooler ténylegesen elküldi a bájtokat a hálózati
        nyomtatónak - ha a nyomtatót a job ténylegesen elküldése előtt távolítanánk el, a
        nyomtatás félbeszakadhatna)."""
        self.emit('task_progress', {'task': 'store_print', 'log': '🧹 Nyomtatási sor ürülésére várakozás...'})
        wait_ps = (
            f"$deadline = (Get-Date).AddSeconds(60); "
            f"while ((Get-Date) -lt $deadline) {{ "
            f"$jobs = Get-PrintJob -PrinterName '{_ps_quote(printer_name)}' -ErrorAction SilentlyContinue; "
            f"if (-not $jobs) {{ break }}; Start-Sleep -Milliseconds 500 }}"
        )
        self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', wait_ps], timeout=70)

        self.emit('task_progress', {'task': 'store_print', 'log': '🧹 Bolti nyomtató eltávolítása erről a gépről...'})
        remove_ps = (
            f"Remove-Printer -Name '{_ps_quote(printer_name)}' -ErrorAction SilentlyContinue; "
            f"Remove-PrinterPort -Name '{_ps_quote(STORE_PRINTER_PORT_NAME)}' -ErrorAction SilentlyContinue"
        )
        if staged_driver_published_name:
            remove_ps += f"; Remove-PrinterDriver -Name '{_ps_quote(STORE_PRINTER_HP_DRIVER_NAME)}' -ErrorAction SilentlyContinue"
        self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', remove_ps], timeout=60)

        # A Remove-PrinterDriver csak a nyomtatási alrendszerből veszi ki a drivert - az
        # INF-csomag maga még ott marad a driver store-ban, amíg pnputil /delete-driver
        # ki nem törli onnan is. Csak akkor tesszük, ha MI stageeltük (staged_driver_
        # published_name) - egy máshonnan (pl. Windows Update-ről korábban) már megvolt
        # driver csomagját nem töröljük, azt nem mi hoztuk létre.
        if staged_driver_published_name:
            self._run(['pnputil', '/delete-driver', staged_driver_published_name, '/uninstall', '/force'], timeout=60)

        self.emit('task_progress', {'task': 'store_print', 'log': '✅ Bolti nyomtató eltávolítva erről a gépről.'})

    def _find_hp_driver_inf(self, stress_dir):
        """Megkeresi a becsomagolt HP LaserJet 1320 driver INF fájlját (HPDriver mappa a
        stresstools.zip-ben, egy már működő gépről `pnputil /export-driver` exporttal
        kinyerve) - _resolve_store_printer_driver ezzel stageeli a drivert a driver
        store-ba `pnputil /add-driver`-rel, mielőtt Add-PrinterDriver hivatkozna rá."""
        for root, dirs, files in os.walk(stress_dir):
            for file in files:
                if file.lower() in HP_DRIVER_INF_FILENAMES:
                    return os.path.join(root, file)
        return None

    def _resolve_store_printer_driver(self):
        """Eldönti, melyik drivert használja a bolti nyomtató felvételéhez, ha az még nincs
        felvéve. Terepen bizonyított tapasztalat (két különböző random gépen tesztelve):
        NEM garantált, hogy a HP LaserJet 1320 drivere - vagy akár maga a referenciaként
        vett nyomtató - jelen van bármelyik gépen, ahol ez a funkció fut, ÉS az
        `Add-PrinterDriver -Name` ÖNMAGÁBAN NEM tölt le semmit a Windows Update-ről (ezt
        elsőre feltételeztük, de éles hiba - "The specified driver does not exist in the
        driver store" - bizonyította a tévedést: az interaktív "Nyomtató hozzáadása"
        varázsló automatikus driver-felismerése egy MÁS, PowerShell-ből el nem érhető
        mechanizmust használ). Emiatt egyre általánosabb, egyre kevésbé kényelmes (de még
        mindig működő) lehetőségeket próbálunk sorban:
          1) ha VÉLETLENÜL már fel van véve egy nyomtató ezzel a referencia névvel ezen a
             gépen, az ő drivere (legjobb eset - pontosan ez a modell, semmi extra munka)
          2) a stresstools.zip-be csomagolt HP driver (HPDriver mappa) `pnputil
             /add-driver ... /install`-lal a driver store-ba stageelve, majd
             Add-PrinterDriver-rel regisztrálva - ez internet nélkül, BÁRMELYIK gépen
             működik, mert nem külső forrásra (Windows Update), hanem a saját
             becsomagolt fájljainkra támaszkodik
        Ha egyik sem sikerül (a ZIP-ben sincs meg az INF, vagy a pnputil stageelés
        elhasal), Exception-t dob - ez esetben a nyomtatót egyszer manuálisan, kézzel kell
        hozzáadni ezen a gépen.

        Visszatérési érték: (driver_name, staged_published_name). staged_published_name
        None, ha a driver már eleve megvolt (1. eset) - ilyenkor a hívó (print_via_
        store_printer) NEM törölheti a drivert nyomtatás után, hiszen az nem általunk lett
        stageelve, más (pl. a referencia nyomtató) is használhatja. Ha viszont mi
        stageeltük most (2. eset), a "oemXX.inf" publikált nevet adjuk vissza, hogy a hívó
        ezzel pontosan visszatudja vonni (`pnputil /delete-driver`) - lásd a "ne maradjon
        rajta az ügyfél gépén a mi driverünk" elvárást a print_via_store_printer végén."""
        ref_ps = f"(Get-Printer -Name '{_ps_quote(STORE_PRINTER_REFERENCE_NAME)}' -ErrorAction Stop).DriverName"
        res = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ref_ps], encoding='utf-8')
        driver_name = (res.stdout or '').strip() if res else ''
        if driver_name:
            self.emit('task_progress', {'task': 'store_print', 'log': f'✅ Meglévő HP driver újrahasznosítva: {driver_name}'})
            return driver_name, None

        self.emit('task_progress', {'task': 'store_print', 'log': '📦 HP driver keresése a becsomagolt fájlok között...'})
        stress_dir = self._download_stresstools()
        inf_path = self._find_hp_driver_inf(stress_dir) if stress_dir else None
        if not inf_path:
            raise Exception(
                f"Nincs meg a becsomagolt HP LaserJet 1320 driver (HPDriver mappa) a "
                f"stresstools.zip-ben, és ezen a gépen sincs máshonnan telepítve. Egyszer, "
                f"ezen a gépen, kézzel kell hozzáadni a nyomtatót (Nyomtatók és szkennerek "
                f"-> Nyomtató hozzáadása -> IP-cím: {STORE_PRINTER_IP}) - utána a program "
                f"már felismeri és újra tudja használni."
            )

        self.emit('task_progress', {'task': 'store_print', 'log': '⬇️ HP driver telepítése a driver store-ba (pnputil)...'})
        stage_res = self._run(['pnputil', '/add-driver', inf_path, '/install'], timeout=120)
        # A pnputil kilépési kódja NEM megbízható sikerjelzés: élesben tesztelve, ha a
        # driver már staged, "Driver package added successfully. (Already exists in the
        # system)" szöveggel tér vissza, miközben a kilépési kódja 5 (nem 0!) - a szöveges
        # kimenetet kell nézni, nem a returncode-ot.
        stage_out = (stage_res.stdout or '') if stage_res else ''
        if not stage_res or 'successfully' not in stage_out.lower():
            err_detail = (stage_res.stderr or stage_res.stdout or 'ismeretlen hiba') if stage_res else 'ismeretlen hiba'
            raise Exception(f"A HP driver telepítése (pnputil /add-driver) sikertelen: {err_detail}")

        # A "Published Name:  oemXX.inf" sor kell ahhoz, hogy a hívó nyomtatás után
        # pontosan EZT a stageelt csomagot tudja visszavonni (pnputil /delete-driver) -
        # az oemXX szám gépenként/futásonként más lehet, nem lehet előre feltételezni.
        published_match = re.search(r'Published Name\s*:\s*(oem\d+\.inf)', stage_out, re.IGNORECASE)
        staged_published_name = published_match.group(1) if published_match else None

        install_ps = f"Add-PrinterDriver -Name '{_ps_quote(STORE_PRINTER_HP_DRIVER_NAME)}' -ErrorAction Stop"
        ires = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', install_ps], encoding='utf-8', timeout=60)
        if ires and ires.returncode == 0:
            self.emit('task_progress', {'task': 'store_print', 'log': f'✅ HP driver telepítve: {STORE_PRINTER_HP_DRIVER_NAME}'})
            return STORE_PRINTER_HP_DRIVER_NAME, staged_published_name

        err_detail = (ires.stderr or ires.stdout or 'ismeretlen hiba') if ires else 'ismeretlen hiba'
        raise Exception(
            f"A HP driver a pnputil stageelés után sem regisztrálható nyomtató-driverként: "
            f"{err_detail}\nEgyszer, ezen a gépen, kézzel kell hozzáadni a nyomtatót "
            f"(Nyomtatók és szkennerek -> Nyomtató hozzáadása -> IP-cím: {STORE_PRINTER_IP}) "
            f"- utána a program már felismeri és újra tudja használni."
        )

    def _find_msedge_exe(self):
        """Megkeresi a telepített Edge böngészőt (msedge.exe) - a riport HTML->PDF
        alakításához kell. FONTOS: ez NEM ugyanaz, mint a WebView2 Runtime, amit az app
        amúgy is megkövetel (check_webview2_runtime) - az egy beágyazható futtatókörnyezet,
        önálló msedge.exe nélkül is jelen lehet, ezért ezt külön, a szokásos telepítési
        útvonalakon keressük."""
        candidates = [
            os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return None

    def _find_sumatra_exe(self, stress_dir):
        """Megkeresi a SumatraPDF.exe-t a stresstools.zip kicsomagolt mappájában - a néma
        (dialógus nélküli) PDF-nyomtatáshoz kell (print_via_store_printer). Ugyanabba a
        ZIP-be kerül, mint a stabilitás-teszt eszközök, hogy ne kelljen külön letöltési
        logika/URL egy apró segédprogramért."""
        for root, dirs, files in os.walk(stress_dir):
            for file in files:
                if file.lower() in SUMATRA_PDF_FILENAMES:
                    return os.path.join(root, file)
        return None

    def print_via_store_printer(self):
        """A legutóbb generált Rendszer Riport kinyomtatása a Microstore bolti hálózati
        nyomtatójára, egyetlen kattintással. Ha a nyomtató még nincs felvéve a Windows
        nyomtatói közé, felveszi - a driver kiválasztását lásd _resolve_store_printer_driver
        (terepen bizonyítva: NEM garantált, hogy a HP LaserJet 1320 drivere - vagy akár
        maga a referencia nyomtató - jelen van azon a gépen, ahol ez fut, ezért ott több,
        egyre általánosabb lehetőséget próbálunk sorban, nem csak egyet). A nyomtatás maga
        headless Edge-dzsel PDF-be alakítja a riportot (ugyanaz a motor, ami a report
        egy-oldalas zoom-alapú tördelését is renderelte - a nyomtatott PDF pontosan azt
        adja, amit böngészőben látni), majd a SumatraPDF-fel (stresstools.zip) néma
        nyomtatással a nyomtatóra küldi."""
        logging.info("[API] print_via_store_printer()")
        report_path = self._last_report_path
        if not report_path or not os.path.exists(report_path):
            self.emit('toast', {'message': '⚠️ Nincs elérhető generált riport - előbb generáld le a Rendszer Riportot!', 'type': 'warning'})
            return

        def worker():
            self.emit('task_start', {'task': 'store_print', 'title': 'Nyomtatás a Bolti Nyomtatóra'})
            self.emit('task_progress', {'task': 'store_print', 'log': f'📡 Nyomtató keresése a hálózaton ({STORE_PRINTER_IP})...', 'indeterminate': True})

            # 1) Elérhető-e egyáltalán a nyomtató a hálózaton? A nyers nyomtatási (JetDirect,
            # 9100/tcp) porthoz csatlakozunk - ez megbízhatóbb jel, mint egy ICMP ping, mert
            # sok nyomtató blokkolja/nem válaszol pingre, de a nyomtatási portot figyeli.
            reachable = False
            try:
                with socket.create_connection((STORE_PRINTER_IP, 9100), timeout=3):
                    reachable = True
            except OSError:
                reachable = False
            if not reachable:
                raise Exception(f"A bolti nyomtató ({STORE_PRINTER_IP}) nem érhető el a hálózaton - lehet, hogy nem a bolt hálózatán vagy, vagy a nyomtató ki van kapcsolva.")

            # 2) Már fel van-e véve Windows nyomtatóként ehhez az IP-hez?
            find_ps = (
                f"$port = Get-PrinterPort | Where-Object {{ $_.PrinterHostAddress -eq '{STORE_PRINTER_IP}' }} | Select-Object -First 1; "
                "if ($port) { $p = Get-Printer | Where-Object { $_.PortName -eq $port.Name } | Select-Object -First 1; if ($p) { Write-Output $p.Name } }"
            )
            res = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', find_ps], encoding='utf-8')
            existing_name = (res.stdout or '').strip() if res else ''

            # we_added_printer/staged_driver_published_name: mit hoztunk létre MI EBBEN a
            # futásban, hogy a végén pontosan azt (és csakis azt) takarítsuk el - lásd a
            # "ne maradjon rajta az ügyfél gépén a bolti nyomtató/driver" elvárást lentebb.
            # Ha a nyomtató már eleve ott volt (pl. a bolt saját, állandó gépén), ahhoz
            # NEM nyúlunk hozzá utólag sem.
            we_added_printer = False
            staged_driver_published_name = None
            printer_name = existing_name or None

            # A takarítást (5. lépés) `finally`-ben végezzük, NEM csak a sikeres út végén -
            # terepen bizonyítva: ha a nyomtató/driver hozzáadása után VALAMI MÁS lépés
            # (pl. a PDF-generálás) hasal el, az a régi kód mellett félig hozzáadott
            # állapotban hagyta a gépet (nyomtató+port megvan, de a program hibával leáll)
            # - ez nemcsak az "ügyfél gépén ne maradjon nyoma" elvárást sérti, hanem egy
            # következő próbálkozást is elront (lásd: "Add-PrinterPort: The specified port
            # already exists" - a leftover port miatt). A `finally` biztosítja, hogy amit MI
            # adtunk hozzá, az sikeres ÉS sikertelen kilépéskor is eltakarodjon.
            try:
                if existing_name:
                    self.emit('task_progress', {'task': 'store_print', 'log': f'✅ A nyomtató már fel van véve: {printer_name}'})
                else:
                    self.emit('task_progress', {'task': 'store_print', 'log': '➕ A nyomtató még nincs felvéve - driver előkészítése...'})
                    driver_name, staged_driver_published_name = self._resolve_store_printer_driver()

                    # A porthoz ÉS a nyomtató nevéhez is külön, idempotens ellenőrzés kell:
                    # terepen bizonyítva, hogy a port és a nyomtató objektum egymástól
                    # függetlenül is szinkronon kívülre kerülhet (egy korábbi, félbeszakadt
                    # próbálkozásból a port megmaradt, miközben a nyomtató objektum már nem
                    # volt megtalálható a fenti existing_name lekérdezéssel) - egy sima,
                    # feltétel nélküli `Add-PrinterPort`/`Add-Printer -ErrorAction Stop`
                    # ilyenkor rögtön elhasalna ("already exists"), mielőtt egyáltalán
                    # esélyt kapna az egyébként ártalmatlan újrahasznosításra.
                    add_ps = (
                        f"if (-not (Get-PrinterPort -Name '{_ps_quote(STORE_PRINTER_PORT_NAME)}' -ErrorAction SilentlyContinue)) "
                        f"{{ Add-PrinterPort -Name '{_ps_quote(STORE_PRINTER_PORT_NAME)}' -PrinterHostAddress '{STORE_PRINTER_IP}' -ErrorAction Stop }}; "
                        f"if (-not (Get-Printer -Name '{_ps_quote(STORE_PRINTER_NAME)}' -ErrorAction SilentlyContinue)) "
                        f"{{ Add-Printer -Name '{_ps_quote(STORE_PRINTER_NAME)}' -DriverName '{_ps_quote(driver_name)}' -PortName '{_ps_quote(STORE_PRINTER_PORT_NAME)}' -ErrorAction Stop }}"
                    )
                    ares = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', add_ps], encoding='utf-8')
                    if not ares or ares.returncode != 0:
                        err_detail = (ares.stderr or ares.stdout or 'ismeretlen hiba') if ares else 'ismeretlen hiba'
                        raise Exception(f"Nem sikerült felvenni a nyomtatót: {err_detail}")
                    printer_name = STORE_PRINTER_NAME
                    we_added_printer = True
                    self.emit('task_progress', {'task': 'store_print', 'log': f'✅ Nyomtató felvéve: {printer_name}'})

                # 3) HTML -> PDF headless Edge-dzsel.
                self.emit('task_progress', {'task': 'store_print', 'log': '🖨️ PDF előállítása a riportból...'})
                msedge = self._find_msedge_exe()
                if not msedge:
                    raise Exception("Nem található az Edge böngésző (msedge.exe) ezen a gépen - a PDF-generáláshoz szükséges.")

                pdf_path = os.path.splitext(report_path)[0] + '_print.pdf'
                file_url = 'file:///' + report_path.replace('\\', '/')
                pdf_cmd = [
                    msedge, '--headless', '--disable-gpu', '--no-sandbox',
                    f'--print-to-pdf={pdf_path}', '--no-pdf-header-footer',
                    '--run-all-compositor-stages-before-draw', '--virtual-time-budget=3000',
                    file_url,
                ]
                self._run(pdf_cmd, timeout=60)
                # A msedge --print-to-pdf hívás visszatérése nem mindig jelenti azt, hogy a
                # PDF fájl írása is befejeződött (terepen bizonyítva: a subprocess ~0.6mp
                # alatt visszatért, miközben a PDF ténylegesen csak egy kicsit később jelent
                # meg a lemezen - valószínűleg egy háttérben tovább futó gyerekfolyamat
                # fejezte csak be az írást) - ezért rövid ideig újrapróbálkozunk ahelyett,
                # hogy egyetlen azonnali ellenőrzés után hibát adnánk.
                for _ in range(20):
                    if os.path.exists(pdf_path):
                        break
                    time.sleep(0.5)
                else:
                    raise Exception("A riport PDF-be alakítása sikertelen.")

                # 4) PDF -> néma nyomtatás a bolti nyomtatóra.
                self.emit('task_progress', {'task': 'store_print', 'log': '📤 Nyomtatás küldése...'})
                stress_dir = self._download_stresstools()
                sumatra = self._find_sumatra_exe(stress_dir) if stress_dir else None
                if not sumatra:
                    raise Exception("A SumatraPDF nem található (a stresstools.zip-ben kell lennie) - néma nyomtatás nem lehetséges.")

                self._run([sumatra, '-print-to', printer_name, '-silent', '-exit-on-print', pdf_path], timeout=60)
                try: os.remove(pdf_path)
                except: pass

                self.emit('task_progress', {'task': 'store_print', 'log': f'✅ Kinyomtatva: {printer_name}'})
                self.emit('task_complete', {'task': 'store_print', 'status': f'✅ Riport kinyomtatva a bolti nyomtatóra ({printer_name})!'})
            finally:
                # 5) Takarítás: az ügyfél gépén ne maradjon rajta a mi bolti nyomtatónk/
                # driverünk - csak azt távolítjuk el, amit MI adtunk hozzá ebben a futásban
                # (we_added_printer/staged_driver_published_name), a bolt saját, állandó
                # gépén már eleve ott lévő nyomtatóhoz/driverhez nem nyúlunk. Egy esetleges
                # takarítási hiba nem írja felül/nyeli el a fenti try-ban ténylegesen
                # történteket (sikert vagy hibát) - csak figyelmeztetésként logolva.
                if we_added_printer:
                    try:
                        self._cleanup_store_printer(printer_name, staged_driver_published_name)
                    except Exception as cleanup_err:
                        logging.warning(f"[STORE_PRINT] Takarítási hiba: {cleanup_err}")
                        self.emit('task_progress', {'task': 'store_print', 'log': f'⚠️ A bolti nyomtató eltávolítása ezen a gépen nem sikerült: {cleanup_err}'})

        self._safe_thread('store_print', worker)
