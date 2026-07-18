"""Rendszer Riport (HTML hardver-adatlap S.M.A.R.T. adatokkal) - KÖZÖS mag (GUI + CLI).

A teljes adatgyűjtés (smartctl, akkumulátor, WMI/registry) és a HTML-generálás EGY
példányban itt él. A smartctl a stresstools.zip-ből jön: a GUI le is tölti
(_download_stresstools), a CLI csak a már meglévő kicsomagolt példányt használja
(find_existing_smartctl) - ha nincs, a riport S.M.A.R.T. szekció nélkül készül el.

Fontos, terepen bizonyított szabályok (lásd CLAUDE.md):
  - GPU VRAM: a Win32_VideoController.AdapterRAM 32 bites, 4 GB fölött hamis -
    a registry HardwareInformation.qwMemorySize-t olvassuk (GPU-Z technika);
  - a <style> -webkit-print-color-adjust: exact nélkül nyomtatáskor eltűnnek a
    háttérszínek;
  - a megjegyzés-sorok border-bottom-os div-ek (NEM repeating-linear-gradient);
  - az egyoldalas zsugorítás `zoom`-mal megy, NEM transform: scale()-lel.
"""

# === AUTO-IMPORTS ===
import os
import time
import logging
import json
import tempfile
from html import escape as html_escape
from app.common import _app_data_dir
from datetime import datetime
# === /AUTO-IMPORTS ===


def find_smartctl(stress_dir):
    """A smartctl.exe megkeresése a kicsomagolt stresstools mappában (vagy None)."""
    if not stress_dir:
        return None
    for root, dirs, files in os.walk(stress_dir):
        for f in files:
            if f.lower() == "smartctl.exe":
                return os.path.join(root, f)
    return None


def find_existing_stresstools_dir():
    """A már kicsomagolt stresstools mappa (ha van kész marker) - letöltés NÉLKÜL.
    Ugyanaz az útvonal-logika, mint a GUI _download_stresstools-ában (WinPE-ben
    C:\\DV_Temp, különben %TEMP%)."""
    is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
    temp_dir = r'C:\DV_Temp' if is_pe else tempfile.gettempdir()
    stress_dir = os.path.join(temp_dir, "DriverVarázsló_Stress")
    if os.path.exists(os.path.join(stress_dir, ".extract_complete")):
        return stress_dir
    return None


def find_existing_smartctl():
    """smartctl.exe a már meglévő stresstools-ból, letöltés nélkül (vagy None)."""
    return find_smartctl(find_existing_stresstools_dir())


def _collect_smart_data(run, smartctl_exe):
    """S.M.A.R.T. adatok begyűjtése smartctl-lel. Üres lista, ha nincs smartctl vagy
    nem talált lemezt."""
    smart_data = []
    if not smartctl_exe:
        return smart_data

    # Friss letöltés/kicsomagolás után a Windows Defender (felhős ellenőrzés) néha
    # pár másodpercig blokkolja vagy üresen futtatja az új exe-t, ezért pár próbálkozást
    # engedünk, mielőtt feladnánk - lassú gépen/neten ez korábban "nem talált semmit" hibát adott.
    devices = []
    max_scan_attempts = 3
    for scan_attempt in range(1, max_scan_attempts + 1):
        try:
            logging.info(f"[REPORT] smartctl --scan futtatása... (próba {scan_attempt}/{max_scan_attempts})")
            scan_res = run([smartctl_exe, "--scan", "-j"], encoding='utf-8')
            scan_data = json.loads(scan_res.stdout) if scan_res.stdout.strip() else {}
            devices = scan_data.get("devices", [])
        except Exception as e:
            logging.error(f"smartctl scan hiba (próba {scan_attempt}/{max_scan_attempts}): {e}")
            devices = []

        if devices:
            break
        if scan_attempt < max_scan_attempts:
            logging.warning("[REPORT] smartctl nem talált devices tömböt, újrapróbálás 2s múlva...")
            time.sleep(2)

    if not devices:
        logging.warning("[REPORT] smartctl nem talált devices tömböt a kimenetben (több próbálkozás után sem)!")
        return smart_data

    try:
        logging.info(f"[REPORT] smartctl talált eszközök száma: {len(devices)}")
        seen_serials = set()
        for dev in devices:
            dev_name = dev.get("name")
            dev_scan_type = dev.get("type", "")
            if dev_name:
                logging.info(f"[REPORT] Adatok lekérése: {dev_name} (type={dev_scan_type or '?'})")
                info_data = {}
                for info_attempt in range(1, 3):
                    info_cmd = [smartctl_exe]
                    if dev_scan_type:
                        info_cmd += ["-d", dev_scan_type]
                    info_cmd += ["-a", dev_name, "-j"]
                    try:
                        info_res = run(info_cmd, encoding='utf-8')
                        info_data = json.loads(info_res.stdout) if info_res.stdout.strip() else {}
                    except Exception as e:
                        logging.error(f"smartctl -a hiba ({dev_name}, próba {info_attempt}/2): {e}")
                        info_data = {}
                    if info_data:
                        break
                    if info_attempt < 2:
                        time.sleep(2)

                serial = info_data.get("serial_number", "")
                if serial and serial in seen_serials:
                    logging.info(f"[REPORT] Duplikált lemez átugrása (serial: {serial})")
                    continue
                if serial:
                    seen_serials.add(serial)

                model = info_data.get("model_name", "Ismeretlen Meghajtó")

                size_gb = "?"
                user_cap = info_data.get("user_capacity", {}).get("bytes", 0)
                if user_cap:
                    size_gb = f"{round(user_cap / (1024**3), 1)} GB"

                dev_type = info_data.get("device", {}).get("protocol", "Unspecified")
                rotation = info_data.get("rotation_rate", 0)

                if dev_type.lower() == "nvme":
                    dev_type_str = "NVMe SSD"
                elif dev_type.lower() == "ata":
                    if rotation == 0:
                        dev_type_str = "SATA SSD"
                    else:
                        dev_type_str = f"SATA HDD ({rotation} RPM)"
                else:
                    dev_type_str = dev_type.upper()

                hours = "?"
                power_on = info_data.get("power_on_time", {}).get("hours")
                if power_on is not None:
                    try:
                        p_hours = int(power_on)
                        d = p_hours // 24
                        h = p_hours % 24
                        if d > 0:
                            if h > 0:
                                hours = f"{d} nap {h} óra"
                            else:
                                hours = f"{d} nap"
                        else:
                            hours = f"{h} óra"
                    except Exception:
                        hours = f"{power_on} óra"

                temp = "?"
                temperature = info_data.get("temperature", {}).get("current")
                if temperature is not None:
                    temp = f"{temperature} °C"

                health_txt = "Ismeretlen"
                health = "-1"
                perf = "100%"

                if dev_type.lower() == "nvme":
                    used = info_data.get("nvme_smart_health_information_log", {}).get("percentage_used")
                    if used is not None:
                        health_pct = 100 - used
                        health = f"{health_pct}%"
                        health_txt = f"{health_pct}%"
                else:
                    status = info_data.get("smart_status", {}).get("passed")
                    if status is True:
                        health_txt = "100%"
                        health = "100%"
                    elif status is False:
                        health_txt = "Hibás (SMART Fail)"
                        health = "0%"
                        perf = "0%"

                    endurance = info_data.get("endurance_used", {}).get("current_percent")
                    if endurance is not None:
                        health_pct = 100 - endurance
                        health = f"{health_pct}%"
                        health_txt = f"{health_pct}%"
                    else:
                        attrs = info_data.get("ata_smart_attributes", {}).get("table", [])
                        for attr in attrs:
                            if attr.get("name") == "SSD_Life_Left" or attr.get("id") == 231 or attr.get("name") == "Media_Wearout_Indicator" or attr.get("id") == 233:
                                val = attr.get("value")
                                if val is not None:
                                    health = f"{val}%"
                                    health_txt = f"{val}%"
                                    break

                logging.info(f"[REPORT] Lemez: {model} | Méret: {size_gb} | Típus: {dev_type_str} | Health: {health_txt} | Temp: {temp}")
                smart_data.append({
                    "Name": model.strip(),
                    "Size": size_gb,
                    "Health": health_txt,
                    "Performance": perf,
                    "Hours": str(hours),
                    "Temp": str(temp),
                    "Type": dev_type_str,
                    "RawHealth": health.replace('%', '').strip() if health != "-1" else "-1"
                })
    except Exception as e:
        logging.error(f"smartctl futtatási hiba: {e}")
    return smart_data


def generate_system_report(run, smartctl_exe=None, note=None):
    """A teljes HTML rendszer-riport generálása. Visszatérés: a mentett fájl útvonala
    (az _app_data_dir()-ben); hibánál kivételt dob."""
    smart_data = _collect_smart_data(run, smartctl_exe)

    # Akkumulátor információk
    batt_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$tempXML = "$env:TEMP\batt_$(Get-Random).xml"
try {
    powercfg /batteryreport /xml /output $tempXML | Out-Null
    [xml]$batt = Get-Content $tempXML -ErrorAction Stop
    if ($batt.BatteryReport.Batteries.Battery) {
        $b = $batt.BatteryReport.Batteries.Battery
        if ($b -is [array]) { $b = $b[0] }
        $des = $b.DesignCapacity
        $full = $b.FullChargeCapacity
        $name = $b.Id
        @{Name=$name; Design=$des; Full=$full} | ConvertTo-Json -Compress
    }
} catch {}
finally {
    if (Test-Path $tempXML) { Remove-Item $tempXML -Force -ErrorAction SilentlyContinue }
}
"""
    res_batt = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", batt_script], encoding='utf-8')
    batt_data = {}
    if res_batt.stdout.strip():
        try:
            batt_data = json.loads(res_batt.stdout.strip())
        except Exception as e:
            logging.debug(f"[REPORT] Akkumulátor JSON értelmezési hiba (akku-szekció kimarad): {e}")

    # Alapvető WMI hardver adatok
    ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$data = @{}
try { $data.CS = Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model | ConvertTo-Json -Compress } catch {}
try { $data.BB = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product | ConvertTo-Json -Compress } catch {}
try { $data.CPU = Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors | ConvertTo-Json -Compress } catch {}
try { $data.RAM = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity, Speed, Manufacturer, PartNumber) | ConvertTo-Json -Compress } catch {}
try { $data.RAMTotal = Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory | ConvertTo-Json -Compress } catch {}
try {
    # Win32_VideoController.AdapterRAM egy 32 bites (UInt32) WMI mezo, ami max. kb. 4 GB-ot
    # tud kifejezni - 4 GB-nal tobb VRAM eseten (pl. egy 12 GB-os RTX 3060-nal) a legtobb
    # modern driver (kulonosen NVIDIA) emiatt egy 0xFFFFFFFF "tullepes" ertekkel ter vissza,
    # ami byte-ban ertelmezve pont ~4.0 GB-nak nez ki - EZ HAMIS, nem a tenyleges VRAM meret.
    # A valodi (64 bites) ertek a registry-ben van, ugyanott, ahonnan a GPU-Z/HWiNFO is
    # kiolvassa: a video-adapter osztaly (Class GUID) alatti szamozott almappak
    # HardwareInformation.qwMemorySize erteke - a megfelelo almappat a MatchingDeviceId
    # (PNPDeviceID elotag) alapjan azonositjuk.
    $gpus = Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, PNPDeviceID
    $result = @()
    $regBase = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    foreach ($gpu in $gpus) {
        $vram = [int64]0
        if ($gpu.AdapterRAM) { $vram = [int64]$gpu.AdapterRAM }
        if (Test-Path $regBase) {
            $matched = $null
            Get-ChildItem $regBase -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^\d{4}$' } | ForEach-Object {
                if (-not $matched) {
                    $props = Get-ItemProperty -Path $_.PSPath -ErrorAction SilentlyContinue
                    if ($props.MatchingDeviceId -and $gpu.PNPDeviceID -and $gpu.PNPDeviceID.ToLower().StartsWith($props.MatchingDeviceId.ToLower())) {
                        if ($props.'HardwareInformation.qwMemorySize') {
                            $matched = [int64]$props.'HardwareInformation.qwMemorySize'
                        }
                    }
                }
            }
            if ($matched -and $matched -gt 0) { $vram = $matched }
        }
        $result += [PSCustomObject]@{ Name = $gpu.Name; AdapterRAM = $vram }
    }
    $data.GPU = (@($result) | ConvertTo-Json -Compress)
} catch {}
try { $data.OS = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, OSArchitecture | ConvertTo-Json -Compress } catch {}
ConvertTo-Json $data
"""
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
    raw_data = json.loads(res.stdout.strip())

    def safe_json(k):
        try:
            return json.loads(raw_data.get(k, "{}")) if raw_data.get(k) else {}
        except Exception:
            return {}

    def safe_json_list(k):
        try:
            parsed = json.loads(raw_data.get(k, "[]")) if raw_data.get(k) else []
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            return []

    cs = safe_json("CS")
    bb = safe_json("BB")
    man = (cs.get("Manufacturer") or "").strip()
    mod = (cs.get("Model") or "").strip()
    oem_junk = {"to be filled by o.e.m.", "default string", "system manufacturer", "system product name", "not applicable", ""}
    if man.lower() in oem_junk:
        man = (bb.get("Manufacturer") or "").strip()
    if mod.lower() in oem_junk:
        mod = (bb.get("Product") or "").strip()
    if man.lower() in oem_junk:
        man = "Ismeretlen gyártó"
    if mod.lower() in oem_junk:
        mod = "Ismeretlen modell"
    pc_model = f"{man} - {mod}"

    cpu = safe_json("CPU")
    if isinstance(cpu, list) and len(cpu) > 0:
        cpu = cpu[0]
    ram_list = safe_json_list("RAM")
    gpu_list = safe_json_list("GPU")
    os_info = safe_json("OS")

    # HTML Generator (Kompakt A4 méretre optimalizálva, 2 oszlopos grid)
    # g(): WMI/CIM néha a kulcsot megtartja null értékkel ("None" jelenne meg helyette);
    # e(): minden hardver-/firmware-eredetű string HTML-escape-elve kerül a riportba,
    # nehogy egy '<'/'>'/'&'-et tartalmazó modell-/gyártónév eltörje a HTML-t.
    def g(d, key, default='?'):
        v = d.get(key, default)
        return default if v is None else v

    def e(value):
        return html_escape(str(value))

    html = f"""<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8">
<style>
@page {{ size: A4; margin: 15mm; }}
/* Nyomtatáskor a böngészők alapból eldobják a háttérszíneket (tintaspórolás) - enélkül a
sötét "Gyors Összefoglaló" doboz és a színes badge-ek is simán fehérré válnának nyomtatva/
PDF-be mentve, pedig pont a kontraszt a lényegük. */
* {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; color-adjust: exact !important; }}
body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #fff; color: #333; margin: 0 auto; padding: 20px; font-size: 13px; overflow-x: hidden; max-width: 180mm; box-sizing: border-box; }}
h1 {{ color: #46286e; border-bottom: 2px solid #d488ff; padding-bottom: 5px; font-size: 24px; margin-bottom: 5px; }}
.subtitle {{ color: #666; font-size: 13px; margin-top: 0; margin-bottom: 20px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; align-items: start; }}
.section {{ background: #f9f6ff; padding: 10px 14px; border-radius: 6px; border-left: 4px solid #b855ff; margin-bottom: 10px; page-break-inside: avoid; break-inside: avoid; }}
.item-block {{ page-break-inside: avoid; break-inside: avoid; }}
.section h2 {{ margin-top: 0; color: #46286e; font-size: 16px; margin-bottom: 10px; border-bottom: 1px solid #e0d8f0; padding-bottom: 4px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th, td {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #e0d8f0; }}
th {{ background: #eee8f8; color: #46286e; width: 35%; font-weight: 600; }}
.item-title {{ font-weight: bold; color: #46286e; margin-top: 8px; margin-bottom: 4px; font-size: 13px; }}
.badge {{ display: inline-block; padding: 2px 6px; border-radius: 8px; font-size: 11px; font-weight: bold; background: #e0d8f0; color: #46286e; margin-left: 5px; }}
.health-Healthy {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
.health-Warning {{ background: #fff3cd; color: #856404; border: 1px solid #ffeeba; }}
.health-Unhealthy {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
.section.summary-box {{ background: #2b2b30; border: 2px solid #111; border-left: 4px solid #6b6b74; color: #eaeaea; box-shadow: 0 4px 16px rgba(0,0,0,0.3); }}
.section.summary-box h2 {{ color: #fff; border-bottom: 1px solid #55555c; }}
.summary-list {{ list-style: none; margin: 0; padding: 0; }}
.summary-list li {{ padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.08); font-size: 12.5px; }}
.summary-list li:last-child {{ border-bottom: none; }}
.summary-list b {{ color: #d488ff; font-weight: 600; }}
.summary-list .si {{ display: inline-block; width: 22px; }}
.note-section {{ margin-top: 4px; padding: 10px 14px; border: 1px dashed #999; border-radius: 6px; background: #fafafa; page-break-inside: avoid; }}
.note-section h2 {{ margin: 0 0 6px 0; color: #46286e; font-size: 14px; }}
.note-content {{ font-family: 'Roboto', 'Segoe UI', -apple-system, sans-serif; font-weight: 500; font-size: 19px; color: #2a2a2a; }}
.note-line {{ min-height: 28px; line-height: 28px; border-bottom: 1px solid #ccc; white-space: pre-wrap; word-break: break-word; }}
</style>
</head>
<body>
<div id="page-ruler" style="position:absolute; visibility:hidden; height:267mm; width:1px; top:0; left:0;"></div>
<div id="report-root">
<h1>DriverVarázsló - Rendszer Adatlap</h1>
<p class="subtitle">Gép típusa: <strong style="color:#000; font-size:14px;">{e(pc_model)}</strong> | Generálva: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

<div class="grid">
    <div class="col">
        <div class="section">
            <h2>💻 Operációs Rendszer</h2>
            <table>
                <tr><th>Verzió</th><td>{e(g(os_info, 'Caption', 'Ismeretlen'))} ({e(g(os_info, 'OSArchitecture', 'Ismeretlen'))})</td></tr>
            </table>
        </div>

        <div class="section">
            <h2>🧠 Processzor (CPU)</h2>
            <table>
                <tr><th>Modell</th><td>{e(g(cpu, 'Name', 'Ismeretlen'))}</td></tr>
                <tr><th>Magok / Szálak</th><td>{e(g(cpu, 'NumberOfCores'))} Mag / {e(g(cpu, 'NumberOfLogicalProcessors'))} Szál</td></tr>
            </table>
        </div>

        <div class="section">
            <h2>🧩 Memória (RAM)</h2>"""

    tot_gb = "Ismeretlen"
    try:
        tot = json.loads(raw_data.get("RAMTotal", "{}")).get('TotalPhysicalMemory')
        if tot:
            tot_gb = f"{round(int(tot)/(1024**3), 1)} GB"
    except Exception as ex:
        logging.debug(f"[REPORT] RAM-összeg értelmezési hiba ('Ismeretlen' marad): {ex}")

    html += f"<p style='margin: 0 0 8px 0;'><strong>Összes fizikai memória:</strong> {tot_gb} ({len(ram_list)} db modul)</p>"
    if ram_list:
        jedec_map = {
            "80AD": "SK Hynix", "80CE": "Samsung", "802C": "Micron",
            "0198": "Kingston", "029E": "Corsair", "04CB": "A-DATA",
            "00CE": "Samsung", "014F": "Transcend", "02FE": "Elpida",
            "0D0B": "Crucial", "0298": "Kingston"
        }
        html += "<table><tr><th>Gyártó / Cikkszám</th><th>Kapacitás</th><th>Sebesség</th></tr>"
        for r in ram_list:
            cap = r.get('Capacity')
            cap_gb = f"{round(int(cap)/(1024**3), 1)} GB" if cap else "?"

            man = str(g(r, 'Manufacturer')).strip()
            if len(man) >= 4 and all(c in '0123456789ABCDEFabcdef' for c in man[:4]):
                hex_pfx = man[:4].upper()
                if hex_pfx in jedec_map:
                    man = jedec_map[hex_pfx]

            man_part = f"{man} {g(r, 'PartNumber')}".strip()
            html += f"<tr><td>{e(man_part)}</td><td>{e(cap_gb)}</td><td>{e(g(r, 'Speed'))} MHz</td></tr>"
        html += "</table>"

    html += """</div>
    </div>

    <div class="col">"""

    # Akku szekció ha van
    if batt_data.get('Design') and batt_data.get('Full'):
        try:
            des = int(batt_data['Design'])
            full = int(batt_data['Full'])
            health_pct = round((full / des) * 100)
            h_class = "health-Healthy" if health_pct > 80 else "health-Warning" if health_pct > 50 else "health-Unhealthy"
            batt_icon = "🔋" if health_pct >= 20 else "🪫"

            html += f"""
        <div class="section">
            <h2>🔋 Akkumulátor Állapot</h2>
            <table>
                <tr><th>Gyári kapacitás</th><td>{des} mWh</td></tr>
                <tr><th>Jelenlegi max.</th><td>{full} mWh</td></tr>
                <tr><th>Egészség</th><td><span style="font-size:18px; vertical-align:middle;">{batt_icon}</span> <span class="badge {h_class}" style="font-size:13px; padding:4px 8px;">{health_pct}%</span></td></tr>
            </table>
        </div>"""
        except Exception as ex:
            logging.debug(f"[REPORT] Akkumulátor-szekció renderelési hiba (kimarad a riportból): {ex}")

    html += """
        <div class="section">
            <h2>🎮 Videókártyák (GPU)</h2>"""

    gpu_summary_list = []
    if not gpu_list:
        html += "<p>Nem található dedikált/integrált videókártya.</p>"
    else:
        gpu_blocks = []
        for gd in gpu_list:
            name = g(gd, 'Name', 'Ismeretlen')
            ram = gd.get('AdapterRAM')
            if ram:
                ram_gb = round(int(ram)/(1024**3), 1)
                ram_gb_str = f"{ram_gb} GB"
                if ("intel" in name.lower() or "amd radeon" in name.lower() or "vega" in name.lower()) and ram_gb <= 2.0:
                    ram_gb_str += " (Megosztott / Dinamikus VRAM)"
            else:
                ram_gb_str = "Ismeretlen"

            gpu_blocks.append(f"<div class='item-block'><table><tr><th>Modell</th><td>{e(name)}</td></tr><tr><th>VRAM</th><td>{e(ram_gb_str)}</td></tr></table></div>")
            gpu_summary_list.append(f"{name} ({ram_gb_str})")
        html += "<br>".join(gpu_blocks)

    html += """</div>
        <div class="section">
            <h2>💾 Háttértárak (S.M.A.R.T. Adatok)</h2>"""

    storage_summary_list = []
    if not smart_data:
        html += "<p>Nem található háttértár információ vagy nem olvasható a S.M.A.R.T.</p>"
    else:
        smart_blocks = []
        for s in smart_data:
            h = g(s, 'Health', 'Ismeretlen')
            p = g(s, 'Performance', '100%')
            h_class = ""
            p_class = ""
            raw_h = g(s, 'RawHealth', '-1')
            try:
                pct = int(raw_h)
                if pct > 80:
                    h_class = "health-Healthy"
                elif pct > 40:
                    h_class = "health-Warning"
                elif pct >= 0:
                    h_class = "health-Unhealthy"

                if pct >= 0:
                    p_class = "health-Healthy" if p == "100%" else "health-Warning"
            except Exception as ex:
                logging.debug(f"[REPORT] S.M.A.R.T. health-érték értelmezési hiba (szín nélkül jelenik meg): {ex}")

            smart_blocks.append(f"""<div class="item-block"><div class="item-title">{e(g(s, 'Name', 'Ismeretlen'))} <span class="badge">{e(g(s, 'Size'))}</span> <span class="badge">{e(g(s, 'Type'))}</span></div>
                <table>
                    <tr><th>Kond. / Telj.</th><td><span class="badge {h_class}">❤️ {e(h)}</span> <span class="badge {p_class}">⚡ {e(p)}</span></td></tr>
                    <tr><th>Üzemidő / Hőm.</th><td>{e(g(s, 'Hours'))} / {e(g(s, 'Temp'))}</td></tr>
                </table></div>""")
            storage_summary_list.append(f"{g(s, 'Name', 'Ismeretlen')} ({g(s, 'Size', '?')}, {g(s, 'Type', '?')})")
        html += "<br>".join(smart_blocks)

    # Gyors összefoglaló doboz - szándékosan sötét/szürke, hogy elüssön a riport
    # világos "papíros" stílusától, mint egy gyors terminál-jellegű kivonat a lap alján.
    summary_rows = [
        ("🖥️", "Alaplap", pc_model),
        ("🧠", "Processzor", g(cpu, 'Name', 'Ismeretlen')),
        ("🎮", "Videokártya", ", ".join(gpu_summary_list) if gpu_summary_list else "Nincs adat"),
        ("🧩", "Memória", f"{tot_gb} ({len(ram_list)} db modul)"),
        ("💾", "Háttértár", ", ".join(storage_summary_list) if storage_summary_list else "Nincs adat"),
        ("🪟", "Operációs rendszer", f"{g(os_info, 'Caption', 'Ismeretlen')} ({g(os_info, 'OSArchitecture', 'Ismeretlen')})"),
    ]
    summary_items = "".join(
        f'<li><span class="si">{icon}</span><b>{e(label)}:</b> {e(value)}</li>'
        for icon, label, value in summary_rows
    )
    html += f"""</div>
        <div class="section summary-box">
            <h2>📋 Gyors Összefoglaló</h2>
            <ul class="summary-list">{summary_items}</ul>
        </div>"""

    html += "\n    </div>\n</div>"

    # Soronként külön <div>, mindegyik saját alsó szegéllyel (border-bottom) - EZ ad
    # egyenletes vastagságú vonalakat nyomtatásban is. Egy repeating-linear-gradient
    # háttérkép helyette a nyomtatási átméretezés (fractional DPI) miatt hol vékonyabb,
    # hol vastagabb vonalat rajzolt ki (böngészőnként/nyomtatásonként eltérő
    # kerekítéssel) - a border-bottom ezzel szemben minden sorban egyformán 1px.
    # Legalább 4 sor mindig látszik (üresen is, ha a megjegyzés rövidebb), hogy legyen
    # hely tollal írni - ha a megjegyzés hosszabb, minden sora megjelenik, plusz még
    # egy üres sor a végén a folytatáshoz.
    note_lines = (note or '').split('\n')
    while len(note_lines) < 4:
        note_lines.append('')
    note_lines.append('')
    note_lines_html = "".join(f'<div class="note-line">{e(line)}</div>' for line in note_lines)

    # Egy oldalra kényszerítő "shrink-to-fit": #page-ruler egy 267mm-es (A4 mínusz a
    # @page 15mm margói) rejtett elem, aminek a lemért px-magassága adja a valódi
    # nyomtatható területet DPI-találgatás nélkül. A root.scrollHeight ehhez képest
    # méri a tényleges tartalmat, és ha túlcsordulna, zsugorítja - de csak 0.75-ös
    # olvashatósági padlóig, az alatt inkább szépen 2. oldalra csúszik (lásd .section/
    # .item-block page-break-inside:avoid), mintsem olvashatatlanná váljon.
    # FONTOS: ez `zoom`-mal megy, NEM `transform: scale()`-lel - élesben tesztelve
    # (Chrome headless --print-to-pdf) kiderült, hogy a transform pusztán vizuális
    # (nem befolyásolja a nyomtatási lapszámítást, ami a transzformáció ELŐTTI
    # magassággal dolgozik), így a tartalom vizuálisan zsugorodna, de nyomtatáskor
    # mégis 2 oldalra törne. A `zoom` Chromium-ban valódi layout-újraszámolást vált ki
    # (a body ezért van rögzítve 180mm szélességre is - így képernyőn és nyomtatáskor
    # ugyanaz a sortördelés, nem téveszti meg a mérést egy szélesebb böngészőablak).
    html += f"""
<div class="note-section">
    <h2>📝 Megjegyzés</h2>
    <div class="note-content">{note_lines_html}</div>
</div>
</div>
<script>
(function() {{
    var root = document.getElementById('report-root');
    var ruler = document.getElementById('page-ruler');
    if (!root || !ruler) return;
    var bodyStyle = getComputedStyle(document.body);
    var vPad = parseFloat(bodyStyle.paddingTop) + parseFloat(bodyStyle.paddingBottom);
    var targetHeight = ruler.getBoundingClientRect().height - vPad;
    var actualHeight = root.scrollHeight;
    var MIN_SCALE = 0.75;
    if (targetHeight > 0 && actualHeight > targetHeight) {{
        var scale = Math.max(MIN_SCALE, targetHeight / actualHeight);
        root.style.zoom = scale;
    }}
}})();
</script>
</body></html>"""

    comp_name = os.environ.get('COMPUTERNAME', 'PC')
    safe_name = f"Rendszer_Riport_{comp_name}"

    # A DriverVarázsló saját adatmappájába mentjük (nem az exe mellé - lásd _app_data_dir).
    final_path = os.path.join(_app_data_dir(), f"{safe_name}.html")

    with open(final_path, "w", encoding="utf-8") as f:
        f.write(html)

    if not os.path.exists(final_path):
        raise Exception("A fájl mentése sikertelen volt!")
    return final_path
