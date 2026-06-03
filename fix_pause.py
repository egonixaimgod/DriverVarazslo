import sys
import re

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

new_set_wu_pause = r"""    def _set_wu_pause(self, pause=True):
        if pause:
            ps = r'''
            $pauseDate = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            $nowDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            if (!(Test-Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings')) { New-Item -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Force | Out-Null }
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            '''
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate', '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
        else:
            ps = r'''
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            '''
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])
            self._run(['reg', 'delete', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate', '/v', 'ExcludeWUDriversInQualityUpdate', '/f'])
"""

# Replace _set_wu_pause
start_idx = text.find('    def _set_wu_pause(self, pause=True):')
end_idx = text.find('    def _search_wu_api(self):')
if start_idx != -1 and end_idx != -1:
    text = text[:start_idx] + new_set_wu_pause + '\n' + text[end_idx:]

# Remove redundant registry calls
text = text.replace("self._run(['reg', 'add', r'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])\n                self._set_wu_pause(pause=False)", "self._set_wu_pause(pause=False)")
text = text.replace("self._run(['reg', 'add', r'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])\n                self._set_wu_pause(pause=True)", "self._set_wu_pause(pause=True)")

# Clean up _install_wu_api
text = text.replace("self._run(['reg', 'add', r'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])\n            self._set_wu_pause(pause=False)", "self._set_wu_pause(pause=False)")
text = text.replace("self._run(['reg', 'add', r'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])\n                self._set_wu_pause(pause=True)", "self._set_wu_pause(pause=True)")

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)
