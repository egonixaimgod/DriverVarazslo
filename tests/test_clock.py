"""Hibás rendszeróra + a hiányos keresés őszinte zárása (2026-11-09, terepi naplókból).

HP ProDesk 600 G5 (Build 410): a gép órája 2024-11-16-ot mutatott 2026-11-09-én, minden
TLS-kapcsolat `certificate is not yet valid`-del bukott, a lánc 0 drivert telepített, és
"A FOLYAMAT SIKERESEN BEFEJEZŐDÖTT!"-et írt. A valódi órához, hálózathoz, registryhez és
ütemezett feladathoz semmi nem nyúl: az időforrás, az óra-beállító és a `_run` csonk."""
import datetime
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401
from tests.catalog_stub import quiet
from app import clock_core, common, wusettings_core
from app.gui import autofix as af
from app.gui.hwscan import NO_BIND_REASON_OS

UTC = datetime.timezone.utc
TRUE_NOW = datetime.datetime(2026, 11, 9, 16, 50, tzinfo=UTC)      # a HP-lánc valódi indulása
HP_CLOCK = datetime.datetime(2024, 11, 16, 10, 37, tzinfo=UTC)     # amit a gép mutatott


class _FakeClock:
    """A gép órája: a setter csak akkor hat, ha `works`."""

    def __init__(self, start, works=True, err=0):
        self.t, self.works, self.err, self.set_calls = start, works, err, []

    def now(self):
        return self.t

    def set(self, target):
        self.set_calls.append(target)
        if self.err:
            return self.err
        if self.works:
            self.t = target
        return 0


def _net():
    return TRUE_NOW, time.monotonic()


class EnsureClockTest(unittest.TestCase):
    def setUp(self):
        quiet()

    def test_jo_orahoz_nem_nyul(self):
        clk = _FakeClock(TRUE_NOW + datetime.timedelta(hours=2))      # időzóna-szerű eltérés
        state, off = clock_core.ensure_clock(net=_net, setter=clk.set, now=clk.now)
        self.assertEqual(state, 'ok')
        self.assertEqual(clk.set_calls, [])
        self.assertAlmostEqual(off, -7200, delta=5)

    def test_hp_ket_evet_keso_ora_beallitva_es_visszaolvasva(self):
        clk = _FakeClock(HP_CLOCK)
        state, off = clock_core.ensure_clock(net=_net, setter=clk.set, now=clk.now)
        self.assertEqual(state, 'fixed')
        self.assertEqual(len(clk.set_calls), 1)
        self.assertLess(abs((clk.t - TRUE_NOW).total_seconds()), 5)
        self.assertEqual(clock_core.drift_text(off), '723 napot késett')

    def test_sieto_ora_is(self):
        clk = _FakeClock(TRUE_NOW + datetime.timedelta(hours=15))
        state, off = clock_core.ensure_clock(net=_net, setter=clk.set, now=clk.now)
        self.assertEqual((state, clock_core.drift_text(off)), ('fixed', '15 órát sietett'))

    def test_hibakod_a_beallitasnal_failed(self):
        clk = _FakeClock(HP_CLOCK, err=1314)                          # ERROR_PRIVILEGE_NOT_HELD
        self.assertEqual(clock_core.ensure_clock(net=_net, setter=clk.set, now=clk.now)[0], 'failed')

    def test_a_siker_a_visszaolvasas_nem_a_visszateresi_kod(self):
        clk = _FakeClock(HP_CLOCK, works=False)                       # "sikerült", de az óra maradt
        self.assertEqual(clock_core.ensure_clock(net=_net, setter=clk.set, now=clk.now)[0], 'failed')

    def test_idoforras_nelkul_nem_tudjuk_es_nem_allitunk(self):
        clk = _FakeClock(HP_CLOCK)
        state, off = clock_core.ensure_clock(net=lambda: None, setter=clk.set, now=clk.now)
        self.assertEqual((state, off, clk.set_calls), ('unknown', None, []))


class NetworkUtcTest(unittest.TestCase):
    def setUp(self):
        quiet()

    def test_ket_egyezo_forras_kell(self):
        def fetch(url):
            if 'msft' in url:
                raise OSError('nincs válasz')
            return TRUE_NOW
        got = clock_core.network_utc(fetch=fetch)
        self.assertEqual(got[0], TRUE_NOW)                            # a 2. és a 3. forrás egyezett

    def test_egy_forras_keves(self):
        def fetch(url):
            if 'gstatic' in url:
                return TRUE_NOW
            raise OSError('nincs válasz')
        self.assertIsNone(clock_core.network_utc(fetch=fetch))

    def test_egymasnak_ellentmondo_forrasokra_nem_allitunk_orat(self):
        answers = iter([TRUE_NOW, HP_CLOCK])
        self.assertIsNone(clock_core.network_utc(fetch=lambda url: next(answers)))

    def test_date_fejlec_es_gyorsitotar_kora(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.headers = {'Date': 'Mon, 09 Nov 2026 16:50:00 GMT', 'Age': '120'}
        with mock.patch('urllib.request.urlopen', return_value=resp) as uo:
            t = clock_core._http_date('http://pelda/')
        self.assertEqual(t, TRUE_NOW + datetime.timedelta(seconds=120))
        self.assertEqual(uo.call_args[0][0].full_url, 'http://pelda/')     # sima HTTP, nem TLS

    def test_minden_idoforras_sima_http(self):
        # HTTPS-forrás a rossz óra miatt épp akkor bukna el, amikor kell.
        self.assertTrue(all(u.startswith('http://') for u in clock_core.CLOCK_TIME_URLS))


def _res(stdout='', rc=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=rc)


class _Chain(af.GuiAutofixMixin):
    """A lánc gazdája: a külvilágot érintő lépések felvevők, a lánc-állapot egy temp mappa."""

    def __init__(self, clock=('ok', 0.0), **flags):
        self.target_os_path = ''
        self.resume_mode = self.resume_step1 = self.resume_clockfix = False
        self.__dict__.update(flags)
        self._cancel_flag = False
        self._clock = clock
        self.emitted, self.scheduled, self.reboots, self.cmds = [], [], [], []

    def emit(self, event, payload=None):
        self.emitted.append((event, payload))

    def text(self):
        return '\n'.join(str((p or {}).get('log') or (p or {}).get('status') or '')
                         for _e, p in self.emitted if isinstance(p, dict))

    def _run(self, cmd, **k):
        self.cmds.append(' '.join(cmd) if isinstance(cmd, list) else cmd)
        return _res()

    def _safe_thread(self, task, target):
        target()

    def _wait_for_internet(self, *a, **k):
        return True

    def _disable_sleep_sync(self, *a, **k):
        pass

    def _schedule_autofix_resume(self, flag, task_id='autofix', required=True):
        self.scheduled.append(flag)
        return True

    def _reboot_or_cancel(self, status, task_id='autofix'):
        self.reboots.append(status)


class _ChainCase(unittest.TestCase):
    def setUp(self):
        quiet()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target, kw in ((af, {'_app_data_dir': mock.Mock(return_value=self.tmp)}),
                           (wusettings_core, {'pause_for_days': mock.Mock(return_value=True)})):
            p = mock.patch.multiple(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.pause = wusettings_core.pause_for_days

    def chain(self, clock, cls=None, **flags):
        c = (cls or _Chain)(**flags)
        p = mock.patch.object(clock_core, 'ensure_clock', side_effect=lambda: clock)
        p.start()
        self.addCleanup(p.stop)
        return c


KEEP = [{'published': 'oem7.inf', 'original': 'ninja.inf', 'egyeb': 'x'}]
DRIFT = 62_500_000.0        # ~723 nap


class ClockInChainTest(_ChainCase):
    def test_jo_oranal_a_lanc_valtozatlanul_indul(self):
        c = self.chain(('ok', 3.0))
        c.run_autofix(keep_packages=KEEP)
        self.assertEqual(c.scheduled, ['--resume-step1'])
        self.assertEqual(len(c.reboots), 1)
        self.assertEqual(c._autofix_stats_get('keep_packages'), [{'published': 'oem7.inf', 'original': 'ninja.inf', 'reason': 'manual'}])
        self.assertIsNone(c._autofix_stats_get('clock_fixed'))
        self.assertNotIn('hibás', c.text())                             # jó óránál nincs óra-szöveg

    def test_hibas_ora_beallitas_utan_ujraindul_es_meg_semmihez_nem_nyul(self):
        c = self.chain(('fixed', DRIFT))
        c.run_autofix(keep_packages=KEEP)
        self.assertEqual(c.scheduled, ['--resume-clockfix'])           # nem a törlő láb jön
        self.assertEqual(len(c.reboots), 1)
        self.pause.assert_not_called()                                 # a WU-szünet az új A lábé
        self.assertEqual(c._autofix_stats_get('clock_keep_packages'),
                         [{'published': 'oem7.inf', 'original': 'ninja.inf', 'reason': 'manual'}])
        self.assertIn('723 napot késett', c.text())
        self.assertIn('ELÖLRŐL', c.text())

    def test_ujrainditas_utan_jo_ora_a_fix_elolrol_a_mentett_dontesekkel(self):
        # az előző folyamat hagyta hátra (a régi lánc-állapot része):
        first = self.chain(('fixed', DRIFT))
        first.run_autofix(keep_packages=KEEP)
        c = self.chain(('ok', 1.0), resume_clockfix=True, skip_printer_drivers=True)
        c.run_autofix()                                                # a felület alapértékekkel hívja
        self.assertTrue(any('Unregister-ScheduledTask' in x for x in c.cmds[:1]))
        self.assertEqual(c.scheduled, ['--resume-step1'])
        self.pause.assert_called_once()
        self.assertTrue(c._autofix_skip_printers)                      # argv-ből, nem a JS-alapértékből
        self.assertEqual(c._autofix_stats_get('keep_packages'),
                         [{'published': 'oem7.inf', 'original': 'ninja.inf', 'reason': 'manual'}])
        self.assertEqual(c._autofix_stats_get('clock_fixed'), '723 napot késett')   # a záró jelentésnek

    def test_szoftver_megtartas_oka_atmegy_az_ora_javitason(self):
        # 2026-11-13: a "Szoftveres driverek megtartása" kapcsoló kivétele (reason='software')
        # az óra-javítás utáni ELÖLRŐL induló A lábon is ugyanazzal az okkal marad meg.
        first = self.chain(('fixed', DRIFT))
        first.run_autofix(keep_packages=[dict(KEEP[0], reason='software')])
        c = self.chain(('ok', 1.0), resume_clockfix=True, skip_printer_drivers=True)
        c.run_autofix()
        self.assertEqual(c._autofix_stats_get('keep_packages'),
                         [{'published': 'oem7.inf', 'original': 'ninja.inf', 'reason': 'software'}])
        self.assertIsNone(c._autofix_stats_get('clock_keep_packages'))  # az új lánc tiszta lappal indul
        self.assertIn('az újraindítás után is pontos', c.text())

    def test_ujrainditas_utan_megint_rossz_ora_nincs_ujrainditasi_hurok(self):
        c = self.chain(('fixed', DRIFT), resume_clockfix=True)
        c.run_autofix()
        self.assertEqual(c.scheduled, ['--resume-step1'])               # megy tovább, nem --resume-clockfix
        self.assertIn('CMOS', c.text())

    def test_nem_allithato_ora_megall_a_torles_elott(self):
        for flags in ({}, {'resume_clockfix': True}):
            c = self.chain(('failed', DRIFT), **flags)
            c.run_autofix()
            self.assertEqual((c.scheduled, c.reboots), ([], []), flags)
            self.pause.assert_not_called()
            self.assertIn('semmi nem törlődött', c.text())
            self.assertTrue(any(e == 'task_complete' and p['status'].startswith('❌') for e, p in c.emitted))

    def test_ellenorizhetetlen_ora_nem_allitja_meg_a_lancot(self):
        c = self.chain(('unknown', None))
        c.run_autofix()
        self.assertEqual(c.scheduled, ['--resume-step1'])

    def test_telepito_lab_elejen_csak_javit_sosem_allit_meg(self):
        for state in ('ok', 'unknown', 'fixed', 'failed'):
            c = self.chain((state, None if state == 'unknown' else DRIFT))
            self.assertTrue(c._autofix_clock_gate('leg'), state)
            self.assertEqual((c.scheduled, c.reboots), ([], []), state)

    def test_az_ora_javito_lab_is_viszi_a_dontes_flageket(self):
        captured = []

        def run(cmd, **k):
            captured.append(cmd[-1])
            return _res('ARGS=' + cmd[-1].replace('\n', ' '))

        c = _Chain()
        c._run = run
        c._autofix_skip_printers = c._autofix_wifi_mode = c._autofix_rebuild_wifi = True
        c._autofix_wu_pause = c._autofix_use_catalog = False
        with mock.patch.object(af, '_app_exe_path', return_value=r'C:\DV\DriverVarazslo.exe'):
            self.assertTrue(af.GuiAutofixMixin._schedule_autofix_resume(c, '--resume-clockfix'))
        for flag in ('--resume-clockfix', '--skip-printer-drivers', '--wifi-mode',
                     '--rebuild-wifi-driver', '--no-wu-pause', '--no-catalog'):
            self.assertIn(flag, captured[0])


class _LastLeg(_Chain):
    """A lánc ZÁRÓ lába: a keresés eredményét a teszt adja meg."""

    gaps = []

    def _scan_and_install_wu_sync(self, task_id='autofix'):
        self._autofix_search_gaps = list(self.gaps)
        return 0

    def _emit_autofix_summary(self, chain_total, **kw):
        self.summary_kw = kw

    def upload_run_log(self, kind, title):
        self.uploaded = title

    def _noop(self, *a, **k):
        return None

    _chain_timer_leg_begin = _finish_deferred_inf_cleanup = _set_wu_pause = _disable_wu_sync = _noop
    _fix_driver_regressions = _sync_driver_store_apps = _get_third_party_drivers = _noop

    def _finish_pending_deletes(self):
        return 'ok', 0


class HonestClosingTest(_ChainCase):
    """4. elv: 0 driver + el sem jutott keresés után nem jöhet "SIKERESEN BEFEJEZŐDÖTT"."""

    def run_last_leg(self, gaps):
        c = self.chain(('ok', 0.0), cls=_LastLeg, resume_mode=True)
        c.gaps = gaps
        c._autofix_stats_set('settle_done', True)
        c._autofix_stats_set('rebind_done', True)
        c._autofix_stats_set('clock_fixed', '723 napot késett')
        with mock.patch.object(af.dupdrivers_core, 'auto_cleanup_duplicates'):
            c.run_autofix()
        self.assertFalse([p for e, p in c.emitted if e == 'task_error'])
        return c

    def test_teljes_kereses_utan_marad_a_sikeres_zaras(self):
        c = self.run_last_leg([])
        self.assertIn('A FOLYAMAT SIKERESEN BEFEJEZŐDÖTT!', c.text())
        self.assertIn('Teljesen befejezve', c.text())
        self.assertEqual(c.summary_kw['clock_note'], '723 napot késett')

    def test_hp_bukott_wu_es_nem_valaszolo_katalogus(self):
        gaps = ['a Windows Update keresése elbukott (0x80248014)',
                'a Microsoft Update Catalog 44 eszközre nem válaszolt']
        c = self.run_last_leg(gaps)
        t = c.text()
        for hamis in ('SIKERESEN BEFEJEZŐDÖTT', '🎉', 'Teljesen befejezve'):
            self.assertNotIn(hamis, t)
        self.assertIn('NEM TELJES', t)
        self.assertIn('0x80248014', t)
        self.assertIn('44 eszközre nem válaszolt', t)
        self.assertIn('A KERESÉS NEM VOLT TELJES', c.uploaded)
        self.assertEqual(c.summary_kw['search_gaps'], gaps)

    def test_ulepito_ujrainditas_elott_sem_allitja_hogy_minden_fent_van(self):
        c = self.chain(('ok', 0.0), cls=_LastLeg, resume_mode=True)
        c.gaps = ['a Microsoft Update Catalog 44 eszközre nem válaszolt']
        c.run_autofix()                                                # settle_done még nincs
        self.assertEqual(len(c.reboots), 1)
        self.assertNotIn('Minden driver fent van', c.text())
        self.assertIn('a keresés nem volt teljes', c.text())


class ReportWordingTest(_ChainCase):
    def test_mas_windowsra_keszult_csomag_nem_feltelepult(self):
        # ASUS E1504FA (Build 404): az OS-vétós AMD SMBUS csomagok a "feltelepült, de az
        # eszköz nem vette át" csoportban álltak, pedig fel sem mentek.
        c = self.chain(('ok', 0.0))
        c._get_third_party_drivers = lambda *a, **k: []
        c._emit_catalog_no_bind([
            {'pnp': 'PCI\\VEN_1022&DEV_790B', 'name': 'AMD SMBUS', 'reason': NO_BIND_REASON_OS,
             'title': 'Advanced Micro Devices, Inc System Driver Update (2.0.0.29)'},
            {'pnp': 'PCI\\VEN_1022&DEV_790B', 'name': 'AMD SMBUS', 'reason': NO_BIND_REASON_OS,
             'title': 'Advanced Micro Devices, Inc System Driver Update (2.0.0.26)'}])
        t = c.text()
        self.assertIn('MÁS WINDOWS-VERZIÓRA', t)
        self.assertIn('1 eszközhöz', t)
        self.assertNotIn('feltelepült', t)
        self.assertNotIn('kártyája', t)                                # megszűnt felületi elem

    def test_elveszett_gyari_driver_nem_lecserelodott(self):
        # MSI MS-7D69 (Build 404): amdhdaudbus.inf "LECSERÉLŐDÖTT (az eszköz működik)", miközben
        # a hangvezérlő a Windows hdaudbus.inf-jére esett vissza.
        c = self.chain(('ok', 0.0))
        current = [{'published': 'oem5.inf', 'original': 'amdgpio2.inf', 'class': 'System',
                    'provider': 'Advanced Micro Devices, Inc', 'version': '2.2.0.137'}]
        c._driver_list_checked = lambda: (current, None)
        c._health_lost_vendor = [{'name': 'High Definition Audio hangvezérlő', 'inf': 'oem30.inf',
                                  'provider': 'Advanced Micro Devices', 'version': '10.0.0.35'}]
        c._emit_missing_packages([
            {'original': 'amdhdaudbus.inf', 'provider': 'Advanced Micro Devices', 'version': '10.0.0.35',
             'class': 'System', 'devices': []},
            {'original': 'smbusamd.inf', 'provider': 'Advanced Micro Devices, Inc', 'version': '5.12.0.38',
             'class': 'System', 'devices': []}])
        t = c.text()
        self.assertIn('1 csomag LECSERÉLŐDÖTT', t)
        self.assertIn('smbusamd.inf', t)
        self.assertNotIn('amdhdaudbus.inf', t)


class RamtestLogNoiseTest(unittest.TestCase):
    def test_a_hci_figyelo_ciklus_segedei_nem_irnak_hivasnaplot(self):
        # ASRock B450M (Build 407): 54 944 sorból 54 136 ezek [CALL]-párja volt.
        for name in ('_stress_user32', '_ramtest_ctrl_text', '_ramtest_dlg_item',
                     '_ramtest_hci_dismiss_popups'):
            self.assertIn(name, common._CALL_LOG_EXCLUDE)


if __name__ == '__main__':
    unittest.main()
