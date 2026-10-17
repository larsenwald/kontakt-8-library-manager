# klib.spec
# Build with: pyinstaller klib.spec

import os
from PyInstaller.building.build_main import Analysis, PYZ, EXE

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('kontakt_manager.html', '.'),   # bundle the UI alongside the exe
    ],
    hiddenimports=[
        'webview',
        'webview.platforms.winforms',
        'clr',
        'apsw',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='KLib',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no terminal window
    uac_admin=True,         # removing HKLM library registration needs admin
    onefile=True,           # single portable .exe
    icon=None,              # swap in an .ico file here if you have one
)
