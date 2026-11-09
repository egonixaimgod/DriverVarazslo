"""A rendszeróra ellenőrzése és javítása (2026-11-09, HP ProDesk 600 G5 SFF, Build 410).

TEREP: a gép órája 2024-11-16-ot mutatott 2026-11-09-én. A Python TLS minden kapcsolatra
`certificate is not yet valid`-et adott (a tanúsítványok a gép szerint a jövőben
készültek), a katalógus 44 eszközre nem válaszolt, a Windows Update 0x80248014-gyel
utasította el a keresést - a lánc 30 perc alatt 0 drivert telepített egy letörölt gépre.

A PONTOS IDŐ FORRÁSA sima HTTP `Date` fejléc: HTTPS-t pont a rossz óra miatt nem lehet
használni. Ez ugyanannyira hitelesítetlen, mint a Windows saját időszinkronja (NTP),
ezért KÉT független kiszolgálónak kell egyeznie, különben nem nyúlunk az órához.
A bizonyíték: docs/terep/lanc-autofix.md (grep: `Hibás rendszeróra`)."""
import datetime
import email.utils
import logging
import time
import urllib.request

from app import win32

# Ennél kisebb eltérés nem töri el a TLS-t, és időzóna-/dual-boot-eltérés is lehet
# (legfeljebb ±14 óra lenne, a gyakorlatban 1-3) - ahhoz nem nyúlunk.
CLOCK_MAX_DRIFT_S = 12 * 3600
CLOCK_SOURCES_AGREE_S = 300
CLOCK_TIME_URLS = (
    'http://www.msftconnecttest.com/connecttest.txt',   # a Windows saját hálózat-próbája
    'http://www.gstatic.com/generate_204',
    'http://detectportal.firefox.com/success.txt',
)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _http_date(url, timeout=6):
    """Egy kiszolgáló órája (UTC, tz-aware) a válasz `Date` fejlécéből. Hibára kivétel."""
    req = urllib.request.Request(url, headers={'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw, age = resp.headers.get('Date'), resp.headers.get('Age')
    t = email.utils.parsedate_to_datetime(raw)      # None/hibás fejlécre TypeError/ValueError
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    # Gyorsítótárból kiszolgált válasznál a Date a RÉGI válasz ideje, az Age a kora.
    if age and age.strip().isdigit():
        t += datetime.timedelta(seconds=int(age))
    return t


def network_utc(fetch=_http_date, urls=CLOCK_TIME_URLS):
    """A pontos idő: (UTC datetime, a mérés time.monotonic()-ja), vagy None, ha nincs két
    egymással egyező forrás (nincs net, vagy valamelyik kiszolgáló órája gyanús)."""
    got = []
    for url in urls:
        try:
            got.append((fetch(url), time.monotonic(), url))
        except Exception as e:
            logging.info(f"[CLOCK] Az időforrás nem válaszolt ({url}): {e}")
            continue
        if len(got) == 2:
            break
    if len(got) < 2:
        logging.warning(f"[CLOCK] A pontos idő nem állapítható meg: {len(got)} forrás válaszolt, 2 kell.")
        return None
    (t1, m1, u1), (t2, m2, u2) = got
    diff = abs((t2 - t1).total_seconds() - (m2 - m1))
    if diff > CLOCK_SOURCES_AGREE_S:
        logging.warning(f"[CLOCK] A két időforrás NEM egyezik ({u1}: {t1}, {u2}: {t2}, eltérés "
                        f"{diff:.0f} mp) - az órához nem nyúlunk.")
        return None
    return t2, m2


def drift_text(offset_s):
    """Az eltérés emberi szövege: '723 napot késett' / '15 órát sietett'."""
    irany = 'késett' if offset_s > 0 else 'sietett'
    n = abs(offset_s)
    return f"{n / 86400:.0f} napot {irany}" if n >= 2 * 86400 else f"{n / 3600:.0f} órát {irany}"


def ensure_clock(net=network_utc, setter=win32.set_system_time_utc, now=_utc_now):
    """Ellenőrzi a rendszerórát, és ha CLOCK_MAX_DRIFT_S-nél jobban eltér, beállítja.

    Visszaad: (állapot, eltérés mp-ben; + = a gép késik):
      'ok'      - az óra rendben, semmihez nem nyúltunk
      'unknown' - nem ellenőrizhető (nincs két egyező időforrás); eltérés None
      'fixed'   - hibás volt, beállítva, és a VISSZAOLVASÁS szerint most pontos
      'failed'  - hibás, és a beállítás nem sikerült (vagy a visszaolvasás szerint nem hatott)"""
    got = net()
    if got is None:
        return 'unknown', None
    true_utc, mono = got

    def true_now():
        return true_utc + datetime.timedelta(seconds=time.monotonic() - mono)

    local = now()
    offset = (true_now() - local).total_seconds()
    if abs(offset) <= CLOCK_MAX_DRIFT_S:
        logging.info(f"[CLOCK] A rendszeróra rendben (eltérés a pontos időtől: {offset:+.0f} mp).")
        return 'ok', offset
    target = true_now()
    logging.warning(f"[CLOCK] A rendszeróra HIBÁS: a gép szerint {local:%Y-%m-%d %H:%M:%S} UTC, "
                    f"a pontos idő {target:%Y-%m-%d %H:%M:%S} UTC ({drift_text(offset)}) - beállítás...")
    err = setter(target)
    if err:
        logging.error(f"[CLOCK] A rendszeróra beállítása NEM sikerült (SetSystemTime hibakód: {err}).")
        return 'failed', offset
    left = (true_now() - now()).total_seconds()
    if abs(left) > 60:
        logging.error(f"[CLOCK] A beállítás sikert jelzett, de a visszaolvasott óra még mindig "
                      f"{left:+.0f} mp-cel tér el - nem hatott.")
        return 'failed', offset
    logging.warning(f"[CLOCK] A rendszeróra beállítva és visszaolvasva (maradék eltérés: {left:+.1f} mp).")
    return 'fixed', offset
