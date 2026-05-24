"""
DeepScan Local - Windows Explorer Context Menu Integration
Designer: Sedat Telli | sedattelli.com

Registers / unregisters a right-click context menu entry for all files
under HKEY_CURRENT_USER (no admin rights required).

Registry path:
  HKCU/Software/Classes/*/shell/DeepScanLocal/
  HKCU/Software/Classes/*/shell/DeepScanLocal/command/
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

_KEY_PATH     = r"Software\Classes\*\shell\DeepScanLocal"
_CMD_PATH     = _KEY_PATH + r"\command"
_MENU_LABEL   = "DeepScan Local ile Ara"


def _exe_path() -> str:
    """Return the running executable path (frozen or script)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    # Running from source — return python + script path
    script = Path(__file__).with_name("main.py")
    return f'"{sys.executable}" "{script}"'


def install(exe_path: str | None = None) -> bool:
    """Add DeepScan Local to Explorer right-click context menu for all files."""
    if exe_path is None:
        exe_path = _exe_path()

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _KEY_PATH) as key:
            winreg.SetValueEx(key, "",      0, winreg.REG_SZ, _MENU_LABEL)
            winreg.SetValueEx(key, "Icon",  0, winreg.REG_SZ,
                              exe_path.strip('"') + ",0" if not exe_path.startswith('"') else exe_path[1:exe_path.index('"', 1)] + ",0")

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _CMD_PATH) as key:
            # %1 = selected file path (quoted by Explorer automatically)
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'{exe_path} "%1"')

        return True
    except Exception:
        return False


def uninstall() -> bool:
    """Remove DeepScan Local from Explorer context menu."""
    success = True
    for path in (_CMD_PATH, _KEY_PATH):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass
        except Exception:
            success = False
    return success


def is_installed() -> bool:
    """Return True if the context menu entry is present in the registry."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH):
            return True
    except FileNotFoundError:
        return False
