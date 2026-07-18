"""DriverVarázsló GUI - DriverStore duplikátum-takarítás (RAPR / Driver Store Explorer elv).

Ugyanabból a driverből (azonos EREDETI inf-név) a DriverStore-ban több verzió is
felhalmozódhat (minden frissítés otthagyja a régit) - ezek gigákat foglalhatnak.
Ez a mixin a third-party csomagokat eredeti inf-név szerint csoportosítja, a NEM
legújabb verziókat felajánlja törlésre, és pnputil /delete-driver-rel törli őket.
Biztonsági szabályok:
  - a jelenlévő eszközök által AKTÍVAN használt publikált inf-ek (Win32_PnPSignedDriver
    InfName) SOSEM törölhetők - hiába régebbi a verziójuk, egy eszköz épp azon fut;
  - csak oemXX.inf publikált nevű (third-party) csomagot törlünk, gyárit soha;
  - először /force nélkül próbálkozunk, és csak sikertelen törlésnél adunk /force-ot.
Csak élő rendszeren fut (offline cél-OS-nél elutasít)."""

# === AUTO-IMPORTS ===
import re
import json
import logging
import threading
# === /AUTO-IMPORTS ===


def _dup_version_key(vstr):
    """Verzió-string ('31.0.15.5222') -> int-tuple a rendezéshez. Értelmezhetetlen -> (0,)."""
    try:
        parts = tuple(int(p) for p in re.findall(r'\d+', vstr or ''))
        return parts if parts else (0,)
    except Exception:
        return (0,)


class GuiDupDriversMixin:
    """DriverStore duplikátum-takarítás. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _get_active_published_infs(self):
        """A jelenlévő eszközök által ténylegesen használt publikált inf-nevek halmaza
        kisbetűvel (pl. {'oem12.inf'}) - ezek a duplikátum-törlésből ki vannak zárva.
        Hiba esetén None: a hívó ilyenkor NEM törölhet (inkább nem takarítunk, mint
        hogy egy aktív drivert lőjünk ki)."""
        try:
            ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                  "Get-WmiObject Win32_PnPSignedDriver | Where-Object { $_.InfName } | "
                  "Select-Object InfName | ConvertTo-Json -Compress")
            res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=120)
            data = json.loads(res.stdout) if res and (res.stdout or '').strip() else []
            if isinstance(data, dict):
                data = [data]
            active = {str(d.get('InfName') or '').strip().lower() for d in data}
            active.discard('')
            logging.info(f"[DUPDRV] Aktívan használt inf-ek: {len(active)} db")
            return active
        except Exception as e:
            logging.error(f"[DUPDRV] Aktív inf-lista lekérdezése sikertelen: {e}")
            return None

    def list_duplicate_drivers(self):
        """Duplikátum-csoportok összegyűjtése háttérszálon; eredmény a
        'dup_drivers_loaded' eventben. Egy csoport = azonos eredeti inf-név; a legújabb
        verzió megmarad ('keep'), a többi törölhető jelölt ('dups'), kivéve az aktívan
        használtakat (azok 'active' jelölést kapnak és nem törölhetők)."""
        logging.info("[API] list_duplicate_drivers()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ A duplikátum-takarítás csak Élő rendszeren működik!', 'type': 'error'})
            self.emit('dup_drivers_loaded', {'groups': [], 'error': 'offline'})
            return

        def worker():
            try:
                drivers = self._get_third_party_drivers()
                active_infs = self._get_active_published_infs()
                groups = {}
                for d in drivers:
                    orig = (d.get('original') or '').strip().lower()
                    if not orig or not (d.get('published') or '').lower().startswith('oem'):
                        continue
                    groups.setdefault(orig, []).append(d)

                result = []
                for orig, items in groups.items():
                    if len(items) < 2:
                        continue
                    items_sorted = sorted(items, key=lambda d: _dup_version_key(d.get('version')), reverse=True)
                    keep, rest = items_sorted[0], items_sorted[1:]
                    dups = []
                    for d in rest:
                        pub_l = (d.get('published') or '').lower()
                        dups.append({
                            'published': d.get('published', ''), 'version': d.get('version', ''),
                            'provider': d.get('provider', ''), 'class': d.get('class', ''),
                            # active_infs None (lekérdezési hiba) -> mindent aktívnak
                            # jelölünk = semmi sem törölhető (biztonságos irány).
                            'active': (active_infs is None) or (pub_l in active_infs),
                        })
                    result.append({
                        'original': orig,
                        'keep': {'published': keep.get('published', ''), 'version': keep.get('version', '')},
                        'provider': keep.get('provider', ''), 'class': keep.get('class', ''),
                        'dups': dups,
                    })
                result.sort(key=lambda g: (g['provider'].lower(), g['original']))
                deletable = sum(1 for g in result for d in g['dups'] if not d['active'])
                logging.info(f"[DUPDRV] {len(result)} duplikátum-csoport, {deletable} törölhető régi verzió")
                self.emit('dup_drivers_loaded', {'groups': result, 'deletable': deletable})
            except Exception as e:
                logging.error(f"[DUPDRV] Listázási hiba: {e}", exc_info=True)
                self.emit('dup_drivers_loaded', {'groups': [], 'error': str(e)})

        # Read-only listázás - a load_drivers mintájára nem foglalja a _task_busy-t.
        threading.Thread(target=worker, daemon=True, name="dup-list").start()

    def delete_duplicate_drivers(self, published_names):
        """A kijelölt régi duplikátum-verziók törlése (pnputil /delete-driver).
        Védelem: a lista újra-ellenőrzésre kerül az aktív inf-ek ellen (a felület
        állapota elavulhatott), gyári (nem oemXX) név sosem törlődik."""
        logging.info(f"[API] delete_duplicate_drivers({published_names})")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ A duplikátum-takarítás csak Élő rendszeren működik!', 'type': 'error'})
            return
        names = [str(n).strip() for n in (published_names or []) if str(n).strip().lower().startswith('oem')]
        if not names:
            self.emit('toast', {'message': '⚠️ Nincs törölhető elem kijelölve!', 'type': 'warning'})
            return

        def worker():
            self.emit('task_start', {'task': 'dupclean', 'title': f'Driver-duplikátumok törlése ({len(names)} db)'})
            active_infs = self._get_active_published_infs()
            if active_infs is None:
                self.emit('task_progress', {'task': 'dupclean', 'log': '❌ Az aktívan használt driverek listája nem kérdezhető le - biztonsági okból NEM törlünk.'})
                self.emit('task_complete', {'task': 'dupclean', 'status': '❌ Megszakítva (biztonsági ellenőrzés sikertelen)'})
                return
            ok = fail = skipped = 0
            total = len(names)
            for i, name in enumerate(names):
                if self._check_cancel():
                    self.emit('task_progress', {'task': 'dupclean', 'log': '\n❗ Megszakítva!'})
                    break
                if name.lower() in active_infs:
                    skipped += 1
                    self.emit('task_progress', {'task': 'dupclean', 'log': f'  ⏭ {name} - időközben aktív lett, kihagyva', 'current': i + 1, 'total': total})
                    continue
                res = self._run(['pnputil', '/delete-driver', name], ok_codes=(0, 3010))
                deleted = bool(res) and (res.returncode == 0 or 'deleted' in (res.stdout or '').lower() or 'törölve' in (res.stdout or '').lower())
                if not deleted:
                    # Második kör /force-szal: a nem használt, de valamihez még bejegyzett
                    # régi verziókat csak így engedi el a pnputil.
                    res = self._run(['pnputil', '/delete-driver', name, '/force'], ok_codes=(0, 3010))
                    deleted = bool(res) and (res.returncode == 0 or 'deleted' in (res.stdout or '').lower() or 'törölve' in (res.stdout or '').lower())
                if deleted:
                    ok += 1
                    self.emit('task_progress', {'task': 'dupclean', 'log': f'  ✅ {name} törölve', 'current': i + 1, 'total': total, 'counter': f'{i + 1}/{total}'})
                else:
                    fail += 1
                    self.emit('task_progress', {'task': 'dupclean', 'log': f'  ❌ {name} törlése sikertelen: {(res.stdout or "")[:120] if res else "?"}', 'current': i + 1, 'total': total})
            msg = f'Kész! Törölve: {ok}, Sikertelen: {fail}' + (f', Kihagyva: {skipped}' if skipped else '')
            self.emit('task_complete', {'task': 'dupclean', 'status': msg})
            # Friss listák: a duplikátum-nézet és a fő driver-lista is változott.
            self.list_duplicate_drivers()

        self._safe_thread('dupclean', worker)
