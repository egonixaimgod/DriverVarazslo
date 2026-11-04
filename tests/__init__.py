"""DriverVarázsló offline regressziós tesztkészlet.

Futtatás a repó gyökeréből (hálózat, rendszergazda és telepítés nélkül):

    python -m unittest discover -s tests -v

Minden teszt a TERepi esetekből épül (a CLAUDE.md mérési adataival): ha egy teszt
elbukik, egy már egyszer kijavított hiba jött vissza. Új javításhoz új teszt jár.
A `tests` mappa NEM build-bemenet (a PyInstaller csak a `driver_tool.py`-ból elérhető
importokat csomagolja).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# VÉDŐHÁLÓ (2026-11-03): teszt SOHA nem írhat a fejlesztői gép valódi WU-registryjébe.
# A szünet beírása/törlése 2026-11-03 óta mindenhol Pythonból (winreg) megy - egy csonkolt
# `run` már nem véd meg tőle, és a UX\Settings kulcs nem emelt jogon is írható (mérve).
# Aki ezt a két függvényt hívja, annak mockolnia kell (mock.patch.object felülírja ezt).
from app import wusettings_core as _wc  # noqa: E402


def _no_real_registry(*a, **k):
    raise RuntimeError(f"Teszt NEM írhat a valódi registrybe (wusettings_core._reg_write/_reg_delete): {a}")


_wc._reg_write = _no_real_registry
_wc._reg_delete = _no_real_registry

# VÉDŐHÁLÓ (2026-11-04): a kézi driver-törlés óta MENT is (backup_core), és ez a valódi
# `C:\DriverVarazslo`-ba írna, a DISM-et pedig nem a csonkolt `run`-on, hanem Popen-nel
# (`_stream_cmd`) indítja. Teszt egyiket sem teheti: a mentések gyökere egy eldobható
# mappa, a `_stream_cmd` pedig hibát dob (aki kell, mock.patch.object-tel felülírja).
import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from app import backup_core as _bc  # noqa: E402

from app import wu_core as _wu  # noqa: E402

TEST_APP_DATA = tempfile.mkdtemp(prefix='dv_test_appdata_')
atexit.register(shutil.rmtree, TEST_APP_DATA, True)
_bc._app_data_dir = lambda: TEST_APP_DATA
# A wu_core is: a 2026-11-03-i Wi-Fi-teszt a valódi `export_wlan_profiles`-t futtatta, ami a
# VALÓDI `C:\DriverVarazslo\wlan_backup`-ot törölte és hozta létre (2026-11-04-én vettük észre).
_wu._app_data_dir = lambda: TEST_APP_DATA


def _no_real_process(cmd, *a, **k):
    raise RuntimeError(f"Teszt NEM indíthat valódi folyamatot (backup_core._stream_cmd): {cmd}")


REAL_STREAM_CMD = _bc._stream_cmd     # a saját tesztje (csonkolt Popen-nel) ezt hívja
_bc._stream_cmd = _no_real_process
