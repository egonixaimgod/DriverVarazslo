"""Az 1 kattintásos fix "Szoftveres driverek megtartása" kapcsolója (2026-11-13, explicit user
decision: *„az 1 katt fixnél be lehessen pipálni azt, hogy pl a szoftverek driverét ne
törölje”*). A felület a valódi ui.html függvényein fut (node), a lánc a kivételt
`reason`-nel együtt viszi a lábak között."""
import os
import shutil
import subprocess
import unittest

import tests  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class KeepSoftwareUiTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'nincs node a gépen')
    def test_felulet_forgatokonyvek(self):
        r = subprocess.run(['node', os.path.join(ROOT, 'tests', 'keep_software_harness.js'),
                            os.path.join(ROOT, 'ui.html')],
                           capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('MIND RENDBEN', r.stdout)

    def test_a_kapcsolo_minden_helyen_ott_van(self):
        html = open(os.path.join(ROOT, 'ui.html'), encoding='utf-8').read()
        self.assertIn('id="mc-keep-software"', html)
        # elemzés közben tiltva, váltáskor újrarajzol, nyitáskor alapból KI
        self.assertIn("'mc-skip-printers', 'mc-keep-software'", html)
        self.assertIn("getElementById('mc-keep-software').onchange = renderDeletePreview", html)
        self.assertIn("getElementById('mc-keep-software').checked = false", html)


class KeepReasonInChainTest(unittest.TestCase):
    """A `reason` nem veszhet el a lánc-állapotban (a törlés egy KÉSŐBBI lábon fut)."""

    def test_reason_megmarad_a_keep_packages_ben(self):
        src = open(os.path.join(ROOT, 'app', 'gui', 'autofix.py'), encoding='utf-8').read()
        self.assertIn("'reason': ('software' if k.get('reason') == 'software' else 'manual')", src)
        self.assertIn("'reason': k.get('reason') or 'manual'", src)   # óra-javítás utáni A láb
        self.assertIn("Szoftver-megtartás (kapcsoló)", src)


if __name__ == '__main__':
    unittest.main()
