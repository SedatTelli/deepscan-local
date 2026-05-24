"""
DeepScan Local - NTFS MFT Fast Scanner
Designer: Sedat Telli | sedattelli.com

Uses FSCTL_ENUM_USN_DATA to enumerate every file on an NTFS volume by reading
the Master File Table directly — the same technique used by Everything.
~50-100x faster than os.walk for drives with many files.

Requires read access to the raw volume device (\\\\.\\C:).
On Windows 10/11 this typically needs admin privileges.
Falls back gracefully to None so the caller can use os.walk instead.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import struct
from typing import Optional

# ── Win32 constants ──────────────────────────────────────────────────────────
_GENERIC_READ           = 0x80000000
_FILE_SHARE_READ        = 0x00000001
_FILE_SHARE_WRITE       = 0x00000002
_OPEN_EXISTING          = 3
_FSCTL_ENUM_USN_DATA    = 0x000900B3
_FILE_ATTR_DIRECTORY    = 0x00000010
_FILE_ATTR_REPARSE      = 0x00000400   # junction / symlink — skip
_FILE_ATTR_SYSTEM       = 0x00000004
_SKIP_ATTRS             = _FILE_ATTR_REPARSE

_k32 = ctypes.windll.kernel32
_k32.CreateFileW.restype  = ctypes.c_void_p
_k32.CloseHandle.restype  = ctypes.wintypes.BOOL
_k32.DeviceIoControl.restype = ctypes.wintypes.BOOL


class _MFT_ENUM_DATA(ctypes.Structure):
    _fields_ = [
        ("StartFileReferenceNumber", ctypes.c_uint64),
        ("LowUsn",  ctypes.c_int64),
        ("HighUsn", ctypes.c_int64),
    ]


_BUF_SIZE = 524288   # 512 KB read buffer — bigger = fewer syscalls


def _open_volume(drive_root: str) -> Optional[int]:
    """
    Open a raw volume handle (e.g. \\\\.\\C:).
    Returns the handle as int, or None if inaccessible.
    """
    # drive_root like "C:\" or "C:" → we need "\\.\C:"
    letter = drive_root.strip("\\/")[0].upper()
    path   = f"\\\\.\\{letter}:"
    h = _k32.CreateFileW(
        path,
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None, _OPEN_EXISTING, 0, None,
    )
    # INVALID_HANDLE_VALUE is -1 as size_t
    if h is None or h in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF, ctypes.c_void_p(-1).value):
        return None
    return h


def _enum_mft(handle: int) -> Optional[dict[int, tuple[str, int, bool]]]:
    """
    Run FSCTL_ENUM_USN_DATA and collect all MFT entries.
    Returns {ref_num: (name, parent_ref, is_dir)} or None on failure.
    """
    buf  = (ctypes.c_byte * _BUF_SIZE)()
    done = ctypes.wintypes.DWORD(0)
    med  = _MFT_ENUM_DATA(
        StartFileReferenceNumber=0,
        LowUsn=0,
        HighUsn=0x7FFFFFFFFFFFFFFF,
    )

    entries: dict[int, tuple[str, int, bool]] = {}

    while True:
        ok = _k32.DeviceIoControl(
            handle,
            _FSCTL_ENUM_USN_DATA,
            ctypes.byref(med), ctypes.sizeof(med),
            buf, _BUF_SIZE,
            ctypes.byref(done),
            None,
        )
        if not ok or done.value <= 8:
            break

        raw   = bytes(buf[: done.value])
        # First 8 bytes = next StartFileReferenceNumber for continuation
        med.StartFileReferenceNumber = struct.unpack_from("<Q", raw, 0)[0]

        offset = 8
        while offset + 60 <= done.value:
            rec_len = struct.unpack_from("<I", raw, offset)[0]
            if rec_len == 0 or offset + rec_len > done.value:
                break

            ref_num    = struct.unpack_from("<Q", raw, offset +  8)[0] & 0xFFFFFFFFFFFF
            parent_ref = struct.unpack_from("<Q", raw, offset + 16)[0] & 0xFFFFFFFFFFFF
            attrs      = struct.unpack_from("<I", raw, offset + 52)[0]
            fname_len  = struct.unpack_from("<H", raw, offset + 56)[0]
            fname_off  = struct.unpack_from("<H", raw, offset + 58)[0]

            if not (attrs & _SKIP_ATTRS):
                is_dir = bool(attrs & _FILE_ATTR_DIRECTORY)
                name   = raw[
                    offset + fname_off: offset + fname_off + fname_len
                ].decode("utf-16-le", errors="replace")
                entries[ref_num] = (name, parent_ref, is_dir)

            offset += rec_len

    return entries or None


def _build_paths(
    entries: dict[int, tuple[str, int, bool]],
    drive_root: str,
) -> list[str]:
    """
    Reconstruct absolute file paths from MFT ref-num entries.
    NTFS root directory is always ref_num 5.
    Uses iterative BFS — safe for any directory depth.
    """
    ROOT = 5
    root = drive_root[:3]  # e.g. "C:\"
    if not root.endswith("\\"):
        root += "\\"

    # path_map: ref_num → full path string
    path_map: dict[int, str] = {ROOT: root}
    # queue of ref_nums whose parent path is now known
    from collections import deque
    queue: deque[int] = deque([ROOT])

    # Build children index for efficient BFS
    children: dict[int, list[int]] = {}
    for ref, (_n, parent, _d) in entries.items():
        children.setdefault(parent, []).append(ref)

    while queue:
        parent_ref = queue.popleft()
        parent_path = path_map[parent_ref]
        for child_ref in children.get(parent_ref, []):
            if child_ref in path_map:
                continue
            if child_ref not in entries:
                continue
            name, _p, is_dir = entries[child_ref]
            full = parent_path + name + ("\\" if is_dir else "")
            path_map[child_ref] = full
            if is_dir:
                queue.append(child_ref)

    # Return only files (not directories) that were successfully resolved
    return [
        path_map[ref]
        for ref, (_n, _p, is_dir) in entries.items()
        if not is_dir and ref in path_map
    ]


def fast_scan_drive(drive_root: str) -> Optional[list[str]]:
    """
    Return all file paths on *drive_root* using NTFS MFT enumeration.

    *drive_root* — e.g. "C:\\" or "C:".

    Returns a flat list of absolute file path strings, or **None** when MFT
    access is unavailable (non-NTFS volume, no admin rights, etc.).
    The caller should fall back to os.walk when None is returned.
    """
    handle = _open_volume(drive_root)
    if handle is None:
        return None
    try:
        entries = _enum_mft(handle)
        if not entries:
            return None
        return _build_paths(entries, drive_root)
    except Exception:
        return None
    finally:
        _k32.CloseHandle(handle)
