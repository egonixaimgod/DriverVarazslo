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
  TIMEOUT: <név> - a törlés beragadt, GHOST_REMOVE_TIMEOUT után kilőttük
  DONE: <szöveg> - összegző sor
"""

# === AUTO-IMPORTS ===
import re
# === /AUTO-IMPORTS ===

# EGY beragadt eszköz nem viheti el a teljes törlési fázist.
#
# MÉRVE (2026-09-01, Dell Latitude 5580, Build 288): a 39 szellemeszközből 38 törlése
# MÁSODPERCEK alatt megvolt, egyetlen "Synaptics Pointing Device" viszont
# 14:30:31 -> 14:35:26 = **4 perc 55 mp**-ig blokkolt, majd sikertelenül tért vissza.
# A script sorosan megy, tehát ez a teljes fázist 6 percre nyújtotta - a lánc egyik
# legnagyobb egyedi időrablója, nulla haszonnal (az eszköz úgysem törlődött).
#
# Ugyanaz a jelenség, mint a wedged-PnP delete-nél: a Windows belső query-remove
# időkorlátja ~143 mp, és az azt követő újrapróbálkozások adják ki a ~5 percet.
# Az érték ezért - a DELETE_DRIVER_TIMEOUT-tal egyezően - JÓVAL 143 alatt van, különben
# soha nem sülne el. Egy szellemeszköz törlése egészséges esetben tizedmásodperc, tehát
# 60 mp bőven biztonságos: ami ennyi alatt nem ment, az be van ragadva.
GHOST_REMOVE_TIMEOUT = 60


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
        # A kimenet FÁJLBA megy, nem a mi csővezetékünkbe: egyrészt a pnputil szövege
        # összekeveredne a fenti sor-protokollal, másrészt a Start-Process csak így tud
        # időkorlátot kapni (a `&` operátornak nincs ilyenje). Két külön fájl kell - a
        # Start-Process visszautasítja, ha a stdout és a stderr ugyanoda menne.
        '$dvOut = [System.IO.Path]::GetTempFileName()\n'
        '$dvErr = [System.IO.Path]::GetTempFileName()\n'
        'foreach ($dev in $ghosts) {\n'
        '    $id = $dev.PNPDeviceID\n'
        '    $name = $dev.Name\n'
        '    if (-not $name) { $name = "Ismeretlen eszköz" }\n'
        '    Write-Output "RM: $name"\n'
        '    $code = 1\n'
        '    $res = ""\n'
        '    $stuck = $false\n'
        '    try {\n'
        "        $argList = @('/remove-device', ('\"' + $id + '\"'))\n"
        "        $p = Start-Process -FilePath 'pnputil.exe' -ArgumentList $argList -NoNewWindow "
        '-PassThru -RedirectStandardOutput $dvOut -RedirectStandardError $dvErr\n'
        # A .Handle megfogása KÖTELEZŐ, és nem "óvatosságból" van itt - MÉRVE:
        # -PassThru mellett a .NET csak akkor gyorsítótárazza a kilépési kódot, ha a
        # handle nyitva volt; e sor nélkül az $p.ExitCode a kilépés után $null, a
        # '$code -eq 0' feltétel tehát MINDIG hamis, és a program MINDEN sikeres
        # törlést FAIL-ként jelentene. Pontosan az a néma hamis eredmény, amiből ez a
        # projekt már eleget látott (0xC0000142, a magyar pnputil "törlése nem sikerült").
        '        $null = $p.Handle\n'
        f'        if ($p.WaitForExit({GHOST_REMOVE_TIMEOUT * 1000})) {{\n'
        '            $code = $p.ExitCode\n'
        '            $res = (Get-Content $dvOut -Raw -ErrorAction SilentlyContinue)\n'
        '        } else {\n'
        '            $stuck = $true\n'
        '            try { $p.Kill() } catch { }\n'
        '        }\n'
        '        try { $p.Dispose() } catch { }\n'
        '    } catch { $code = 1 }\n'
        '    if ($stuck) {\n'
        '        Write-Output "TIMEOUT: $name"\n'
        # A 3010 (újraindítás kell) is SIKER: a magyar szövegben se 'deleted', se 'successfully'
        # nincs, ezért a kód nélkül egy sikeres eltávolítás FAIL-nek látszott.
        '    } elseif ($code -eq 0 -or $code -eq 3010 -or $res -match "deleted" -or $res -match "törölve" -or $res -match "successfully") {\n'
        '        Write-Output "OK: $name"\n'
        '        $count++\n'
        '    } else {\n'
        '        Write-Output "FAIL: $name"\n'
        '    }\n'
        '}\n'
        # Ügyfélgépen nem hagyunk szemetet. A rövid várakozás azért kell, mert egy
        # KILŐTT folyamat átirányítás-handle-je nem szabadul fel azonnal (mérve: a
        # törlés csak a timeout-ág után hasalt el) - ez az egyetlen eset, ahol a fájl
        # bent maradhatna. Ha mégsem sikerül, az néma marad: pár bájtos temp fájl a
        # Windows saját temp mappájában, nem indok a törlési fázis elhasalására.
        'Start-Sleep -Milliseconds 300\n'
        'Remove-Item $dvOut, $dvErr -Force -ErrorAction SilentlyContinue\n'
        'Write-Output "DONE: Törölve: $count / $total"\n'
    )


def parse_ghost_line(line):
    """Egy script-kimeneti sor -> (esemény, adat) pár, vagy None (üres/ismeretlen sor).

    Események: ('skipped', int), ('total', int), ('rm', név), ('ok', név),
    ('fail', név), ('timeout', név), ('done', szöveg), ('other', nyers sor)."""
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
    if line.startswith("TIMEOUT:"):
        return ('timeout', line[8:].strip())
    if line.startswith("DONE:"):
        return ('done', line[5:].strip())
    return ('other', line)
