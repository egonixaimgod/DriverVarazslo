"""A Linpack Xtreme WMIC-pótléka (app/stress_defs.py: LINPACK_WMIC_SHIM).

A Linpack v1.1.8 indító-batch-ének WMIC-sorait SZÓ SZERINT futtatjuk (a Linpack.exe által a
%TEMP%-be kibontott .bat-ból, 2026-10-16), a pótlék melletti munkamappában, olyan PATH-szal,
amiben nincs System32\\wbem - vagyis úgy, mint egy WMIC nélküli Win11 24H2+ gépen.
"""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from app.gui.stress import GuiStressMixin

# A Linpack batch-éből (checkwmic + runstresstest), csak a kiírás került a végére.
LINPACK_LINES = r'''@echo off
SETLOCAL
:checkwmic
for /f "tokens=*" %%K IN ('wmic os get version') DO set WMIC=%%K
if not defined WMIC echo NOWMIC
if not defined WMIC goto end
for /f "tokens=*" %%D IN ('wmic cpu get NumberOfCores ^| findstr /r "^[0-9]"') DO set CORES=%%D
for /f "tokens=*" %%E IN ('wmic computersystem get NumberOfProcessors ^| findstr /r "^[0-9]"') DO set CPUS=%%E
for /f "tokens=*" %%F IN ('wmic cpu get NumberOfLogicalProcessors ^| findstr /r "^[0-9]"') DO set THREADS=%%F
set CORES=%CORES: =%
set CPUS=%CPUS: =%
set THREADS=%THREADS: =%
set /a MAXTHREADS="THREADS * CPUS"
echo RESULT %CPUS% %CORES% %THREADS% %MAXTHREADS%
:end
'''


class _F(GuiStressMixin):
    pass


@unittest.skipUnless(os.name == 'nt', 'cmd.exe kell hozzá')
class LinpackWmicShimTest(unittest.TestCase):
    def _run_linpack_lines(self, path_dirs):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, 'Linpack.exe')
            # A cmd munkamappa-keresése kikapcsolva: a pótlékot a PATH-on kell megtalálnia.
            with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(path_dirs),
                                              'NoDefaultCurrentDirectoryInExePath': '1'}):
                env = _F()._prepare_linpack_wmic(exe)
            self.assertIsNotNone(env)
            bat = os.path.join(d, 'shimtest.bat')
            with open(bat, 'w', encoding='ascii', newline='\r\n') as f:
                f.write(LINPACK_LINES)
            out = subprocess.run(['cmd', '/c', bat], cwd=d, env=env,
                                 capture_output=True, text=True, timeout=120).stdout
        line = next((ln for ln in out.splitlines() if ln.startswith(('RESULT', 'NOWMIC'))), out)
        self.assertTrue(line.startswith('RESULT'), f'a Linpack WMIC-ellenőrzése elbukna: {out!r}')
        cpus, cores, threads, maxthreads = (int(x) for x in line.split()[1:])
        self.assertGreaterEqual(cpus, 1)
        self.assertGreaterEqual(cores, 1)
        # A batch MAXTHREADS-e = a gép összes logikai processzora (1 processzorcsoportos gépen).
        self.assertEqual(maxthreads, os.cpu_count())
        return cpus, cores, threads

    def test_cim_path_without_system_wmic(self):
        sysroot = os.environ.get('SystemRoot', r'C:\Windows')
        self._run_linpack_lines([os.path.join(sysroot, 'System32'),
                                 os.path.join(sysroot, 'System32', 'WindowsPowerShell', 'v1.0')])

    def test_fallback_without_powershell(self):
        # PowerShell sincs a PATH-on -> a pótlék a %NUMBER_OF_PROCESSORS% + 1 foglalat tartalékra esik.
        sysroot = os.environ.get('SystemRoot', r'C:\Windows')
        cpus, _cores, threads = self._run_linpack_lines([os.path.join(sysroot, 'System32')])
        self.assertEqual((cpus, threads), (1, os.cpu_count()))


if __name__ == '__main__':
    unittest.main()
