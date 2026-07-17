"""A CLI backend (CliApi) összerakása a feature-mixinekből.

CLI verzió API - ugyanazokat a funkciókat hívja mint a GUI, de konzolra ír.
A szöveges menü (run_cli_mode) az app/cli/menu.py-ban van.

Az install_call_logging itt is minden metódust log-csomagolóba tesz, így a CLI
futások is teljes hívás-naplót írnak a debug logba.
"""
from app.common import install_call_logging
from app.cli.base import CliBaseMixin
from app.cli.drivers import CliDriversMixin
from app.cli.backup import CliBackupMixin
from app.cli.bcd import CliBcdMixin
from app.cli.wu import CliWuMixin
from app.cli.ghost import CliGhostMixin
from app.cli.tempclean import CliTempCleanMixin
from app.cli.blockscript import CliBlockScriptMixin
from app.cli.autofix import CliAutofixMixin
from app.cli.nicpack import CliNicPackMixin


class CliApi(CliBaseMixin, CliDriversMixin, CliBackupMixin, CliBcdMixin,
             CliWuMixin, CliGhostMixin, CliTempCleanMixin, CliBlockScriptMixin,
             CliAutofixMixin, CliNicPackMixin):
    """CLI verzió API - ugyanazokat a funkciókat hívja mint a GUI, de konzolra ír."""
    pass


install_call_logging(CliApi)
