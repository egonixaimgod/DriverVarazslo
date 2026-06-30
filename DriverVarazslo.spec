# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['driver_tool.py'],
    pathex=[],
    binaries=[],
    datas=[('ui.html', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
