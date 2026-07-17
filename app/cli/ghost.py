"""DriverVarázsló CLI - CLI: szellemeszközök törlése."""

# === AUTO-IMPORTS ===
import re
# === /AUTO-IMPORTS ===


class CliGhostMixin:
    """CLI: szellemeszközök törlése. A CliApi része (összerakás: app/cli/api.py)."""

    def delete_ghost_devices(self):
        """Nem csatlakoztatott (szellem) eszközök törlése (a GUI verzió CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!")
            return

        print("\n👻 Szellemeszközök keresése és törlése...")
        print("-" * 50)

        ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { $_.Present -eq $false -and $_.InstanceId -ne $null -and $_.PNPClass -ne 'SoftwareDevice' -and $_.PNPClass -ne 'Net' -and $_.PNPClass -ne 'System' }
$count = 0
$total = @($ghosts).Count
if ($total -eq 0) {
    Write-Output "DONE: Nincs szellemeszköz a rendszerben."
    exit
}
Write-Output "TOTAL: $total"
foreach ($dev in $ghosts) {
    $id = $dev.PNPDeviceID
    $name = $dev.Name
    if (-not $name) { $name = "Ismeretlen eszköz" }
    Write-Output "RM: $name"
    $res = & pnputil /remove-device "$($id)" 2>&1
    if ($LASTEXITCODE -eq 0 -or $res -match "deleted" -or $res -match "törölve" -or $res -match "successfully") {
        Write-Output "OK: $name"
        $count++
    } else {
        Write-Output "FAIL: $name"
    }
}
Write-Output "DONE: Törölve: $count / $total"
"""
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
        success = 0
        total = 0
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("TOTAL:"):
                m = re.search(r'TOTAL:\s*(\d+)', line)
                if m:
                    total = int(m.group(1))
                print(f"Összesen {total} db szellemeszköz azonosítva...")
            elif line.startswith("RM:"):
                print(f"  🗑 Próbálkozás: {line[3:].strip()}...", end=" ", flush=True)
            elif line.startswith("OK:"):
                success += 1
                print("✅")
            elif line.startswith("FAIL:"):
                print("❌ (valószínűleg védett eszköz)")
            elif line.startswith("DONE:"):
                print(line[5:].strip())

        print("-" * 50)
        print(f"✅ Szellemeszközök törlése kész! Törölve: {success} / {total}")
        return success, total
