"""DriverVarázsló CLI - CLI: DriverStore duplikátum-takarítás (a közös logika és a
biztonsági szabályok: app/dupdrivers_core.py)."""

# === AUTO-IMPORTS ===
from app import dupdrivers_core
# === /AUTO-IMPORTS ===


class CliDupDriversMixin:
    """CLI: DriverStore duplikátum-takarítás. A CliApi része (összerakás: app/cli/api.py)."""

    def clean_duplicate_drivers(self):
        """Duplikátum-csoportok listázása + a régi verziók interaktív törlése
        (a GUI duplikátum-paneljének CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ A duplikátum-takarítás csak Élő rendszeren működik!")
            return

        print("\n🧹 DRIVERSTORE DUPLIKÁTUM-TAKARÍTÁS")
        print("-" * 60)
        print("Azonos driver több felhalmozódott verziójából a legújabb marad,")
        print("a régiek törölhetők. Aktívan használt driver SOSEM törlődik.")
        print("-" * 60)

        drivers = self.get_third_party_drivers()
        active_infs = dupdrivers_core.get_active_published_infs(self._run)
        groups, deletable = dupdrivers_core.build_duplicate_groups(drivers, active_infs)

        if not groups:
            print("\n✅ Nincs duplikált driver-verzió a DriverStore-ban.")
            return

        # Sorszámozott lista a törölhető (nem aktív) régi verziókról
        candidates = []
        print()
        for g in groups:
            print(f"📦 {g['original']} ({g['provider']}, {g['class']})")
            print(f"     MARAD: {g['keep']['published']} - v{g['keep']['version']}")
            for d in g['dups']:
                if d['active']:
                    print(f"     🔒 AKTÍV (nem törölhető): {d['published']} - v{d['version']}")
                else:
                    candidates.append(d['published'])
                    print(f"     [{len(candidates):2}] törölhető: {d['published']} - v{d['version']}")
        print("-" * 60)
        print(f"Összesen: {len(groups)} csoport, {deletable} törölhető régi verzió.")

        if not candidates:
            print("\nℹ️  Minden régi verzió aktív használatban van - nincs mit törölni.")
            return

        sel = input("\nTörlendő sorszámok (pl: 1,3,5 vagy 'mind', üres = mégse): ").strip()
        if not sel:
            print("❌ Megszakítva.")
            return
        if sel.lower() == 'mind':
            to_delete = list(candidates)
        else:
            indices = [int(x.strip()) - 1 for x in sel.split(',') if x.strip().isdigit()]
            to_delete = [candidates[i] for i in indices if 0 <= i < len(candidates)]
        if not to_delete:
            print("❌ Nincs érvényes kijelölés.")
            return

        confirm = input(f"Biztosan törölsz {len(to_delete)} régi driver-verziót? (i/n): ").strip().lower()
        if confirm != 'i':
            print("❌ Megszakítva.")
            return

        # Az aktív-lista ÚJRA lekérdezve közvetlenül törlés előtt (a lista elavulhatott).
        active_infs = dupdrivers_core.get_active_published_infs(self._run)
        if active_infs is None:
            print("❌ Az aktívan használt driverek listája nem kérdezhető le - biztonsági okból NEM törlünk.")
            return

        print()
        ok, fail, skipped = dupdrivers_core.delete_duplicate_packages(self._run, print, to_delete, active_infs)
        print("-" * 60)
        print(f"✅ Kész! Törölve: {ok}, Sikertelen: {fail}" + (f", Kihagyva: {skipped}" if skipped else ""))
