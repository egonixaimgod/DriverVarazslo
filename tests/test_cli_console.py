"""A CLI konzol-rétege (app/cli/console.py, 2026-10-13: rich + questionary).

A nyilas menük valódi billentyűleütésekkel futnak (prompt_toolkit pipe-bemenet, kimenet
nélkül), így konzolablak nélkül is ellenőrizhető, hogy a választás a helyes kulcsot adja
vissza - és hogy a begépelős tartalék is működik, ha a questionary nem tud rajzolni.
"""
import io
import unittest
from unittest import mock

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from app.cli import console as ui

DOWN, UP, ENTER, SPACE = '\x1b[B', '\x1b[A', '\r', ' '


def keys(text, fn):
    """`fn()` futtatása úgy, mintha a technikus a `text` billentyűket ütné le."""
    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()):
            inp.send_text(text)
            return fn()


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        ui._Q_BROKEN = False
        self.buf = io.StringIO()
        ui._console = Console(file=self.buf, width=100, highlight=False, markup=False, emoji=False)

    def tearDown(self):
        ui.progress_done()
        ui._console = None
        ui._Q_BROKEN = False

    ITEMS = [(None, 'DRIVEREK', None), ('1', 'Első', 'leírás'), ('2', 'Második', None),
             (None, 'RENDSZER', None), ('10', 'Tizedik', None)]

    def test_menu_arrows_skip_separators(self):
        self.assertEqual(keys(ENTER, lambda: ui.menu(self.ITEMS)), '1')
        self.assertEqual(keys(DOWN + DOWN + ENTER, lambda: ui.menu(self.ITEMS)), '10')
        # a vissza-pont a lista végén: '0'
        self.assertEqual(keys(DOWN * 3 + ENTER, lambda: ui.menu(self.ITEMS)), '0')

    def test_menu_ctrl_c_is_back(self):
        self.assertEqual(keys('\x03', lambda: ui.menu(self.ITEMS)), '0')

    def test_confirm(self):
        self.assertTrue(keys(ENTER, lambda: ui.confirm('Mehet?', True)))
        self.assertFalse(keys(ENTER, lambda: ui.confirm('Mehet?', False)))
        self.assertFalse(keys(DOWN + ENTER, lambda: ui.confirm('Mehet?', True)))
        self.assertTrue(keys('i' + ENTER, lambda: ui.confirm('Mehet?', False)))
        # Ctrl+C = az alapérték (a régi viselkedés)
        self.assertTrue(keys('\x03', lambda: ui.confirm('Mehet?', True)))

    def test_ask_default_and_text(self):
        self.assertEqual(keys(ENTER, lambda: ui.ask('Név', 'alap')), 'alap')
        self.assertEqual(keys('\x7f' * 4 + 'abc' + ENTER, lambda: ui.ask('Név', 'alap')), 'abc')

    def test_checkbox_defaults_and_toggle(self):
        labels = ['a', 'b', 'c']
        self.assertEqual(keys(ENTER, lambda: ui.pick_indices('?', 3, labels=labels, defaults=[0, 2])), [0, 2])
        self.assertEqual(keys(DOWN + SPACE + ENTER, lambda: ui.pick_indices('?', 3, labels=labels)), [1])
        self.assertEqual(keys('\x03', lambda: ui.pick_indices('?', 3, labels=labels, defaults=[0])), [])

    def test_single_select_and_cancel(self):
        labels = ['x', 'y']
        self.assertEqual(keys(DOWN + ENTER, lambda: ui.pick_indices('?', 2, labels=labels, single=True)), [1])
        self.assertEqual(keys(DOWN + DOWN + ENTER, lambda: ui.pick_indices('?', 2, labels=labels, single=True)), [])

    def test_fallback_when_questionary_breaks(self):
        """Ha a questionary nem tud rajzolni: WARNING, és begépelős mód (a lista kiírva)."""
        import logging
        logging.disable(logging.NOTSET)   # a catalog_stub letiltja; itt a naplósor az ellenőrzött viselkedés
        with mock.patch('questionary.select', side_effect=RuntimeError('nincs konzol')), \
                mock.patch('builtins.input', return_value='2'), \
                self.assertLogs(level='WARNING') as logs:
            self.assertEqual(ui.menu(self.ITEMS), '2')
        self.assertIn('begépelős', logs.output[0])
        self.assertTrue(ui._Q_BROKEN)
        with mock.patch('builtins.input', return_value='1,3'):
            self.assertEqual(ui.pick_indices('?', 3, labels=['a', 'b', 'c']), [0, 2])
        self.assertIn('  3  c', self.buf.getvalue())
        with mock.patch('builtins.input', return_value=''):
            self.assertTrue(ui.confirm('?', True))

    def test_missing_questionary_falls_back(self):
        """Ha a questionary kimaradt az exéből: semmi nem dob, a régi begépelős mód jön."""
        with mock.patch.object(ui, 'questionary', None), mock.patch.object(ui, 'Choice', None), \
                mock.patch('builtins.input', return_value='2'):
            self.assertEqual(ui.pick_indices('?', 3, labels=['a', 'b', 'c']), [1])
            self.assertEqual(ui.pick_indices('?', 3, labels=['a', 'b', 'c'], single=True), [1])
            self.assertEqual(ui.menu(self.ITEMS), '2')
            self.assertEqual(ui.ask('?'), '2')

    def test_parse_indices(self):
        self.assertEqual(ui.parse_indices('1,3,5-7', 10), [0, 2, 4, 5, 6])
        self.assertEqual(ui.parse_indices('mind', 3), [0, 1, 2])
        self.assertEqual(ui.parse_indices('0, 4, 99, 2', 4), [3, 1])
        self.assertEqual(ui.parse_indices('', 4), [])

    def test_rendering_never_raises_and_keeps_brackets(self):
        """A naplósorok `[kód=4]`-et és ANSI-színt is hordoznak: nem lehet belőlük jelölés,
        és a kiírás nem dobhat."""
        ui.panel('CÍM', [f"{ui.RED}piros{ui.RESET} [kód=4] :x:", 'második'], color=ui.YELLOW)
        ui.table(['#', 'Név'], [['1', f'{ui.GREEN}zöld{ui.RESET}'], ['2', '[b]nem félkövér[/b]']])
        ui.title('Cím', 'alcím')
        ui.kv('Címke', 'érték')
        ui.ok('jó')
        ui.err('rossz')
        ui.progress(3, 10, 'halad')
        ui.busy('pörög')
        ui.progress_done()
        out = self.buf.getvalue()
        for s in ('[kód=4]', ':x:', '[b]nem félkövér[/b]', 'piros', 'zöld', 'CÍM', 'érték'):
            self.assertIn(s, out)


class AutofixKeepTest(unittest.TestCase):
    """A CLI fix törlési előnézete (menu._autofix_keep_packages): a zárolt sorok nem
    kerülnek a pipálható listára, és CSAK a kipipált szabad sor kerül a keep_packages-be -
    ugyanaz a szabály, mint a GUI `delRowForce` + `keepPackages`."""
    ROWS = [
        {'published': 'oem1.inf', 'original': 'boot.inf', 'group': 'boot'},
        {'published': 'oem2.inf', 'original': 'prn.inf', 'group': 'printer'},
        {'published': 'oem3.inf', 'original': 'wifi.inf', 'group': 'wifi'},
        {'published': 'oem4.inf', 'original': 'ninja.inf', 'group': 'normal', 'usage': 'active'},
        {'published': 'oem5.inf', 'original': 'gpu.inf', 'group': 'normal'},
    ]

    def setUp(self):
        ui._Q_BROKEN = False
        ui._console = Console(file=io.StringIO(), width=100)

    def tearDown(self):
        ui._console = None

    def run_keep(self, keystrokes, preview=None, **opts):
        from app.cli import menu
        api = mock.Mock()
        api.get_autofix_delete_preview.return_value = preview or {'drivers': self.ROWS, 'boot_detected': True}
        args = dict(skip_printer=True, wifi_mode=True, rebuild_wifi=False)
        args.update(opts)
        return keys(keystrokes, lambda: menu._autofix_keep_packages(api, **args))

    def test_only_free_rows_are_offered_and_kept(self):
        # szabad sorok: ninja, gpu (boot/nyomtató/Wi-Fi zárolt) -> az elsőt pipáljuk
        self.assertEqual(self.run_keep(SPACE + ENTER), [{'published': 'oem4.inf', 'original': 'ninja.inf'}])
        self.assertEqual(self.run_keep(ENTER), [])

    def test_switches_free_rows(self):
        # nyomtató-megtartás KI, Wi-Fi mód KI: a nyomtató és a Wi-Fi sor is szabad, pipálható
        got = self.run_keep(SPACE + DOWN + SPACE + ENTER, skip_printer=False, wifi_mode=False)
        self.assertEqual([k['original'] for k in got], ['prn.inf', 'wifi.inf'])

    def test_preview_error_keeps_nothing(self):
        self.assertEqual(self.run_keep('', preview={'drivers': [], 'error': 'dism 87'}), [])


if __name__ == '__main__':
    unittest.main()
