"""BitLocker állapot-lekérdezés + kikapcsolás - KÖZÖS mag (GUI nézet + CLI menüpont).

A státusz-szöveg magyar, és a szín-hozzárendelés a magyar kulcsszavakra illeszt
(lásd CLAUDE.md) - a szövegek átfogalmazása némán elrontaná a badge-színeket,
ezért mindkét felület EZT az egy példányt használja. Csak élő rendszeren fut."""

# === AUTO-IMPORTS ===
import logging
# === /AUTO-IMPORTS ===


BITLOCKER_STATUS_PS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$vol = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction SilentlyContinue
if (-not $vol) { Write-Output "Ismeretlen"; exit }
$ps = $vol.ProtectionStatus
$vs = $vol.VolumeStatus
$pct = $vol.EncryptionPercentage
if ($ps -eq 'On' -or $vs -eq 'FullyEncrypted') {
    Write-Output "Titkosítva (Aktív)"
} elseif ($vs -eq 'EncryptionInProgress') {
    Write-Output "Titkosítás folyamatban ($pct%)"
} elseif ($vs -eq 'DecryptionInProgress') {
    Write-Output "Dekódolás folyamatban ($pct%)"
} elseif ($ps -eq 'Off' -or $vs -eq 'FullyDecrypted') {
    Write-Output "Nincs titkosítva (Kikapcsolva)"
} else {
    Write-Output "Állapot: $vs ($pct%)"
}
"""


def get_bitlocker_status(run):
    """A rendszermeghajtó BitLocker állapota: {'status': magyar szöveg, 'color': kulcs}."""
    try:
        res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", BITLOCKER_STATUS_PS], encoding='utf-8')
        status_text = (res.stdout or '').strip()

        color = 'unknown'
        if 'Titkosítva' in status_text:
            color = 'enabled'
        elif 'Dekódolás folyamatban' in status_text:
            color = 'warning'
        elif 'Nincs titkosítva' in status_text:
            color = 'disabled'

        return {'status': status_text, 'color': color}
    except Exception as e:
        logging.error(f"[BITLOCKER] Status hiba: {e}")
        return {'status': f'Hiba: {e}', 'color': 'unknown'}


def disable_bitlocker(run):
    """Disable-BitLocker a rendszermeghajtón (háttérben induló dekódolás).
    Visszatérés: (sikerült-e a parancs, stderr hibaszöveg)."""
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
               r"Disable-BitLocker -MountPoint $env:SystemDrive"])
    return res.returncode == 0, (res.stderr or '')
