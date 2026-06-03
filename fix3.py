import sys
with open('driver_tool.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
lines[2649] = '                self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])\n'
lines[2650] = '                self._run(["dism", "/Cleanup-Wim"])\n'
with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
