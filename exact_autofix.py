    def run_autofix(self):
        logging.info("[API] run_autofix() indítása")
        if self.target_os_path:
            self.emit('toast', {'message': 'Az 1 kattintásos fix csak az Élő (jelenlegi) rendszeren futtatható le biztonságosan!', 'type': 'error'})
            return

        def worker():
            import ctypes
            import sys
            
            # --- KONZOL ALLOKALASA ---
            try:
                ctypes.windll.kernel32.AllocConsole()
                sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8")
                print("==================================================")
                print(" DRIVERDOKTOR 1-KATTINTASOS FIX (KONZOL)")
                print("==================================================")
                print("FIGYELEM: Ha az ablak kifeheredik a videokartya driver torlesekor,")
                print("a folyamat itt a hatterben zavartalanul tavabb fut!\n")
            except Exception as alloc_e:
                logging.error(f"[AUTOFIX] Konzol hiba: {alloc_e}")

            def c_print(msg, p_type='log', **kwargs):
                # Prints to console
                try:
                    clean_msg = msg.replace('✅', '[OK]').replace('⚠️', '[FIGYELMEZTETES]').replace('🗑', '[TORLES]').replace('🎉', '[KESZ]')
                    print(clean_msg)
                except:
                    pass
                
                # Emits to UI
                kwargs[p_type] = msg
                kwargs['task'] = 'autofix'
                self.emit('task_progress', kwargs)

            self.emit('task_start', {'task': 'autofix', 'title': '1 Kattintásos Driver Javítás és Frissítés'})
            try:
                import datetime
                
                # 1. Rendszer visszaállítása
                desc = "DriverDoktor AutoFix - " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                c_print('[1/4] Registry Mentés (Restore Point) készítése folyamatban...', phase='Registry/Rendszer Mentés', indeterminate=True)
                
                self._run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
                self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
                ps_cmd = f'Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction SilentlyContinue'
                res1 = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
                if res1.returncode == 0:
                    c_print('✅ Registry mentés / Visszaállítási pont elkészült.\n')
                else:
                    c_print('⚠️ Visszaállítási pont elutasítva a rendszer által. (Rendszervédelem talán nincs bekapcsolva a C: meghajtón) - FOLYTATÁS...\n')
                
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")

                # 2. WU Letiltása
                c_print('[2/4] Windows automata driver frissítések letiltása a Registryben...', phase='Windows Update Letiltása', indeterminate=True)
                reg_cmd = ['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f']
                self._run(reg_cmd)
                c_print('✅ Automatikus driver telepítés letiltva.\n')
                
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")

                # 3. Third party driverek törlése
                c_print('[3/4] Third-party driverek összegyűjtése és törlése...', phase='Driverek Eltávolítása', indeterminate=True)
                drivers = self._get_third_party_drivers()
                total = len(drivers)
                if total > 0:
                    c_print(f'{total} db third-party driver eltávolítása...\n')
                    for i, drv in enumerate(drivers):
                        if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")
                        
                        name = drv.get('published', '')
                        if not name: continue
                        
                        c_print(f'🗑 Törlés ({i+1}/{total}): {name}', current=i+1, total=total)
                        self._run(['pnputil', '/delete-driver', name, '/uninstall', '/force'])
                    c_print('✅ Driverek eltávolítva.\n')
                else:
                    c_print('✅ Nincs third-party driver a rendszerben.\n')
                
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")

                # 4. Keresés és visszaépítés
                c_print('[4/4] Új eszközök szkennelése PnP Util-lal...', phase='Új Driverek Keresése', indeterminate=True)
                self._run(['pnputil', '/scan-devices'])
                time.sleep(3)
                
                c_print('Hivatalos driverek keresése és telepítése (Windows Update). Ez percekig is eltarthat...')
                ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $PresentHWIDs = @()
    Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true } | ForEach-Object {
        if ($_.HardwareID) { foreach ($hid in $_.HardwareID) { $PresentHWIDs += $hid.ToUpper() } }
        if ($_.PNPDeviceID) { $PresentHWIDs += $_.PNPDeviceID.ToUpper() }
    }

    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try { $SM = New-Object -ComObject Microsoft.Update.ServiceManager; $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null } catch {}
    $Searcher.ServerSelection = 3
    $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    
    Write-Output "--- KERESÉS ---"
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    
    $ToInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($U in $Result.Updates) {
        $matchFound = $false
        foreach ($hwid in $U.DriverHardwareID) {
            $hUpper = $hwid.ToUpper()
            foreach ($target in $PresentHWIDs) {
                if ($hUpper.Contains($target) -or $target.Contains($hUpper)) { $matchFound = $true; break }
            }
            if ($matchFound) { break }
        }
        if ($matchFound) {
            if (-not $U.EulaAccepted) { $U.AcceptEula() }
            $ToInstall.Add($U) | Out-Null
        } else {
            Write-Output "❌ GHOST ESZKÖZ KIHAGYVA: $($U.Title)"
        }
    }

    if ($ToInstall.Count -eq 0) { Write-Output "✅ Szerveren nincs újabb valós illesztőprogram ehhez a géphez."; exit }
    
    $Count = $ToInstall.Count
    Write-Output "✅ Telepítendő driverek száma: $Count"
    
    Write-Output "--- LETÖLTÉS ---"
    $Downloader = $Session.CreateUpdateDownloader()
    $Downloader.Updates = $ToInstall
    $Downloader.Download() | Out-Null
    
    Write-Output "--- TELEPÍTÉS ---"
    $Installer = $Session.CreateUpdateInstaller()
    
    $s = 0; $f = 0
    for ($i = 0; $i -lt $Count; $i++) {
        $U = $ToInstall.Item($i)
        $Title = $U.Title
        Write-Output "▶ Telepítés alatt: $Title"
        
        $SingleUpdateColl = New-Object -ComObject Microsoft.Update.UpdateColl
        $SingleUpdateColl.Add($U) | Out-Null
        
        $Installer.Updates = $SingleUpdateColl
        try { 
            $IR = $Installer.Install()
            $RC = $IR.ResultCode 
            if ($RC -eq 2 -or $RC -eq 3) {
                Write-Output "  ✅ SIKERES: $Title"
                $s++
            } else {
                Write-Output "  ⚠️ SIKERTELEN: $Title"
                $f++
            }
        } catch {
            Write-Output "  ⚠️ Hiba történt: $Title"
            $f++
        }
    }
    Write-Output "--- RENDES ÖSSZEGZÉS ---"
    Write-Output "Összesen telepítve: $s sikeres, $f hibás."
} catch { Write-Output "⚠️ Nem sikerült a WU szinkronizálása $_" }
"""
                res_wu = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
                for line in res_wu.stdout.splitlines():
                    if line.strip():
                        c_print(line.strip())
                
                c_print('\n🎉 MINDEN LÉPÉS KÉSZ!')
                
                try:
                    print("\n==================================================")
                    print(" A FOLYAMAT SIKERESEN BEFEJEZŐDÖTT!")
                    print("==================================================")
                    
                    import os
                    for cd in range(20, 0, -1):
                        sys.stdout.write(f"\rA gep {cd} masodperc mulva ujraindul... (Megszakitashoz zard be ezt az ablakot)")
                        sys.stdout.flush()
                        time.sleep(1)
                    print("\nUjrainditas inditasa...")
                    
                    os.system("shutdown /r /t 0 /f")
                    ctypes.windll.kernel32.FreeConsole()
                except:
                    pass

                self.emit('task_complete', {'task': 'autofix', 'status': 'Befejezve (Újraindítás Vár)'})
                time.sleep(1)
                self.emit('ask_reboot', None)

            except Exception as e:
                if str(e) == "Magyar_Megszakit_Flag":
                    self.emit('task_error', {'task': 'autofix', 'error': 'Felhasználó által megszakítva.'})
                else:
                    logging.error(f"[AUTOFIX] Hiba: {e}")
                    self.emit('task_error', {'task': 'autofix', 'error': str(e)})
                    
        self._safe_thread('autofix', worker)

