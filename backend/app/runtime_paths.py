from pathlib import Path
import sys


def app_root() -> Path:
    # In a PyInstaller build, writable customer data/licensing lives beside the EXE.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundle_root() -> Path:
    # Read-only bundled resources (frontend) live in _MEIPASS in a one-file build.
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return app_root()
