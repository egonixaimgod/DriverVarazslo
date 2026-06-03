import sys
with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Bug 2: Replace process.terminate()
text = text.replace('process.terminate()', 'self._run([\'taskkill\', \'/F\', \'/T\', \'/PID\', str(process.pid)])')

# Bug 4: extract_wim cleanup
old_wim = 'self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])'
new_wim = 'self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])\n                self._run(["dism", "/Cleanup-Wim"])'
text = text.replace(old_wim, new_wim)

# Bug 5: single quotes for HWIDs
old_hwid = 'hwid_list_ps = ",".join(f\'"{h}"\' for h in pool_hwids)'
new_hwid = 'hwid_list_ps = ",".join(f"\'{h}\'" for h in pool_hwids)'
text = text.replace(old_hwid, new_hwid)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)
