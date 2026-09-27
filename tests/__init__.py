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
