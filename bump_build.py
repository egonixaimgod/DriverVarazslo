"""rebuild.bat segédszkriptje: BUILD_NUMBER növelése.

A helyi driver_tool.py-ban lévő szám mellett a GitHubon már publikált
BUILD_NUMBER-t is lekérdezi, és a NAGYOBBIKBÓL indul ki. Enélkül egy
elavult helyi checkout-ról futtatott rebuild visszaléptetné a publikált
build-számot (ez korábban megtörtént: 155 -> 150), ami az auto-updatert
(csak `new_build > BUILD_NUMBER` esetén kínál fel frissítést) csendben
"befagyasztja" minden már magasabb build-en lévő felhasználónál.

KÉT TÁVOLI FORRÁST NÉZÜNK, ÉS EZ 2026-09-17 ÓTA VAN ÍGY. A raw.githubusercontent.com
egy Fastly CDN mögött ül `Cache-Control: max-age=300` fejléccel (mérve), tehát az ELŐZŐ
kiadás utáni öt percben a RÉGI build-számot adja vissza - vagyis két gyors egymás utáni
kiadásnál pontosan azt a visszaléptetést okozná, ami ellen ez a szkript készült. A
GitHub Releases API (`build-N` címkék) ilyenkor már a friss számot mutatja, ezért
mindkettőt megkérdezzük, és a három szám (helyi / raw / kiadás) maximumából emelünk.
"""
import re
import ssl
import json
import urllib.request

REPO = "egonixaimgod/DriverVarazslo"
REMOTE_URL = f"https://raw.githubusercontent.com/{REPO}/main/driver_tool.py"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
BUILD_RE = re.compile(r'^BUILD_NUMBER\s*=\s*(\d+)', re.M)
BUILD_TAG_RE = re.compile(r'^build-(\d+)$', re.I)

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    content = f.read()

match = BUILD_RE.search(content)
local_build = int(match.group(1))

ctx = ssl.create_default_context()

remote_build = local_build
try:
    req = urllib.request.Request(REMOTE_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        remote_src = resp.read().decode('utf-8', errors='replace')
    remote_match = BUILD_RE.search(remote_src)
    if remote_match:
        remote_build = int(remote_match.group(1))
except Exception:
    pass  # nincs net / GitHub elérhetetlen - a helyi számból indulunk ki

release_build = local_build
try:
    req = urllib.request.Request(RELEASES_URL, headers={
        'User-Agent': 'DriverVarazslo-bump', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    # Csak a `build-N` címkék számítanak: a repóban asset-tároló kiadások is vannak
    # (stresstools.zip, mas.zip, block.bat), azok címkéjéből nem jön build-szám.
    for rel in (data if isinstance(data, list) else []):
        m = BUILD_TAG_RE.match((rel.get('tag_name') or '').strip())
        if m:
            release_build = max(release_build, int(m.group(1)))
except Exception:
    pass

new_build = max(local_build, remote_build, release_build) + 1
content = content[:match.start(1)] + str(new_build) + content[match.end(1):]

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(content)

# version_info.txt (Windows fájlverzió-erőforrás) szinkronban tartása a BUILD_NUMBER-rel.
# Enélkül a PyInstaller-be sütött fájlverzió (Tulajdonságok ablak) minden kiadással
# egyre jobban elmaradt volna a valós BUILD_NUMBER-től (ez korábban meg is történt: a fájl
# 1.0.148.0-n ragadt, miközben a build szám már 165-nél járt).
VERSION_INFO_PATH = 'version_info.txt'
with open(VERSION_INFO_PATH, 'r', encoding='utf-8') as f:
    vi_content = f.read()

vi_content = re.sub(r'(filevers=\(1, 0, )\d+(, 0\))', rf'\g<1>{new_build}\g<2>', vi_content)
vi_content = re.sub(r'(prodvers=\(1, 0, )\d+(, 0\))', rf'\g<1>{new_build}\g<2>', vi_content)
vi_content = re.sub(r"(u'FileVersion', u'1\.0\.)\d+(\.0')", rf'\g<1>{new_build}\g<2>', vi_content)
vi_content = re.sub(r"(u'ProductVersion', u'1\.0\.)\d+(\.0')", rf'\g<1>{new_build}\g<2>', vi_content)

with open(VERSION_INFO_PATH, 'w', encoding='utf-8') as f:
    f.write(vi_content)

print(new_build)
