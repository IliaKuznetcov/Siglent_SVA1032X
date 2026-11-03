# -*- mode: python ; coding: utf-8 -*-


from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['siglent_app.py'],
    pathex=[],
    binaries=[],
    datas=[('../THIRD_PARTY_NOTICES.md', '.'), ('SVA_logo.ico', '.')],
    hiddenimports=collect_submodules('pyvisa'),
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
    name='Siglent_SVA1032X',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['SVA_logo.ico'],
    version='windows_version_info.txt',
)
