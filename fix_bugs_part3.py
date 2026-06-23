import re
import os

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Zombi Folyamatok leállítása (atexit handler a fájl elején a logging után)
old_atexit = '''    def thread_exception_handler(args):'''
new_atexit = '''    def cleanup_zombies():
        try:
            pid = os.getpid()
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
    import atexit
    atexit.register(cleanup_zombies)

    def thread_exception_handler(args):'''
text = text.replace(old_atexit, new_atexit)

# 2. WinPE Temp mappa áthelyezése
old_temp_wim = '''            sys_temp = os.environ.get('SystemDrive', 'C:') + '\\\\DV_Temp'
            mount_dir = os.path.join(sys_temp, f"WIM_Mount_Temp_{int(time.time())}")'''
new_temp_wim = '''            is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
            if is_pe and self.target_os_path:
                sys_temp = os.path.join(self.target_os_path, 'DV_Temp')
            else:
                sys_temp = os.environ.get('SystemDrive', 'C:') + '\\\\DV_Temp'
            mount_dir = os.path.join(sys_temp, f"WIM_{int(time.time())}")'''
text = text.replace(old_temp_wim, new_temp_wim)

old_temp_wu = '''            temp_dir = os.path.join(os.environ.get('SystemDrive', 'C:'), 'DV_Temp', 'wu')'''
new_temp_wu = '''            is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
            if is_pe and self.target_os_path:
                temp_dir = os.path.join(self.target_os_path, 'DV_Temp', 'wu')
            else:
                temp_dir = os.path.join(os.environ.get('SystemDrive', 'C:'), 'DV_Temp', 'wu')'''
text = text.replace(old_temp_wu, new_temp_wu)

# 3. /ForceUnsigned kivétele
text = text.replace("'/Add-Driver', f'/Driver:{ext_path}', '/Recurse', '/ForceUnsigned'", "'/Add-Driver', f'/Driver:{ext_path}', '/Recurse'")
text = text.replace("'/Add-Driver', f'/Driver:{driver_path}', '/Recurse', '/ForceUnsigned', f'/ScratchDir:{scratch}'", "'/Add-Driver', f'/Driver:{driver_path}', '/Recurse', f'/ScratchDir:{scratch}'")

# 4. AutoFix és scan sleep növelése
text = text.replace("self._run(['pnputil', '/scan-devices'])\n            time.sleep(3)", "self._run(['pnputil', '/scan-devices'])\n            time.sleep(10)")
text = text.replace("self._run(['pnputil', '/scan-devices'])\n                    time.sleep(3.5)", "self._run(['pnputil', '/scan-devices'])\n                    time.sleep(10)")
text = text.replace("self._run(['pnputil', '/scan-devices'])\n                time.sleep(3)", "self._run(['pnputil', '/scan-devices'])\n                time.sleep(10)")

# 5. Ghost Devices
old_ghost = "$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { $_.Present -eq $false -and $_.InstanceId -ne $null }"
new_ghost = "$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { $_.Present -eq $false -and $_.InstanceId -ne $null -and $_.PNPClass -ne 'SoftwareDevice' -and $_.PNPClass -ne 'Net' -and $_.PNPClass -ne 'System' }"
text = text.replace(old_ghost, new_ghost)

# 6. Temp fájlok agresszívabb takarítása
old_rmtree_wu = r'''            finally:
                logging.debug(f"[CATALOG_INSTALL] Temp dir törlése: {temp_dir}")
                shutil.rmtree(temp_dir, ignore_errors=True)'''
new_rmtree_wu = r'''            finally:
                logging.debug(f"[CATALOG_INSTALL] Temp dir törlése: {temp_dir}")
                for _ in range(3):
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=False)
                        break
                    except Exception:
                        time.sleep(2)
                shutil.rmtree(temp_dir, ignore_errors=True)'''
text = text.replace(old_rmtree_wu, new_rmtree_wu)

old_rmtree_wim = r'''                self._run(["dism", "/Cleanup-Wim"])
                shutil.rmtree(mount_dir, ignore_errors=True)
                if wim.lower().endswith('.esd') and 'wim_to_mount' in locals() and os.path.exists(wim_to_mount):
                    os.remove(wim_to_mount)'''
new_rmtree_wim = r'''                self._run(["dism", "/Cleanup-Wim"])
                for _ in range(3):
                    try:
                        shutil.rmtree(mount_dir, ignore_errors=False)
                        break
                    except Exception:
                        time.sleep(2)
                shutil.rmtree(mount_dir, ignore_errors=True)
                if wim.lower().endswith('.esd') and 'wim_to_mount' in locals() and os.path.exists(wim_to_mount):
                    try: os.remove(wim_to_mount)
                    except Exception: pass'''
text = text.replace(old_rmtree_wim, new_rmtree_wim)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)
