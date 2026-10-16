"""A stressz-ablakok elrendezése (app/gui/stress_automation.py).

Élőben mérve (2026-10-16, WT 1.25): Windows Terminalban a konzol GetConsoleWindow-ja egy
LÁTHATÓ PseudoConsoleWindow, amit mozgatva a WT-ablak nem mozdul; a valódi ablak a
gyökér-tulajdonosa. A verdikt a visszaolvasott LÁTHATÓ keret (DWM), nem a SetWindowPos
visszatérési értéke; a ~7 px láthatatlan keretet ráhagyjuk, hogy a negyedek rés nélkül
illeszkedjenek.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from app.gui.stress_automation import GuiStressAutomationMixin

INSET = (7, 0, 7, 7)  # Win10/11: láthatatlan keret balra, felül, jobbra, alul


class FakeUser32:
    def __init__(self, owners=None, jumps_back=0, frozen=False):
        self.owners = owners or {}     # hwnd -> gyökér-tulajdonos
        self.jumps_back = jumps_back   # ennyi mozgatás után a program visszaugrik
        self.frozen = frozen           # a program soha nem mozdul
        self.pos = {}                  # hwnd -> külső ablak-rect
        self.moves = []
        self.shown = []

    def GetAncestor(self, hwnd, flag):
        assert flag == 3  # GA_ROOTOWNER
        return self.owners.get(hwnd, hwnd)

    def IsIconic(self, hwnd):
        return False

    def IsZoomed(self, hwnd):
        return False

    def ShowWindow(self, hwnd, cmd):
        self.shown.append((hwnd, cmd))
        return True

    def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
        self.moves.append((hwnd, x, y, w, h))
        if not self.frozen and len(self.moves) > self.jumps_back:
            self.pos[hwnd] = (x, y, x + w, y + h)
        return True  # a valódi API is "sikert" ad, akkor is, ha a program nem mozdult

    def GetWindowRect(self, hwnd, prect):
        r = prect._obj
        r.left, r.top, r.right, r.bottom = self.pos.get(hwnd, (300, 200, 1100, 800))
        return True


class FakeDwm:
    def __init__(self, user32, inset=INSET, hr=0):
        self.user32, self.inset, self.hr = user32, inset, hr

    def DwmGetWindowAttribute(self, hwnd, attr, pvalue, size):
        assert attr == 9  # DWMWA_EXTENDED_FRAME_BOUNDS
        l, t, r, b = self.user32.pos.get(hwnd, (300, 200, 1100, 800))
        rect = pvalue._obj
        rect.left, rect.top, rect.right, rect.bottom = (l + self.inset[0], t + self.inset[1],
                                                        r - self.inset[2], b - self.inset[3])
        return self.hr


def _mixin(user32, **dwm_kw):
    f = GuiStressAutomationMixin()
    f._stress_dwmapi = lambda: FakeDwm(user32, **dwm_kw)
    return f


@mock.patch('app.gui.stress_automation.time.sleep', lambda s: None)
class StressLayoutTest(unittest.TestCase):
    def test_terminal_hosted_console_resolves_to_real_window(self):
        u = FakeUser32(owners={10: 20})  # 10 = PseudoConsoleWindow, 20 = WT-ablak
        self.assertEqual(_mixin(u)._top_level_window(u, 10), 20)

    def test_classic_console_and_gui_window_stay(self):
        u = FakeUser32()
        self.assertEqual(_mixin(u)._top_level_window(u, 30), 30)
        u.GetAncestor = lambda h, f: None
        self.assertEqual(_mixin(u)._top_level_window(u, 30), 30)
        self.assertIsNone(_mixin(u)._top_level_window(u, None))

    def test_visible_frame_fills_the_quadrant_exactly(self):
        u = FakeUser32()
        ok, attempt, vis = _mixin(u)._place_window(u, 20, 0, 520, 960, 520, False)
        self.assertEqual((ok, attempt, vis), (True, 1, (0, 520, 960, 1040)))
        # a külső rect a láthatatlan kerettel nagyobb
        self.assertEqual(u.moves[-1], (20, -7, 520, 974, 527))

    def test_nonsense_dwm_frame_is_ignored(self):
        u = FakeUser32()
        ok, _attempt, vis = _mixin(u, inset=(-50, 0, 0, 0))._place_window(u, 20, 0, 520, 960, 520, False)
        self.assertEqual((ok, vis), (True, (0, 520, 960, 1040)))
        ok, _attempt, vis = _mixin(u, hr=-1)._place_window(u, 21, 0, 520, 960, 520, False)
        self.assertEqual((ok, vis), (True, (0, 520, 960, 1040)))

    def test_place_retries_when_window_jumps_back(self):
        u = FakeUser32(jumps_back=1)
        ok, attempt, _vis = _mixin(u)._place_window(u, 20, 0, 520, 960, 520, False)
        self.assertEqual((ok, attempt), (True, 2))

    def test_place_never_moved_is_not_success(self):
        u = FakeUser32(frozen=True)
        ok, attempt, _vis = _mixin(u)._place_window(u, 20, 0, 520, 960, 520, False)
        self.assertEqual((ok, attempt), (False, 3))
        self.assertEqual(len(u.moves), 3)

    def test_normal_window_is_not_activated(self):
        # A régi kód minden próbánál SW_RESTORE-t küldött (előtérbe rántotta az ablakot).
        u = FakeUser32()
        _mixin(u)._place_window(u, 20, 0, 0, 960, 520, False)
        self.assertEqual(u.shown, [])


class MinimizeOthersTest(unittest.TestCase):
    def test_minimize_without_activation_skipping_kept_tool_and_cloaked(self):
        # hwnd: 1 = megtartott, 2 = normál, 3 = TOOLWINDOW, 4 = cloaked
        u = mock.Mock()
        u.EnumWindows.side_effect = lambda proc, lp: [proc(h, 0) for h in (1, 2, 3, 4)]
        u.IsWindowVisible.return_value = True
        u.GetWindowTextLengthW.return_value = 5
        u.IsIconic.return_value = False
        u.GetClassNameW.return_value = 0
        u.GetWindowLongW.side_effect = lambda h, idx: 0x80 if h == 3 else 0

        class Dwm:
            def DwmGetWindowAttribute(self, h, attr, p, size):
                p._obj.value = 1 if h == 4 else 0
                return 0

        f = GuiStressAutomationMixin()
        f._stress_user32 = lambda: u
        f._stress_dwmapi = lambda: Dwm()
        f.emit = lambda *a: None
        with mock.patch('app.gui.stress_automation.ctypes.WINFUNCTYPE', return_value=lambda fn: fn):
            f._minimize_other_windows([1])
        u.ShowWindow.assert_called_once_with(2, 7)  # SW_SHOWMINNOACTIVE


# A gyerekfolyamat SAJÁT konzolon fut (mint a CLI), és egy másik konzolt olvas a valódi kóddal.
# Mérve (2026-10-16): a régi kód után a kiírás OSError 9-et adott, a stdin érvénytelen volt.
_CLI_CHILD = r'''
import ctypes, ctypes.wintypes as W, json, msvcrt, subprocess, sys, threading, time
sys.path.insert(0, sys.argv[2])
from app.gui.stress_automation import GuiStressAutomationMixin
class F(GuiStressAutomationMixin):
    _console_attach_lock = threading.Lock()
sys.stdout = open('CONOUT$', 'w', encoding='utf-8'); sys.stdin = open('CONIN$', 'r', encoding='utf-8')
target = subprocess.Popen('cmd /c "echo DVMARKER& ping -n 20 127.0.0.1 >nul"', creationflags=subprocess.CREATE_NEW_CONSOLE)
time.sleep(1.5)
f = F(); res = {}
scr = f._read_console_screen(target.pid); res['read'] = bool(scr and 'DVMARKER' in scr)
res['window'] = bool(f._find_console_window_for_pid(target.pid))
try:
    sys.stdout.write('ok\n'); sys.stdout.flush(); res['stdout'] = True
except Exception as e:
    res['stdout'] = repr(e)
m = W.DWORD(); res['stdin'] = bool(ctypes.windll.kernel32.GetConsoleMode(W.HANDLE(msvcrt.get_osfhandle(sys.stdin.fileno())), ctypes.byref(m)))
json.dump(res, open(sys.argv[1], 'w')); target.kill(); f._console_keeper.kill()
'''


@unittest.skipUnless(os.name == 'nt', 'Windows-konzol kell hozzá')
class CliOwnConsoleSurvivesTest(unittest.TestCase):
    def test_reading_linpack_console_keeps_cli_console(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'res.json')
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE: ne villanjon fel ablak a teszt alatt
            subprocess.Popen([sys.executable, '-c', _CLI_CHILD, out, repo],
                             creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si).wait(timeout=90)
            with open(out) as fh:
                res = json.load(fh)
        self.assertEqual(res, {'read': True, 'window': True, 'stdout': True, 'stdin': True})


if __name__ == '__main__':
    unittest.main()
