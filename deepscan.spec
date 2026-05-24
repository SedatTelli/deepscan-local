# -*- mode: python ; coding: utf-8 -*-
#
# DeepScan Local — PyInstaller spec file
# Designer: Sedat Telli | sedattelli.com
#
# Build with:  pyinstaller deepscan.spec --clean --noconfirm
# Output:      dist\DeepScanLocal\DeepScanLocal.exe   (--onedir layout)
#
# The --onedir layout is required because customtkinter loads its theme
# JSON files at runtime from its package directory.  --onefile would need
# additional hook work to embed those assets.

from pathlib import Path
import customtkinter as _ctk

# Locate customtkinter asset directory so its themes & fonts are bundled
_ctk_dir = Path(_ctk.__file__).parent

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # Bundle the entire customtkinter package (themes, fonts, images)
        (str(_ctk_dir), "customtkinter"),
        # Bundle the app icon so it can be copied to AppData at runtime
        ("icon.ico", "."),
    ],
    hiddenimports=[
        # customtkinter internal modules
        "customtkinter",
        "customtkinter.windows",
        "customtkinter.windows.widgets",
        "customtkinter.windows.widgets.appearance_mode",
        "customtkinter.windows.widgets.theme",

        # Pillow
        "PIL._tkinter_finder",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",

        # pystray Windows backend
        "pystray",
        "pystray._win32",

        # watchdog Windows backend
        "watchdog.observers",
        "watchdog.observers.winapi",

        # File parsers
        "docx",
        "openpyxl",
        "openpyxl.cell._writer",
        "pptx",
        "pypdf",

        # MFT fast scanner
        "mft_scanner",

        # Ranking
        "rapidfuzz",
        "rapidfuzz.fuzz",
        "rapidfuzz.process",

        # Standard lib (sometimes missed on older PyInstaller)
        "sqlite3",
        "queue",
        "winreg",
        "ctypes.wintypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Exclude heavy libs we don't use (keeps the bundle smaller)
    excludes=[
        "matplotlib", "numpy", "scipy", "pandas",
        "IPython", "notebook", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DeepScanLocal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # No console / black window on startup
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DeepScanLocal",
)
