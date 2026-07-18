"""DriverVarázsló CLI - CLI alap: init, _run (subprocess wrapper), progress-kiírás."""

# === AUTO-IMPORTS ===
import os
import subprocess
import time
import logging
# === /AUTO-IMPORTS ===


class CliBaseMixin:
    """CLI alap: init, _run (subprocess wrapper), progress-kiírás. A CliApi része (összerakás: app/cli/api.py)."""

    def __init__(self):
        self.target_os_path = None
        self.sys_drive = os.environ.get('SystemDrive', 'C:') + '\\'
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self._nw = subprocess.CREATE_NO_WINDOW
        self._cancel_flag = False
    
    def _run(self, cmd, *, ok_codes=(0,), **kwargs):
        """Parancs futtatás (CLI verzió). ok_codes: a hívó által várt (nem hibának
        számító) visszatérési kódok - lásd DriverToolApi._run azonos paraméterét."""
        cmd_str = cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)
        logging.debug(f"[CMD_CLI] Futtatás: {cmd_str[:300]}")
        # stdin alapból DEVNULL - lásd DriverToolApi._run azonos sorát (érvénytelenné vált
        # örökölt stdin handle elleni védelem; CLI-ben konzisztencia okán ugyanígy).
        kwargs.setdefault('stdin', subprocess.DEVNULL)
        start = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                                  startupinfo=self._si, creationflags=self._nw, **kwargs)
            elapsed = time.time() - start
            if result.returncode not in ok_codes:
                logging.warning(f"[CMD_CLI] Visszatérési kód: {result.returncode} ({elapsed:.1f}s)")
                if result.stderr:
                    logging.warning(f"[CMD_CLI] stderr: {result.stderr[:4000]}")
            elif result.returncode != 0:
                logging.debug(f"[CMD_CLI] OK - várt kód: {result.returncode} ({elapsed:.1f}s)")
            else:
                logging.debug(f"[CMD_CLI] OK ({elapsed:.1f}s)")
            
            if result.stdout:
                out_txt = result.stdout.strip()
                if len(out_txt) > 4000: out_txt = out_txt[:4000] + '... [TRUNCATED]'
                logging.debug(f"[CMD_CLI] stdout: {out_txt}")
            return result
        except Exception as e:
            logging.error(f"[CMD_CLI] Kivétel: {e}")
            class DummyRes:
                returncode = 1
                stdout = ""
                stderr = str(e)
            return DummyRes()
    
    def _print_progress(self, msg, end='\n'):
        """Progress kiírás."""
        print(msg, end=end, flush=True)
    
