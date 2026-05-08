import re
import io

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Remove ExcludeWUDriversInQualityUpdate
text = re.sub(r"self\._run\(\s*\[\'reg\', \'delete\', r\'HKLM\\\\SOFTWARE\\\\Policies\\\\Microsoft\\\\Windows\\\\WindowsUpdate\',[^\)]+\)\n?", "", text)
text = re.sub(r"self\._run\(\s*\[\'reg\', \'add\', r\'HKLM\\\\SOFTWARE\\\\Policies\\\\Microsoft\\\\Windows\\\\WindowsUpdate\',[^\)]+\)\n?", "", text)
text = text.replace("winreg.DeleteValue(key, \"ExcludeWUDriversInQualityUpdate\")", "pass")
text = text.replace("winreg.SetValueEx(key, \"ExcludeWUDriversInQualityUpdate\", 0, winreg.REG_DWORD, 0)", "pass")
text = text.replace("winreg.SetValueEx(key, \"ExcludeWUDriversInQualityUpdate\", 0, winreg.REG_DWORD, 1)", "pass")

# 2. Fix the try/finally block in _install_wu_api
# Find the exact loop in _install_wu_api
loop_search = """            for line in process.stdout:
                if self._check_cancel():
                    process.terminate()
                    process.wait()  # Prevent zombie process
                    self.emit('task_progress', {'task': 'wu_install', 'log': '\\n❗ Megszakítva!'})
                    self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                    return
                line = line.strip()
                if not line:
                    continue"""

loop_replace = """            self._run(['reg', 'add', r'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])
            try:
                for line in process.stdout:
                    if self._check_cancel():
                        process.terminate()
                        process.wait()  # Prevent zombie process
                        self.emit('task_progress', {'task': 'wu_install', 'log': '\\n❗ Megszakítva!'})
                        self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                        return
                    line = line.strip()
                    if not line:
                        continue"""

text = text.replace(loop_search, loop_replace)

end_search = """                else:
                    self.emit('task_progress', {'task': 'wu_install', 'log': line})
            process.wait()

            if success > 0:"""

end_replace = """                else:
                    self.emit('task_progress', {'task': 'wu_install', 'log': line})
                process.wait()
            finally:
                self._run(['reg', 'add', r'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])

            if success > 0:"""

text = text.replace(end_search, end_replace)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("Done")