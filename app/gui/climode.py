"""DriverVarázsló GUI - átváltás CLI módba (a felső sáv kapcsolója).

MIÉRT (explicit user decision, 2026-08-29): a bolt régi gépein a grafikus felület
használhatatlanul lassú, a CLI viszont mindenhol elindul. A technikusnak ezért kell egy
kapcsoló, amivel a program ÚJRAINDUL szöveges módban - ne kelljen parancssorból,
`--cli` kapcsolóval kézzel indítania.

MIÉRT ÚJ FOLYAMAT ÉS NEM "átrajzolás": a CLI-nek saját konzolablak kell (a windowed exe-nek
alapból nincs `sys.stdout`-ja, lásd common.ensure_console), a futó WebView2-ablakot pedig
nem lehet konzollá alakítani. Egy új példány indítása `--cli`-vel az egyetlen tiszta út,
és egyben azt is garantálja, hogy a nehéz WebView2 réteg TÉNYLEG kikerül a képből - a
mostani folyamat kilép.
"""

# === AUTO-IMPORTS ===
import os
import logging
import subprocess

from app.common import relaunch_detached
# === /AUTO-IMPORTS ===


class GuiCliModeMixin:
    """A felső sáv "CLI mód" kapcsolója. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def switch_to_cli_mode(self):
        """Újraindítja a programot szöveges (CLI) módban, saját konzolablakban.

        A futó grafikus példány ezután kilép - két példány egyszerre amúgy sem futhat (a
        program egypéldányos mutexet használ), és a felhasználó szándéka egyértelmű."""
        logging.info("[API] switch_to_cli_mode() - átváltás CLI módra")
        # AZ INDÍTÁS A KÖZÖS `common.relaunch_detached`-BEN VAN (2026-09-28). A három csapda
        # (egypéldányos mutex, öröklött `_PYI_*` környezet -> közös `_MEI` mappa, `cmd /c
        # start` + DETACHED_PROCESS a `taskkill /T` és a rejtett konzol ellen) itt derült ki
        # 2026-08-29/31-én, a részletes indoklás és a mérések ott és a CLAUDE.md "A CLI mód
        # teljes értékű felület" szekciójában. Kiemelve azért, mert a WebView2-telepítés
        # utáni újraindítás egy csupasz `os.execv`-vel ugyanebbe a `_MEI`-csapdába futott
        # (Build 344) - két példány előbb-utóbb eltér, ez a projekt legrégebbi hibája.
        if not relaunch_detached(['--cli'], 'CLI módra váltás'):
            self.emit('toast', {'message': '❌ A CLI mód indítása nem sikerült - a részletek '
                                           'a naplóban.', 'type': 'error'})
            return {'success': False, 'error': 'relaunch failed'}

        # A jelenlegi (grafikus) példány kilép. A kilépés a szokásos úton megy: a
        # cleanup_zombies + os._exit párost a belépési pont végzi, ezért itt csak
        # jelezzük a felületnek, hogy záródik - a tényleges kilépést a JS kéri.
        self.emit('toast', {'message': '🖥️ CLI mód indul egy új ablakban...', 'type': 'success'})
        return {'success': True}

    def exit_app(self):
        """A program azonnali lezárása (a CLI-re váltás után hívja a felület).

        `os._exit(0)`, mert a WebView2 szál és a daemon-szálak nem mindig bomlanak le
        rendesen (lásd CLAUDE.md "Process model").

        SZÁNDÉKOSAN NINCS `taskkill /T` (folyamatfa-kilövés), amit a belépési pont
        `cleanup_zombies()`-a használ: itt épp azért lépünk ki, hogy egy MÁSIK folyamat
        (a CLI ablak) átvegye a munkát, és a fa-kilövésnek itt nincs mit nyernie.

        VISSZAVONVA (2026-08-31): egy korábbi változat `/T`-t tett ide azzal az
        indoklással, hogy az "a bootloader szülőt is elviszi, így nem jut el a
        `_MEIxxxxx` takarításáig". Ez KÉTSZERESEN téves volt: a `taskkill /T` a megadott
        PID LESZÁRMAZOTTAIT lövi ki, nem az ŐSEIT, tehát a bootloader szülőt eleve nem is
        érintette - és a "Failed to remove temporary directory" ablaknak nem is ez volt az
        oka. A valódi ok (mérve, lásd `switch_to_cli_mode`): az új CLI példány örökölte a
        `_PYI_*` környezeti változókat, ezért a MI temp mappánkban futott, és a
        bootloaderünk azt nem tudta törölni. Ott van javítva, ahol keletkezett."""
        logging.info("[API] exit_app() - kilépés (CLI módra váltás után)")
        try:
            logging.shutdown()
        except Exception:
            pass
        try:
            subprocess.run(['taskkill', '/F', '/PID', str(os.getpid())],
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        except Exception as e:
            logging.debug(f"[CLI-MODE] taskkill nem futott le: {e}")
        os._exit(0)
