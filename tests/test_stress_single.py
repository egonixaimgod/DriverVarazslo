"""Egyenkénti stressz-indítás: a FurMark/Prime95/Linpack 1:1 a tömeges út beállításaival
indul (explicit user decision, 2026-11-03)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.gui.stress import GuiStressMixin
from app.stress_defs import STRESS_CLICK_SEQUENCES, STRESS_SINGLE_AS_BULK, STRESS_TOOL_ARGS


class _F(GuiStressMixin):
    def __init__(self, exe):
        self.events, self.launches, self._stress_pids, self.exe = [], [], {}, exe

    def emit(self, ev, data):
        self.events.append((ev, data))

    def _lock_power_for_stress(self):
        pass

    def _find_installed_tool_exe(self, name):
        return r'C:\Program Files\Telepitett\x.exe'  # nem szabad ezt használnia

    def _download_stresstools(self, **kw):
        return os.path.dirname(self.exe)

    def _find_stress_tool_exes(self, d, keys):
        return {k: self.exe for k in keys}

    def _build_linpack_console_script(self):
        return [('menu', '7', False)]

    def _launch_stress_exe(self, exe, name, **kw):
        self.launches.append((exe, kw))
        if kw.get('result_sink') is not None:
            kw['result_sink'][kw['result_key']] = True
        return 1234


class SingleAsBulkTest(unittest.TestCase):
    def _run(self, name):
        with tempfile.NamedTemporaryFile(suffix='.exe', delete=False) as f:
            exe = f.name
        self.addCleanup(os.remove, exe)
        api = _F(exe)
        with mock.patch('threading.Thread') as T:
            api.start_stress_tool(name)
            T.call_args.kwargs['target']()  # a worker szinkron
        return api

    def test_bulk_beallitasok(self):
        for name in STRESS_SINGLE_AS_BULK:
            api = self._run(name)
            exe, kw = api.launches[0]
            self.assertEqual(exe, api.exe, name)  # portable, nem a telepített
            self.assertEqual(kw.get('args'), STRESS_TOOL_ARGS.get(name), name)
            self.assertEqual(kw.get('click_sequence'), STRESS_CLICK_SEQUENCES.get(name), name)
            self.assertEqual(bool(kw.get('console_script')), name == 'linpack', name)

    def test_tobbi_program_automatizalas_nelkul(self):
        api = self._run('cpuz')
        exe, kw = api.launches[0]
        self.assertEqual(kw, {})


if __name__ == '__main__':
    unittest.main()
