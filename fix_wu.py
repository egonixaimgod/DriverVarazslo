import re

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Remove all self._run that modify ExcludeWUDriversInQualityUpdate
text = re.sub(r"^[ \t]*self\._run\(\s*\[\'reg\', \'(add|delete)\', r\'HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\',.*?\'ExcludeWUDriversInQualityUpdate\'.*?\]\)\n", "", text, flags=re.MULTILINE)

# There is one broken on multiple lines
text = re.sub(r"^[ \t]*self\._run\(\s*\[\'reg\', \'(add|delete)\', r\'HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\',\n[ \t]*\'/v\', \'ExcludeWUDriversInQualityUpdate\'.*?\]\)\n", "", text, flags=re.MULTILINE)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("Done")