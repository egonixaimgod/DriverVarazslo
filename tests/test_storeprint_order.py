"""Bolti nyomtatás (2026-11-28, terep: Lenovo 20S1, Build 441): a PDF a nyomtató felvétele
ELŐTT készül, és először a program saját WebView2-jével. Egy bukott PDF után nem
nyúlunk a nyomtatóhoz (terepen ez 2 percnyi felvétel + levétel volt a semmiért)."""
import os
import tempfile
import unittest
from unittest import mock

from app.gui import storeprint
from app.gui.storeprint import GuiStorePrintMixin


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class P(GuiStorePrintMixin):
    def __init__(self, report):
        self._last_report_path = report
        self.cmds, self.events = [], []
        self._window = None

    def emit(self, ev, data=None):
        self.events.append((ev, data))

    def _run(self, cmd, **kw):
        self.cmds.append(cmd)
        raise AssertionError(f"nem várt parancs: {cmd}")

    def _safe_thread(self, name, fn):
        try:
            fn()
        except Exception as e:
            self.error = str(e)


class StorePrintOrderTest(unittest.TestCase):
    def setUp(self):
        fd, self.report = tempfile.mkstemp(suffix='.html')
        os.close(fd)

    def tearDown(self):
        os.remove(self.report)

    def test_bukott_pdf_utan_nincs_nyomtato_parancs(self):
        p = P(self.report)
        p._html_to_pdf = lambda h, o: False
        with mock.patch.object(storeprint.socket, 'create_connection', return_value=_Conn()):
            p.print_via_store_printer()
        self.assertEqual(p.cmds, [])
        self.assertIn('PDF', p.error)

    def test_webview_ut_az_elso_es_sikernel_nincs_edge(self):
        p = P(self.report)
        p._window = object()
        p._webview_html_to_pdf = mock.Mock(return_value=True)
        p._find_msedge_exe = mock.Mock(side_effect=AssertionError('Edge nem kell'))
        self.assertTrue(p._html_to_pdf(self.report, self.report + '.pdf'))
        p._webview_html_to_pdf.assert_called_once()


if __name__ == '__main__':
    unittest.main()
