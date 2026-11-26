# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['driver_tool.py'],
    pathex=[],
    binaries=[],
    datas=[('ui.html', '.'), ('bolti_tabla_sablon.docx', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # A pywebview SSL-MÓDJÁNAK FÜGGŐSÉGEI - ez a program SOHA nem használja őket.
    #
    # MÉRVE (2026-09-21, Build 323): az exe 15,72 MB-ról 16,58 MB-ra nőtt, és a növekmény
    # java része `cryptography\hazmat\bindings\_rust.pyd` (3,25 MB) + `bcrypt\_bcrypt.pyd`
    # (137 KB). A kódunk EGYIKET SEM importálja - a `webview` húzza be őket a
    # `__generate_ssl_cert()` függvényéből, ami KIZÁRÓLAG `webview.start(ssl=True)` esetén
    # fut le. Mi `webview.start(func=on_start, debug=False)`-t hívunk (driver_tool.py),
    # tehát az ág halott kód az exében.
    #
    # MIÉRT MOST JELENT MEG: a `cryptography` a `paramiko` függősége, ami a build gép
    # Python 3.12-es környezetében telepítve van; a korábbi (Python 3.14-es) környezetben
    # nem volt, ezért a pywebview opcionális importja elbukott és PyInstaller ki sem tette.
    # Vagyis a méret attól függött, mi van épp a build gépen telepítve - ez a kizárás
    # teszi a build eredményét ettől függetlenné.
    #
    # BIZTONSÁGOS: a pywebview `try/except ImportError`-ban importálja, és hiányában egy
    # beszédes `WebViewException`-t dob - de csak az SSL-ágon, amit nem érintünk. Ha
    # valaha `ssl=True` kellene, ezt a listát kell kiüríteni.
    #
    # AZ AUTO-UPDATER MINDEN FIELDED GÉPRE LETÖLTI AZ EXÉT, tehát a 3,4 MB nem elméleti.
    #
    # NUMPY + CHARDET (2026-11-26, mérve): a build gép Python 3.12-ről 3.14-re váltott (Build 433),
    # és az exe 24,4 MB-ról 39,4 MB-ra nőtt. A CArchive-TOC összevetése (Build 432 vs 437): új
    # `numpy` (~9 MB: openblas DLL, _multiarray_umath, random, fft) és `chardet` (models.bin).
    # A kódunk EGYIKET SEM importálja: a numpy-t a Pillow tölti be opcionálisan (try/except), a
    # chardet-et a `pygments.lexer` (a `rich` függősége, csak az `encoding='chardet'` ágon).
    # Ugyanaz a helyzet, mint fent: a méret attól függött, mi van épp a build gépen telepítve.
    excludes=['cryptography', 'bcrypt', 'paramiko', 'nacl', 'numpy', 'chardet'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DriverVarazslo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX tömörítés kikapcsolva: a csomagolt (packed) exe-ket a malware-szerzők
    # is előszeretettel használják aláírás-felismerés megkerülésére, ezért a
    # Defender/heurisztikus AV-motorok UPX-es PyInstaller exe-ket sokkal
    # gyakrabban jelölnek meg/törölnek, mint tömörítetlent.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    uac_admin=True,
    icon=['icon_drivervarazslo.ico'],
)
