"""A GUI backend (DriverToolApi) összerakása a feature-mixinekből.

Egy feature = egy fájl (nagyjából a bal oldali menüpontok szerint). A mixinek
metódusnevei nem ütköznek, mindegyik ugyanazon a self-en dolgozik - a viselkedés
azonos a korábbi, egyfájlos osztályéval. A pywebview js_api-ként EZT az osztályt
kapja meg, a frontend (ui.html) számára semmi nem változott.

Az install_call_logging minden metódust log-csomagolóba tesz (lásd app/common.py):
minden hívás paraméterei, futásideje és eredménye/kivétele a debug logba kerül.
"""
from app.common import install_call_logging
from app.gui.base import GuiBaseMixin
from app.gui.updater import GuiUpdaterMixin
from app.gui.stress import GuiStressMixin
from app.gui.stress_automation import GuiStressAutomationMixin
from app.gui.toolsinstall import GuiToolsInstallMixin
from app.gui.drivers import GuiDriversMixin
from app.gui.dupdrivers import GuiDupDriversMixin
from app.gui.bcd import GuiBcdMixin
from app.gui.ghost import GuiGhostMixin
from app.gui.tempclean import GuiTempCleanMixin
from app.gui.hwscan import GuiHwScanMixin
from app.gui.wu import GuiWuMixin
from app.gui.autofix import GuiAutofixMixin
from app.gui.backup import GuiBackupMixin
from app.gui.bitlocker import GuiBitlockerMixin
from app.gui.report import GuiReportMixin
from app.gui.storeprint import GuiStorePrintMixin
from app.gui.blockscript import GuiBlockScriptMixin
from app.gui.nvidia import GuiNvidiaMixin
from app.gui.vendorgpu import GuiVendorGpuMixin
from app.gui.oemdrivers import GuiOemDriversMixin
from app.gui.nicpack import GuiNicPackMixin
from app.gui.benchmark import GuiBenchmarkMixin


class DriverToolApi(GuiBaseMixin, GuiUpdaterMixin, GuiStressMixin,
                    GuiStressAutomationMixin, GuiToolsInstallMixin,
                    GuiDriversMixin, GuiDupDriversMixin,
                    GuiBcdMixin, GuiGhostMixin, GuiTempCleanMixin, GuiHwScanMixin,
                    GuiWuMixin, GuiAutofixMixin, GuiBackupMixin, GuiBitlockerMixin,
                    GuiReportMixin, GuiStorePrintMixin, GuiBlockScriptMixin,
                    GuiNvidiaMixin, GuiVendorGpuMixin, GuiOemDriversMixin,
                    GuiNicPackMixin, GuiBenchmarkMixin):
    """A GUI backend - a pywebview js_api-ja. Minden feature a saját mixin-fájljában."""
    pass


install_call_logging(DriverToolApi)
