"""A CLI backend (CliApi) összerakása - TELJES funkcionalitás, konzolos megjelenítéssel.

A CLI ugyanazokat a feature-mixineket kapja meg, mint a GUI (app/gui/*.py), és csak a
KIMENETET cseréli: a `CliBridgeMixin` az `emit`-et konzolra rajzolja, a `_safe_thread`-et
pedig szinkronná teszi. Így a CLI-ben nincs külön driver-kereső, AutoFix vagy katalógus-
motor, amit karban kellene tartani - lásd app/cli/bridge.py fejlécét.

AZ MRO SORRENDJE SZÁMÍT:
  1. `CliBridgeMixin` - ELÖL, mert az `emit`/`_safe_thread`/`select_file` felülírásainak
     nyerniük kell a GuiBaseMixin azonos nevű metódusaival szemben.
  2. `GuiBaseMixin` - a közös állapot (hw_updates_pool, _task_busy, argv-kapcsolók...),
     amire minden feature-mixin épít, és a parancsfuttató `_run` (a CLI is ezt használja;
     a külön CLI-s `_run` 2026-10-11-én törölve, mert már eltért a GUI-étól).
  3. A feature-mixinek (GUI + a CLI saját, szöveges menü-metódusai).

A `CliApi.__init__` a GuiBaseMixin.__init__-et futtatja: az állítja be azt az állapotot,
amit a feature-mixinek elvárnak, és az intézi az induláskori takarításokat is (elmaradt
energiagazdálkodás-visszaállítás, ottfelejtett WU-házirend, halasztott INF-kivezetés) -
ezekre a CLI-ben ugyanúgy szükség van, a lánc lábai ugyanis CLI-ben is futhatnak.

A pywebview import NEM új függőség: az `app/common.py` amúgy is behúzza, tehát a CLI eddig
is megkövetelte. Ami a régi gépeken elhasal, az a `webview.start()` (WebView2 + .NET), nem
az import - lásd CLAUDE.md "Régi Windows-támogatás".
"""
from app.common import install_call_logging

# --- CLI-specifikus réteg (a sorrend miatt elöl) ---
from app.cli.bridge import CliBridgeMixin

# --- A közös GUI-alap: állapot + argv-kapcsolók + cél-OS kezelés ---
from app.gui.base import GuiBaseMixin

# --- A GUI feature-mixinjei: ezeket kapja meg a CLI változtatás nélkül ---
from app.gui.drivers import GuiDriversMixin
from app.gui.dupdrivers import GuiDupDriversMixin
from app.gui.backup import GuiBackupMixin
from app.gui.bcd import GuiBcdMixin
from app.gui.ghost import GuiGhostMixin
from app.gui.bluetooth import GuiBluetoothMixin
from app.gui.tempclean import GuiTempCleanMixin
from app.gui.hwscan import GuiHwScanMixin
from app.gui.wu import GuiWuMixin
from app.gui.autofix import GuiAutofixMixin
from app.gui.rebind import GuiRebindMixin
from app.gui.bitlocker import GuiBitlockerMixin
from app.gui.report import GuiReportMixin
from app.gui.storeprint import GuiStorePrintMixin
from app.gui.blockscript import GuiBlockScriptMixin
from app.gui.display import GuiDisplayMixin
from app.gui.winact import GuiWinActMixin
from app.gui.logs import GuiLogsMixin
from app.gui.stress import GuiStressMixin
from app.gui.stress_automation import GuiStressAutomationMixin
from app.gui.toolsinstall import GuiToolsInstallMixin
from app.gui.benchmark import GuiBenchmarkMixin
from app.gui.updater import GuiUpdaterMixin
from app.gui.shoplabel import GuiShopLabelMixin
from app.gui.storeapps import GuiStoreAppsMixin
from app.gui.powerplan import GuiPowerPlanMixin
from app.gui.climode import GuiCliModeMixin

# --- A CLI saját metódusai ---
#
# CSAK AZ MARAD, AMI NEM DUPLIKÁTUM. A régi CLI feature-mixinek (app/cli/drivers.py,
# backup.py, wu.py, ghost.py, blockscript.py, autofix.py, bitlocker.py, report.py, bcd.py,
# dupdrivers.py, tempclean.py) ugyanazokat a metódusneveket vitték, mint a GUI párjaik, és
# a CliApi már nem hozta be őket: holt kód volt, ezért 2026-10-11-én TÖRÖLVE (a gitben
# megvannak). A menü közvetlenül a GUI-mixinek metódusait hívja, a megjelenítést a
# CliBridgeMixin adja hozzá. Ne éleszd újra őket.
from app.cli.updater import CliUpdaterMixin


class CliApi(CliBridgeMixin, GuiBaseMixin,
             # GUI feature-mixinek
             GuiDriversMixin, GuiDupDriversMixin, GuiBackupMixin, GuiBcdMixin,
             GuiGhostMixin, GuiBluetoothMixin, GuiTempCleanMixin, GuiHwScanMixin, GuiWuMixin,
             GuiAutofixMixin, GuiRebindMixin,
             GuiBitlockerMixin, GuiReportMixin, GuiStorePrintMixin,
             GuiBlockScriptMixin,
             GuiDisplayMixin, GuiWinActMixin, GuiLogsMixin,
             GuiStressMixin, GuiStressAutomationMixin, GuiToolsInstallMixin,
             GuiBenchmarkMixin, GuiUpdaterMixin, GuiShopLabelMixin,
             GuiStoreAppsMixin, GuiPowerPlanMixin,
             GuiCliModeMixin,   # vissza a grafikus módba (switch_to_gui_mode)
             # CLI-specifikus (nem duplikátum): a konzolos frissítés-ellenőrzés
             CliUpdaterMixin):
    """A CLI backend: ugyanaz a funkcionalitás, mint a GUI-é, konzolos kimenettel."""

    def __init__(self):
        # A menünek szánt adat-események tárolója - a GuiBaseMixin.__init__ ELŐTT kell
        # léteznie, mert az induláskori takarítások már emit-elhetnek.
        self._cli_events = {}
        self._cli_reboot_offer = False
        self._cli_task_title = ''
        self._cli_last_problems = []
        self._cli_last_scan_status = None
        GuiBaseMixin.__init__(self)


install_call_logging(CliApi)
