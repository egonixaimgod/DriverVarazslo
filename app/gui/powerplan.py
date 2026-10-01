"""DriverVarázsló GUI - Teljesítmény mód: a gép energiasémája a ReviOS-értékekkel
maximumra húzott "Teljesítménycentrikus" sémára (a mag: app/powerplan_core.py).

KÉT HÍVÓ, EGY FÜGGVÉNY (2026-10-01, explicit user decision: *"ezt egy külön gombbal is
tudja a program ... ugyanazt a teljesítménycentrikus profilt állítsa be, mint amit az 1
katt fix végén"*): az 1 kattintásos fix a lánc legvégén, és az Operációs rendszer nézet
"Teljesítmény mód" gombja UGYANAZT a `_apply_performance_power_plan`-t hívja - a mag,
a kiírt szövegek és a visszaállítási tanács is közös. Ha a kettő külön kódot kapna, egy
későbbi módosítás után a gomb mást állítana be, mint a fix, és senki nem venné észre.
A függvény 2026-10-01-ig az `app/gui/autofix.py`-ban élt; azért költözött ide, mert
már nem csak a fix része."""

# === AUTO-IMPORTS ===
import logging
from app import powerplan_core
# === /AUTO-IMPORTS ===


class GuiPowerPlanMixin:
    """Teljesítmény mód (energiaséma). A DriverToolApi és a CliApi része (összerakás:
    app/gui/api.py, app/cli/api.py)."""

    def _apply_performance_power_plan(self, task_id='autofix'):
        """A gép teljesítmény-módba állítása + a teljes kiírás a `task_id` csatornára.

        Hívói: az 1 kattintásos fix a lánc legvégén (explicit user decision, 2026-09-01:
        a szervizből kiadott gép ne legyen lassú), és az Operációs rendszer nézet
        gombja (`apply_performance_mode`, 2026-10-01).

        A magja `app/powerplan_core.py`; itt csak a kiírás történik. A Fast Startup
        jegyzet mintáját követi: ez az ügyfél gépének TARTÓS, észrevehető változása,
        tehát nem elég megcsinálni - ki is kell MONDANI, a visszaállítás módjával
        együtt. Egy energiabeállítás, amiről az ügyfél nem tud, ugyanolyan
        megválaszolhatatlan bejelentés lesz, mint annak idején a színprofil-törlés.

        A fixben a hívás helye a lánc legvége, szándékosan: a lánc közben a
        `_disable_sleep_sync` tartja ébren a gépet, és a több újraindítás bármelyike
        felülírhatná a sémát.

        Visszatérés: a mag eredmény-dictje (`ok`, `kept`, `applied`, `failed`, ...) - a
        kézi gomb ebből írja meg a záró státuszt; a fix nem használja."""
        self.emit('task_progress', {'task': task_id, 'log': '\n⚡ Teljesítmény-mód beállítása (hogy a gép ne legyen lassú a szerviz után)...'})
        res = powerplan_core.apply_performance_plan(
            self._run, log=lambda m: self.emit('task_progress', {'task': task_id, 'log': m}))
        if not res.get('ok'):
            return res
        prev = res.get('previous_name') or res.get('previous_guid') or 'ismeretlen'
        if res.get('kept'):
            # A már teljesítményre hangolt séma marad (2026-09-28, lásd powerplan_core).
            self.emit('task_progress', {'task': task_id, 'log': f'⚡ Energiaséma: "{prev}" MARAD - ez már teljesítményre hangolt séma, a maximum-beállítások erre kerültek rá.'})
        else:
            self.emit('task_progress', {'task': task_id, 'log': f'⚡ Energiaséma: "{prev}" → TELJESÍTMÉNYCENTRIKUS (a ReviOS Ultra Performance beállításaival).'})
        if res.get('applied'):
            self.emit('task_progress', {'task': task_id, 'log': '   Maximumra állítva: ' + ', '.join(res['applied']) + '.'})
        # A következményt is kimondjuk. Egy laptop akkumulátoros üzemideje ezzel
        # ÉRZÉKELHETŐEN csökken, és a ventilátor is többet szólhat - ha ezt a technikus
        # nem tudja, a következő ügyfél-bejelentés erről fog szólni.
        self.emit('task_progress', {'task': task_id, 'log': '   Laptopnál ez akkumulátoron rövidebb üzemidőt és több ventilátorzajt jelent - cserébe a gép nem lassul vissza.'})
        self.emit('task_progress', {'task': task_id, 'log': '   Visszaállítás: Gépház > Rendszer > Energiaellátás, vagy rendszergazdaként: powercfg /setactive SCHEME_BALANCED'})
        if res.get('failed'):
            logging.info(f"[POWER] Teljesítmény-mód ({task_id}): ezen a gépen nem elérhető beállítások: "
                         f"{', '.join(res['failed'])}")
        return res

    def apply_performance_mode(self):
        """Az Operációs rendszer nézet "Teljesítmény mód" gombja: UGYANAZ a beállítás,
        mint az 1 kattintásos fix végén (lásd a modul fejlécét).

        Offline módban nem fut: a `powercfg` a FUTÓ rendszer sémáit állítja, egy másik
        lemezen lévő Windowsét nem - ott a "kész" üzenet hazugság lenne."""
        logging.info("[API] apply_performance_mode()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ A teljesítmény mód csak a futó rendszeren állítható be (offline módban nem).', 'type': 'error'})
            return

        def worker():
            self.emit('task_start', {'task': 'powerplan', 'title': '⚡ Teljesítmény mód'})
            res = self._apply_performance_power_plan('powerplan') or {}
            # A verdikt a mag VISSZAOLVASÁSA (`ok` csak akkor igaz, ha a séma tényleg
            # aktív lett) - nem a powercfg visszatérési kódja.
            if not res.get('ok'):
                status = '❌ A teljesítmény mód beállítása nem sikerült - a gép energiasémája NEM változott (részletek a naplóban).'
            elif res.get('failed'):
                status = f"✅ Teljesítmény mód beállítva ({len(res['failed'])} beállítás ezen a gépen nem elérhető)."
            else:
                status = '✅ Teljesítmény mód beállítva.'
            logging.info(f"[POWER] Kézi teljesítmény-mód: {status}")
            self.emit('task_complete', {'task': 'powerplan', 'status': status})
            self.emit('power_plan_status', self.get_power_plan_status())

        self._safe_thread('powerplan', worker)

    def get_power_plan_status(self):
        """Az aktív energiaséma a nézet állapotsorához: név + fajta. A fajtát a GUID-ból
        döntjük el, NEM a névből (a név lokalizált - lásd powerplan_core)."""
        logging.info("[API] get_power_plan_status()")
        if self.target_os_path:
            return {'name': '', 'kind': 'offline'}
        guid, name = powerplan_core.read_active_scheme(self._run)
        if not guid:
            return {'name': '', 'kind': 'unknown'}
        kind = {powerplan_core.HIGH_PERFORMANCE_GUID: 'performance',
                powerplan_core.ULTIMATE_PERFORMANCE_GUID: 'ultimate',
                powerplan_core.BALANCED_GUID: 'balanced',
                powerplan_core.POWER_SAVER_GUID: 'saver'}.get(guid, 'custom')
        return {'name': name or guid, 'guid': guid, 'kind': kind}
