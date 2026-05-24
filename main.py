"""
DeepScan Local - Main Entry Point
Designer: Sedat Telli | sedattelli.com

Architecture overview:
  Thread 1 (main / tkinter):  customtkinter event loop  ← only thread that
                               touches any tkinter widget.
  Thread 2 (WinAPI loop):     RegisterHotKey + GetMessageW message pump.
                               Posts "SHOW" to hotkey_queue on Ctrl+Shift+F2.
  Thread 3 (pystray):         System tray icon + menu.
  Thread 4 (indexer):         Initial full scan (daemon).
  Thread 5 (watcher):         Watchdog file-system monitor (daemon).

Inter-thread communication:
  All background threads write to `hotkey_queue` (queue.Queue).
  The tkinter thread drains the queue via root.after(100, _poll_queue).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes

# DPI-awareness must be set before any window is created.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Per-Monitor v1
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()    # System-aware fallback
    except Exception:
        pass

import datetime
import json
import os
import queue
import re as _re
import subprocess
import sys
import threading
import time
import tkinter
import tkinter.messagebox
import webbrowser
from pathlib import Path
from typing import Optional

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as Item, Menu

from config import (
    APP_NAME, APP_DISPLAY_NAME, DESIGNER, DESIGNER_CREDIT, DESIGNER_URL,
    ICON_PATH, SKIP_DIRS, INDEXED_EXTENSIONS,
    set_autostart, load_config, save_config, log_error,
    get_indexable_drives, refresh_exclusions, refresh_active_extensions,
)
from locales import get_text
from indexer import Indexer, get_index_stats
from ranker import rank_results
from parser import get_snippet, normalize as _normalize
from watcher import FileWatcher

# Queue message tokens
_MSG_SHOW    = "SHOW"
_MSG_REINDEX = "REINDEX"
_MSG_QUIT    = "QUIT"


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

class _RECT(ctypes.Structure):
    _fields_ = [
        ("left",   ctypes.c_long),
        ("top",    ctypes.c_long),
        ("right",  ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork",    _RECT),
        ("dwFlags",   ctypes.c_ulong),
    ]


def _work_area() -> tuple[int, int, int, int]:
    """Return work area (left, top, right, bottom) of the monitor under the cursor.

    Uses GetMonitorInfoW so results are always correct regardless of:
    - DPI scaling (100 / 125 / 150 / 200 %)
    - Multi-monitor setups (returns the monitor the user is actually on)
    - Taskbar position (bottom / top / side)
    - Per-monitor vs system DPI awareness mode
    """
    u32 = ctypes.windll.user32
    pt  = ctypes.wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    hmon = u32.MonitorFromPoint(pt, 2)   # MONITOR_DEFAULTTONEAREST = 2
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    u32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    return mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom


_SWP_NOSIZE     = 0x0001
_SWP_NOZORDER   = 0x0004
_SWP_NOACTIVATE = 0x0010


def _snap_win32(hwnd: int, wb: int) -> None:
    """Ensure window outer-bottom does not exceed wb (work-area bottom, physical px).

    Called AFTER update_idletasks() so Tk has already sent WM_WINDOWPOSCHANGED.
    GetWindowRect and GetMonitorInfoW always share the same coordinate space
    (virtual/logical/physical depending on process DPI mode), so the comparison
    is safe regardless of DPI awareness level.
    """
    try:
        r = _RECT()
        u32 = ctypes.windll.user32
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.bottom > wb:
            u32.SetWindowPos(
                hwnd, 0,
                r.left, r.top - (r.bottom - wb),
                0, 0,
                _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE,
            )
    except Exception:
        pass


# Callback type for EnumChildWindows  (stdcall: BOOL CALLBACK(HWND, LPARAM))
_ENUM_CHILD_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)


def _find_windows_search_rect() -> Optional[tuple[int, int, int, int]]:
    """
    Return (left, top, right, bottom) of the Windows Search bar in the taskbar,
    or None if it cannot be located.

    Strategy: enumerate child windows of Shell_TrayWnd; the search bar is the
    widest child that fits inside the taskbar and is not as wide as the taskbar
    itself (80 px < width < 500 px).
    """
    taskbar_hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
    if not taskbar_hwnd:
        return None

    tb = _RECT()
    ctypes.windll.user32.GetWindowRect(taskbar_hwnd, ctypes.byref(tb))
    tb_h = tb.bottom - tb.top

    candidates: list[tuple[int, int, int, int]] = []   # (width, left, top, right, bottom)

    def _cb(hwnd: int, _: int) -> bool:
        r = _RECT()
        if (ctypes.windll.user32.IsWindowVisible(hwnd) and
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))):
            w = r.right - r.left
            h = r.bottom - r.top
            # Matches search bar shape: wide, not taller than taskbar, inside taskbar vertically
            if (80 < w < 500 and h <= tb_h + 2 and
                    r.top >= tb.top - 2 and r.bottom <= tb.bottom + 2):
                candidates.append((w, r.left, r.top, r.right, r.bottom))
        return True

    _proc = _ENUM_CHILD_PROC(_cb)
    ctypes.windll.user32.EnumChildWindows(taskbar_hwnd, _proc, 0)

    if not candidates:
        return None

    # Widest matching child = the search bar
    candidates.sort(reverse=True)
    _, left, top, right, bottom = candidates[0]
    return left, top, right, bottom


def _apply_acrylic(hwnd: int, dark: bool) -> None:
    """
    Apply Windows acrylic blur-behind to any HWND via SetWindowCompositionAttribute.
    Works on Windows 10 1809+ and Windows 11.  Silently no-ops on older builds.
    Color format for GradientColor: 0xAABBGGRR (little-endian ABGR).
    """
    try:
        class _Accent(ctypes.Structure):
            _fields_ = [
                ("AccentState",   ctypes.c_int),
                ("AccentFlags",   ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId",   ctypes.c_int),
            ]

        class _WCAD(ctypes.Structure):
            _fields_ = [
                ("Attribute",  ctypes.c_int),
                ("pData",      ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        # ABGR: A=opacity, B, G, R
        # Dark  → 72% opaque very dark navy  (#18181e → R=0x18 G=0x18 B=0x1e)
        # Light → 82% opaque near-white       (#f2f4f8 → R=0xf2 G=0xf4 B=0xf8)
        color = 0xB8_1E_18_18 if dark else 0xD1_F8_F4_F2

        a = _Accent()
        a.AccentState   = 4     # ACCENT_ENABLE_ACRYLICBLURBEHIND
        a.GradientColor = color

        d = _WCAD()
        d.Attribute  = 19       # WCA_ACCENT_POLICY
        d.pData      = ctypes.addressof(a)
        d.SizeOfData = ctypes.sizeof(a)

        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(d))
    except Exception as exc:
        log_error(f"Acrylic: {exc}")


# ---------------------------------------------------------------------------
# Icon generation  (no external .ico required)
# ---------------------------------------------------------------------------

def _make_icon(size: int = 64) -> Image.Image:
    """
    Modern search-app icon: blue rounded-square + white magnifying glass.
    Readable on both dark and light taskbar themes at 16–64 px.
    """
    import math

    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Background: blue rounded square ──────────────────────────────────────
    bg_r = max(4, size // 7)          # corner radius
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=bg_r,
        fill=(37, 99, 235, 255),      # #2563eb
    )

    # ── Magnifying glass ─────────────────────────────────────────────────────
    lw  = max(2, size // 13)          # stroke width
    pad = size * 0.15                 # inset from edge

    # Lens circle — upper-left quadrant
    lr  = size * 0.24                 # lens radius
    cx  = pad + lr                    # lens centre x
    cy  = pad + lr                    # lens centre y
    draw.ellipse(
        [cx - lr, cy - lr, cx + lr, cy + lr],
        outline=(255, 255, 255, 255),
        width=lw,
    )

    # Handle — from SE edge of lens toward bottom-right, capped inside icon
    angle = math.radians(45)          # 45° = south-east direction
    hx0   = cx + lr * math.cos(angle)
    hy0   = cy + lr * math.sin(angle)
    hx1   = size - pad - lw
    hy1   = size - pad - lw
    draw.line([(hx0, hy0), (hx1, hy1)], fill=(255, 255, 255, 255), width=lw + 1)

    return img


def _save_ico(img: Image.Image) -> None:
    """
    Write a proper multi-resolution ICO to ICON_PATH.
    Prefers copying the bundled/source icon.ico (7 sizes, 16–256 px) over
    saving the PIL-generated one, which only has a few sizes and can produce
    a blank taskbar icon on some Windows versions.
    """
    import shutil

    # 1. Frozen bundle: icon.ico is packed next to the exe's _internal folder
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "icon.ico"
        if bundled.exists():
            try:
                shutil.copy2(str(bundled), str(ICON_PATH))
                return
            except Exception:
                pass

    # 2. Running from source: use the source icon.ico if it exists alongside main.py
    src_ico = Path(__file__).with_name("icon.ico")
    if src_ico.exists():
        try:
            shutil.copy2(str(src_ico), str(ICON_PATH))
            return
        except Exception:
            pass

    # 3. Fallback: save PIL-generated ICO with all standard Windows sizes
    try:
        sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        frames = [img.resize((s, s), Image.LANCZOS) for s in [sz[0] for sz in sizes]]
        frames[0].save(
            str(ICON_PATH), format="ICO",
            append_images=frames[1:],
            sizes=sizes,
        )
    except Exception as exc:
        log_error(f"Icon save: {exc}")


# ---------------------------------------------------------------------------
# File-type badge colours  (extension → hex colour)
# ---------------------------------------------------------------------------

_EXT_COLORS: dict[str, str] = {
    # Documents
    ".pdf":  "#e53935",
    ".docx": "#1e88e5",
    ".xlsx": "#43a047",
    ".pptx": "#fb8c00",
    ".txt":  "#78909c",
    ".md":   "#8e24aa",
    ".csv":  "#00897b",
    ".json": "#00acc1",
    ".xml":  "#6d4c41",
    # Adobe
    ".psd":  "#d81b60",
    ".ai":   "#ff6f00",
    # Images
    ".jpg":  "#ec407a",
    ".jpeg": "#ec407a",
    ".png":  "#5c6bc0",
    ".gif":  "#26a69a",
    ".bmp":  "#8d6e63",
    ".svg":  "#ef6c00",
    ".webp": "#42a5f5",
    ".tiff": "#ab47bc",
    ".tif":  "#ab47bc",
    # Videos
    ".mp4":  "#1565c0",
    ".avi":  "#283593",
    ".mkv":  "#00695c",
    ".mov":  "#bf360c",
    ".wmv":  "#6a1b9a",
    ".flv":  "#b71c1c",
    ".webm": "#2e7d32",
    ".m4v":  "#0277bd",
}
_DEFAULT_COLOR = "#546e7a"

# Extension categories used in the Settings → Uzantılar tab
_EXT_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("Belgeler", "📄", [".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".json", ".xml"]),
    ("Resimler", "🖼", [".psd", ".ai", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tiff", ".tif"]),
    ("Videolar", "🎬", [".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"]),
]


def _ext_color(suffix: str) -> str:
    return _EXT_COLORS.get(suffix.lower(), _DEFAULT_COLOR)


def _ext_label(suffix: str) -> str:
    return suffix.lstrip(".").upper()[:4]


# ---------------------------------------------------------------------------
# Query syntax parser  (ext:pdf  size:>10mb  modified:today)
# ---------------------------------------------------------------------------

def parse_query(raw: str) -> tuple[str, dict]:
    """
    Strip filter tokens from a search query and return (clean_query, filters).

    Supported syntax:
      ext:pdf            → extension filter (comma-separated: ext:pdf,docx)
      size:>10mb         → size filter  (operators: > < =, units: b kb mb gb)
      modified:today     → date filter  (today / yesterday / week / month)
      before:2024-01-15  → files modified before this date (YYYY-MM-DD)
      after:2023-06-01   → files modified after this date  (YYYY-MM-DD)
      regex:pattern      → regex matched against filename
    """
    filters: dict = {}

    m = _re.search(r'\bext:(\S+)', raw, _re.IGNORECASE)
    if m:
        exts = [('.' + e.lstrip('.').lower()) for e in m.group(1).split(',')]
        filters['extensions'] = exts
        raw = raw[:m.start()] + raw[m.end():]

    m = _re.search(r'\bsize:([<>=])(\d+(?:\.\d+)?)(b|kb|mb|gb)?', raw, _re.IGNORECASE)
    if m:
        op, val, unit = m.group(1), float(m.group(2)), (m.group(3) or 'b').lower()
        mult = {'b': 1, 'kb': 1024, 'mb': 1024 ** 2, 'gb': 1024 ** 3}
        filters['size_op']    = op
        filters['size_bytes'] = int(val * mult.get(unit, 1))
        raw = raw[:m.start()] + raw[m.end():]

    m = _re.search(r'\bmodified:(today|yesterday|week|month)\b', raw, _re.IGNORECASE)
    if m:
        filters['modified'] = m.group(1).lower()
        raw = raw[:m.start()] + raw[m.end():]

    m = _re.search(r'\bbefore:(\d{4}-\d{2}-\d{2})\b', raw, _re.IGNORECASE)
    if m:
        filters['before'] = m.group(1)
        raw = raw[:m.start()] + raw[m.end():]

    m = _re.search(r'\bafter:(\d{4}-\d{2}-\d{2})\b', raw, _re.IGNORECASE)
    if m:
        filters['after'] = m.group(1)
        raw = raw[:m.start()] + raw[m.end():]

    m = _re.search(r'\bregex:(\S+)', raw, _re.IGNORECASE)
    if m:
        filters['regex'] = m.group(1)
        raw = raw[:m.start()] + raw[m.end():]

    # ── Boolean operators (AND / OR / NOT) ────────────────────────────────────
    _BOOL_RE = _re.compile(r'\b(AND|OR|NOT)\b', _re.IGNORECASE)
    if _BOOL_RE.search(raw.strip()):
        parts = []
        for token in raw.strip().split():
            upper = token.upper()
            if upper in ('AND', 'OR', 'NOT'):
                parts.append(upper)
            else:
                norm_tok = _normalize(token)
                if norm_tok:
                    parts.append(f'"{norm_tok}"')
        if parts and any(p not in ('AND', 'OR', 'NOT') for p in parts):
            filters['fts_expr'] = ' '.join(parts)
            # Keep raw as-is for filename supplement search

    return raw.strip(), filters


# ---------------------------------------------------------------------------
# Snippet highlight helper
# ---------------------------------------------------------------------------

def _insert_highlighted(txt_widget: tkinter.Text, text: str, query: str) -> None:
    """Insert *text* into a disabled tkinter.Text, bolding any query term matches."""
    terms = []
    q = query.strip().lower()
    if q:
        terms.append(q)
    for w in q.split():
        if len(w) >= 2 and w not in terms:
            terms.append(w)

    if not terms:
        txt_widget.insert("end", text)
        return

    pattern = _re.compile(
        "|".join(_re.escape(t) for t in sorted(terms, key=len, reverse=True)),
        _re.IGNORECASE,
    )

    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            txt_widget.insert("end", text[pos : m.start()])
        txt_widget.insert("end", m.group(), "hl")
        pos = m.end()
    if pos < len(text):
        txt_widget.insert("end", text[pos:])


# ---------------------------------------------------------------------------
# Search result card  (Windows-Search-like design)
# ---------------------------------------------------------------------------

class ResultCard(ctk.CTkFrame):
    """Single search result — coloured badge + filename + path + date + snippet."""

    _BASE   = ("#ffffff", "#1e1e2e")
    _HOVER  = ("#eff6ff", "#252540")
    _BORDER = ("#e5e7eb", "#2d2d42")

    def __init__(self, master, result: dict, query: str, on_open, on_preview=None, **kwargs):
        kwargs.setdefault("corner_radius", 12)
        kwargs.setdefault("fg_color", self._BASE)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", self._BORDER)
        super().__init__(master, **kwargs)

        self._result     = result
        self._on_open    = on_open
        self._on_preview = on_preview
        self._no_click: set = set()

        path       = Path(result["path"])
        suffix     = path.suffix.lower()
        is_app     = result.get("entry_type") == "app"
        is_folder  = result.get("entry_type") == "folder"
        connected  = bool(result.get("is_connected", 1))
        snippet    = "" if (is_app or is_folder) else get_snippet(result.get("content", ""), query)
        if is_app:
            color = "#7c3aed"
            badge_text = "APP"
        elif is_folder:
            color = "#0369a1"
            badge_text = "DIR"
        else:
            color = _ext_color(suffix) if connected else "#616161"
            badge_text = _ext_label(suffix)

        _sz = result.get("file_size", 0) or 0
        if _sz >= 1024 ** 3:
            size_str = f"{_sz / 1024**3:.1f} GB"
        elif _sz >= 1024 ** 2:
            size_str = f"{_sz / 1024**2:.1f} MB"
        elif _sz >= 1024:
            size_str = f"{_sz / 1024:.0f} KB"
        elif _sz > 0:
            size_str = f"{_sz} B"
        else:
            size_str = ""

        try:
            date_str = datetime.datetime.fromtimestamp(
                result.get("modified_time", 0)
            ).strftime("%d.%m.%Y")
        except Exception:
            date_str = ""

        self.grid_columnconfigure(1, weight=1)

        # ── Left: coloured extension badge (rowspan=4 covers all rows) ───────
        badge = ctk.CTkFrame(self, width=52, height=52, corner_radius=12, fg_color=color)
        badge.grid(row=0, column=0, rowspan=4, padx=(12, 10), pady=12, sticky="n")
        badge.grid_propagate(False)

        ctk.CTkLabel(
            badge,
            text=badge_text,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="white",
        ).place(relx=0.5, rely=0.5, anchor="center")

        # ── Row 0: filename (+ optional disconnected badge) ──────────────────
        name_row = ctk.CTkFrame(self, fg_color="transparent")
        name_row.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=(12, 2))
        name_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            name_row,
            text=path.name,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=("#0f172a", "#f1f5f9") if connected else ("#9e9e9e", "#64748b"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        if not connected:
            ctk.CTkLabel(
                name_row,
                text=f"  {get_text('disconnected')}  ",
                font=ctk.CTkFont(family="Segoe UI", size=9, weight="bold"),
                text_color="#ef5350",
                fg_color=("#fee2e2", "#3b1212"),
                corner_radius=4,
                anchor="e",
            ).grid(row=0, column=1, sticky="e", padx=(4, 0))

        # ── Row 1: path (left) + date (right) ───────────────────────────────
        meta_row = ctk.CTkFrame(self, fg_color="transparent")
        meta_row.grid(row=1, column=1, sticky="ew", padx=(0, 12))
        meta_row.grid_columnconfigure(0, weight=1)

        parent_str = str(path.parent)
        if len(parent_str) > 58:
            parent_str = "…" + parent_str[-56:]

        ctk.CTkLabel(
            meta_row,
            text=parent_str,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#6b7280", "#9ca3af"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        meta_right = ctk.CTkFrame(meta_row, fg_color="transparent")
        meta_right.grid(row=0, column=1, sticky="e")

        if size_str and not is_app:
            ctk.CTkLabel(
                meta_right,
                text=size_str,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                text_color=("#9ca3af", "#6b7280"),
                anchor="e",
            ).pack(side="left", padx=(0, 6))

        if date_str:
            ctk.CTkLabel(
                meta_right,
                text=date_str,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                text_color=("#9ca3af", "#6b7280"),
                anchor="e",
            ).pack(side="left")

        # ── Row 2: snippet with keyword highlight ───────────────────────────
        self._txt_widgets: list[tkinter.Text] = []
        snippet_frame = ctk.CTkFrame(self, fg_color="transparent", height=70)
        snippet_frame.grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=(2, 4))
        snippet_frame.grid_propagate(False)
        snippet_frame.grid_columnconfigure(0, weight=1)

        if snippet:
            dark    = ctk.get_appearance_mode() == "Dark"
            base_bg = self._BASE[1 if dark else 0]
            txt_fg  = "#94a3b8" if dark else "#4b5563"
            hl_fg   = "#818cf8" if dark else "#6366f1"

            txt = tkinter.Text(
                snippet_frame,
                wrap="word", height=4,
                font=("Segoe UI", 11),
                bg=base_bg, fg=txt_fg,
                relief="flat", bd=0,
                highlightthickness=0,
                selectbackground=base_bg,
                inactiveselectbackground=base_bg,
                cursor="arrow",
                padx=0, pady=0,
            )
            txt.tag_configure("hl", foreground=hl_fg, font=("Segoe UI", 11, "bold"))
            txt.config(state="normal")
            _insert_highlighted(txt, snippet, query)
            txt.config(state="disabled")
            txt.grid(row=0, column=0, sticky="nw")
            self._txt_widgets.append(txt)

        # ── Row 3: action buttons ────────────────────────────────────────────
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=3, column=1, sticky="e", padx=(0, 12), pady=(0, 8))
        self._no_click.add(btn_row)

        loc_btn = ctk.CTkButton(
            btn_row,
            text="📂  Klasörde Göster",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            width=148, height=26,
            fg_color="#2563eb", hover_color="#1d4ed8",
            text_color="white", corner_radius=7,
            command=self._go_to_location,
        )
        loc_btn.pack(side="left", padx=(0, 6))
        self._no_click.add(loc_btn)

        copy_btn = ctk.CTkButton(
            btn_row,
            text="📋  Yolu Kopyala",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            width=120, height=26,
            fg_color=("#e0e7ff", "#1e1b4b"),
            hover_color=("#c7d2fe", "#312e81"),
            text_color=("#3730a3", "#818cf8"),
            corner_radius=7,
            command=self._copy_path,
        )
        copy_btn.pack(side="left")
        self._no_click.add(copy_btn)

        self._bind_recursive()

    # ── Hover helpers ─────────────────────────────────────────────────────────

    def _set_hover(self, on: bool) -> None:
        color = self._HOVER if on else self._BASE
        self.configure(fg_color=color)
        if self._txt_widgets:
            dark   = ctk.get_appearance_mode() == "Dark"
            txt_bg = color[1 if dark else 0]
            for tw in self._txt_widgets:
                try:
                    tw.configure(
                        bg=txt_bg,
                        selectbackground=txt_bg,
                        inactiveselectbackground=txt_bg,
                    )
                except Exception:
                    pass

    def _bind_recursive(self):
        for w in [self, *self._iter_children()]:
            w.bind("<Button-1>",        self._click)
            w.bind("<Double-Button-1>", self._double_click)
            w.bind("<Button-3>",        self._show_context_menu)
            w.bind("<Enter>",    lambda _e: self._set_hover(True))
            w.bind("<Leave>",    lambda _e: self._set_hover(False))
        for tw in self._txt_widgets:
            tw.bind("<Button-1>",        self._click)
            tw.bind("<Double-Button-1>", self._double_click)
            tw.bind("<Button-3>",        self._show_context_menu)
            tw.bind("<Enter>",    lambda _e: self._set_hover(True))
            tw.bind("<Leave>",    lambda _e: self._set_hover(False))

    def _iter_children(self):
        stack = list(self.winfo_children())
        while stack:
            w = stack.pop()
            if w in self._no_click:
                continue
            yield w
            stack.extend(w.winfo_children())

    # ── Actions ───────────────────────────────────────────────────────────────

    def _click(self, _event=None):
        path = self._result["path"]
        etype = self._result.get("entry_type")
        if etype == "app":
            try:
                os.startfile(path)
            except Exception as exc:
                log_error(f"App launch {path}: {exc}")
        elif etype == "folder":
            try:
                os.startfile(path)
            except Exception as exc:
                log_error(f"Folder open {path}: {exc}")
        elif self._on_preview:
            self._on_preview(self._result)
        else:
            self._on_open(path)

    def _double_click(self, _event=None):
        etype = self._result.get("entry_type")
        if etype == "folder":
            try:
                os.startfile(self._result["path"])
            except Exception:
                pass
        elif etype != "app":
            self._on_open(self._result["path"])

    def _go_to_location(self, _e=None):
        p = self._result["path"]
        _opened = False
        try:
            shell32 = ctypes.windll.shell32
            ole32   = ctypes.windll.ole32
            shell32.SHParseDisplayName.restype         = ctypes.c_long
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
            pidl  = ctypes.c_void_p()
            sfgao = ctypes.c_ulong(0)
            hr = shell32.SHParseDisplayName(
                p, None, ctypes.byref(pidl), 0, ctypes.byref(sfgao)
            )
            if hr == 0 and pidl.value:
                shell32.SHOpenFolderAndSelectItems(pidl.value, 0, None, 0)
                ole32.CoTaskMemFree(pidl)
                _opened = True
        except Exception as exc:
            log_error(f"SHOpenFolderAndSelectItems: {exc}")
        if not _opened:
            try:
                os.startfile(str(Path(p).parent))
            except Exception:
                pass

    def _copy_path(self, _e=None):
        try:
            self.clipboard_clear()
            self.clipboard_append(self._result["path"])
        except Exception:
            pass

    def _copy_file(self, _e=None):
        """Copy the actual file to clipboard (CF_HDROP) so it can be pasted in Explorer."""
        path = self._result["path"]
        try:
            # Build a DROPFILES structure followed by a null-terminated wide path + double-null
            import struct
            path_w  = (path + "\0\0").encode("utf-16-le")
            # DROPFILES header: pFiles offset (20), pt.x, pt.y, fNC, fWide
            header  = struct.pack("<LLLLL", 20, 0, 0, 0, 1)  # 20 bytes, fWide=1
            data    = header + path_w

            CF_HDROP   = 15
            GMEM_MOVEABLE = 0x0002
            k32 = ctypes.windll.kernel32
            u32 = ctypes.windll.user32

            h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                return
            ptr = k32.GlobalLock(h)
            if not ptr:
                k32.GlobalFree(h)
                return
            ctypes.memmove(ptr, data, len(data))
            k32.GlobalUnlock(h)

            if u32.OpenClipboard(None):
                u32.EmptyClipboard()
                u32.SetClipboardData(CF_HDROP, h)
                u32.CloseClipboard()
        except Exception as exc:
            log_error(f"Copy file to clipboard: {exc}")

    def _open_terminal_cmd(self):
        folder = str(Path(self._result["path"]).parent)
        try:
            subprocess.Popen(["cmd", "/k", f'cd /d "{folder}"'], creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception as exc:
            log_error(f"CMD open: {exc}")

    def _open_terminal_ps(self):
        folder = str(Path(self._result["path"]).parent)
        try:
            subprocess.Popen(
                ["powershell", "-NoExit", "-Command", f'Set-Location "{folder}"'],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as exc:
            log_error(f"PowerShell open: {exc}")

    def _open_admin(self):
        try:
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", self._result["path"], None, None, 1
            )
        except Exception as exc:
            log_error(f"Admin open: {exc}")

    def _open_vscode(self):
        path = self._result["path"]
        for cmd in ("code", "code.cmd"):
            try:
                subprocess.Popen([cmd, path])
                return
            except FileNotFoundError:
                continue
        log_error("VS Code not found in PATH")

    def _show_context_menu(self, event):
        etype  = self._result.get("entry_type")
        is_app = etype == "app"
        is_dir = etype == "folder"
        menu   = tkinter.Menu(self, tearoff=0)
        menu.add_command(label="Klasörü Aç" if is_dir else "Dosyayı Aç", command=self._click)
        if not is_app:
            menu.add_command(label="Klasörde Göster", command=self._go_to_location)
        menu.add_command(label="Yolu Kopyala", command=self._copy_path)
        if not is_app and not is_dir:
            menu.add_command(label="Dosyayı Kopyala", command=self._copy_file)
        if not is_app:
            menu.add_separator()
            menu.add_command(label="Terminal (CMD) Burada Aç",        command=self._open_terminal_cmd)
            menu.add_command(label="Terminal (PowerShell) Burada Aç", command=self._open_terminal_ps)
        if not is_app and not is_dir:
            menu.add_separator()
            menu.add_command(label="Yönetici Olarak Aç", command=self._open_admin)
            menu.add_command(label="VS Code ile Aç",     command=self._open_vscode)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


# ---------------------------------------------------------------------------
# Taskbar search bar  (always-visible pill near the taskbar)
# ---------------------------------------------------------------------------

class TaskbarBar(ctk.CTkToplevel):
    """
    Persistent search bar embedded in the taskbar area.
    Layout: [⠿ grip | ⌕ | ──── Ara... ────]
    • Only the ⠿ grip zone allows dragging (prevents accidental moves).
    • Min-distance threshold (6 px) further guards against micro-jitter.
    • Corner transparency via chroma-key so the pill floats cleanly.
    • Position persists across restarts (saved to config.json).
    • Right-click → context menu.
    """

    BAR_W = 230
    BAR_H = 36
    # A unique near-black colour used as chroma key for corner transparency.
    # Tkinter's -transparentcolor attribute makes every pixel of this colour
    # transparent, so the rounded pill appears to float without a background.
    _CHROMA = "#010203"

    def __init__(self, master: ctk.CTk, on_click, quit_cb=None) -> None:
        super().__init__(master)
        self._on_click = on_click
        self._quit_cb  = quit_cb
        self._dragged  = False
        self._dx = self._dy = 0
        self._press_x = self._press_y = 0

        # Read current autostart state from registry for checkbutton
        import winreg as _wr
        _is_on = False
        try:
            with _wr.OpenKey(_wr.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, _wr.KEY_READ) as _k:
                try:
                    _wr.QueryValueEx(_k, APP_NAME)
                    _is_on = True
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        self._autostart_var = tkinter.BooleanVar(value=_is_on)

        self._winevent_hook = None
        self._winevent_cb   = None

        self._setup_window()
        self._build_ui()
        # Defer _position so it fires AFTER CTkToplevel's own after(0) callbacks
        # that reposition the window during event-loop startup.
        self.after(80,  self._position)
        self.after(140, lambda: self.attributes("-transparentcolor", self._CHROMA))
        self.after(260, self._position)   # second call overrides any CTk internal repositioning
        self.after(100, self._keep_on_top)
        self.after(300, self._install_foreground_hook)
        self.after(400, self._start_topmost_thread)
        # Re-show immediately if anything withdraws us (CTkToplevel internals,
        # Windows focus management, etc.)
        self.bind("<Unmap>", self._on_unmap)

    # ── Window setup ─────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        self.overrideredirect(True)
        self.attributes("-topmost",    True)
        self.attributes("-toolwindow", True)
        self.configure(fg_color=self._CHROMA)

    # ── UI build ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._pill = ctk.CTkFrame(
            self, height=32, corner_radius=16,
            fg_color=("#3c3f41", "#3c3f41"),
            border_width=2, border_color=("#9ea3ab", "#9ea3ab"),
        )
        self._pill.pack(fill="both", expand=True, padx=3, pady=2)
        self._pill.grid_columnconfigure(2, weight=1)   # col 2 = entry (expands)
        self._pill.grid_propagate(False)

        # ── col 0: Drag grip (ONLY draggable zone) ───────────────────────────
        self._grip = ctk.CTkLabel(
            self._pill,
            text="⠿",
            width=22, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=15),
            text_color="#6b7280",
            cursor="fleur",
        )
        self._grip.grid(row=0, column=0, padx=(7, 0))

        self._grip.bind("<ButtonPress-1>",   self._press)
        self._grip.bind("<B1-Motion>",       self._do_drag)
        self._grip.bind("<ButtonRelease-1>", self._release)
        self._grip.bind("<Enter>", lambda _: self._grip.configure(text_color="#c0c6ce"))
        self._grip.bind("<Leave>", lambda _: self._grip.configure(text_color="#6b7280"))
        self._grip.bind("<Button-3>", self._show_context_menu)

        # ── col 1: Search icon ───────────────────────────────────────────────
        ctk.CTkLabel(
            self._pill, text="⌕", width=22, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=15),
            text_color="#9ea3aa",
        ).grid(row=0, column=1, padx=(4, 0))

        # ── col 2: Click-to-open-search label ───────────────────────────────
        click_lbl = ctk.CTkLabel(
            self._pill,
            text="Ara...",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color="#9ea3aa",
            anchor="w",
            cursor="hand2",
        )
        click_lbl.grid(row=0, column=2, sticky="ew", padx=(3, 8))
        click_lbl.bind("<Button-1>", self._on_pill_click)

        # Right-click on pill background → context menu; left-click → open popup
        self._pill.bind("<Button-1>", self._on_pill_click)
        self._pill.bind("<Button-3>", self._show_context_menu)
        self._pill.bind("<Enter>", lambda _: self._pill.configure(border_color="#c0c5ce"))
        self._pill.bind("<Leave>", lambda _: self._pill.configure(border_color="#9ea3ab"))

    # ── Public API ───────────────────────────────────────────────────────────

    def _on_pill_click(self, _event=None) -> None:
        """Open search popup when bar is clicked (but not after a drag)."""
        if not self._dragged:
            self._on_click()

    # ── Positioning (with persistence) ───────────────────────────────────────

    # ── Prevent any external code from hiding the bar ───────────────────────
    # CTkToplevel internals and Windows focus management both call withdraw().
    # We override it to a no-op so the bar can never be hidden programmatically.
    # The only way to stop showing it is via _shutdown() → root.quit().
    def withdraw(self) -> None:
        pass

    def iconify(self) -> None:
        pass

    def _position(self) -> None:
        cfg = load_config()
        sx, sy = cfg.get("bar_x"), cfg.get("bar_y")

        if sx is not None and sy is not None:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            sx, sy = int(sx), int(sy)
            if (0 <= sx <= sw - self.BAR_W) and (0 <= sy <= sh - self.BAR_H):
                # Auto-correct: if bar overlaps Shell_TrayWnd (taskbar zone) it will
                # always be covered. Detect overlap and move above taskbar instead.
                _wl, _wt, _wr, wb = _work_area()
                bar_bottom = sy + self.BAR_H
                if bar_bottom > wb:
                    # Saved position is inside the taskbar area — move above it.
                    sy = wb - self.BAR_H - 4
                    cfg2 = load_config()
                    cfg2["bar_y"] = sy
                    save_config(cfg2)
                self.geometry(f"{self.BAR_W}x{self.BAR_H}+{sx}+{sy}")
                return

        x, y = self._default_xy()
        self.geometry(f"{self.BAR_W}x{self.BAR_H}+{x}+{y}")

    # SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE|SWP_SHOWWINDOW — the last flag
    # forces the window back to visible state even if it was unmapped/hidden.
    _SWP_TOPMOST_FLAGS = 0x0053

    def _assert_topmost(self) -> None:
        """One-shot: push our window to the top of the TOPMOST z-band."""
        try:
            ctypes.windll.user32.SetWindowPos(
                self.winfo_id(), -1, 0, 0, 0, 0, self._SWP_TOPMOST_FLAGS,
            )
        except Exception:
            self.lift()

    def _on_unmap(self, _e=None) -> None:
        """Safety net: if OS somehow unmaps the bar, restore it immediately."""
        try:
            # ShowWindow(SW_SHOWNA=8) makes the window visible without activating it.
            # Must call this BEFORE SetWindowPos — z-order resets only work on visible windows.
            ctypes.windll.user32.ShowWindow(self.winfo_id(), 8)
        except Exception:
            pass
        self.after(10, self._assert_topmost)

    def _keep_on_top(self) -> None:
        """Polling at 100 ms — re-asserts TOPMOST z-order."""
        self._assert_topmost()
        self.after(100, self._keep_on_top)

    def _install_foreground_hook(self) -> None:
        """
        Register a WinEvent hook (EVENT_SYSTEM_FOREGROUND) on a dedicated
        thread with its own GetMessage loop.  The callback fires on that thread
        and calls SetWindowPos directly — no waiting for the tkinter event loop.
        This is the fastest possible Win32 response path.
        """
        try:
            _WinEventProc = ctypes.WINFUNCTYPE(
                None,
                ctypes.wintypes.HANDLE,   # hWinEventHook
                ctypes.wintypes.DWORD,    # event
                ctypes.wintypes.HWND,     # hwnd
                ctypes.wintypes.LONG,     # idObject
                ctypes.wintypes.LONG,     # idChild
                ctypes.wintypes.DWORD,    # idEventThread
                ctypes.wintypes.DWORD,    # dwmsEventTime
            )
            bar_hwnd  = self.winfo_id()
            swp_flags = self._SWP_TOPMOST_FLAGS
            user32    = ctypes.windll.user32

            def _cb(hook, event, hwnd, obj, child, tid, ts):
                user32.SetWindowPos(bar_hwnd, -1, 0, 0, 0, 0, swp_flags)

            # Keep reference alive on self so the callback is never GC'd
            self._winevent_cb = _WinEventProc(_cb)
            cb_ref = self._winevent_cb

            def _hook_thread():
                # Register hook from THIS thread so callbacks fire here directly
                hook = user32.SetWinEventHook(
                    0x0003, 0x0003,  # EVENT_SYSTEM_FOREGROUND only
                    None, cb_ref, 0, 0,
                    0x0000,          # WINEVENT_OUTOFCONTEXT
                )
                self._winevent_hook = hook
                msg = ctypes.wintypes.MSG()
                # Own message pump — delivers WinEvent callbacks without tkinter delay
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

            threading.Thread(
                target=_hook_thread, daemon=True, name="DSL-winevent"
            ).start()

        except Exception as exc:
            log_error(f"WinEvent hook failed: {exc}")

    def _start_topmost_thread(self) -> None:
        """
        30 fps daemon thread that re-asserts TOPMOST z-order completely
        independent of the tkinter event loop.  Eliminates the visible blink
        when Start menu or any TOPMOST window activates — the tkinter after()
        timer at 100 ms leaves a perceptible gap; this thread closes it.
        """
        hwnd      = self.winfo_id()
        user32    = ctypes.windll.user32
        swp_flags = self._SWP_TOPMOST_FLAGS

        def _loop():
            while True:
                try:
                    # ShowWindow(SW_SHOWNA=8) ensures the window is visible before
                    # SetWindowPos — a hidden/unmapped window ignores z-order calls.
                    user32.ShowWindow(hwnd, 8)
                    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, swp_flags)
                except Exception:
                    pass
                time.sleep(0.010)

        threading.Thread(target=_loop, daemon=True, name="DSL-topmost").start()

    def _default_xy(self) -> tuple[int, int]:
        """
        Position bar just ABOVE the taskbar (inside the work area).
        This avoids z-order conflicts with Shell_TrayWnd — since the taskbar
        and our bar no longer overlap, the taskbar's TOPMOST re-assertions
        cannot cover us.
        """
        wl, _wt, wr, wb = _work_area()
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)

        # Vertically: just above the taskbar (work area bottom edge)
        y = wb - self.BAR_H - 4

        import winreg as _wr
        alignment = 1
        try:
            with _wr.OpenKey(
                _wr.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
            ) as k:
                alignment, _ = _wr.QueryValueEx(k, "TaskbarAl")
        except Exception:
            pass

        # Centre-aligned (Windows 11 default) → centred horizontally
        # Left-aligned → left side with a small offset
        START_W = 52
        if alignment == 1:
            x = screen_w // 2 - self.BAR_W // 2
        else:
            x = wl + START_W + 4

        return x, y

    def _move_above_taskbar(self) -> None:
        """Snap the bar to just above the taskbar and save the new position."""
        _, _, _, wb = _work_area()
        x = max(0, self.winfo_x())
        y = wb - self.BAR_H - 4
        self.geometry(f"{self.BAR_W}x{self.BAR_H}+{x}+{y}")
        cfg = load_config()
        cfg["bar_x"] = x
        cfg["bar_y"] = y
        save_config(cfg)

    # ── Drag (grip zone only) ─────────────────────────────────────────────────

    _MIN_DRAG_PX = 6   # Must move at least 6 px before drag starts

    def _press(self, event) -> None:
        self._dragged   = False
        self._press_x   = event.x_root
        self._press_y   = event.y_root
        self._dx        = event.x_root - self.winfo_x()
        self._dy        = event.y_root - self.winfo_y()

    def _do_drag(self, event) -> None:
        dist = ((event.x_root - self._press_x) ** 2 +
                (event.y_root - self._press_y) ** 2) ** 0.5
        if dist >= self._MIN_DRAG_PX:
            self._dragged = True
            self.geometry(f"+{event.x_root - self._dx}+{event.y_root - self._dy}")

    def _release(self, _event=None) -> None:
        if self._dragged:
            cfg = load_config()
            cfg["bar_x"] = self.winfo_x()
            cfg["bar_y"] = self.winfo_y()
            save_config(cfg)

    # ── Context menu ─────────────────────────────────────────────────────────

    def _show_context_menu(self, event) -> None:
        menu = tkinter.Menu(self, tearoff=0,
                            bg="#2b2d30", fg="#dde1e7",
                            activebackground="#3c3f41", activeforeground="#ffffff",
                            bd=0, relief="flat")
        # Section header — indicates the drag zone
        menu.add_command(label="⠿  Sürükle  (sol tutamaçtan)",
                         state="disabled", foreground="#6b7280")
        menu.add_separator()
        menu.add_command(label="Konumu Sıfırla", command=self._reset_position)
        menu.add_command(label="Görev Çubuğu Üstüne Taşı", command=self._move_above_taskbar)
        menu.add_separator()
        menu.add_checkbutton(
            label="Başlangıçta Çalıştır",
            variable=self._autostart_var,
            command=self._toggle_autostart,
            selectcolor="#4ade80",
        )
        if self._quit_cb:
            menu.add_separator()
            menu.add_command(label="Çıkış", command=self._quit_cb)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _reset_position(self) -> None:
        cfg = load_config()
        cfg.pop("bar_x", None)
        cfg.pop("bar_y", None)
        save_config(cfg)
        x, y = self._default_xy()
        self.geometry(f"{self.BAR_W}x{self.BAR_H}+{x}+{y}")

    def _toggle_autostart(self) -> None:
        # _autostart_var is already flipped by the checkbutton before this fires
        set_autostart(self._autostart_var.get())


# ---------------------------------------------------------------------------
# Settings panel  (Klasörler + Uzantılar tabs)
# ---------------------------------------------------------------------------

class SettingsPanel(ctk.CTkToplevel):
    """
    Modal settings window with two tabs:
      • Klasörler  — folder exclusions (extra_exclusions)
      • Uzantılar  — indexed file-extension toggles (disabled_extensions)
    """

    def __init__(self, master: ctk.CTk, app=None) -> None:
        super().__init__(master)
        self._app = app
        self.title(f"{APP_DISPLAY_NAME} — Ayarlar")
        self.geometry("500x680")
        self.resizable(False, False)
        self.grab_set()

        cfg = load_config()
        self._current_excl: set[str]  = {e.lower() for e in cfg.get("extra_exclusions", [])}
        self._disabled_ext: set[str]  = {e.lower() for e in cfg.get("disabled_extensions", [])}
        _excl_drives_raw   = {d.upper().rstrip("\\") for d in cfg.get("excluded_drives", [])}
        self._scan_usb_var  = ctk.BooleanVar(value=cfg.get("scan_removable_drives", False))
        self._scan_net_var  = ctk.BooleanVar(value=cfg.get("scan_network_drives",   False))
        self._folder_vars:  dict[str, ctk.BooleanVar] = {}
        self._drive_vars:   dict[str, ctk.BooleanVar] = {}
        self._ext_vars:     dict[str, ctk.BooleanVar] = {}
        self._excl_drives_init = _excl_drives_raw

        # Hotkey state
        hk = cfg.get("hotkey", {})
        self._hk_alt_var   = ctk.BooleanVar(value="alt"   in hk.get("modifiers", ["alt","ctrl"]))
        self._hk_ctrl_var  = ctk.BooleanVar(value="ctrl"  in hk.get("modifiers", ["alt","ctrl"]))
        self._hk_shift_var = ctk.BooleanVar(value="shift" in hk.get("modifiers", []))
        self._hk_key_var   = ctk.StringVar(value=hk.get("key", ""))

        self._build_ui()
        self.lift()
        self.focus_force()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ── Header ──────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, height=46, corner_radius=0,
                            fg_color=("#1e40af", "#0d2137"))
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        ctk.CTkLabel(
            hdr, text="⚙  Ayarlar",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color="white", anchor="w",
        ).place(relx=0, rely=0.5, anchor="w", x=16)

        # ── Tab view ─────────────────────────────────────────────────────────
        tabs = ctk.CTkTabview(
            self,
            fg_color=("#f8fafc", "#181828"),
            segmented_button_fg_color=("#e5e7eb", "#252540"),
            segmented_button_selected_color=("#1e40af", "#2563eb"),
            segmented_button_selected_hover_color=("#1d4ed8", "#3b82f6"),
            segmented_button_unselected_color=("#e5e7eb", "#252540"),
            segmented_button_unselected_hover_color=("#d1d5db", "#2d2d42"),
            text_color=("#111827", "#e2e8f0"),
            text_color_disabled=("#6b7280", "#64748b"),
        )
        tabs.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        tabs.add("Klasörler")
        tabs.add("Uzantılar")
        tabs.add("Kısayol")
        tabs.add("İstatistikler")
        tabs.add("Gelişmiş")

        self._build_folders_tab(tabs.tab("Klasörler"))
        self._build_extensions_tab(tabs.tab("Uzantılar"))
        self._build_hotkey_tab(tabs.tab("Kısayol"))
        self._build_stats_tab(tabs.tab("İstatistikler"))
        self._build_advanced_tab(tabs.tab("Gelişmiş"))

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 12))
        btn_row.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            btn_row, text="İptal",
            fg_color="transparent", border_width=1,
            text_color=("#374151", "#94a3b8"),
            hover_color=("#f3f4f6", "#374151"),
            command=self.destroy, width=100,
        ).grid(row=0, column=1, padx=(4, 0))

        ctk.CTkButton(
            btn_row, text="Kaydet",
            fg_color=("#1e40af", "#2563eb"),
            hover_color=("#1d4ed8", "#3b82f6"),
            command=self._save, width=100,
        ).grid(row=0, column=2, padx=(6, 0))

    # ── Klasörler tab ────────────────────────────────────────────────────────

    def _build_folders_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)   # folder scroll expands

        # ── Section 0: Drive selection (top) ────────────────────────────────
        drives_sec = ctk.CTkFrame(
            parent, fg_color=("#e0e7ff", "#1a1f3a"), corner_radius=8
        )
        drives_sec.grid(row=0, column=0, sticky="ew", padx=4, pady=(6, 4))

        ctk.CTkLabel(
            drives_sec,
            text="Taranacak Sürücüler  (işareti kaldır = o disk taranmasın)",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=("#1e40af", "#818cf8"),
            anchor="w",
        ).pack(anchor="w", padx=12, pady=(8, 4))

        drives_row = ctk.CTkFrame(drives_sec, fg_color="transparent")
        drives_row.pack(fill="x", padx=12, pady=(0, 8))

        all_fixed = self._get_fixed_drives()
        for root in all_fixed:
            key = root.upper().rstrip("\\")
            letter = root[0].upper()
            label  = self._drive_label(root)
            is_excluded = key in self._excl_drives_init
            var = ctk.BooleanVar(value=not is_excluded)   # True = scan (checked)
            self._drive_vars[root] = var

            cell = ctk.CTkFrame(drives_row, fg_color="transparent")
            cell.pack(side="left", padx=(0, 16))

            ctk.CTkCheckBox(
                cell,
                text=f"{letter}:  {label}",
                variable=var,
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=("#111827", "#e2e8f0"),
                checkmark_color="#3b82f6",
            ).pack()

        # ── Section 1: Top-level folder exclusions (scrollable) ──────────────
        ctk.CTkLabel(
            parent,
            text="Tarama Dışı Klasörler  —  işaretlenenler tüm sürücülerde atlanır",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#6b7280", "#94a3b8"),
            justify="left", anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 2))

        scroll = ctk.CTkScrollableFrame(
            parent, fg_color=("#f0f2f5", "#1a1a2e"), corner_radius=8,
        )
        scroll.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))

        for folder in sorted(self._get_root_folders(), key=str.lower):
            fl = folder.lower()
            is_builtin = any(fl == s or fl in s or s in fl for s in SKIP_DIRS)
            var = ctk.BooleanVar(value=(fl in self._current_excl or is_builtin))
            self._folder_vars[folder] = var

            row_f = ctk.CTkFrame(scroll, fg_color="transparent")
            row_f.pack(fill="x", pady=1)

            ctk.CTkCheckBox(
                row_f,
                text=folder,
                variable=var,
                font=ctk.CTkFont(family="Segoe UI", size=12),
                state="disabled" if is_builtin else "normal",
                text_color=("#9ca3af", "#6b7280") if is_builtin else ("#111827", "#e2e8f0"),
            ).pack(side="left", padx=12, pady=3)

            if is_builtin:
                ctk.CTkLabel(
                    row_f, text=" Sistem ",
                    font=ctk.CTkFont(family="Segoe UI", size=9),
                    text_color="#9ca3af",
                    fg_color=("#e5e7eb", "#374151"),
                    corner_radius=4,
                ).pack(side="right", padx=8)

        # ── Section 2: USB + Network (bottom) ────────────────────────────────
        extra_sec = ctk.CTkFrame(
            parent, fg_color=("#e8f0fe", "#141e35"), corner_radius=8
        )
        extra_sec.grid(row=3, column=0, sticky="ew", padx=4, pady=(4, 2))

        ctk.CTkLabel(
            extra_sec,
            text="Ek Sürücüler",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=("#1e40af", "#7c9fd4"),
            anchor="w",
        ).pack(anchor="w", padx=14, pady=(8, 2))

        ctk.CTkCheckBox(
            extra_sec,
            text="USB / Harici bellek  —  takılı çıkarılabilir diskler",
            variable=self._scan_usb_var,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=("#111827", "#93c5fd"),
            checkmark_color="#3b82f6",
        ).pack(anchor="w", padx=14, pady=(2, 4))

        ctk.CTkCheckBox(
            extra_sec,
            text="Ağ sürücüleri  —  map edilmiş paylaşımlı klasörler (A:, Z:, …)",
            variable=self._scan_net_var,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=("#111827", "#93c5fd"),
            checkmark_color="#3b82f6",
        ).pack(anchor="w", padx=14, pady=(0, 10))

    # ── Uzantılar tab ────────────────────────────────────────────────────────

    def _build_extensions_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            parent,
            text=(
                "İşareti kaldırılan uzantılar bir sonraki taramadan itibaren\n"
                "dizine alınmaz ve mevcut kayıtları silinir."
            ),
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=("#6b7280", "#94a3b8"),
            justify="left", anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 4))

        scroll = ctk.CTkScrollableFrame(
            parent, fg_color=("#f0f2f5", "#1a1a2e"), corner_radius=8,
        )
        scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

        for cat_name, cat_icon, exts in _EXT_CATEGORIES:
            # ── Category header ──────────────────────────────────────────────
            hdr = ctk.CTkFrame(scroll, fg_color=("#e2e8f0", "#252540"),
                               corner_radius=6, height=28)
            hdr.pack(fill="x", padx=6, pady=(8, 3))
            hdr.pack_propagate(False)
            ctk.CTkLabel(
                hdr,
                text=f"{cat_icon}  {cat_name}",
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=("#1e40af", "#93c5fd"),
                anchor="w",
            ).place(relx=0, rely=0.5, anchor="w", x=10)

            # ── Extension rows ───────────────────────────────────────────────
            for ext in exts:
                enabled = ext.lower() not in self._disabled_ext
                var = ctk.BooleanVar(value=enabled)
                self._ext_vars[ext] = var

                color = _ext_color(ext)
                row_f = ctk.CTkFrame(scroll, fg_color="transparent")
                row_f.pack(fill="x", pady=1)

                badge = ctk.CTkFrame(row_f, width=42, height=22,
                                     corner_radius=6, fg_color=color)
                badge.pack(side="left", padx=(20, 6), pady=2)
                badge.pack_propagate(False)
                ctk.CTkLabel(
                    badge, text=_ext_label(ext),
                    font=ctk.CTkFont(family="Segoe UI", size=9, weight="bold"),
                    text_color="white",
                ).place(relx=0.5, rely=0.5, anchor="center")

                ctk.CTkCheckBox(
                    row_f,
                    text=ext,
                    variable=var,
                    font=ctk.CTkFont(family="Segoe UI", size=12),
                    text_color=("#111827", "#e2e8f0"),
                ).pack(side="left", pady=2)

    # ── İstatistikler tab ────────────────────────────────────────────────────

    # ── Kısayol tab ──────────────────────────────────────────────────────────

    def _build_hotkey_tab(self, parent) -> None:
        _KEYS = [
            ("(Yok)", ""),
            ("F1","F1"),("F2","F2"),("F3","F3"),("F4","F4"),
            ("F5","F5"),("F6","F6"),("F7","F7"),("F8","F8"),
            ("F9","F9"),("F10","F10"),("F11","F11"),("F12","F12"),
            ("A","A"),("B","B"),("C","C"),("D","D"),("E","E"),
            ("F","F"),("G","G"),("H","H"),("I","I"),("J","J"),
            ("K","K"),("L","L"),("M","M"),("N","N"),("O","O"),
            ("P","P"),("Q","Q"),("R","R"),("S","S"),("T","T"),
            ("U","U"),("V","V"),("W","W"),("X","X"),("Y","Y"),("Z","Z"),
        ]
        _KEY_VK = {
            "F1":0x70,"F2":0x71,"F3":0x72,"F4":0x73,
            "F5":0x74,"F6":0x75,"F7":0x76,"F8":0x77,
            "F9":0x78,"F10":0x79,"F11":0x7A,"F12":0x7B,
            **{chr(c): 0x41 + (c - ord("A")) for c in range(ord("A"), ord("Z")+1)},
        }
        self._key_vk_map = _KEY_VK

        parent.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(parent, fg_color=("#e0e7ff","#1a1f3a"), corner_radius=10)
        card.grid(row=0, column=0, sticky="ew", padx=8, pady=(14, 6))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text="Global Kısayol Tuşu",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=("#1e40af","#818cf8"), anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(12,6))

        # Modifier checkboxes
        mod_row = ctk.CTkFrame(card, fg_color="transparent")
        mod_row.grid(row=1, column=0, columnspan=2, sticky="w", padx=16, pady=(0,8))

        for text, var in [("Alt", self._hk_alt_var),
                          ("Ctrl", self._hk_ctrl_var),
                          ("Shift", self._hk_shift_var)]:
            ctk.CTkCheckBox(
                mod_row, text=text, variable=var,
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=("#111827","#e2e8f0"),
                checkmark_color="#3b82f6",
                command=self._update_hotkey_preview,
            ).pack(side="left", padx=(0, 16))

        # Extra key dropdown
        ctk.CTkLabel(
            card, text="+ Ek tuş:",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=("#6b7280","#94a3b8"), anchor="w",
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(0,4))

        key_labels = [lbl for lbl, _ in _KEYS]
        key_values = [val for _, val in _KEYS]
        cur = self._hk_key_var.get()
        cur_idx = key_values.index(cur) if cur in key_values else 0

        self._key_combo = ctk.CTkOptionMenu(
            card,
            values=key_labels,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=("#f0f2f5","#252540"),
            button_color=("#1e40af","#2563eb"),
            button_hover_color=("#1d4ed8","#3b82f6"),
            text_color=("#111827","#e2e8f0"),
            command=lambda v: self._update_hotkey_preview(),
        )
        self._key_combo.set(key_labels[cur_idx])
        self._key_combo.grid(row=2, column=0, sticky="w", padx=16, pady=(0,12))
        self._key_labels = key_labels
        self._key_values = key_values

        # Preview label
        self._hotkey_preview = ctk.CTkLabel(
            card, text="",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=("#6366f1","#818cf8"),
        )
        self._hotkey_preview.grid(row=3, column=0, columnspan=2, pady=(0,14))
        self._update_hotkey_preview()

        ctk.CTkLabel(
            parent,
            text="Not: Kısayol kaydedildikten sonra hemen aktif olur.",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#9ca3af","#6b7280"),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=16, pady=(4,0))

    def _update_hotkey_preview(self) -> None:
        parts = []
        if self._hk_alt_var.get():   parts.append("Alt")
        if self._hk_ctrl_var.get():  parts.append("Ctrl")
        if self._hk_shift_var.get(): parts.append("Shift")
        idx = self._key_labels.index(self._key_combo.get()) if hasattr(self, "_key_labels") else 0
        key = self._key_values[idx] if hasattr(self, "_key_values") else ""
        if key:
            parts.append(key)
        preview = " + ".join(parts) if parts else "(tuş seçilmedi)"
        try:
            self._hotkey_preview.configure(text=f"Kısayol: {preview}")
        except Exception:
            pass

    def _build_stats_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)

        try:
            stats = get_index_stats()
        except Exception:
            stats = {}

        total   = stats.get("total_files", 0)
        db_sz   = stats.get("db_size_bytes", 0)
        last_ts = stats.get("last_indexed", 0)
        by_drv  = stats.get("by_drive", {})

        try:
            last_str = datetime.datetime.fromtimestamp(last_ts).strftime("%d.%m.%Y %H:%M") if last_ts else "—"
        except Exception:
            last_str = "—"

        def _mb(b: int) -> str:
            if b >= 1024 ** 3:
                return f"{b / 1024**3:.1f} GB"
            return f"{b / 1024**2:.1f} MB"

        rows = [
            ("📁  Toplam İndeksli Dosya",  f"{total:,}".replace(",", ".")),
            ("🗄  Veritabanı Boyutu",       _mb(db_sz)),
            ("🕐  Son İndeksleme",          last_str),
        ]

        card = ctk.CTkFrame(parent, fg_color=("#e0e7ff", "#1a1f3a"), corner_radius=10)
        card.grid(row=0, column=0, sticky="ew", padx=8, pady=(10, 6))

        for i, (label, value) in enumerate(rows):
            ctk.CTkLabel(
                card, text=label,
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=("#374151", "#94a3b8"), anchor="w",
            ).grid(row=i, column=0, sticky="w", padx=16, pady=(8 if i == 0 else 4, 4 if i < len(rows)-1 else 8))

            ctk.CTkLabel(
                card, text=value,
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                text_color=("#1e40af", "#818cf8"), anchor="e",
            ).grid(row=i, column=1, sticky="e", padx=16, pady=(8 if i == 0 else 4, 4 if i < len(rows)-1 else 8))

        card.grid_columnconfigure(1, weight=1)

        if by_drv:
            ctk.CTkLabel(
                parent, text="Sürücüye Göre Dağılım",
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=("#6366f1", "#818cf8"), anchor="w",
            ).grid(row=1, column=0, sticky="w", padx=16, pady=(8, 2))

            drv_card = ctk.CTkFrame(parent, fg_color=("#f0f2f5", "#1a1a2e"), corner_radius=8)
            drv_card.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
            drv_card.grid_columnconfigure(1, weight=1)

            for j, (drv, cnt) in enumerate(sorted(by_drv.items())):
                ctk.CTkLabel(
                    drv_card, text=f"  {drv or '(bilinmiyor)'}",
                    font=ctk.CTkFont(family="Segoe UI", size=12),
                    text_color=("#374151", "#94a3b8"), anchor="w",
                ).grid(row=j, column=0, sticky="w", padx=8, pady=3)
                ctk.CTkLabel(
                    drv_card, text=f"{cnt:,} dosya".replace(",", "."),
                    font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                    text_color=("#1e40af", "#818cf8"), anchor="e",
                ).grid(row=j, column=1, sticky="e", padx=12, pady=3)

    def _build_advanced_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)

        try:
            from context_menu import is_installed, install, uninstall
            _ctx_available = True
        except ImportError:
            _ctx_available = False

        # ── Context menu card ────────────────────────────────────────────────
        ctx_card = ctk.CTkFrame(parent, fg_color=("#e0e7ff", "#1a1f3a"), corner_radius=10)
        ctx_card.grid(row=0, column=0, sticky="ew", padx=8, pady=(12, 6))
        ctx_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            ctx_card,
            text="🖱  Sağ Tık Menüsü (Explorer)",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=("#1e40af", "#818cf8"), anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 2))

        ctk.CTkLabel(
            ctx_card,
            text="Dosyalara sağ tıkladığınızda 'DeepScan Local ile Ara' seçeneği çıkar.",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#6b7280", "#94a3b8"), anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 6))

        self._ctx_status_lbl = ctk.CTkLabel(
            ctx_card, text="",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#16a34a", "#4ade80"), anchor="w",
        )
        self._ctx_status_lbl.grid(row=2, column=0, sticky="w", padx=14, pady=(0, 4))

        btn_row = ctk.CTkFrame(ctx_card, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))

        if _ctx_available:
            installed = is_installed()
            self._ctx_status_lbl.configure(
                text="✓ Menü yüklü" if installed else "✗ Menü yüklü değil",
                text_color=("#16a34a", "#4ade80") if installed else ("#6b7280", "#9ca3af"),
            )

            def _toggle_ctx():
                if is_installed():
                    ok = uninstall()
                    if ok:
                        self._ctx_status_lbl.configure(
                            text="✗ Menü kaldırıldı", text_color=("#6b7280", "#9ca3af"))
                        ctx_btn.configure(text="Menüyü Yükle")
                else:
                    ok = install()
                    if ok:
                        self._ctx_status_lbl.configure(
                            text="✓ Menü yüklü", text_color=("#16a34a", "#4ade80"))
                        ctx_btn.configure(text="Menüyü Kaldır")

            ctx_btn = ctk.CTkButton(
                btn_row,
                text="Menüyü Kaldır" if installed else "Menüyü Yükle",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                width=140, height=30,
                fg_color=("#991b1b" if installed else "#1e40af",
                           "#7f1d1d" if installed else "#1e3a8a"),
                hover_color=("#7f1d1d" if installed else "#1d4ed8",
                              "#991b1b" if installed else "#2563eb"),
                command=_toggle_ctx,
            )
            ctx_btn.pack(side="left", padx=4)
        else:
            self._ctx_status_lbl.configure(
                text="context_menu modülü bulunamadı",
                text_color=("#ef4444", "#f87171"),
            )

        # ── Duplicate finder shortcut ────────────────────────────────────────
        dup_card = ctk.CTkFrame(parent, fg_color=("#fef2f2", "#1a0000"), corner_radius=10,
                                border_width=1, border_color=("#fca5a5", "#7f1d1d"))
        dup_card.grid(row=1, column=0, sticky="ew", padx=8, pady=6)
        dup_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            dup_card,
            text="♻  Yinelenen Dosya Tespiti",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=("#991b1b", "#f87171"), anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 2))

        ctk.CTkLabel(
            dup_card,
            text="MD5 karma ile aynı içerikli dosyaları bulur ve silmenizi sağlar.",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#6b7280", "#94a3b8"), anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 6))

        if self._app:
            ctk.CTkButton(
                dup_card,
                text="Yinelenenleri Tara",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                width=160, height=30,
                fg_color=("#991b1b", "#7f1d1d"),
                hover_color=("#7f1d1d", "#991b1b"),
                command=lambda: (self.withdraw(), self._app._show_duplicates()),
            ).grid(row=2, column=0, sticky="w", padx=14, pady=(0, 10))

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_fixed_drives(self) -> list[str]:
        """Return all fixed (internal/external HDD/SSD) drive roots."""
        from config import DRIVE_FIXED, DRIVE_REMOVABLE
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                dt = ctypes.windll.kernel32.GetDriveTypeW(root)
                if dt == DRIVE_FIXED:
                    try:
                        import os as _os
                        _os.listdir(root)
                        drives.append(root)
                    except OSError:
                        pass
        return drives

    def _drive_label(self, root: str) -> str:
        """Return volume label for a drive root (e.g. 'Yerel Disk')."""
        buf = ctypes.create_unicode_buffer(261)
        try:
            ctypes.windll.kernel32.GetVolumeInformationW(
                root, buf, 261, None, None, None, None, 0
            )
        except Exception:
            pass
        return buf.value or root.rstrip("\\")

    def _get_root_folders(self) -> list[str]:
        names: set[str] = set()
        for drive in get_indexable_drives():
            try:
                for entry in Path(drive).iterdir():
                    if entry.is_dir() and not entry.is_symlink():
                        names.add(entry.name)
            except (PermissionError, OSError):
                pass
        return sorted(names, key=str.lower)

    def _save(self) -> None:
        cfg = load_config()

        # Folders
        _builtin_lower = {s.lower() for s in SKIP_DIRS}
        selected_folders = [
            name for name, var in self._folder_vars.items()
            if var.get() and name.lower() not in _builtin_lower
        ]
        cfg["extra_exclusions"] = selected_folders
        refresh_exclusions()

        # Extensions
        disabled_exts = [
            ext for ext, var in self._ext_vars.items()
            if not var.get()
        ]
        cfg["disabled_extensions"] = disabled_exts
        refresh_active_extensions()

        # Drive-level exclusions (unchecked drives)
        excluded = [
            root for root, var in self._drive_vars.items()
            if not var.get()
        ]
        cfg["excluded_drives"] = excluded

        # USB / removable drives and network shares
        cfg["scan_removable_drives"] = self._scan_usb_var.get()
        cfg["scan_network_drives"]   = self._scan_net_var.get()

        # Hotkey
        modifiers = []
        vk_codes  = []
        if self._hk_alt_var.get():   modifiers.append("alt");   vk_codes.append(0x12)
        if self._hk_ctrl_var.get():  modifiers.append("ctrl");  vk_codes.append(0x11)
        if self._hk_shift_var.get(): modifiers.append("shift"); vk_codes.append(0x10)
        idx = self._key_labels.index(self._key_combo.get())
        key = self._key_values[idx]
        if key and key in self._key_vk_map:
            vk_codes.append(self._key_vk_map[key])
        cfg["hotkey"] = {"modifiers": modifiers, "key": key, "vk_codes": vk_codes}

        save_config(cfg)
        if self._app:
            self._app.reload_hotkey()
        self.destroy()


# ---------------------------------------------------------------------------
# File preview window  (Option B — separate floating window)
# ---------------------------------------------------------------------------

class FilePreviewWindow(ctk.CTkToplevel):
    """Floating window that previews the selected search result."""

    W = 440
    H = 560

    def __init__(self, master) -> None:
        super().__init__(master)
        self.title("Önizleme")
        self.geometry(f"{self.W}x{self.H}")
        self.resizable(True, True)
        self.attributes("-topmost", True)
        self.attributes("-toolwindow", True)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self._result: Optional[dict] = None
        self._build_ui()
        self.withdraw()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Filename label (row 0)
        self._title_lbl = ctk.CTkLabel(
            self, text="",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=("#0f172a", "#f1f5f9"),
            anchor="w",
        )
        self._title_lbl.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))

        # Scrollable content (row 1)
        self._content = ctk.CTkScrollableFrame(
            self, corner_radius=0,
            fg_color=("#f8faff", "#0f0f1a"),
        )
        self._content.grid(row=1, column=0, sticky="nsew")
        self._content.grid_columnconfigure(0, weight=1)

        # Footer with Open button (row 2)
        ftr = ctk.CTkFrame(self, height=40, corner_radius=0,
                           fg_color=("#eef2ff", "#13132a"))
        ftr.grid(row=2, column=0, sticky="ew")
        ftr.grid_propagate(False)

        ctk.CTkButton(
            ftr, text="Dosyayı Aç",
            width=120, height=28,
            fg_color="#2563eb", hover_color="#1d4ed8",
            text_color="white", corner_radius=7,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._open_file,
        ).pack(side="right", padx=10, pady=6)

    def show_file(self, result: dict, query: str,
                  popup_x: int, popup_y: int, popup_w: int) -> None:
        self._result = result

        fname = Path(result["path"]).name
        display = fname if len(fname) <= 48 else fname[:45] + "…"
        self._title_lbl.configure(text=display)
        self.title(display)

        for w in self._content.winfo_children():
            w.destroy()

        suffix = Path(result["path"]).suffix.lower()
        _IMG = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
        if suffix in _IMG:
            self._build_image(result["path"])
        else:
            self._build_text(result.get("content", "") or "", query)

        # Popup ile aynı alt kenara hizala, tam sağına yerleştir
        wl, wt, wr, wb = _work_area()
        h = min(self.H, wb - wt)
        y = wb - h
        x = popup_x + popup_w + 4          # popup'ın hemen sağı
        if x + self.W > wr:                # sağa sığmazsa sola
            x = max(wl, popup_x - self.W - 4)
        # deiconify önce: CTkToplevel eski konumu geri yükler.
        # geometry sonra: o konumun üzerine yazar.
        self.deiconify()
        self.geometry(f"{self.W}x{h}+{x}+{y}")
        self.lift()
        # Win32 snap: title-bar yüksekliği veya DPI ölçek uyuşmazlığından
        # kaynaklanan taskbar taşmasını fiziksel koordinatlarda düzelt.
        # after(0) → event loop bir tur döndükten sonra çalışır,
        # bu noktada Windows WM_WINDOWPOSCHANGED'ı işlemiş olur.
        self.after(0, lambda: _snap_win32(self.winfo_id(), wb))

    def _build_image(self, path: str) -> None:
        if not Path(path).exists():
            ctk.CTkLabel(
                self._content,
                text="Dosya artık mevcut değil",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=("#ef4444", "#f87171"),
            ).pack(padx=12, pady=30)
            return
        try:
            img = Image.open(path)
            img.load()
            orig_w, orig_h = img.size
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.thumbnail((self.W - 32, 340), Image.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            ctk.CTkLabel(self._content, image=ctk_img, text="").pack(padx=8, pady=12)
            ctk.CTkLabel(
                self._content,
                text=f"{orig_w} × {orig_h} px",
                font=ctk.CTkFont(family="Segoe UI", size=10),
                text_color=("#6b7280", "#9ca3af"),
            ).pack()
        except Exception:
            ctk.CTkLabel(
                self._content,
                text="Görsel yüklenemedi",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=("#9ca3af", "#6b7280"),
            ).pack(padx=12, pady=20)

    def _build_text(self, content: str, query: str) -> None:
        if not content.strip():
            ctk.CTkLabel(
                self._content,
                text="İçerik önizlemesi mevcut değil",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=("#9ca3af", "#6b7280"),
            ).pack(padx=12, pady=30)
            return

        dark = ctk.get_appearance_mode() == "Dark"
        bg   = "#111827" if dark else "#f8faff"
        fg   = "#d1d5db" if dark else "#374151"
        hl   = "#818cf8" if dark else "#6366f1"

        txt = tkinter.Text(
            self._content,
            wrap="word",
            font=("Segoe UI", 11),
            bg=bg, fg=fg,
            relief="flat", bd=0,
            highlightthickness=0,
            selectbackground=bg,
            cursor="arrow",
            padx=10, pady=8,
        )
        txt.tag_configure("hl", foreground=hl, font=("Segoe UI", 11, "bold"))
        txt.config(state="normal")
        preview = content[:4000] + ("\n\n[…]" if len(content) > 4000 else "")
        _insert_highlighted(txt, preview, query)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)

    def _open_file(self) -> None:
        if self._result:
            try:
                os.startfile(self._result["path"])
            except Exception as exc:
                log_error(f"Preview open: {exc}")


# ---------------------------------------------------------------------------
# Duplicates Window
# ---------------------------------------------------------------------------

class DuplicatesWindow(ctk.CTkToplevel):
    """Shows groups of duplicate files (same MD5 hash) with delete helpers."""

    def __init__(self, master, indexer) -> None:
        super().__init__(master)
        self._indexer = indexer
        self.title(f"{APP_DISPLAY_NAME} — Yinelenen Dosyalar")
        self.geometry("680x580")
        self.resizable(True, True)
        self.minsize(500, 400)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(self, height=46, corner_radius=0,
                           fg_color=("#991b1b", "#3b0a0a"))
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        ctk.CTkLabel(
            hdr, text="♻  Yinelenen Dosyalar",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color="white", anchor="w",
        ).place(relx=0, rely=0.5, anchor="w", x=16)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color=("#f8fafc", "#181828"))
        self._scroll.grid(row=1, column=0, sticky="nsew")
        self._scroll.grid_columnconfigure(0, weight=1)

        footer = ctk.CTkFrame(self, height=34, corner_radius=0,
                              fg_color=("#fee2e2", "#1a0000"))
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_propagate(False)
        self._status = ctk.CTkLabel(
            footer, text="Karma hesaplanıyor…",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#7f1d1d", "#f87171"), anchor="w",
        )
        self._status.pack(side="left", padx=14)

        ctk.CTkButton(
            footer, text="Tümünü Tara",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            width=100, height=24,
            fg_color=("#991b1b", "#7f1d1d"),
            hover_color=("#7f1d1d", "#991b1b"),
            command=self._start_scan,
        ).pack(side="right", padx=10, pady=5)

        self.lift()
        self.focus_force()
        self._start_scan()

    def _start_scan(self) -> None:
        self._status.configure(text="Karma hesaplanıyor…")
        for w in self._scroll.winfo_children():
            w.destroy()

        def worker():
            try:
                def cb(done: int, total: int) -> None:
                    self.after(0, lambda: self._status.configure(
                        text=f"Hesaplanıyor… {done}/{total}"
                    ))
                self._indexer.compute_hashes(progress_cb=cb)
                groups = self._indexer.find_duplicates()
                self.after(0, lambda: self._show_groups(groups))
            except Exception as exc:
                log_error(f"Duplicate scan: {exc}")
                self.after(0, lambda: self._status.configure(text="Hata oluştu."))

        threading.Thread(target=worker, daemon=True, name="DSL-dupe").start()

    def _show_groups(self, groups: list[list[dict]]) -> None:
        for w in self._scroll.winfo_children():
            w.destroy()

        if not groups:
            ctk.CTkLabel(
                self._scroll, text="✓  Yinelenen dosya bulunamadı",
                font=ctk.CTkFont(family="Segoe UI", size=14),
                text_color=("#16a34a", "#4ade80"),
            ).pack(pady=40)
            self._status.configure(text="Yinelenen dosya yok.")
            return

        total_wasted = sum(
            g[0].get("file_size", 0) * (len(g) - 1) for g in groups
        )
        if total_wasted >= 1024 ** 3:
            waste_str = f"{total_wasted / 1024**3:.1f} GB"
        elif total_wasted >= 1024 ** 2:
            waste_str = f"{total_wasted / 1024**2:.1f} MB"
        else:
            waste_str = f"{total_wasted / 1024:.0f} KB"

        self._status.configure(
            text=f"{len(groups)} grup · ~{waste_str} boşa harcanan alan"
        )

        for group in groups:
            sz = group[0].get("file_size", 0)
            sz_str = (
                f"{sz / 1024**2:.1f} MB" if sz >= 1024**2
                else f"{sz / 1024:.0f} KB" if sz >= 1024
                else f"{sz} B"
            )

            grp_frame = ctk.CTkFrame(
                self._scroll, corner_radius=10,
                border_width=1, border_color=("#fca5a5", "#7f1d1d"),
                fg_color=("#fff7f7", "#1e0a0a"),
            )
            grp_frame.pack(fill="x", padx=8, pady=4)
            grp_frame.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                grp_frame,
                text=f"  {len(group)} kopya · {sz_str} · {group[0].get('file_hash','')[:12]}…",
                font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                text_color=("#991b1b", "#f87171"), anchor="w",
            ).pack(fill="x", padx=10, pady=(6, 2))

            for r in group:
                path_str = r.get("path", "")
                row_f = ctk.CTkFrame(grp_frame, fg_color="transparent")
                row_f.pack(fill="x", padx=10, pady=2)
                row_f.grid_columnconfigure(0, weight=1)

                ctk.CTkLabel(
                    row_f,
                    text=path_str[-72:] if len(path_str) > 72 else path_str,
                    font=ctk.CTkFont(family="Segoe UI", size=10),
                    text_color=("#374151", "#cbd5e1"), anchor="w",
                ).grid(row=0, column=0, sticky="ew")

                ctk.CTkButton(
                    row_f, text="Aç",
                    width=40, height=22,
                    font=ctk.CTkFont(family="Segoe UI", size=9),
                    fg_color=("#3b82f6", "#1e40af"),
                    hover_color=("#2563eb", "#1d4ed8"),
                    command=lambda p=path_str: self._open_path(p),
                ).grid(row=0, column=1, padx=(4, 0))

                ctk.CTkButton(
                    row_f, text="Sil",
                    width=40, height=22,
                    font=ctk.CTkFont(family="Segoe UI", size=9),
                    fg_color=("#ef4444", "#991b1b"),
                    hover_color=("#dc2626", "#7f1d1d"),
                    command=lambda p=path_str, rf=row_f: self._delete_file(p, rf),
                ).grid(row=0, column=2, padx=(2, 0))

            ctk.CTkFrame(grp_frame, height=4, fg_color="transparent").pack()

    def _open_path(self, path: str) -> None:
        try:
            os.startfile(path)
        except Exception as exc:
            log_error(f"Open duplicate: {exc}")

    def _delete_file(self, path: str, row_frame) -> None:
        try:
            Path(path).unlink()
            self._indexer.remove_paths([path])
            row_frame.destroy()
        except Exception as exc:
            log_error(f"Delete duplicate {path}: {exc}")
            tkinter.messagebox.showerror("Hata", f"Dosya silinemedi:\n{exc}", parent=self)


# ---------------------------------------------------------------------------
# Full Results Window  (shows all results beyond the popup's visible cap)
# ---------------------------------------------------------------------------

class FullResultsWindow(ctk.CTkToplevel):
    """Standalone window listing all ranked search results with scroll."""

    W = 600
    H = 700

    def __init__(self, master) -> None:
        super().__init__(master)
        self.title(f"{APP_DISPLAY_NAME} — Tüm Sonuçlar")
        self.geometry(f"{self.W}x{self.H}")
        self.resizable(True, True)
        self.minsize(480, 400)

        self._result_cards: list = []
        self._selected_idx = -1

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        hdr = ctk.CTkFrame(self, height=46, corner_radius=0,
                           fg_color=("#1e40af", "#0d2137"))
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        self._hdr_label = ctk.CTkLabel(
            hdr, text="🔍  Tüm Sonuçlar",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color="white", anchor="w",
        )
        self._hdr_label.place(relx=0, rely=0.5, anchor="w", x=16)

        # Scrollable results
        self._results_frame = ctk.CTkScrollableFrame(
            self, fg_color=("#f8fafc", "#181828"),
        )
        self._results_frame.grid(row=1, column=0, sticky="nsew")
        self._results_frame.grid_columnconfigure(0, weight=1)

        # Footer
        footer = ctk.CTkFrame(self, height=28, corner_radius=0,
                              fg_color=("#eef2ff", "#0c0c1a"))
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_propagate(False)
        self._status = ctk.CTkLabel(
            footer, text="", height=28,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#6b7280", "#6366f1"), anchor="w",
        )
        self._status.pack(side="left", padx=14)

        self.lift()
        self.focus_force()

    def show_results(self, results: list[dict], query: str) -> None:
        for w in self._results_frame.winfo_children():
            w.destroy()
        self._result_cards = []
        self._selected_idx = -1

        if not results:
            ctk.CTkLabel(
                self._results_frame, text="Sonuç bulunamadı",
                font=ctk.CTkFont(family="Segoe UI", size=14),
                text_color=("#6b7280", "#94a3b8"),
            ).pack(pady=40)
            self._hdr_label.configure(text="🔍  Tüm Sonuçlar — 0 sonuç")
            return

        self._hdr_label.configure(text=f"🔍  Tüm Sonuçlar — {len(results)} sonuç")
        self._status.configure(text=f"{len(results)} sonuç  ·  ↑↓ gezin  ·  Enter aç")

        today     = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        _GROUP_LABELS = ["Bugün", "Dün", "Daha Eski"]
        groups: dict[str, list[dict]] = {k: [] for k in _GROUP_LABELS}
        for res in results:
            try:
                d = datetime.datetime.fromtimestamp(res.get("modified_time", 0)).date()
            except Exception:
                d = today
            if d == today:
                groups["Bugün"].append(res)
            elif d == yesterday:
                groups["Dün"].append(res)
            else:
                groups["Daha Eski"].append(res)

        show_headers = sum(1 for g in _GROUP_LABELS if groups[g]) > 1
        for group_name in _GROUP_LABELS:
            if not groups[group_name]:
                continue
            if show_headers:
                ctk.CTkLabel(
                    self._results_frame, text=group_name,
                    font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                    text_color=("#6366f1", "#818cf8"), anchor="w",
                ).pack(fill="x", padx=14, pady=(8, 2))
            for res in groups[group_name]:
                card = ResultCard(
                    self._results_frame, result=res, query=query,
                    on_open=lambda p: os.startfile(p),
                )
                card.pack(fill="x", padx=6, pady=2)
                self._result_cards.append(card)

        self.bind("<Up>",   lambda _e: self._nav(-1))
        self.bind("<Down>", lambda _e: self._nav(1))
        self.bind("<Return>", lambda _e: self._open_selected())

    def _nav(self, direction: int) -> None:
        if not self._result_cards:
            return
        n = len(self._result_cards)
        _SEL = ("#dbeafe", "#1e3a5f")
        _BASE = ("#ffffff", "#1e1e2e")
        if 0 <= self._selected_idx < n:
            self._result_cards[self._selected_idx].configure(fg_color=_BASE)
        self._selected_idx = max(0, min(n - 1, self._selected_idx + direction))
        self._result_cards[self._selected_idx].configure(fg_color=_SEL)

    def _open_selected(self) -> None:
        idx = self._selected_idx if self._selected_idx >= 0 else 0
        if idx < len(self._result_cards):
            self._result_cards[idx]._click()


# ---------------------------------------------------------------------------
# Search popup window  (results only — no search entry)
# ---------------------------------------------------------------------------

class SearchPopup(ctk.CTkToplevel):
    """
    Borderless popup that appears above the taskbar.
    Layout: header | scrollable results | search entry (bottom) | branding
    Search input is owned here; results are pushed by DeepScanApp.
    """

    W = 518
    H = 476

    _HISTORY_PATH = Path(os.environ.get("LOCALAPPDATA", "C:/Temp")) / "DeepScanLocal" / "search_history.json"
    _MAX_HISTORY  = 10

    def __init__(self, master: ctk.CTk, on_query_change, app=None) -> None:
        super().__init__(master)
        self._is_shown        = False
        self._on_query_change = on_query_change
        self._app             = app
        self._history: list[str] = self._load_history()
        self._history_frame: Optional[ctk.CTkFrame] = None
        self._preview_win: Optional[FilePreviewWindow] = None
        self._full_win: Optional[FullResultsWindow] = None
        self._all_results: list[dict] = []
        self._current_query: str = ""

        self._setup_window()
        self._build_ui()
        # Start hidden: park far off-screen + fully transparent.
        self.geometry(f"{self.W}x{self.H}+-32000+-32000")
        self.attributes("-alpha", 0.0)

    # ── Window setup ────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        self.title(APP_DISPLAY_NAME)
        self.resizable(False, False)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-toolwindow", True)
        # Background intentionally plain — acrylic is applied in _show()
        self.configure(fg_color=("#f2f4f8", "#18181e"))
        # X button / WM_DELETE_WINDOW → hide popup instead of minimizing/destroying
        self.protocol("WM_DELETE_WINDOW", self.hide)
        # CTkToplevel sometimes resets overrideredirect during init; re-apply after it settles
        self.after(150, lambda: self.overrideredirect(True))

    # ── UI build ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)   # row 2 = results scrollable

        # ── Accent line (3 px at very top) ──────────────────────────────────
        ctk.CTkFrame(
            self, height=3, corner_radius=0,
            fg_color=("#3b82f6", "#6d28d9"),
        ).grid(row=0, column=0, sticky="ew")

        # ── Top bar: logo + app name + ⚙ + ESC hint ────────────────────────
        top = ctk.CTkFrame(
            self, height=36, corner_radius=0,
            fg_color=("#eef2ff", "#13132a"),
        )
        top.grid(row=1, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        top.grid_propagate(False)

        # Logo icon (28×28) next to app name
        try:
            _logo_pil = Image.open(str(ICON_PATH)).resize((22, 22), Image.LANCZOS)
            _logo_ctk = ctk.CTkImage(light_image=_logo_pil, dark_image=_logo_pil, size=(22, 22))
            ctk.CTkLabel(top, image=_logo_ctk, text="").grid(
                row=0, column=0, padx=(10, 4), pady=0,
            )
        except Exception:
            pass

        ctk.CTkLabel(
            top,
            text=APP_DISPLAY_NAME,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=("#1d4ed8", "#818cf8"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(0, 4))

        ctk.CTkButton(
            top,
            text="⚙",
            width=28, height=24,
            corner_radius=8,
            fg_color="transparent",
            hover_color=("#dbeafe", "#1e1b4b"),
            text_color=("#6b7280", "#818cf8"),
            font=ctk.CTkFont(family="Segoe UI", size=14),
            command=self._open_settings,
        ).grid(row=0, column=2, padx=(0, 4))

        ctk.CTkButton(
            top,
            text="✕",
            width=26, height=26,
            corner_radius=6,
            fg_color="transparent",
            hover_color=("#fee2e2", "#3b0f0f"),
            text_color=("#9ca3af", "#6b7280"),
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            border_width=1,
            border_color=("#e5e7eb", "#374151"),
            command=self.hide,
        ).grid(row=0, column=3, sticky="e", padx=(0, 10), pady=5)

        # ── Results scroll area ──────────────────────────────────────────────
        self._results_frame = ctk.CTkScrollableFrame(
            self,
            corner_radius=0,
            fg_color=("#f5f7ff", "#0f0f1a"),
        )
        self._results_frame.grid(row=2, column=0, sticky="nsew")
        self._results_frame.grid_columnconfigure(0, weight=1)
        # Smooth scrolling + force inner frame to always fill the full canvas width.
        # Without this, pack(fill="x") on cards only fills the inner-frame width
        # which CTkScrollableFrame never auto-stretches to the canvas width.
        try:
            c = self._results_frame._parent_canvas
            c.configure(yscrollincrement=1)
            def _fit_canvas_width(e, _c=c):
                items = _c.find_all()
                if items:
                    _c.itemconfigure(items[0], width=e.width)
            c.bind("<Configure>", _fit_canvas_width)
        except Exception:
            pass

        # ── Row 3: Search entry (bottom) ─────────────────────────────────────
        search_row = ctk.CTkFrame(
            self, height=56, corner_radius=0,
            fg_color=("#eef2ff", "#13132a"),
        )
        search_row.grid(row=3, column=0, sticky="ew")
        search_row.grid_columnconfigure(1, weight=1)
        search_row.grid_propagate(False)

        ctk.CTkLabel(
            search_row, text="⌕", width=32,
            font=ctk.CTkFont(family="Segoe UI", size=20),
            text_color=("#6366f1", "#818cf8"),
        ).grid(row=0, column=0, padx=(14, 2), pady=10)

        self._var = ctk.StringVar()
        self._entry = ctk.CTkEntry(
            search_row,
            textvariable=self._var,
            placeholder_text="Uygulama, dosya veya klasör ara...",
            height=36,
            border_width=1,
            border_color=("#c7d2fe", "#4338ca"),
            fg_color=("#ffffff", "#1e1b4b"),
            text_color=("#0f172a", "#f1f5f9"),
            placeholder_text_color=("#9ca3af", "#6b7280"),
            font=ctk.CTkFont(family="Segoe UI", size=13),
            corner_radius=8,
        )
        self._entry.grid(row=0, column=1, sticky="ew", padx=(4, 14), pady=10)
        self._var.trace_add("write", self._on_type)
        self._entry.bind("<Escape>",  lambda _: self.hide())
        self._entry.bind("<Down>",    lambda _: self._nav(+1))
        self._entry.bind("<Up>",      lambda _: self._nav(-1))
        self._entry.bind("<Return>",  lambda _: self._on_return())
        self._entry.bind("<FocusIn>", lambda _: self._on_entry_focus())

        self._selected_idx: int = -1
        self._result_cards: list = []

        # ── Row 4: Footer: status left, branding right ───────────────────────
        footer = ctk.CTkFrame(
            self, height=28, corner_radius=0,
            fg_color=("#eef2ff", "#0c0c1a"),
        )
        footer.grid(row=4, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        footer.grid_propagate(False)

        self._status = ctk.CTkLabel(
            footer, text="", height=28,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=("#6b7280", "#6366f1"),
            anchor="w",
        )
        self._status.grid(row=0, column=0, sticky="ew", padx=14)

        self._show_all_btn = ctk.CTkButton(
            footer,
            text="Tümünü Göster →",
            font=ctk.CTkFont(family="Segoe UI", size=9),
            text_color=("#1e40af", "#818cf8"),
            fg_color="transparent",
            hover_color=("#dbeafe", "#1e1b4b"),
            width=1, height=18,
            corner_radius=4,
            command=self._open_full_results,
            state="disabled",
        )
        self._show_all_btn.grid(row=0, column=1, sticky="e", padx=(4, 6))

        brand_row = ctk.CTkFrame(footer, fg_color="transparent")
        brand_row.grid(row=0, column=2, sticky="e", padx=(4, 10))

        ctk.CTkLabel(
            brand_row,
            text=f"{DESIGNER_CREDIT}  ·  ",
            font=ctk.CTkFont(family="Segoe UI", size=9),
            text_color=("#3b82f6", "#818cf8"),
        ).pack(side="left")

        ctk.CTkButton(
            brand_row,
            text=DESIGNER_URL.replace("https://", ""),
            font=ctk.CTkFont(family="Segoe UI", size=9),
            text_color=("#3b82f6", "#818cf8"),
            fg_color="transparent",
            hover_color=("#dbeafe", "#1e1b4b"),
            width=1, height=18,
            corner_radius=4,
            command=lambda: webbrowser.open(DESIGNER_URL),
        ).pack(side="left")

    # ── Preview window (Option B: separate floating window) ─────────────────

    def show_preview(self, result: dict, query: str = "") -> None:
        if self._preview_win is None or not self._preview_win.winfo_exists():
            self._preview_win = FilePreviewWindow(self)
        self._preview_win.show_file(
            result, query,
            self.winfo_x(), self.winfo_y(), self.W,
        )

    def _close_preview(self) -> None:
        if self._preview_win and self._preview_win.winfo_exists():
            self._preview_win.withdraw()

    # ── Public API ───────────────────────────────────────────────────────────

    def push_results(self, results: list[dict], query: str, all_results: list[dict] | None = None) -> None:
        """Called from DeepScanApp with ranked results to display."""
        if not self._is_shown:
            return  # User dismissed popup while search was running — don't reopen
        self._all_results = all_results if all_results is not None else results
        self._current_query = query
        self._show_results(results, query)
        # Enable "show all" button only when there are more results than shown
        has_more = len(self._all_results) > len(results)
        self._show_all_btn.configure(
            state="normal" if (self._all_results and has_more) else "disabled",
            text=f"Tümünü Göster ({len(self._all_results)}) →" if has_more else "Tümünü Göster →",
        )

    def _open_full_results(self) -> None:
        if not self._all_results:
            return
        if self._full_win is None or not self._full_win.winfo_exists():
            self._full_win = FullResultsWindow(self)
        self._full_win.show_results(self._all_results, self._current_query)
        self._full_win.deiconify()
        self._full_win.lift()
        self._full_win.focus_force()

    def is_shown(self) -> bool:
        return self._is_shown

    def show_and_focus(self) -> None:
        """Show popup and place cursor in the search entry."""
        self._show()
        def _focus():
            try:
                u32 = ctypes.windll.user32
                k32 = ctypes.windll.kernel32
                hwnd = u32.GetAncestor(self.winfo_id(), 2)
                if hwnd:
                    # AttachThreadInput bypasses Windows foreground-lock so
                    # SetForegroundWindow works even when called from a hotkey
                    # while another app (e.g. desktop/Explorer) has focus.
                    fg_hwnd = u32.GetForegroundWindow()
                    fg_tid  = u32.GetWindowThreadProcessId(fg_hwnd, None)
                    our_tid = k32.GetCurrentThreadId()
                    if fg_tid and fg_tid != our_tid:
                        u32.AttachThreadInput(fg_tid, our_tid, True)
                        u32.SetForegroundWindow(hwnd)
                        u32.AttachThreadInput(fg_tid, our_tid, False)
                    else:
                        u32.SetForegroundWindow(hwnd)
                self.lift()
                self.focus_force()
                self._entry.focus_force()
            except Exception:
                pass
        self.after(80, _focus)

    def hide(self) -> None:
        if not self._is_shown:
            return
        self._is_shown = False
        self._close_preview()
        try:
            self.attributes("-alpha", 0.0)
            self.geometry(f"{self.W}x{self.H}+-32000+-32000")
        except Exception as exc:
            log_error(f"Popup hide: {exc}")
        self._hide_history_dropdown()
        self._var.set("")
        self._clear_results()
        self._status.configure(text="")
        self._show_all_btn.configure(state="disabled", text="Tümünü Göster →")
        self._all_results = []

    def _on_type(self, *_) -> None:
        q = self._var.get()
        if q:
            self._hide_history_dropdown()
        else:
            self._show_history_dropdown()
        self._on_query_change(q)

    def _on_entry_focus(self) -> None:
        if not self._var.get():
            self._show_history_dropdown()

    def _on_return(self) -> None:
        q = self._var.get().strip()
        if q:
            self._add_to_history(q)
        self._hide_history_dropdown()
        self._open_selected()

    # ── Internal show ────────────────────────────────────────────────────────

    def _show(self) -> None:
        if self._is_shown:
            return
        self._is_shown = True
        try:
            self._position()
            self.update_idletasks()
            # Win32 correction: ensures outer bottom is exactly at work-area
            # boundary regardless of DPI mode or Tk coord-space differences.
            _snap_win32(self.winfo_id(), _work_area()[3])

            dark = ctk.get_appearance_mode() == "Dark"
            # Try Windows acrylic glass effect; always set alpha for fallback
            _apply_acrylic(self.winfo_id(), dark=dark)
            self.attributes("-alpha", 0.91 if dark else 0.94)

            self.lift()
        except Exception as exc:
            log_error(f"Popup show: {exc}")
            self._is_shown = False

    def _position(self) -> None:
        wl, wt, wr, wb = _work_area()
        w = min(self.W, wr - wl)
        h = min(self.H, wb - wt)
        x = wl + max(0, (wr - wl - w) // 2)
        y = wb - h          # tam work area altına yapıştır — taskbar üstü
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ── Results rendering ─────────────────────────────────────────────────────

    def _show_results(self, results: list[dict], query: str) -> None:
        self._clear_results()
        self._selected_idx = -1

        if not results:
            empty = ctk.CTkFrame(self._results_frame, fg_color="transparent")
            empty.pack(expand=True, pady=60, anchor="center")
            ctk.CTkLabel(
                empty, text="🔍",
                font=ctk.CTkFont(size=40),
            ).pack()
            ctk.CTkLabel(
                empty, text="Sonuç bulunamadı",
                font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                text_color=("#374151", "#94a3b8"),
            ).pack(pady=(6, 2))
            ctk.CTkLabel(
                empty,
                text="Farklı bir kelime deneyin veya ext:pdf gibi filtreler kullanın",
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=("#9ca3af", "#6b7280"),
            ).pack()
            self._status.configure(text=f"0 {get_text('results')}")
            return

        # ── Group results by modification date ──────────────────────────────
        today     = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        week_ago  = today - datetime.timedelta(days=7)

        _GROUP_LABELS = ["Bugün", "Dün", "Daha Eski"]
        groups: dict[str, list[dict]] = {k: [] for k in _GROUP_LABELS}

        for res in results:
            try:
                d = datetime.datetime.fromtimestamp(res.get("modified_time", 0)).date()
            except Exception:
                d = today
            if d == today:
                groups["Bugün"].append(res)
            elif d == yesterday:
                groups["Dün"].append(res)
            else:
                groups["Daha Eski"].append(res)

        filled_groups = [g for g in _GROUP_LABELS if groups[g]]
        show_headers  = len(filled_groups) > 1

        self._result_cards = []
        for group_name in _GROUP_LABELS:
            group_items = groups[group_name]
            if not group_items:
                continue

            if show_headers:
                ctk.CTkLabel(
                    self._results_frame,
                    text=group_name,
                    font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                    text_color=("#6366f1", "#818cf8"),
                    anchor="w",
                ).pack(fill="x", padx=14, pady=(8, 2))

            for res in group_items:
                card = ResultCard(
                    self._results_frame,
                    result=res,
                    query=query,
                    on_open=self._open_file,
                    on_preview=lambda r=res, q=query: self.show_preview(r, q),
                )
                card.pack(fill="x", padx=6, pady=2)
                self._result_cards.append(card)

        self._status.configure(text=f"{len(results)} {get_text('results')}  ·  ↑↓ gezin  ·  Enter aç")
        try:
            self._results_frame._parent_canvas.update_idletasks()
        except Exception:
            pass

    def _clear_results(self) -> None:
        self._close_preview()
        for w in self._results_frame.winfo_children():
            w.destroy()
        self._result_cards = []
        self._selected_idx = -1

    # ── Keyboard navigation ──────────────────────────────────────────────────

    _SEL_COLOR  = ("#dbeafe", "#1e3a5f")
    _BASE_COLOR = ("#ffffff", "#1e1e2e")

    def _nav(self, direction: int) -> None:
        """Move selection up (-1) or down (+1) through result cards."""
        if not self._result_cards:
            return
        n = len(self._result_cards)
        # Deselect previous
        if 0 <= self._selected_idx < n:
            self._result_cards[self._selected_idx].configure(fg_color=self._BASE_COLOR)
        # New index (clamp)
        self._selected_idx = max(0, min(n - 1, self._selected_idx + direction))
        card = self._result_cards[self._selected_idx]
        card.configure(fg_color=self._SEL_COLOR)
        # Scroll into view
        card.update_idletasks()
        try:
            self._results_frame._parent_canvas.yview_moveto(
                card.winfo_y() / max(1, self._results_frame.winfo_reqheight())
            )
        except Exception:
            pass

    def _open_selected(self) -> None:
        """Open the currently selected card (Enter key)."""
        if not self._result_cards:
            return
        idx = self._selected_idx if self._selected_idx >= 0 else 0
        if idx < len(self._result_cards):
            self._result_cards[idx]._click()

    # ── Settings ─────────────────────────────────────────────────────────────

    # ── Search history ───────────────────────────────────────────────────────

    def _load_history(self) -> list[str]:
        try:
            if self._HISTORY_PATH.exists():
                return json.loads(self._HISTORY_PATH.read_text("utf-8"))
        except Exception:
            pass
        return []

    def _save_history(self) -> None:
        try:
            self._HISTORY_PATH.write_text(
                json.dumps(self._history, ensure_ascii=False), "utf-8"
            )
        except Exception:
            pass

    def _add_to_history(self, query: str) -> None:
        q = query.strip()
        if not q:
            return
        if q in self._history:
            self._history.remove(q)
        self._history.insert(0, q)
        self._history = self._history[:self._MAX_HISTORY]
        self._save_history()

    def _show_history_dropdown(self) -> None:
        if self._history_frame or not self._history:
            return
        self._history_frame = ctk.CTkFrame(
            self, corner_radius=10, border_width=1,
            fg_color=("#ffffff", "#1e1e2e"),
            border_color=("#c7d2fe", "#4338ca"),
        )
        self._history_frame.place(
            x=14, rely=1.0, anchor="sw",
            y=-62,          # just above search row
            width=self.W - 28,
        )
        self._history_frame.lift()

        ctk.CTkLabel(
            self._history_frame,
            text="Son Aramalar",
            font=ctk.CTkFont(family="Segoe UI", size=9, weight="bold"),
            text_color=("#9ca3af", "#6b7280"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(6, 2))

        for item in self._history:
            row = ctk.CTkFrame(self._history_frame, fg_color="transparent", cursor="hand2")
            row.pack(fill="x", padx=6, pady=1)

            def _pick(q=item):
                self._hide_history_dropdown()
                self._var.set(q)
                self._entry.icursor("end")
                self._on_query_change(q)

            lbl = ctk.CTkLabel(
                row, text=f"  🕐  {item}",
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=("#374151", "#cbd5e1"),
                anchor="w", cursor="hand2",
            )
            lbl.pack(fill="x", padx=4, pady=2)
            lbl.bind("<Button-1>", lambda _e, fn=_pick: fn())
            row.bind("<Button-1>", lambda _e, fn=_pick: fn())

    def _hide_history_dropdown(self) -> None:
        if self._history_frame:
            self._history_frame.destroy()
            self._history_frame = None

    def _open_settings(self) -> None:
        SettingsPanel(self.master, app=self._app)

    # ── File open ────────────────────────────────────────────────────────────

    def _open_file(self, path: str) -> None:
        self.hide()
        if Path(path).exists():
            os.startfile(path)
        else:
            log_error(f"File not found at open: {path}")


# ---------------------------------------------------------------------------
# Application orchestrator
# ---------------------------------------------------------------------------

class DeepScanApp:
    """Owns all subsystems and the tkinter main loop."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()

        # customtkinter setup
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Root window — invisible master, hosts the tkinter event loop only.
        # Using overrideredirect+alpha instead of withdraw() so wm_state stays
        # "normal": CTkToplevel auto-hides only when master is "withdrawn".
        self._root = ctk.CTk()
        self._root.title(APP_DISPLAY_NAME)
        self._root.geometry("1x1+-32000+-32000")
        self._root.overrideredirect(True)      # no decorations, no taskbar button
        self._root.attributes("-alpha", 0.0)   # fully invisible
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", lambda: None)

        # Generate & cache icon
        self._icon_img = _make_icon(64)
        _save_ico(self._icon_img)

        # Tell Windows which App User Model ID to use for this process.
        # Required for "Pin to taskbar" and proper taskbar grouping.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"Sedat.{APP_NAME}"
            )
        except Exception:
            pass

        # Subsystems
        self._indexer = Indexer(progress_cb=self._on_index_progress)
        self._watcher = FileWatcher(self._indexer)
        self._popup   = SearchPopup(self._root, self._on_query_change, app=self)

        # Floating pill bar is replaced by a proper taskbar button.
        # _taskbar_bar kept as None; _poll_mouse_outside checks for None.
        self._taskbar_bar = None
        self._startup_done = False
        self._root.after(2000, lambda: setattr(self, "_startup_done", True))

        # Tray reference (set in _run_tray)
        self._tray: Optional[pystray.Icon] = None
        self._watcher_paused = False
        self._hotkey_down    = False
        self._hotkey_vk_codes: list[int] = self._load_hotkey_vk_codes()
        self._after_search:      Optional[str] = None   # content-search debounce handle
        self._after_name_search: Optional[str] = None   # name-search debounce handle
        self._search_version: int = 0                   # prevents stale results overwriting
        self._popup_hwnd: Optional[int] = None
        self._lmb_was_down   = False
        self._last_popup_show: float = 0.0   # time.monotonic() when popup last shown

    # ── Startup ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start all background threads then enter the tkinter event loop."""
        # Register in autostart on first launch only (if not already present)
        import winreg as _wr
        try:
            with _wr.OpenKey(_wr.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, _wr.KEY_READ) as _k:
                try:
                    _wr.QueryValueEx(_k, APP_NAME)
                except FileNotFoundError:
                    set_autostart(True)   # first run — register silently
        except OSError:
            pass

        self._start_daemon(self._run_tray,              name="DSL-tray")
        self._start_daemon(self._win32_taskbar_button,  name="DSL-taskbarbtn")
        self._start_daemon(self._initial_scan,          name="DSL-scan")
        self._start_daemon(self._watcher.start,         name="DSL-watcher")
        self._start_daemon(self._indexer.index_apps,    name="DSL-apps")
        self._start_daemon(self._create_start_shortcut, name="DSL-shortcut")

        # Queue poller — bridges background threads → tkinter thread
        self._root.after(100, self._poll_queue)
        # Hotkey via GetAsyncKeyState polling
        self._root.after(50, self._poll_hotkey)
        # Cache popup's true Win32 root HWND once tkinter is settled
        def _cache_popup_hwnd():
            tk_id = self._popup.winfo_id()
            if tk_id:
                ga = ctypes.windll.user32.GetAncestor(tk_id, 2)  # GA_ROOT=2
                self._popup_hwnd = ga if ga else tk_id
        self._root.after(400, _cache_popup_hwnd)
        # Click-outside detection: WindowFromPoint + GetAncestor (ChatGPT approach)
        self._root.after(20, self._poll_click_outside)
        self._root.mainloop()

    @staticmethod
    def _start_daemon(target, name: str) -> None:
        threading.Thread(target=target, daemon=True, name=name).start()

    # ── Hotkey polling via GetAsyncKeyState (no RegisterHotKey needed) ───────

    @staticmethod
    def _load_hotkey_vk_codes() -> list[int]:
        """Read hotkey vk_codes from config.json; fall back to Alt+Ctrl."""
        try:
            cfg = load_config()
            codes = cfg.get("hotkey", {}).get("vk_codes", [])
            if codes:
                return [int(c) for c in codes]
        except Exception:
            pass
        return [0x12, 0x11]  # Alt + Ctrl

    def reload_hotkey(self) -> None:
        """Called by SettingsPanel after saving new hotkey."""
        self._hotkey_vk_codes = self._load_hotkey_vk_codes()
        self._hotkey_down = False

    def _poll_hotkey(self) -> None:
        user32 = ctypes.windll.user32
        pressed = all(
            bool(user32.GetAsyncKeyState(vk) & 0x8000)
            for vk in self._hotkey_vk_codes
        )
        if pressed and not self._hotkey_down:
            self._hotkey_down = True
            self._toggle_popup()
        elif not pressed:
            self._hotkey_down = False
        self._root.after(50, self._poll_hotkey)

    # ── Queue poller (runs on tkinter thread) ────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg == _MSG_SHOW:
                    self._toggle_popup()
                elif msg == _MSG_REINDEX:
                    self._start_daemon(self._reindex, name="DSL-reindex")
                elif msg == _MSG_QUIT:
                    self._shutdown()
                    return
        except queue.Empty:
            pass
        self._root.after(100, self._poll_queue)

    # ── Search pipeline (owned by DeepScanApp, not the popup) ───────────────

    def _on_query_change(self, query: str) -> None:
        """Called by SearchPopup every time the search text changes."""
        # Cancel any pending searches
        if self._after_search:
            self._root.after_cancel(self._after_search)
            self._after_search = None
        if self._after_name_search:
            self._root.after_cancel(self._after_name_search)
            self._after_name_search = None

        if not query.strip():
            self._popup.hide()
            return

        # Increment version so stale threads don't push outdated results
        self._search_version += 1

        # Phase 1 — instant name search (80 ms debounce, fast LIKE query)
        v = self._search_version
        self._after_name_search = self._root.after(
            80, lambda q=query: self._run_name_search(q, v)
        )
        # Phase 2 — full content search (300 ms debounce, FTS5)
        self._after_search = self._root.after(
            300, lambda q=query: self._run_content_search(q, v)
        )

    def _run_name_search(self, query: str, version: int) -> None:
        """Phase 1: filename-only search — results appear almost instantly."""
        self._after_name_search = None
        clean_query, filters = parse_query(query)
        search_q = clean_query.strip() if clean_query.strip() else ("" if filters else query)

        def worker() -> None:
            try:
                if not search_q and not filters:
                    return
                raw = self._indexer.search_names_only(search_q, limit=15, filters=filters or None)
                if search_q:
                    ranked = sorted(
                        rank_results(search_q, raw),
                        key=lambda r: r.get("modified_time", 0),
                        reverse=True,
                    )[:8]
                else:
                    # Filter-only query: skip relevance ranking, sort by recency
                    ranked = sorted(raw, key=lambda r: r.get("modified_time", 0), reverse=True)[:8]
                if ranked and version == self._search_version:
                    self._root.after(0, lambda: self._popup.push_results(ranked, query))
            except Exception as exc:
                log_error(f"Name search: {exc}")

        threading.Thread(target=worker, daemon=True, name="DSL-name").start()

    def _run_content_search(self, query: str, version: int) -> None:
        """Phase 2: full FTS5 + fuzzy content search."""
        self._after_search = None
        clean_query, filters = parse_query(query)
        search_q = clean_query.strip() if clean_query.strip() else ("" if filters else query)

        def worker() -> None:
            try:
                raw = self._indexer.search(search_q, limit=100, filters=filters or None)
                # Remove files that no longer exist on connected drives; clean DB
                verified = []
                to_delete = []
                for r in raw:
                    p = r.get("path", "")
                    if r.get("entry_type") == "app" or not p:
                        verified.append(r)
                        continue
                    if Path(p).exists():
                        verified.append(r)
                    elif r.get("is_connected", 1):
                        to_delete.append(p)
                if to_delete:
                    threading.Thread(
                        target=self._indexer.remove_paths,
                        args=(to_delete,), daemon=True,
                    ).start()
                raw = verified
                if search_q:
                    all_ranked = sorted(
                        rank_results(search_q, raw),
                        key=lambda r: r.get("modified_time", 0),
                        reverse=True,
                    )
                else:
                    # Filter-only query: skip relevance ranking, sort by recency
                    all_ranked = sorted(raw, key=lambda r: r.get("modified_time", 0), reverse=True)
                ranked = all_ranked[:15]
                if version == self._search_version:
                    self._root.after(
                        0,
                        lambda r=ranked, a=all_ranked, q=query:
                            self._popup.push_results(r, q, all_results=a),
                    )
            except Exception as exc:
                log_error(f"Search worker: {exc}")

        threading.Thread(target=worker, daemon=True, name="DSL-search").start()

    # ── Popup / bar management ────────────────────────────────────────────────

    def _toggle_popup(self) -> None:
        """Hotkey / tray / bar click: toggle the search popup."""
        if self._popup.is_shown():
            self._popup.hide()
        else:
            self._last_popup_show = time.monotonic()
            self._popup.show_and_focus()

    def _poll_click_outside(self) -> None:
        """
        Poll every 20 ms for a left-click outside the popup.

        GetCursorPos and GetWindowRect both use physical screen pixels so
        there is no DPI mismatch.  No hooks, no deadlocks.
        """
        u32 = ctypes.windll.user32

        state   = u32.GetAsyncKeyState(0x01)
        lmb_now = bool(state & 0x8000)

        # Ignore clicks for 350 ms after popup opens — prevents the opening
        # click from immediately closing the popup (race between taskbar click
        # and _poll_click_outside).
        cooldown_active = (time.monotonic() - self._last_popup_show) < 0.35

        if self._popup._is_shown:
            # Hide when Win+D / Show-Desktop makes the desktop the foreground window
            fg = u32.GetForegroundWindow()
            if fg:
                cls_buf = ctypes.create_unicode_buffer(32)
                u32.GetClassNameW(fg, cls_buf, 32)
                if cls_buf.value in ("Progman", "WorkerW"):
                    self._popup.hide()

        if (lmb_now and not self._lmb_was_down) and self._popup._is_shown and not cooldown_active:
            pt = _POINT()
            u32.GetCursorPos(ctypes.byref(pt))

            popup_hwnd = self._popup_hwnd
            if popup_hwnd:
                rect = _RECT()
                if u32.GetWindowRect(popup_hwnd, ctypes.byref(rect)):
                    inside = (rect.left <= pt.x <= rect.right and
                              rect.top  <= pt.y <= rect.bottom)
                    if not inside:
                        self._popup.hide()

        self._lmb_was_down = lmb_now
        self._root.after(20, self._poll_click_outside)

    # ── Taskbar button + Start menu shortcut ─────────────────────────────────

    def _win32_taskbar_button(self) -> None:
        """
        Create a real Win32 window on its own daemon thread.
        WS_OVERLAPPEDWINDOW + WS_EX_APPWINDOW:
          • Shows as a proper taskbar button with app icon
          • "Görev çubuğuna sabitle" available via right-click
          • WM_ACTIVATE fires when user clicks the taskbar button → toggle popup
        Window is placed at -32000,-32000 (permanently off-screen).
        """
        try:
            u32  = ctypes.windll.user32
            k32  = ctypes.windll.kernel32

            # On 64-bit Windows WPARAM/LPARAM/LRESULT are pointer-sized (8 bytes).
            # Using c_ssize_t avoids the "int too long to convert" overflow error.
            WNDPROCTYPE = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                ctypes.wintypes.HWND, ctypes.wintypes.UINT,
                ctypes.c_size_t,    # WPARAM
                ctypes.c_ssize_t,   # LPARAM
            )

            WM_ACTIVATE        = 0x0006
            WM_SYSCOMMAND      = 0x0112
            WM_CLOSE           = 0x0010
            WM_DESTROY         = 0x0002
            WM_WINDOWPOSCHANGING = 0x0046
            WA_ACTIVE          = 1
            WA_CLICKACTIVE     = 2
            SC_MINIMIZE        = 0xF020   # fired when user clicks active taskbar btn
            SWP_NOMOVE         = 0x0002
            SWP_NOSIZE         = 0x0001
            SWP_NOZORDER       = 0x0004
            SWP_NOACTIVATE     = 0x0010

            def _wndproc(hwnd, msg, wp, lp):
                if msg == WM_ACTIVATE and (wp & 0xFFFF) == WA_CLICKACTIVE:
                    # Keep window permanently off-screen
                    u32.SetWindowPos(
                        hwnd, None, -32000, -32000, 300, 60,
                        SWP_NOZORDER | SWP_NOSIZE | SWP_NOACTIVATE,
                    )
                    if self._startup_done:
                        # 350 ms cooldown — _poll_click_outside skips the opening click
                        self._last_popup_show = time.monotonic()
                        self._root.after(60, self._popup.show_and_focus)
                    return 0
                if msg == WM_SYSCOMMAND and (wp & 0xFFF0) == SC_MINIMIZE:
                    # Windows sends SC_MINIMIZE when user clicks the taskbar button
                    # while our window is already the active/foreground window.
                    # Intercept: toggle the popup and block the actual minimize.
                    if self._startup_done:
                        self._last_popup_show = time.monotonic()
                        self._root.after(0, self._toggle_popup)
                    return 0
                if msg == WM_CLOSE:
                    # Right-click → "Pencereyi kapat" on taskbar → quit the app
                    self._queue.put(_MSG_QUIT)
                    return 0
                if msg == WM_DESTROY:
                    u32.PostQuitMessage(0)
                    return 0
                return u32.DefWindowProcW(hwnd, msg, wp, lp)

            _proc_ref = WNDPROCTYPE(_wndproc)

            class _WNDCLASS(ctypes.Structure):
                _fields_ = [
                    ("style",         ctypes.c_uint),
                    ("lpfnWndProc",   WNDPROCTYPE),
                    ("cbClsExtra",    ctypes.c_int),
                    ("cbWndExtra",    ctypes.c_int),
                    ("hInstance",     ctypes.c_void_p),
                    ("hIcon",         ctypes.c_void_p),
                    ("hCursor",       ctypes.c_void_p),
                    ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName",  ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p),
                ]

            hinst = k32.GetModuleHandleW(None)

            # LoadImageW returns HANDLE (pointer-size).  Without setting restype
            # ctypes defaults to c_int (32-bit) which truncates the 64-bit handle
            # on 64-bit Windows → invalid icon → blank taskbar button.
            u32.LoadImageW.restype = ctypes.wintypes.HANDLE

            IMAGE_ICON      = 1
            LR_LOADFROMFILE = 0x00000010
            LR_DEFAULTSIZE  = 0x00000040

            # Load icon BEFORE RegisterClass so wc.hIcon can be set.
            # This ensures the taskbar thumbnail always has the correct icon.
            hicon = None
            if ICON_PATH.exists():
                hicon = u32.LoadImageW(
                    None, str(ICON_PATH), IMAGE_ICON, 0, 0,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE,
                )
            if not hicon:
                hicon = u32.LoadImageW(
                    hinst, None, IMAGE_ICON, 0, 0, LR_DEFAULTSIZE,
                )

            wc = _WNDCLASS()
            wc.lpfnWndProc   = _proc_ref
            wc.hInstance     = hinst
            wc.hIcon         = hicon or 0   # set class icon → taskbar uses this
            wc.lpszClassName = "DSL_TBBtn_v3"
            wc.hbrBackground = ctypes.c_void_p(6)  # COLOR_WINDOW
            u32.RegisterClassW(ctypes.byref(wc))

            # WS_OVERLAPPEDWINDOW = has title bar + sys-menu → Windows shows
            # "Görev çubuğuna sabitle" in right-click context menu.
            # WS_EX_APPWINDOW forces taskbar presence unconditionally.
            WS_OVERLAPPEDWINDOW = 0x00CF0000
            hwnd = u32.CreateWindowExW(
                0x00040000,           # WS_EX_APPWINDOW
                "DSL_TBBtn_v3",
                APP_DISPLAY_NAME,
                WS_OVERLAPPEDWINDOW,
                -32000, -32000, 300, 60,
                None, None, hinst, None,
            )
            if not hwnd:
                log_error("Win32TaskbarButton: CreateWindowExW returned NULL")
                return

            # Must be shown (SW_SHOWNORMAL) for the taskbar button to appear
            u32.ShowWindow(hwnd, 1)
            u32.UpdateWindow(hwnd)

            # Apply icon to the window instance as well (belt-and-suspenders).
            # wc.hIcon already set the class-level icon above.
            WM_SETICON = 0x0080
            if hicon:
                u32.SendMessageW(hwnd, WM_SETICON, 1, hicon)   # ICON_BIG
                u32.SendMessageW(hwnd, WM_SETICON, 0, hicon)   # ICON_SMALL

            # Own message pump on this thread
            wmsg = ctypes.wintypes.MSG()
            while u32.GetMessageW(ctypes.byref(wmsg), None, 0, 0) != 0:
                u32.TranslateMessage(ctypes.byref(wmsg))
                u32.DispatchMessageW(ctypes.byref(wmsg))

        except Exception as exc:
            log_error(f"Win32TaskbarButton: {exc}")

    def _create_start_shortcut(self) -> None:
        """
        Create a shortcut in the Windows Start Menu Programs folder so the
        app appears in Start → All Apps and can be searched / pinned easily.
        Runs once; skips silently if the shortcut already exists.
        """
        try:
            exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
            start_menu = Path(os.environ.get("APPDATA", "")) / \
                         "Microsoft" / "Windows" / "Start Menu" / "Programs"
            lnk = start_menu / f"{APP_DISPLAY_NAME}.lnk"
            if lnk.exists():
                return
            icon_loc = str(ICON_PATH) if ICON_PATH.exists() else exe
            ps = (
                f'$s=(New-Object -COM WScript.Shell).CreateShortcut("{lnk}");'
                f'$s.TargetPath="{exe}";'
                f'$s.Description="{APP_DISPLAY_NAME}";'
                f'$s.IconLocation="{icon_loc}";'
                f'$s.Save()'
            )
            import subprocess as _sp
            _sp.run(
                ["powershell", "-Command", ps],
                capture_output=True, timeout=15,
            )
        except Exception as exc:
            log_error(f"Start menu shortcut: {exc}")

    # ── Tray icon ────────────────────────────────────────────────────────────

    def _run_tray(self) -> None:
        """Build and run the pystray system-tray icon."""

        def open_search(_icon, _item):
            self._queue.put(_MSG_SHOW)

        def reindex(_icon, _item):
            self._queue.put(_MSG_REINDEX)

        def toggle_watcher(_icon, _item):
            if self._watcher_paused:
                self._watcher.resume()
                self._watcher_paused = False
            else:
                self._watcher.pause()
                self._watcher_paused = True

        def find_dupes(_icon, _item):
            self._root.after(0, self._show_duplicates)

        def about(_icon, _item):
            self._root.after(0, self._show_about)

        def exit_app(_icon, _item):
            _icon.stop()
            self._queue.put(_MSG_QUIT)

        menu = Menu(
            Item(get_text("open_search"), open_search, default=True),
            Item(get_text("reindex"),     reindex),
            Item(lambda _: (
                get_text("resume_watcher") if self._watcher_paused
                else get_text("pause_watcher")
            ), toggle_watcher),
            Menu.SEPARATOR,
            Item("Yinelenenleri Bul", find_dupes),
            Menu.SEPARATOR,
            Item(get_text("about"), about),
            Item(get_text("exit"),  exit_app),
        )

        self._tray = pystray.Icon(
            APP_NAME,
            self._icon_img,
            get_text("tray_tooltip"),
            menu,
        )
        self._tray.run()

    # ── About dialog (must run on tkinter thread) ────────────────────────────

    def _show_about(self) -> None:
        tkinter.messagebox.showinfo(
            get_text("about_title"),
            get_text("about_body"),
            parent=self._root,
        )

    def _show_duplicates(self) -> None:
        DuplicatesWindow(self._root, self._indexer)

    # ── Indexer ──────────────────────────────────────────────────────────────

    def _initial_scan(self) -> None:
        try:
            self._indexer.purge_excluded_extensions()
            self._indexer.full_scan()
        except Exception as exc:
            log_error(f"Initial scan: {exc}")
        finally:
            self._set_tray_title(get_text("tray_tooltip"))

    def _reindex(self) -> None:
        try:
            self._indexer.full_scan()
        except Exception as exc:
            log_error(f"Re-index: {exc}")
        finally:
            self._set_tray_title(get_text("tray_tooltip"))

    def _on_index_progress(self, _path: str, count: int) -> None:
        if count % 50 == 0:   # throttle updates to every 50 files
            self._set_tray_title(f"DeepScan — {count} dosya tarandı")

    def _set_tray_title(self, title: str) -> None:
        try:
            if self._tray:
                self._tray.title = title
        except Exception:
            pass

    # ── Taskbar identity ─────────────────────────────────────────────────────

    def _apply_taskbar_style(self) -> None:
        """Force root window into taskbar (WS_EX_APPWINDOW) so it can be pinned."""
        try:
            hwnd            = self._root.winfo_id()
            GWL_EXSTYLE     = -20
            WS_EX_APPWINDOW  = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        try:
            self._watcher.stop()
        except Exception:
            pass
        try:
            self._indexer.stop()
        except Exception:
            pass
        # Clean up WinEvent hook before destroying windows
        try:
            hook = self._taskbar_bar._winevent_hook
            if hook:
                ctypes.windll.user32.UnhookWinEvent(hook)
        except Exception:
            pass
        self._root.quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Hide the console window — works when launched via python.exe from a shell.
    # No-op when already running under pythonw.exe (no console exists).
    try:
        _hwnd_con = ctypes.windll.kernel32.GetConsoleWindow()
        if _hwnd_con:
            ctypes.windll.user32.ShowWindow(_hwnd_con, 0)   # SW_HIDE
    except Exception:
        pass

    # Register Windows app identity — required for taskbar pinning and Alt+Tab icon.
    # Must be called before any window is created.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "sedattelli.DeepScanLocal.1"
        )
    except Exception:
        pass

    # Prevent duplicate instances via a named mutex
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, APP_NAME)
    _last_err = ctypes.windll.kernel32.GetLastError()
    if _mutex_handle and _last_err == 183:   # ERROR_ALREADY_EXISTS
        sys.exit(0)

    app = DeepScanApp()
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        err = traceback.format_exc()
        log_error(f"FATAL STARTUP ERROR:\n{err}")
        # Show error in a plain messagebox so it's visible even without a console
        try:
            import tkinter as _tk
            import tkinter.messagebox as _mb
            _r = _tk.Tk(); _r.withdraw()
            _mb.showerror("DeepScan Local — Başlatma Hatası", err)
            _r.destroy()
        except Exception:
            print(err, file=sys.stderr)
