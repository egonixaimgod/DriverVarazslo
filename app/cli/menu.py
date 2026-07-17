"""DriverVarázsló CLI - szöveges főmenü és almenük (run_cli_mode). A GUI helyett fut,
ha --cli kapcsolóval indul a program, vagy nincs használható WebView2."""

# === AUTO-IMPORTS ===
import os
import time
from app import common
from app.cli.api import CliApi
# === /AUTO-IMPORTS ===


def run_cli_mode():
    """Parancssoros mód - TELJES funkcionalitás (GUI tükör)."""
    api = CliApi()
    
    def clear_screen():
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def print_header():
        clear_screen()
        print("=" * 60)
        print("  ♻️  DRIVERVARÁZSLÓ - CLI MÓD")
        print("  🖥️  Tiszta rendszer (Build " + str(common.BUILD_NUMBER) + ")")
        print("=" * 60)
        if api.target_os_path:
            print(f"  📌 Offline mód: {api.target_os_path}")
        else:
            print("  📌 Jelenlegi rendszer (online)")
        print("=" * 60)
    
    def main_menu():
        print("""
  FŐMENÜ - Válassz kategóriát:

    💿  1. Driverek kezelése
    💾  2. Mentés és Visszaállítás
    🔄  3. Windows Update
    ⚡  4. 1 Kattintásos Driver Fix
    🧹  5. Temp fájlok törlése (lemez felszabadítás)
    🚫  6. Net Blokkoló script (block.bat) letöltése

    ⚙️   7. Cél OS váltása (offline mód)
    ℹ️   8. GUI-only funkciók (mik nem érhetők el itt)
    ❌  0. Kilépés
""")
    
    def drivers_menu():
        while True:
            print_header()
            print("""
  💿 DRIVEREK KEZELÉSE

    1. Third-party driverek listázása
    2. ÖSSZES driver listázása (veszélyes!)
    3. Driver(ek) törlése
    4. Hardver újraszkennelés
    5. Szellemeszközök (ghost device) törlése
    6. 🛟 LAN driver mentőcsomag telepítése (nicpack.zip)

    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()
            
            if choice == '0':
                break
            elif choice == '1':
                drivers = api.list_drivers(all_drivers=False)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                drivers = api.list_drivers(all_drivers=True)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                all_mode = input("Összes driver mód? (i/n): ").strip().lower() == 'i'
                drivers = api.list_drivers(all_drivers=all_mode)
                if not drivers:
                    input("\nNyomj ENTER-t a folytatáshoz...")
                    continue
                
                sel = input("\nTörlendő sorszámok (pl: 1,3,5 vagy 'mind'): ").strip()
                if sel.lower() == 'mind':
                    to_delete = drivers
                else:
                    indices = [int(x.strip())-1 for x in sel.split(',') if x.strip().isdigit()]
                    to_delete = [drivers[i] for i in indices if 0 <= i < len(drivers)]
                
                if to_delete:
                    reboot = input("Törlés után újraindítás? (i/n): ").strip().lower() == 'i'
                    confirm = input(f"Biztosan törölsz {len(to_delete)} drivert? (i/n): ").strip().lower()
                    if confirm == 'i':
                        api.delete_drivers(to_delete, list_all=all_mode, reboot=reboot)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '4':
                if api.target_os_path:
                    print("❌ Offline módban nem elérhető!")
                else:
                    print("🔄 Hardver újraszkennelés...")
                    api._run(['pnputil', '/scan-devices'])
                    time.sleep(2)
                    print("✅ Kész!")
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '5':
                api.delete_ghost_devices()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '6':
                api.install_nic_pack()
                input("\nNyomj ENTER-t a folytatáshoz...")

    def backup_menu():
        while True:
            print_header()
            print("""
  💾 MENTÉS ÉS VISSZAÁLLÍTÁS

    1. Third-party driverek mentése
    2. ÖSSZES driver mentése (OEM + inbox)
    3. Lementett driverek visszaállítása
    4. WIM-ből gyári driverek kinyerése
    5. Visszaállítási pont létrehozása
    6. BCD boot hiba javítása
    
    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()
            
            if choice == '0':
                break
            elif choice == '1':
                dest = input("Mentés célmappája: ").strip()
                if dest:
                    api.backup_third_party(dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                dest = input("Mentés célmappája: ").strip()
                if dest:
                    api.backup_all(dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                source = input("Lementett driver mappa: ").strip()
                if source:
                    online = input("Online mód (jelenlegi rendszer)? (i/n): ").strip().lower() == 'i'
                    api.restore_drivers(source, online=online)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '4':
                wim = input("install.wim fájl elérési útja: ").strip()
                dest = input("Kinyerés célmappája: ").strip()
                if wim and dest:
                    api.extract_wim(wim, dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '5':
                if api.target_os_path:
                    print("❌ Offline módban nem elérhető!")
                else:
                    api.create_restore_point()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '6':
                api.repair_bcd_standalone_cli()
                input("\nNyomj ENTER-t a folytatáshoz...")
    
    def wu_menu():
        while True:
            print_header()
            status = api.check_wu_status_cli()
            print(f"""
  🔄 WINDOWS UPDATE BEÁLLÍTÁSOK
  
  Jelenlegi állapot: {status}

    1. WU driver letiltás
    2. WU driver engedélyezés + reset
    3. WU szolgáltatások újraindítása
    4. WU szüneteltetése (N napra)
    5. WU szüneteltetés feloldása

    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()

            if choice == '0':
                break
            elif choice == '1':
                api.disable_wu_drivers()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                api.enable_wu_drivers()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                api.restart_wu_services()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '4':
                days_str = input("Hány napra szüneteltessük (pl: 7)? ").strip()
                days = int(days_str) if days_str.isdigit() else 7
                api.pause_wu(days)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '5':
                api.resume_wu()
                input("\nNyomj ENTER-t a folytatáshoz...")
    
    def target_menu():
        print("\n⚙️  CÉL OS VÁLTÁSA")
        print("-" * 40)
        print("Jelenlegi:", api.target_os_path or "Jelenlegi rendszer (online)")
        print()
        path = input("Új cél OS path (üres = visszaállítás jelenlegire): ").strip()
        
        if not path:
            api.target_os_path = None
            print("✅ Visszaállítva: jelenlegi rendszer")
        elif os.path.isdir(os.path.join(path, 'Windows')):
            api.target_os_path = path
            print(f"✅ Cél OS: {api.target_os_path}")
        else:
            print(f"❌ Nem található Windows mappa: {path}")
        
        input("\nNyomj ENTER-t a folytatáshoz...")
    
    # FŐCIKLUS
    while True:
        print_header()
        main_menu()
        
        try:
            choice = input("Választás: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        
        if choice == '0':
            print("\nViszlát! 👋")
            break
        elif choice == '1':
            drivers_menu()
        elif choice == '2':
            backup_menu()
        elif choice == '3':
            wu_menu()
        elif choice == '4':
            print_header()
            api.autofix()
            input("\nNyomj ENTER-t a folytatáshoz...")
        elif choice == '5':
            print_header()
            extra1 = input("Miniatűr (thumbnail) gyorsítótár törlése is? (i/n): ").strip().lower() == 'i'
            extra2 = input("Lomtár (Recycle Bin) ürítése is? (i/n): ").strip().lower() == 'i'
            extra3 = input("Egyéb extra kategóriák is (Delivery Optimization, hibajelentések, DirectX Shader Cache, CBS logok, Crash Dumpok, IE/Edge cache, színprofilok)? (i/n): ").strip().lower() == 'i'
            api.clean_temp_files(thumbnail_cache=extra1, recycle_bin=extra2,
                                  delivery_opt=extra3, wer=extra3, shader_cache=extra3,
                                  cbs_logs=extra3, crash_dumps=extra3, inet_cache=extra3,
                                  color_profiles=extra3)
            input("\nNyomj ENTER-t a folytatáshoz...")
        elif choice == '6':
            print_header()
            api.download_block_script()
            input("\nNyomj ENTER-t a folytatáshoz...")
        elif choice == '7':
            target_menu()
        elif choice == '8':
            print_header()
            print("""
  ℹ️  CSAK A GRAFIKUS FELÜLETEN (GUI) ELÉRHETŐ FUNKCIÓK

  A következő funkciók jelenleg csak a grafikus (nem --cli) módban
  érhetők el, futtasd a programot --cli kapcsoló nélkül, ha ezekre
  van szükséged:

    • BitLocker állapot lekérdezése / kikapcsolása
    • HTML hardverjelentés generálása (S.M.A.R.T. adatokkal)
    • Célzott WU driver keresés és kiválasztásos telepítés
      (a CLI Autofix csak a teljes automatikus telepítést tudja)
    • Stabilitás (stressz) teszt indítása
""")
            input("\nNyomj ENTER-t a folytatáshoz...")
        else:
            print("❌ Érvénytelen választás!")
