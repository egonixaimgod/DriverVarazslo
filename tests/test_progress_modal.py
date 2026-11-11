"""A folyamat-ablak fejléce (2026-11-11, felhasználói bejelentés: "lépés: 12582912 / 32272456"
egy letöltésnél, duplán kiírt sorok, beragadt 100%, az 1 kattintásos fixnél régi szöveg).

A `progress_modal_harness.js` a ui.html VALÓDI függvényeit hajtja meg DOM-csonkkal (node
kell hozzá; ha nincs a gépen, a teszt kimarad)."""

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


class ProgressModalTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'nincs node a gépen')
    def test_fejlec_forgatokonyvek(self):
        r = subprocess.run(['node', os.path.join(ROOT, 'tests', 'progress_modal_harness.js'),
                            os.path.join(ROOT, 'ui.html')],
                           capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('MIND RENDBEN', r.stdout)

    def test_a_kuldok_megmondjak_a_mertekegyseget(self):
        """Ami nem darab, az `unit`-ot küld - különben a felület "lépés"-ként írja ki."""
        self.assertIn("'unit': 'bytes'", _src('app/gui/stress.py'))
        self.assertIn("'unit': 'percent'", _src('app/gui/backup.py'))
        ram = _src('app/gui/ramtest.py')
        self.assertIn("'unit': 'seconds'", ram)
        self.assertIn("'unit': 'percent'", ram)

    def test_egy_letolto_folyamatjelzo_van(self):
        """A szervízprogram-telepítés a közös `_task_dl_progress`-t használja (a saját
        másolata kimaradt volna a mértékegység-javításból)."""
        src = _src('app/gui/toolsinstall.py')
        self.assertIn("self._task_dl_progress('toolsinstall')", src)
        self.assertNotIn("def dl_progress(", src)

    def test_a_naplo_letoltes_szamlalot_kuld(self):
        """A `progress` mezőt a felület soha nem olvasta: a naplóletöltésnek nem volt százaléka."""
        self.assertNotIn("'progress':", _src('app/gui/logs.py'))


if __name__ == '__main__':
    unittest.main()
