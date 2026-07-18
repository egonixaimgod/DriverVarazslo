"""Szellemeszköz (ghost device) törlés - KÖZÖS mag (GUI + CLI + AutoFix).

A PowerShell script és a kimeneti sor-protokoll értelmezése EGY példányban él itt;
korábban 3 másolatban létezett (app/gui/ghost.py, app/cli/ghost.py,
app/gui/autofix.py _delete_ghost_devices_sync), és a másolatok lassan széttartottak
volna (lásd a Build-192-es "egyik út működik, a másik némán törött" tanulságot a
CLAUDE.md-ben). A kiírás (emit vs. print) a hívó mixinek dolga marad.

Sor-protokoll (a script kimenete, parse_ghost_line értelmezi):
  SKIPPED: <n>   - nyomtató-védelem miatt kihagyott szellemeszközök száma (csak
                   skip_classes esetén kerül a scriptbe)
  TOTAL: <n>     - azonosított szellemeszközök száma
  RM: <név>      - törlési próbálkozás indul
  OK: <név>      - sikeres törlés
  FAIL: <név>    - sikertelen törlés (jellemzően védett eszköz)
  DONE: <szöveg> - összegző sor
"""

# === AUTO-IMPORTS ===
import re
# === /AUTO-IMPORTS ===


def build_ghost_ps(skip_classes=None):
    """A szellemeszköz-törlő PowerShell script összeállítása.

    skip_classes: opcionális PNPClass-halmaz (pl. AUTOFIX_PRINTER_SKIP_CLASSES),
    amelyek szellemeszközei NEM törlődnek - az AutoFix nyomtató-védelme használja.
    A skip_classes mindig hardcodeolt konstansból jön (nem felhasználói inputból),
    ezért biztonságos a scriptbe fűzni."""
    skip_classes = skip_classes or set()
    extra_exclusions = ''.join(f" -and $_.PNPClass -ne '{c}'" for c in sorted(skip_classes))
    if skip_classes:
        skip_match = ' -or '.join(f"$_.PNPClass -eq '{c}'" for c in sorted(skip_classes))
        skipped_block = (
            '$skippedGhosts = @(Get-PnpDevice -PresentOnly:$false | Where-Object { '
            '$_.Present -eq $false -and $_.InstanceId -ne $null -and (' + skip_match + ') }).Count\n'
            'Write-Output "SKIPPED: $skippedGhosts"\n'
        )
    else:
        skipped_block = ''
    return (
        '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n'
        + skipped_block +
        '$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { '
        "$_.Present -eq $false -and $_.InstanceId -ne $null -and $_.PNPClass -ne 'SoftwareDevice' "
        "-and $_.PNPClass -ne 'Net' -and $_.PNPClass -ne 'System'" + extra_exclusions + ' }\n'
        '$count = 0\n'
        '$total = @($ghosts).Count\n'
        'if ($total -eq 0) {\n'
        '    Write-Output "DONE: Nincs szellemeszköz a rendszerben."\n'
        '    exit\n'
        '}\n'
        'Write-Output "TOTAL: $total"\n'
        'foreach ($dev in $ghosts) {\n'
        '    $id = $dev.PNPDeviceID\n'
        '    $name = $dev.Name\n'
        '    if (-not $name) { $name = "Ismeretlen eszköz" }\n'
        '    Write-Output "RM: $name"\n'
        '    $res = & pnputil /remove-device "$($id)" 2>&1\n'
        '    if ($LASTEXITCODE -eq 0 -or $res -match "deleted" -or $res -match "törölve" -or $res -match "successfully") {\n'
        '        Write-Output "OK: $name"\n'
        '        $count++\n'
        '    } else {\n'
        '        Write-Output "FAIL: $name"\n'
        '    }\n'
        '}\n'
        'Write-Output "DONE: Törölve: $count / $total"\n'
    )


def parse_ghost_line(line):
    """Egy script-kimeneti sor -> (esemény, adat) pár, vagy None (üres/ismeretlen sor).

    Események: ('skipped', int), ('total', int), ('rm', név), ('ok', név),
    ('fail', név), ('done', szöveg), ('other', nyers sor)."""
    line = line.strip()
    if not line:
        return None
    if line.startswith("SKIPPED:"):
        m = re.search(r'SKIPPED:\s*(\d+)', line)
        return ('skipped', int(m.group(1))) if m else None
    if line.startswith("TOTAL:"):
        m = re.search(r'TOTAL:\s*(\d+)', line)
        return ('total', int(m.group(1))) if m else None
    if line.startswith("RM:"):
        return ('rm', line[3:].strip())
    if line.startswith("OK:"):
        return ('ok', line[3:].strip())
    if line.startswith("FAIL:"):
        return ('fail', line[5:].strip())
    if line.startswith("DONE:"):
        return ('done', line[5:].strip())
    return ('other', line)
