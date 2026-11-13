"""A CLAUDE.md, a .claude/rules/ és a docs/terep/ rendje (explicit user decision, 2026-10-11 és 2026-11-13).

A CLAUDE.md a mindig betöltött, tömör szabálykönyv (legfeljebb 80 000 karakter), a területi
szabályok (12–17. fejezet) a .claude/rules/*.md fájlokban vannak (egyenként legfeljebb 40 000),
amiket a Claude Code a `paths:` mintáik szerint tölt be, a bizonyítékok a docs/terep/*.md-ben.
Ez a teszt bukik, ha egy fájl túlnő a korláton, ha egy szabályfájlnak nincs `paths:` listája
(akkor mindig betöltődne) vagy egy mintája semmire nem illik (egy átnevezett kódfájl némán
leválna a szabályáról), ha egy hivatkozott fájl hiányzik, vagy ha egy fájlra semmi nem hivatkozik.
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_CHARS = 80_000
RULE_MAX_CHARS = 40_000
RULES_DIR = os.path.join(ROOT, '.claude', 'rules')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _rule_paths(text):
    """A frontmatter `paths:` listája; None, ha nincs frontmatter vagy paths."""
    m = re.match(r'---\n(.*?)\n---\n', text, re.S)
    if not m or not re.search(r'^paths:\s*$', m.group(1), re.M):
        return None
    return re.findall(r'^\s+-\s+"([^"]+)"\s*$', m.group(1), re.M)


class ClaudeMdTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(os.path.join(ROOT, 'CLAUDE.md'))
        self.rules = {n: _read(os.path.join(RULES_DIR, n)) for n in sorted(os.listdir(RULES_DIR)) if n.endswith('.md')}

    def test_meretkorlat(self):
        self.assertLessEqual(len(self.text), MAX_CHARS,
                             'A CLAUDE.md túlnőtt a korláton: területi szabályt a .claude/rules/ fájlba, '
                             'bizonyítékot a docs/terep/ fájlba írj.')
        for name, text in self.rules.items():
            self.assertLessEqual(len(text), RULE_MAX_CHARS, f'{name} túlnőtt: bontsd vagy a bizonyítékot vidd a terepbe')

    def test_nincs_vezerlokarakter(self):
        # egy nem-nyers sztringből írt '\0' egyszer bináris fájlnak tüntette fel a CLAUDE.md-t a grep előtt
        for name, text in [('CLAUDE.md', self.text), *self.rules.items()]:
            bad = [hex(ord(c)) for c in text if ord(c) < 32 and c not in '\r\n\t']
            self.assertEqual(bad, [], name)

    def test_szabalyfajlok_paths(self):
        self.assertTrue(self.rules, 'nincs egy szabályfájl sem a .claude/rules/ alatt')
        for name, text in self.rules.items():
            paths = _rule_paths(text)
            self.assertTrue(paths, f'{name}: nincs `paths:` lista, így minden sessionben betöltődne')
            for p in paths:
                self.assertTrue(glob.glob(os.path.join(ROOT, p), recursive=True),
                                f'{name}: a "{p}" minta egyetlen fájlra sem illik (átnevezted a kódfájlt?)')

    def test_szabalyfajlok_hivatkozva(self):
        refs = set(re.findall(r'\.claude/rules/([\w\-]+\.md)', self.text))
        self.assertEqual(sorted(refs - set(self.rules)), [], 'hivatkozott, de nem létező szabályfájl')
        self.assertEqual(sorted(set(self.rules) - refs), [], 'szabályfájl, amire a CLAUDE.md nem hivatkozik')

    def test_terep_hivatkozasok(self):
        refs = set(re.findall(r'docs/terep/[\w\-]+\.md', self.text + ''.join(self.rules.values())))
        letezo = {'docs/terep/' + n for n in os.listdir(os.path.join(ROOT, 'docs', 'terep')) if n.endswith('.md')}
        self.assertEqual(sorted(refs - letezo), [], 'hivatkozott, de nem létező terep-fájl')
        self.assertEqual(sorted(letezo - refs), [], 'terep-fájl, amire semmi nem hivatkozik')


if __name__ == '__main__':
    unittest.main()
