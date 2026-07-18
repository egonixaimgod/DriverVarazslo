"""DriverVarázsló CLI - CLI: szellemeszközök törlése (a közös PS script + sor-protokoll:
app/ghost_core.py)."""

# === AUTO-IMPORTS ===
from app.ghost_core import build_ghost_ps
from app.ghost_core import parse_ghost_line
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

        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", build_ghost_ps()], encoding='utf-8')
        success = 0
        total = 0
        for line in res.stdout.splitlines():
            parsed = parse_ghost_line(line)
            if not parsed:
                continue
            event, data = parsed
            if event == 'total':
                total = data
                print(f"Összesen {total} db szellemeszköz azonosítva...")
            elif event == 'rm':
                print(f"  🗑 Próbálkozás: {data}...", end=" ", flush=True)
            elif event == 'ok':
                success += 1
                print("✅")
            elif event == 'fail':
                print("❌ (valószínűleg védett eszköz)")
            elif event == 'done':
                print(data)

        print("-" * 50)
        print(f"✅ Szellemeszközök törlése kész! Törölve: {success} / {total}")
        return success, total
