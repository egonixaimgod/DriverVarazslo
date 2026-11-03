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
