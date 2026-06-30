"""rebuild.bat segédszkriptje: BUILD_NUMBER növelése.

A helyi driver_tool.py-ban lévő szám mellett a GitHubon már publikált
BUILD_NUMBER-t is lekérdezi, és a kettő közül a nagyobbikból indul ki.
Enélkül egy elavult helyi checkout-ról futtatott rebuild visszaléptetné
a publikált build-számot (ez korábban megtörtént: 155 -> 150), ami az
auto-updatert (csak `new_build > BUILD_NUMBER` esetén kínál fel
frissítést) csendben "befagyasztja" minden már magasabb build-en lévő
felhasználónál.
"""
import re
import ssl
import urllib.request

REMOTE_URL = "https://raw.githubusercontent.com/egonixaimgod/DriverVarazslo/main/driver_tool.py"
BUILD_RE = re.compile(r'^BUILD_NUMBER\s*=\s*(\d+)', re.M)

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    content = f.read()

match = BUILD_RE.search(content)
local_build = int(match.group(1))

remote_build = local_build
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(REMOTE_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        remote_src = resp.read().decode('utf-8', errors='replace')
    remote_match = BUILD_RE.search(remote_src)
    if remote_match:
        remote_build = int(remote_match.group(1))
except Exception:
    pass  # nincs net / GitHub elérhetetlen - a helyi számból indulunk ki

new_build = max(local_build, remote_build) + 1
content = content[:match.start(1)] + str(new_build) + content[match.end(1):]

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(new_build)
