"""DriverVarázsló GUI - Bolti tábla nyomtatás (két gép A5-ös árlapja egy A4-es lapon).

A logika az `app/shoplabel_core.py`-ban van (sablon-kitöltés + Word-os nyomtatás); ez a
mixin a felületi réteg: nyomtató-lista, a legutóbb használt nyomtató megjegyzése, és a
nyomtatás háttérszálon (a Word hideg indítása több másodperc).

SZÁNDÉKOSAN NEM a `_safe_thread`-en fut: az a `_task_busy` kapun megy, tehát egy futó
driver-keresés vagy AutoFix alatt a táblát sem lehetne kinyomtatni - pedig a kettőnek
semmi köze egymáshoz. Saját, szűk zárja van (`_shoplabel_busy`), ugyanaz a minta, mint az
egyenkénti stresszprogram-indításnál.
"""
import os
import json
import time
import logging
import threading
from app.common import _app_data_dir, resource_path, CMD_TIMEOUT_RETURNCODE
from app import shoplabel_core as sc


class GuiShopLabelMixin:
    """Bolti tábla nyomtatás. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _shoplabel_dir(self):
        d = os.path.join(_app_data_dir(), 'bolti_tabla')
        os.makedirs(d, exist_ok=True)
        return d

    def _shoplabel_settings(self, update=None):
        """A legutóbb használt nyomtató (`beallitas.json`). Soha nem dob."""
        p = os.path.join(self._shoplabel_dir(), 'beallitas.json')
        data = {}
        try:
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
        except FileNotFoundError:
            pass
        except Exception as e:
            logging.warning(f"[BOLTI-TABLA] A beállítás-fájl nem olvasható ({p}): {e}")
        if update:
            data.update(update)
            try:
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
            except Exception as e:
                logging.warning(f"[BOLTI-TABLA] A beállítás-fájl nem írható ({p}): {e}")
        return data

    def get_shop_label_info(self):
        """A nézet adatai: nyomtatók, a legutóbb használt, a mezők, és hogy van-e Word.

        Az ELSŐ használatnál szándékosan NEM választunk ki előre nyomtatót (a Windows
        alapértelmezettjét sem): a technikus kifejezetten nem az alapértelmezetten akar
        nyomtatni (explicit user decision, 2026-09-25), tehát egy előre kiválasztott
        alapértelmezett pont azt a hibát kínálná fel, amit el akar kerülni."""
        printers = sc.list_printers(self._run)
        last = self._shoplabel_settings().get('printer') or ''
        names = [p['name'] for p in (printers or [])]
        if last and last not in names:
            logging.info(f"[BOLTI-TABLA] A legutóbb használt nyomtató ('{last}') már nincs a gépen.")
            last = ''
        word = sc.word_installed()
        if not word:
            logging.warning("[BOLTI-TABLA] A Microsoft Word nincs regisztrálva (Word.Application) - "
                            "a bolti tábla nem nyomtatható ezen a gépen.")
        return {
            'printers': printers or [],
            'printers_error': printers is None,
            'last_printer': last,
            'word_installed': word,
            'template_found': os.path.isfile(resource_path(sc.TEMPLATE_FILENAME)),
            'kinds': [{'key': k, 'label': v} for k, v in sc.KIND_LABELS.items()],
            'fields': [{'key': k, 'label': lab, 'laptop_only': lo, 'example': ex}
                       for k, lab, lo, ex in sc.FIELDS],
            'name_example': sc.NAME_EXAMPLE,
            'price_example': sc.PRICE_EXAMPLE,
        }

    def print_shop_labels(self, machines, printer):
        """A két gép táblájának kitöltése és kinyomtatása. Az eredmény a
        `shoplabel_result` eseményben jön ({ok, message}); a visszatérés csak azt mondja
        meg, elindult-e."""
        if getattr(self, '_shoplabel_busy', False):
            return {'started': False, 'error': 'Már folyamatban van egy nyomtatás.'}
        missing = sc.validate_machines(machines)
        if missing:
            logging.info(f"[BOLTI-TABLA] Hiányzó mezők, nem nyomtatunk: {missing}")
            return {'started': False, 'error': 'Hiányzik: ' + ', '.join(missing)}
        if not str(printer or '').strip():
            return {'started': False, 'error': 'Válassz nyomtatót!'}
        self._shoplabel_busy = True
        t = threading.Thread(target=self._shoplabel_worker, args=(machines, printer),
                             name='shoplabel-print', daemon=True)
        t.start()
        return {'started': True}

    def _shoplabel_worker(self, machines, printer):
        res = {'ok': False, 'message': ''}
        try:
            res = self._shoplabel_print_sync(machines, printer)
        except Exception as e:
            logging.error(f"[BOLTI-TABLA] A nyomtatás kivétellel állt le: {e}", exc_info=True)
            res = {'ok': False, 'message': f'Váratlan hiba: {e}'}
        finally:
            self._shoplabel_busy = False
            self.emit('shoplabel_result', res)
            self.emit('toast', {'message': ('🖨️ ' if res.get('ok') else '❌ ') + res.get('message', ''),
                                'type': 'success' if res.get('ok') else 'error'})

    def _shoplabel_print_sync(self, machines, printer):
        tpl_path = resource_path(sc.TEMPLATE_FILENAME)
        if not os.path.isfile(tpl_path):
            logging.error(f"[BOLTI-TABLA] A sablon nem található: {tpl_path}")
            return {'ok': False, 'message': 'A bolti tábla sablonja hiányzik a programból.'}
        if not sc.word_installed():
            return {'ok': False, 'message': ('A Microsoft Word nincs telepítve ezen a gépen - a bolti '
                                             'tábla a Worddel nyomtat. Ezt a bolti gépről futtasd.')}
        with open(tpl_path, 'rb') as f:
            data = sc.fill_template(f.read(), machines)
        # Az utolsó kitöltött tábla megmarad (utólag megnézhető/kézzel is nyomtatható).
        # Ha a Word épp nyitva tartja, időbélyeges névre írunk.
        out = os.path.join(self._shoplabel_dir(), 'bolti_tabla_utolso.docx')
        try:
            with open(out, 'wb') as f:
                f.write(data)
        except PermissionError:
            out = os.path.join(self._shoplabel_dir(), f"bolti_tabla_{time.strftime('%Y%m%d_%H%M%S')}.docx")
            with open(out, 'wb') as f:
                f.write(data)
        logging.info(f"[BOLTI-TABLA] Kitöltött tábla: {out} -> nyomtató: '{printer}' "
                     f"(laponként {sc.PAGES_PER_SHEET[0]} oldal, A4-re méretezve)")
        r = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                       sc.build_word_print_ps(out, printer)], timeout=sc.WORD_PRINT_TIMEOUT)
        if r.returncode == CMD_TIMEOUT_RETURNCODE:
            logging.error(f"[BOLTI-TABLA] A Word {sc.WORD_PRINT_TIMEOUT} mp alatt sem végzett.")
            return {'ok': False, 'message': (f'A Word {sc.WORD_PRINT_TIMEOUT} másodperc alatt sem válaszolt '
                                             '(esetleg egy Word-ablak kérdez valamit a háttérben). '
                                             'Nézd meg a nyomtatási sort, mielőtt újrapróbálod.')}
        p = sc.parse_word_print_output(r.stdout)
        logging.info(f"[BOLTI-TABLA] Word eredmény: {p}")
        if p['default'] and p['default_after'] and p['default'] != p['default_after']:
            logging.warning(f"[BOLTI-TABLA] Az alapértelmezett nyomtató MEGVÁLTOZOTT: "
                            f"'{p['default']}' -> '{p['default_after']}'")
        if not p['ok']:
            msg = sc.error_text(p, printer) if p['error_stage'] else \
                f"A Word nem jelzett sikert (kimenet: {(r.stdout or r.stderr or '')[:300]})"
            logging.error(f"[BOLTI-TABLA] Nem sikerült: {msg}")
            return {'ok': False, 'message': msg}
        self._shoplabel_settings({'printer': printer})
        names = ' + '.join(str(m.get('name') or '').strip() for m in machines)
        return {'ok': True, 'message': f"Elküldve a(z) {printer} nyomtatóra: {names}"}
