# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


# ============================================================
# PROJECT PATHS
# ============================================================

PROJECT_ROOT = Path(SPECPATH)

FRONTEND_DIR = PROJECT_ROOT / "frontend"
LICENSE_FILE = PROJECT_ROOT / "license" / "license.key"

# Final customer release directory
RELEASE_DIR = PROJECT_ROOT / "release"


# ============================================================
# PYINSTALLER IMPORTS
# ============================================================

# Explicitly collect the complete backend package.
hiddenimports = collect_submodules("backend")


# ============================================================
# ANALYSIS
# ============================================================

a = Analysis(
    ["launcher.py"],

    pathex=[
        str(PROJECT_ROOT),
    ],

    binaries=[],

    datas=[
        # Frontend is bundled inside _internal/frontend
        (str(FRONTEND_DIR), "frontend"),
    ],

    hiddenimports=hiddenimports,

    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],

    noarchive=False,
    optimize=0,
)


# ============================================================
# PYZ
# ============================================================

pyz = PYZ(
    a.pure,
)


# ============================================================
# EXECUTABLE
# ============================================================

exe = EXE(
    pyz,
    a.scripts,

    [],

    exclude_binaries=True,

    name="ChandraTrading",

    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],

    console=True,

    disable_windowed_traceback=False,

    argv_emulation=False,

    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


# ============================================================
# COLLECT
# ============================================================

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,

    strip=False,
    upx=True,
    upx_exclude=[],

    name="ChandraTrading",
)


# ============================================================
# NOTE ABOUT LICENSE + DATA
# ============================================================
#
# license\license.key and data\trade_history.json are NOT
# included inside the PyInstaller executable.
#
# They are customer-side writable files and should live beside
# ChandraTrading.exe.
#
# The release assembly is handled by BUILD_WINDOWS.ps1.
#
# Final structure:
#
# release\
# ├── ChandraTrading.exe
# ├── _internal\
# │   └── frontend\
# │       └── index.html
# │
# ├── license\
# │   └── license.key
# │
# └── data\
#     └── trade_history.json
#
# ============================================================