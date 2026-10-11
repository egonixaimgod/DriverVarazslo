"""A CLAUDE.md méretkorlátja és a docs/terep/ hivatkozásai (explicit user decision, 2026-10-11).

A CLAUDE.md a tömör szabálykönyv (legfeljebb 150 000 karakter), a bizonyítékok
a docs/terep/*.md fájlokban vannak. Ez a teszt bukik, ha a fájl túlnő a korláton,
ha egy hivatkozott terep-fájl hiányzik, vagy ha egy terep-fájlra semmi nem hivatkozik
(egy nem hivatkozott fájlt a következő session nem fog megtalálni).
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_CHARS = 150_000


class ClaudeMdTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, 'CLAUDE.md'), encoding='utf-8') as f:
            self.text = f.read()

    def test_meretkorlat(self):
        self.assertLessEqual(len(self.text), MAX_CHARS,
                             'A CLAUDE.md túlnőtt a korláton: a bizonyítékot a docs/terep/ fájlba írd.')

    def test_nincs_vezerlokarakter(self):
        # egy nem-nyers sztringből írt '\0' egyszer bináris fájlnak tüntette fel a CLAUDE.md-t a grep előtt
        bad = [hex(ord(c)) for c in self.text if ord(c) < 32 and c not in '\r\n\t']
        self.assertEqual(bad, [])

    def test_terep_hivatkozasok(self):
        refs = set(re.findall(r'docs/terep/[\w\-]+\.md', self.text))
        letezo = {'docs/terep/' + n for n in os.listdir(os.path.join(ROOT, 'docs', 'terep')) if n.endswith('.md')}
        self.assertEqual(sorted(refs - letezo), [], 'hivatkozott, de nem létező terep-fájl')
        self.assertEqual(sorted(letezo - refs), [], 'terep-fájl, amire a CLAUDE.md nem hivatkozik')


if __name__ == '__main__':
    unittest.main()
