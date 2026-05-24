"""
DeepScan Local - Configuration & System Integration
Designer: Sedat Telli | sedattelli.com

Handles: app paths, blacklist, config.json, Windows Registry autostart,
drive enumeration (FIXED + REMOVABLE only), and error logging.
"""

import os
import sys
import json
import ctypes
import winreg
import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------
APP_NAME         = "DeepScanLocal"
APP_DISPLAY_NAME = "DeepScan Local"
DESIGNER         = "Sedat Telli"
DESIGNER_CREDIT  = "Developed by Sedat Telli"
DESIGNER_URL     = "https://sedattelli.com"

# ---------------------------------------------------------------------------
# App-level paths  (created on first import)
# ---------------------------------------------------------------------------
APP_DATA_DIR  = Path(os.environ.get("LOCALAPPDATA", "C:/Temp")) / APP_NAME
DB_PATH       = APP_DATA_DIR / "index.db"
ERROR_LOG     = APP_DATA_DIR / "error.log"
CONFIG_PATH   = APP_DATA_DIR / "config.json"
ICON_PATH     = APP_DATA_DIR / "icon.ico"

APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Default user-editable configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: dict = {
    "hotkey": {
        "modifiers": ["alt", "ctrl"],
        "key": "",
        "vk_codes": [0x12, 0x11]   # VK_MENU, VK_CONTROL
    },
    "theme": "dark",             # "dark" | "light"
    "max_results": 10,
    "fuzzy_threshold": 70,       # RapidFuzz minimum score (0-100)
    "custom_paths": [],          # Extra directories to index
    "extra_exclusions": [],      # User-defined skip dirs (appended to SKIP_DIRS)
    "disabled_extensions": [],   # Extensions from INDEXED_EXTENSIONS to skip
    "scan_removable_drives": False,  # Scan USB / external drives
    "scan_network_drives":   False,  # Scan mapped network shares (A:, Z:, etc.)
    "excluded_drives":       [],     # Drive roots to skip entirely, e.g. ["D:\\"]
}

# ---------------------------------------------------------------------------
# Directory blacklist  (Recommended / Önerilen)
# All comparisons are done on lowercased path strings.
# ---------------------------------------------------------------------------
SKIP_DIRS: frozenset[str] = frozenset({
    # Windows system dirs
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "config.msi",
    "perflogs",
    "documents and settings",   # NT legacy junction

    # Recycle / shadow
    "$recycle.bin",
    "system volume information",

    # AppData — keep only user-created content
    "appdata\\roaming",
    "appdata\\local\\temp",
    "appdata\\local\\microsoft",
    "appdata\\local\\packages",        # UWP / MSIX containers
    "appdata\\local\\application data",# junction — causes recursion
    "appdata\\local\\elevateddiagnostics",
    "appdata\\local\\history",

    # Browser data (thousands of JS/JSON cache files — not user content)
    "appdata\\local\\google",          # Chrome
    "appdata\\local\\island",          # Island browser
    "appdata\\local\\bravesoft",       # Brave
    "appdata\\local\\microsoftedge",   # Edge (alt path)
    "appdata\\local\\vivaldi",
    "appdata\\local\\opera software",
    "appdata\\local\\yandex",

    # IDE / editor caches
    "appdata\\local\\programs\\microsoft vs code",
    "appdata\\local\\jetbrains",
    ".vscode",
    ".idea",

    # Python runtimes & package trees (not user code)
    "python314", "python313", "python312", "python311",
    "python310", "python39",  "python38",
    "site-packages",          # pip-installed libraries in any Python
    "lib\\python",            # Unix-style Python lib path (WSL/Portable)
    "python_tts",             # Portable TTS engines

    # Dev artefacts
    "node_modules",
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    ".env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
})

# ---------------------------------------------------------------------------
# File extensions to index (content extraction attempted)
# ---------------------------------------------------------------------------
INDEXED_EXTENSIONS: frozenset[str] = frozenset({
    # Office & document formats
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".txt", ".md",  ".csv",
    # Data / config (user-created only — system paths are excluded)
    ".json", ".xml",
    # Adobe creative
    ".psd", ".ai",
    # Images  (EXIF for JPEG; filename-only for others)
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tiff", ".tif",
    # Videos  (indexed by filename — file is never read, size check bypassed)
    ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v",
})

# Extensions whose content is derived from filename/metadata only —
# the file bytes are never read, so the MAX_FILE_BYTES limit does not apply.
METADATA_ONLY_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v",
})

# 50 MB cap — larger files are skipped entirely
MAX_FILE_BYTES: int = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# config.json helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config.json; fall back to defaults if missing or corrupt."""
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                user_cfg = json.load(fh)
            merged = DEFAULT_CONFIG.copy()
            merged.update(user_cfg)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    """Persist config dict to config.json."""
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        log_error(f"save_config failed: {exc}")


# ---------------------------------------------------------------------------
# User-defined exclusions  (module-level cache so is_excluded stays fast)
# ---------------------------------------------------------------------------

_EXTRA_EXCLUSIONS: list[str] = []


def refresh_exclusions() -> None:
    """Reload extra_exclusions from config.json into the module cache."""
    global _EXTRA_EXCLUSIONS
    cfg = load_config()
    _EXTRA_EXCLUSIONS = [e.lower() for e in cfg.get("extra_exclusions", [])]


refresh_exclusions()   # Initialise on first import


# ---------------------------------------------------------------------------
# Active extensions cache  (INDEXED_EXTENSIONS minus user-disabled ones)
# ---------------------------------------------------------------------------

_ACTIVE_EXTENSIONS: frozenset[str] = INDEXED_EXTENSIONS


def refresh_active_extensions() -> None:
    """Rebuild _ACTIVE_EXTENSIONS from config.json."""
    global _ACTIVE_EXTENSIONS
    cfg = load_config()
    disabled = {e.lower() for e in cfg.get("disabled_extensions", [])}
    _ACTIVE_EXTENSIONS = frozenset(e for e in INDEXED_EXTENSIONS if e not in disabled)


refresh_active_extensions()


# ---------------------------------------------------------------------------
# Windows Registry — autostart (HKCU, no admin required)
# ---------------------------------------------------------------------------

def set_autostart(enabled: bool = True) -> None:
    """Register or remove the app from Windows startup."""
    reg_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    # Use sys.executable when frozen by PyInstaller
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        log_error(f"Registry autostart error: {exc}")


# ---------------------------------------------------------------------------
# Error logging
# ---------------------------------------------------------------------------

def log_error(message: str) -> None:
    """Append a timestamped error line to error.log. Never raises."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {message}\n")
    except OSError:
        pass  # Silently swallow if log itself is inaccessible


# ---------------------------------------------------------------------------
# Path exclusion check
# ---------------------------------------------------------------------------

def is_excluded(path: Path) -> bool:
    """Return True if path (or any ancestor) matches the blacklist."""
    lowered = str(path).lower()
    for skip in SKIP_DIRS:
        if skip in lowered:
            return True
    for skip in _EXTRA_EXCLUSIONS:
        if skip in lowered:
            return True
    return False


# ---------------------------------------------------------------------------
# Drive enumeration  — FIXED (3) and REMOVABLE (2) only; skip REMOTE (4)
# ---------------------------------------------------------------------------

DRIVE_REMOVABLE = 2
DRIVE_FIXED     = 3
DRIVE_REMOTE    = 4   # Mapped network shares (UNC / SMB)

def get_indexable_drives() -> list[str]:
    """
    Return drive roots (e.g. ['C:\\', 'D:\\']) that are ready and accessible.
    - Fixed (internal) drives: always included.
    - Removable (USB) drives:  included only when scan_removable_drives=True.
    - Network (mapped shares): included only when scan_network_drives=True.
    - Optical / RAM disk / unknown: always skipped.
    """
    cfg = load_config()
    scan_removable  = cfg.get("scan_removable_drives", False)
    scan_network    = cfg.get("scan_network_drives",   False)
    excluded_drives = {d.upper().rstrip("\\") for d in cfg.get("excluded_drives", [])}

    drives: list[str] = []
    bitmask: int = ctypes.windll.kernel32.GetLogicalDrives()

    for i in range(26):
        if bitmask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            # Skip user-excluded drives
            if root.upper().rstrip("\\") in excluded_drives:
                continue
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
            if drive_type == DRIVE_FIXED:
                pass  # always include internal drives (unless user excluded above)
            elif drive_type == DRIVE_REMOVABLE:
                if not scan_removable:
                    continue
            elif drive_type == DRIVE_REMOTE:
                if not scan_network:
                    continue
            else:
                continue  # optical, RAM disk, unknown
            try:
                os.listdir(root)
                drives.append(root)
            except (PermissionError, OSError):
                pass

    return drives
