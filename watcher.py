"""
DeepScan Local - File System Watcher
Designer: Sedat Telli | sedattelli.com

Responsibilities:
  • React to file create / modify / delete / move events via watchdog.
  • Debounce rapid events (2-second window) to avoid hammering the indexer.
  • Detect newly inserted or removed drives every 10 seconds in a poll loop.
  • Index new drives automatically; flag removed drives as disconnected.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)

from config import INDEXED_EXTENSIONS, get_indexable_drives, log_error


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------

class _DeepScanHandler(FileSystemEventHandler):
    """
    Collects file-system events and feeds them to the Indexer after a
    2-second debounce to avoid repeated parsing of files being written.
    """

    def __init__(self, indexer) -> None:      # indexer: Indexer  (avoid circular import)
        super().__init__()
        self._indexer = indexer
        self._pending: dict[str, float] = {}   # path → last-event timestamp
        self._lock = threading.Lock()

        t = threading.Thread(target=self._flush_loop, daemon=True, name="DSL-debounce")
        t.start()

    # -- Watchdog callbacks --------------------------------------------------

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory and _is_indexable(event.src_path):
            self._queue(event.src_path)

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory and _is_indexable(event.src_path):
            self._queue(event.src_path)

    def on_deleted(self, event: FileDeletedEvent) -> None:
        if not event.is_directory and _is_indexable(event.src_path):
            try:
                self._indexer.remove_file(event.src_path)
            except Exception as exc:
                log_error(f"Watcher delete {event.src_path}: {exc}")

    def on_moved(self, event: FileMovedEvent) -> None:
        if event.is_directory:
            return
        if _is_indexable(event.src_path):
            try:
                self._indexer.remove_file(event.src_path)
            except Exception as exc:
                log_error(f"Watcher move-remove {event.src_path}: {exc}")
        if _is_indexable(event.dest_path):
            self._queue(event.dest_path)

    # -- Debounce queue ------------------------------------------------------

    def _queue(self, path: str) -> None:
        with self._lock:
            self._pending[path] = time.monotonic()

    def _flush_loop(self) -> None:
        """Process pending events that have been idle for ≥ 2 seconds."""
        while True:
            time.sleep(1)
            now = time.monotonic()
            ready: list[str] = []

            with self._lock:
                for path, ts in list(self._pending.items()):
                    if now - ts >= 2.0:
                        ready.append(path)
                        del self._pending[path]

            for path in ready:
                try:
                    self._indexer.update_file(path)
                except Exception as exc:
                    log_error(f"Watcher update {path}: {exc}")


# ---------------------------------------------------------------------------
# FileWatcher — public facade used by main.py
# ---------------------------------------------------------------------------

class FileWatcher:
    def __init__(self, indexer) -> None:
        self._indexer = indexer
        self._observer: Observer | None = None
        self._handler: _DeepScanHandler | None = None
        self._paused = False
        self._known_drives: set[str] = set()

        self._drive_thread = threading.Thread(
            target=self._drive_poll_loop,
            daemon=True,
            name="DSL-drive-monitor",
        )

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Schedule watches on all accessible drives and begin the drive-poll loop."""
        self._observer = Observer()
        self._handler  = _DeepScanHandler(self._indexer)

        drives = get_indexable_drives()   # already filtered to ready drives
        self._known_drives = set(d[:3].upper() for d in drives)

        for drive in drives:
            self._schedule_drive(drive)

        try:
            self._observer.start()
        except Exception as exc:
            log_error(f"Watchdog observer start failed: {exc}")

        self._drive_thread.start()

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def pause(self) -> None:
        """Temporarily stop watchdog while keeping the drive-poll loop alive."""
        if not self._paused and self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            self._paused = True

    def resume(self) -> None:
        """Restart watchdog after a pause."""
        if self._paused:
            self._paused = False
            self._observer = Observer()
            self._handler  = _DeepScanHandler(self._indexer)
            for drive in get_indexable_drives():
                self._schedule_drive(drive)
            self._observer.start()

    def is_paused(self) -> bool:
        return self._paused

    # -- Drive polling -------------------------------------------------------

    def _drive_poll_loop(self) -> None:
        """
        Every 10 seconds: detect inserted / removed drives.
        New drives are indexed immediately.
        Removed drives are flagged as disconnected in the DB.
        """
        while True:
            time.sleep(10)
            try:
                current = {d[:3].upper() for d in get_indexable_drives()}

                added   = current - self._known_drives
                removed = self._known_drives - current

                for dr in removed:
                    log_error(f"Drive removed: {dr}")
                    try:
                        self._indexer.mark_drive_disconnected(dr)
                    except Exception as exc:
                        log_error(f"Connectivity mark error {dr}: {exc}")

                for dr in added:
                    log_error(f"New drive detected: {dr} — indexing")
                    threading.Thread(
                        target=self._index_new_drive,
                        args=(dr,),
                        daemon=True,
                        name=f"DSL-index-{dr}",
                    ).start()

                    # Add live watch for the new drive (if not paused)
                    if not self._paused and self._observer:
                        self._schedule_drive(dr)

                self._known_drives = current

            except Exception as exc:
                log_error(f"Drive poll error: {exc}")

    def _index_new_drive(self, drive_root: str) -> None:
        try:
            self._indexer.mark_drive_connected(drive_root)
            self._indexer._scan_root(drive_root)
        except Exception as exc:
            log_error(f"Index new drive {drive_root}: {exc}")

    def _schedule_drive(self, drive: str) -> None:
        """Add a recursive watchdog watch for *drive* (silent on errors)."""
        if not self._observer or not self._handler:
            return
        try:
            self._observer.schedule(self._handler, drive, recursive=True)
        except Exception as exc:
            log_error(f"Watcher schedule {drive}: {exc}")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _is_indexable(path: str) -> bool:
    return Path(path).suffix.lower() in INDEXED_EXTENSIONS
