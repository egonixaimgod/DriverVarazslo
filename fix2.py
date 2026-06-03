import sys
with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

bad_indent = '            self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])\n                self._run(["dism", "/Cleanup-Wim"])'
good_indent = '            self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])\n            self._run(["dism", "/Cleanup-Wim"])'

text = text.replace(bad_indent, good_indent)
with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)
