"""Az 1 kattintásos fix előre megnyomható „Igen” gombja (2026-11-25, explicit user decision:
*„amíg ki van szürkítve előjáróban rá tudjak nyomni, és akkor ahogy betöltődött minden
egyből indítja is”*). A felület a valódi ui.html függvényein fut (node)."""
import os
import shutil
import subprocess
import unittest

import tests  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FixArmUiTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'nincs node a gépen')
    def test_felulet_forgatokonyvek(self):
        r = subprocess.run(['node', os.path.join(ROOT, 'tests', 'fix_arm_harness.js'),
                            os.path.join(ROOT, 'ui.html')],
                           capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('MIND RENDBEN', r.stdout)

    def test_a_bekotes_minden_helyen_megvan(self):
        html = open(os.path.join(ROOT, 'ui.html'), encoding='utf-8').read()
        self.assertIn('id="mc-yes" onclick="mcYesClick()"', html)
        # a hálózat-felismerés (a Wi-Fi mód pipája) mindkét ágon lezárja a várakozást
        self.assertEqual(html.count('netDone();'), 2)
        # az elemzés alatt a gomb nem `disabled` (különben a kattintás el sem jutna hozzánk)
        self.assertNotIn('yes.disabled = on', html)


if __name__ == '__main__':
    unittest.main()
