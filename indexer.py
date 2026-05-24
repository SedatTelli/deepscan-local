"""
DeepScan Local - SQLite Indexer
Designer: Sedat Telli | sedattelli.com

Responsibilities:
  • Bootstrap SQLite DB with an FTS5 virtual table (search on content_normalized).
  • Full scan: walk all eligible drives respecting the blacklist + extension filter.
  • Incremental update / delete called by watcher.py.
  • FTS5 search with BM25 ranking; falls back to LIKE on syntax errors.
  • Drive connectivity tracking (Disconnected / Takılı Değil flag).

Threading notes:
  Each public method opens its own connection so it is safe to call from
  any thread.  WAL journal mode allows concurrent readers during writes.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from config import (
    DB_PATH,
    SKIP_DIRS,
    INDEXED_EXTENSIONS,
    MAX_FILE_BYTES,
    METADATA_ONLY_EXTENSIONS,
    log_error,
    is_excluded,
    get_indexable_drives,
    load_config,
    _ACTIVE_EXTENSIONS,
    refresh_active_extensions,
)
from parser import extract, normalize

# CPU throttle: sleep between files during the initial full scan
_THROTTLE = 0.01        # seconds per file  (~100 files/sec max)
_COMMIT_EVERY = 200     # commit to disk every N indexed files


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _open_db() -> sqlite3.Connection:
    """Open a WAL-mode connection to the shared index database."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=30000")   # 30s retry on lock
    conn.row_factory = sqlite3.Row
    return conn


def _bootstrap(conn: sqlite3.Connection) -> None:
    """Create tables and indexes on first run (idempotent)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            path              TEXT    UNIQUE NOT NULL,
            content           TEXT    DEFAULT '',
            content_normalized TEXT   DEFAULT '',
            filename_normalized TEXT  DEFAULT '',
            modified_time     REAL    DEFAULT 0,
            indexed_time      REAL    DEFAULT 0,
            file_size         INTEGER DEFAULT 0,
            drive_root        TEXT    DEFAULT '',
            is_connected      INTEGER DEFAULT 1
        );

        -- FTS5 virtual table indexes content_normalized for fast BM25 search.
        -- Rowid is kept in sync with files.id manually.
        CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
            content_normalized,
            tokenize = 'unicode61'
        );

        CREATE INDEX IF NOT EXISTS idx_path     ON files(path);
        CREATE INDEX IF NOT EXISTS idx_drive    ON files(drive_root);
        CREATE INDEX IF NOT EXISTS idx_modified ON files(modified_time);
    """)
    conn.commit()
    # Migrations — safe to run on existing databases
    for migration in (
        "ALTER TABLE files ADD COLUMN filename_normalized TEXT DEFAULT ''",
        "ALTER TABLE files ADD COLUMN entry_type TEXT DEFAULT 'file'",
        "ALTER TABLE files ADD COLUMN file_hash TEXT DEFAULT ''",
    ):
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------------------
# Indexer class
# ---------------------------------------------------------------------------

class Indexer:
    def __init__(self, progress_cb: Optional[Callable[[str, int], None]] = None):
        """
        *progress_cb* is called as ``progress_cb(current_path, total_count)``
        from the indexing thread whenever a file is successfully indexed.
        """
        self._progress_cb = progress_cb
        self._stop = threading.Event()

        # Bootstrap schema once at startup
        with _open_db() as conn:
            _bootstrap(conn)

    # -----------------------------------------------------------------------
    # Full scan
    # -----------------------------------------------------------------------

    def full_scan(self) -> None:
        """
        Scan all eligible drives plus any custom_paths from config.json.
        Skips unchanged files (same mtime).  Marks disconnected drives.
        """
        self._stop.clear()
        cfg = load_config()
        roots: list[str] = get_indexable_drives() + cfg.get("custom_paths", [])

        for root in roots:
            if self._stop.is_set():
                break
            self._scan_root(root)

        self._refresh_connectivity()

    def _scan_root(self, root: str) -> None:
        root_path  = Path(root)
        if not root_path.exists():
            return

        drive_root = str(root_path)[:3].upper()   # e.g. "C:\"
        conn  = _open_db()
        count = 0

        # ── Try MFT fast-scan for whole-drive roots ──────────────────────────
        # MFT gives us ALL file paths on the volume in one shot.  Filtering by
        # SKIP_DIRS and extension still happens, but directory traversal itself
        # is done by the OS kernel — orders of magnitude faster than os.walk.
        # Falls back silently to _walk() when admin rights are absent.
        file_iter = None
        is_drive_root = root_path.resolve() == Path(drive_root).resolve()
        if is_drive_root:
            try:
                from mft_scanner import fast_scan_drive
                mft_paths = fast_scan_drive(drive_root)
                if mft_paths is not None:
                    file_iter = self._mft_filter(mft_paths)
            except Exception as exc:
                log_error(f"MFT scan failed for {drive_root}: {exc}")

        if file_iter is None:
            file_iter = self._walk(root_path)

        try:
            for file_path in file_iter:
                if self._stop.is_set():
                    break

                try:
                    st = file_path.stat()
                except OSError:
                    continue

                if st.st_size > MAX_FILE_BYTES and file_path.suffix.lower() not in METADATA_ONLY_EXTENSIONS:
                    continue

                existing = conn.execute(
                    "SELECT modified_time FROM files WHERE path = ?",
                    (str(file_path),),
                ).fetchone()

                if existing and abs(existing["modified_time"] - st.st_mtime) < 1.0:
                    conn.execute(
                        "UPDATE files SET is_connected=1, drive_root=? WHERE path=?",
                        (drive_root, str(file_path)),
                    )
                    continue

                raw, norm = extract(file_path)
                if not raw:
                    continue

                self._upsert(conn, file_path, raw, norm, st.st_mtime, st.st_size, drive_root)
                count += 1

                if self._progress_cb:
                    self._progress_cb(str(file_path), count)

                if count % _COMMIT_EVERY == 0:
                    conn.commit()

                time.sleep(_THROTTLE)

            conn.commit()

            # ── Index directories for folder search ──────────────────────────
            folder_count = 0
            for folder_path in self._walk_folders(root_path):
                if self._stop.is_set():
                    break
                try:
                    st = folder_path.stat()
                except OSError:
                    continue
                self._upsert_folder(conn, folder_path, st.st_mtime, drive_root)
                folder_count += 1
                if folder_count % _COMMIT_EVERY == 0:
                    conn.commit()
            conn.commit()

        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Directory walkers
    # -----------------------------------------------------------------------

    def _mft_filter(self, paths: list[str]):
        """Filter a flat MFT path list through skip-dirs + extension rules."""
        skip_lower = {s.lower() for s in SKIP_DIRS}
        for path_str in paths:
            if self._stop.is_set():
                return
            p = Path(path_str)
            # Skip if any path component matches SKIP_DIRS
            if any(part.lower() in skip_lower for part in p.parts):
                continue
            # Extension filter
            if p.suffix.lower() not in _ACTIVE_EXTENSIONS:
                continue
            # Skip Office temp files
            if p.name.startswith("~$"):
                continue
            yield p

    def _walk(self, root: Path):
        """Yield eligible files under *root*, respecting the blacklist."""
        try:
            for entry in root.iterdir():
                if self._stop.is_set():
                    return
                try:
                    if entry.is_symlink():
                        continue

                    if entry.is_dir():
                        if _should_skip_dir(entry):
                            continue
                        yield from self._walk(entry)

                    elif entry.is_file() and entry.suffix.lower() in _ACTIVE_EXTENSIONS:
                        # Skip Office lock files (~$filename.docx)
                        if not entry.name.startswith("~$"):
                            yield entry

                except (PermissionError, OSError) as exc:
                    log_error(f"Walk entry {entry}: {exc}")
        except (PermissionError, OSError) as exc:
            log_error(f"Walk root {root}: {exc}")

    def _walk_folders(self, root: Path, _depth: int = 0):
        """Yield non-skipped directories under *root* up to 6 levels deep."""
        if _depth > 6:
            return
        try:
            for entry in root.iterdir():
                if self._stop.is_set():
                    return
                try:
                    if entry.is_symlink() or not entry.is_dir():
                        continue
                    if _should_skip_dir(entry):
                        continue
                    yield entry
                    yield from self._walk_folders(entry, _depth + 1)
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            pass

    # -----------------------------------------------------------------------
    # Upsert (insert or update a single file record + FTS5 row)
    # -----------------------------------------------------------------------

    def _upsert(
        self,
        conn: sqlite3.Connection,
        path: Path,
        raw: str,
        norm: str,
        mtime: float,
        size: int,
        drive_root: str,
    ) -> None:
        now = time.time()
        path_str = str(path)
        fname_norm = normalize(path.stem)

        row = conn.execute(
            "SELECT id FROM files WHERE path = ?", (path_str,)
        ).fetchone()

        if row:
            fid = row["id"]
            conn.execute(
                """UPDATE files SET
                    content=?, content_normalized=?, filename_normalized=?,
                    modified_time=?, indexed_time=?,
                    file_size=?, drive_root=?, is_connected=1
                   WHERE id=?""",
                (raw, norm, fname_norm, mtime, now, size, drive_root, fid),
            )
            conn.execute("DELETE FROM files_fts WHERE rowid=?", (fid,))
            conn.execute(
                "INSERT INTO files_fts(rowid, content_normalized) VALUES (?,?)",
                (fid, norm),
            )
        else:
            cur = conn.execute(
                """INSERT INTO files
                    (path, content, content_normalized, filename_normalized,
                     modified_time, indexed_time, file_size,
                     drive_root, is_connected)
                   VALUES (?,?,?,?,?,?,?,?,1)""",
                (path_str, raw, norm, fname_norm, mtime, now, size, drive_root),
            )
            fid = cur.lastrowid
            conn.execute(
                "INSERT INTO files_fts(rowid, content_normalized) VALUES (?,?)",
                (fid, norm),
            )

    def _upsert_folder(
        self,
        conn: sqlite3.Connection,
        path: Path,
        mtime: float,
        drive_root: str,
    ) -> None:
        """Insert or update a directory record (entry_type='folder')."""
        now      = time.time()
        path_str = str(path)
        name     = path.name
        name_norm = normalize(name)

        row = conn.execute("SELECT id FROM files WHERE path=?", (path_str,)).fetchone()
        if row:
            conn.execute(
                """UPDATE files SET
                    content=?, content_normalized=?, filename_normalized=?,
                    modified_time=?, indexed_time=?, drive_root=?, is_connected=1,
                    entry_type='folder'
                   WHERE id=?""",
                (name, name_norm, name_norm, mtime, now, drive_root, row["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO files
                    (path, content, content_normalized, filename_normalized,
                     modified_time, indexed_time, file_size, drive_root, is_connected, entry_type)
                   VALUES (?,?,?,?,?,?,0,?,1,'folder')""",
                (path_str, name, name_norm, name_norm, mtime, now, drive_root),
            )

    # -----------------------------------------------------------------------
    # Watcher-facing public methods
    # -----------------------------------------------------------------------

    def update_file(self, file_path: str) -> None:
        """Re-index a single file (called by watcher on create/modify)."""
        path = Path(file_path)

        if path.suffix.lower() not in _ACTIVE_EXTENSIONS:
            return
        try:
            st = path.stat()
            if st.st_size > MAX_FILE_BYTES:
                return
        except OSError:
            return

        raw, norm = extract(path)
        if not raw:
            return

        drive_root = str(path)[:3].upper()
        conn = _open_db()
        try:
            self._upsert(conn, path, raw, norm, st.st_mtime, st.st_size, drive_root)
            conn.commit()
        finally:
            conn.close()

    def remove_file(self, file_path: str) -> None:
        """Delete a file record from the index (called by watcher on delete)."""
        conn = _open_db()
        try:
            row = conn.execute(
                "SELECT id FROM files WHERE path=?", (file_path,)
            ).fetchone()
            if row:
                conn.execute("DELETE FROM files_fts WHERE rowid=?", (row["id"],))
                conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
                conn.commit()
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    def search(self, query: str, limit: int = 100, filters: dict | None = None) -> list[dict]:
        """
        Run a hybrid FTS5 / LIKE search.

        Returns a list of dicts:
          path, content, modified_time, is_connected, drive_root, file_size, bm25_score

        *filters* is an optional dict produced by parse_query():
          extensions  → list[str]  e.g. [".pdf", ".docx"]
          size_op     → "<" | ">" | "="
          size_bytes  → int
          modified    → "today" | "yesterday" | "week" | "month"
          before      → "YYYY-MM-DD"
          after       → "YYYY-MM-DD"
          regex       → regex pattern matched against filename
        """
        query = query.strip()
        if not query and not filters:
            return []
        if not query and filters:
            conn = _open_db()
            try:
                rows = conn.execute(
                    """SELECT path, content, modified_time, is_connected,
                              drive_root, file_size, 0.0 AS bm25_score
                       FROM files WHERE is_connected=1
                       ORDER BY modified_time DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                return _apply_filters([dict(r) for r in rows], filters)
            except Exception:
                return []
            finally:
                conn.close()

        norm_query = normalize(query)
        conn = _open_db()
        results: list[dict] = []

        try:
            # ── Content search (FTS5 with LIKE fallback) ─────────────────────
            # Use custom FTS5 expression if provided (e.g. boolean query)
            custom_fts = (filters or {}).get("fts_expr")
            if custom_fts:
                fts_expr = custom_fts
                tokens = [w for w in norm_query.split() if w and w.upper() not in ("AND", "OR", "NOT")]
            else:
                tokens = [w for w in norm_query.split() if w]
                if not tokens:
                    return []
                fts_expr = " ".join(f'"{t}"' for t in tokens)

            try:
                rows = conn.execute(
                    """
                    SELECT f.path, f.content, f.modified_time,
                           f.is_connected, f.drive_root, f.file_size,
                           bm25(files_fts) AS bm25_score
                    FROM   files_fts
                    JOIN   files f ON files_fts.rowid = f.id
                    WHERE  files_fts MATCH ? AND f.is_connected=1
                    ORDER  BY bm25_score
                    LIMIT  ?
                    """,
                    (fts_expr, limit),
                ).fetchall()
                results = [dict(r) for r in rows]
            except sqlite3.OperationalError as exc:
                log_error(f"FTS5 query error for '{query}': {exc}")
                try:
                    rows = conn.execute(
                        """
                        SELECT path, content, modified_time,
                               is_connected, drive_root, file_size,
                               0.0 AS bm25_score
                        FROM   files
                        WHERE  content_normalized LIKE ? AND is_connected=1
                        LIMIT  ?
                        """,
                        (f"%{norm_query}%", limit),
                    ).fetchall()
                    results = [dict(r) for r in rows]
                except sqlite3.Error:
                    pass

            # ── Filename search supplement ────────────────────────────────────
            try:
                seen = {r["path"] for r in results}
                fname_rows = conn.execute(
                    """
                    SELECT path, content, modified_time,
                           is_connected, drive_root, file_size,
                           -0.3 AS bm25_score
                    FROM   files
                    WHERE  filename_normalized LIKE ? AND is_connected=1
                    LIMIT  ?
                    """,
                    (f"%{norm_query}%", limit),
                ).fetchall()
                for r in fname_rows:
                    d = dict(r)
                    if d["path"] not in seen:
                        results.append(d)
                        seen.add(d["path"])
            except sqlite3.Error:
                pass

        finally:
            conn.close()

        if filters:
            results = _apply_filters(results, filters)

        return results

    def search_names_only(self, query: str, limit: int = 20, filters: dict | None = None) -> list[dict]:
        """Fast filename-only search — returns results in milliseconds for instant UI."""
        query = query.strip()
        if not query and not filters:
            return []
        conn = _open_db()
        try:
            if query:
                norm_q = normalize(query)
                rows = conn.execute(
                    """
                    SELECT path, content, modified_time, is_connected,
                           drive_root, file_size, -0.3 AS bm25_score
                    FROM   files
                    WHERE  filename_normalized LIKE ? AND is_connected=1
                    LIMIT  ?
                    """,
                    (f"%{norm_q}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT path, content, modified_time, is_connected,
                              drive_root, file_size, 0.0 AS bm25_score
                       FROM files WHERE is_connected=1
                       ORDER BY modified_time DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            results = [dict(r) for r in rows]
        finally:
            conn.close()
        if filters:
            results = _apply_filters(results, filters)
        return results

    # -----------------------------------------------------------------------
    # App / shortcut indexing
    # -----------------------------------------------------------------------

    def index_apps(self) -> None:
        """Index installed apps from Start Menu and Desktop (.lnk shortcuts).
        Stores them as entry_type='app' so the UI can display and launch them.
        """
        import os
        skip_keywords = ("uninstall", "remove", "help", "readme", "manual", "repair")

        lnk_roots: list[Path] = []
        for env_var, rel in (
            ("APPDATA",      r"Microsoft\Windows\Start Menu\Programs"),
            ("PROGRAMDATA",  r"Microsoft\Windows\Start Menu\Programs"),
            ("USERPROFILE",  "Desktop"),
            ("PUBLIC",       "Desktop"),
        ):
            base = os.environ.get(env_var, "")
            if base:
                lnk_roots.append(Path(base) / rel)

        conn = _open_db()
        try:
            count = 0
            for root in lnk_roots:
                if not root.exists():
                    continue
                for lnk in root.rglob("*.lnk"):
                    name = lnk.stem
                    if any(kw in name.lower() for kw in skip_keywords):
                        continue
                    name_norm = normalize(name)
                    path_str  = str(lnk)
                    try:
                        mtime = lnk.stat().st_mtime
                    except OSError:
                        mtime = 0.0

                    row = conn.execute(
                        "SELECT id FROM files WHERE path=?", (path_str,)
                    ).fetchone()
                    if row:
                        fid = row["id"]
                        conn.execute(
                            """UPDATE files SET
                                content=?, content_normalized=?, filename_normalized=?,
                                modified_time=?, indexed_time=?, entry_type='app', is_connected=1
                               WHERE id=?""",
                            (name, name_norm, name_norm, mtime, time.time(), fid),
                        )
                        conn.execute("DELETE FROM files_fts WHERE rowid=?", (fid,))
                        conn.execute(
                            "INSERT INTO files_fts(rowid, content_normalized) VALUES (?,?)",
                            (fid, name_norm),
                        )
                    else:
                        cur = conn.execute(
                            """INSERT INTO files
                                (path, content, content_normalized, filename_normalized,
                                 modified_time, indexed_time, drive_root, is_connected, entry_type)
                               VALUES (?,?,?,?,?,?,'',1,'app')""",
                            (path_str, name, name_norm, name_norm, mtime, time.time()),
                        )
                        fid = cur.lastrowid
                        conn.execute(
                            "INSERT INTO files_fts(rowid, content_normalized) VALUES (?,?)",
                            (fid, name_norm),
                        )
                    count += 1
                    if count % 50 == 0:
                        conn.commit()
            conn.commit()
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Drive connectivity
    # -----------------------------------------------------------------------

    def _refresh_connectivity(self) -> None:
        """
        Mark files on currently disconnected drives as is_connected=0.
        Called after full_scan to handle drives removed since last run.
        """
        live = {d[:3].upper() for d in get_indexable_drives()}
        conn = _open_db()
        try:
            drive_rows = conn.execute(
                "SELECT DISTINCT drive_root FROM files"
            ).fetchall()
            for row in drive_rows:
                dr = (row["drive_root"] or "")[:3].upper()
                connected = 1 if dr in live else 0
                conn.execute(
                    "UPDATE files SET is_connected=? WHERE drive_root=?",
                    (connected, row["drive_root"]),
                )
            conn.commit()
        finally:
            conn.close()

    def mark_drive_disconnected(self, drive_root: str) -> None:
        """Flag all files on *drive_root* as disconnected (called by watcher)."""
        conn = _open_db()
        try:
            conn.execute(
                "UPDATE files SET is_connected=0 WHERE drive_root=?",
                (drive_root[:3].upper(),),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_drive_connected(self, drive_root: str) -> None:
        """Restore connectivity flag when drive is re-inserted."""
        conn = _open_db()
        try:
            conn.execute(
                "UPDATE files SET is_connected=1 WHERE drive_root=?",
                (drive_root[:3].upper(),),
            )
            conn.commit()
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def remove_paths(self, paths: list[str]) -> None:
        """Delete DB records for files that no longer exist on disk."""
        if not paths:
            return
        conn = _open_db()
        try:
            for p in paths:
                conn.execute("DELETE FROM files WHERE path=?", (p,))
            conn.commit()
        except Exception as exc:
            log_error(f"remove_paths: {exc}")
        finally:
            conn.close()

    def purge_excluded_extensions(self) -> None:
        """Delete DB records whose extension is no longer in INDEXED_EXTENSIONS."""
        conn = _open_db()
        try:
            rows = conn.execute("SELECT id, path FROM files").fetchall()
            to_remove = [
                row["id"] for row in rows
                if Path(row["path"]).suffix.lower() not in INDEXED_EXTENSIONS
            ]
            for fid in to_remove:
                conn.execute("DELETE FROM files_fts WHERE rowid=?", (fid,))
                conn.execute("DELETE FROM files WHERE id=?",        (fid,))
            if to_remove:
                conn.commit()
                log_error(f"Purged {len(to_remove)} records with excluded extensions.")
        finally:
            conn.close()

    def stop(self) -> None:
        """Signal the indexer to abort any running scan."""
        self._stop.set()

    # -----------------------------------------------------------------------
    # Duplicate detection
    # -----------------------------------------------------------------------

    def compute_hashes(self, progress_cb: Optional[Callable[[int, int], None]] = None) -> None:
        """Compute MD5 hashes for all connected files that don't have one yet."""
        import hashlib
        conn = _open_db()
        try:
            rows = conn.execute(
                """SELECT id, path FROM files
                   WHERE is_connected=1 AND (file_hash IS NULL OR file_hash='')
                     AND entry_type != 'folder' AND entry_type != 'app'
                   ORDER BY file_size"""
            ).fetchall()
            total = len(rows)
            for i, row in enumerate(rows):
                if self._stop.is_set():
                    break
                path = Path(row["path"])
                try:
                    h = hashlib.md5(path.read_bytes()).hexdigest()
                    conn.execute("UPDATE files SET file_hash=? WHERE id=?", (h, row["id"]))
                    if (i + 1) % 100 == 0:
                        conn.commit()
                except (OSError, PermissionError):
                    continue
                if progress_cb:
                    progress_cb(i + 1, total)
            conn.commit()
        finally:
            conn.close()

    def find_duplicates(self) -> list[list[dict]]:
        """
        Return groups of files sharing the same MD5 hash (non-empty).
        Each group is a list of result dicts, sorted by modified_time desc.
        Groups are sorted by size descending so large dupes come first.
        """
        conn = _open_db()
        try:
            rows = conn.execute(
                """SELECT path, file_size, modified_time, file_hash, drive_root, is_connected
                   FROM files
                   WHERE file_hash != '' AND file_hash IS NOT NULL
                     AND entry_type != 'folder' AND entry_type != 'app'
                   ORDER BY file_hash, modified_time DESC"""
            ).fetchall()
        finally:
            conn.close()

        from collections import defaultdict
        by_hash: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_hash[r["file_hash"]].append(dict(r))

        groups = [g for g in by_hash.values() if len(g) >= 2]
        groups.sort(key=lambda g: g[0].get("file_size", 0), reverse=True)
        return groups


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def get_index_stats() -> dict:
    """Return index health statistics (no Indexer instance required)."""
    conn = _open_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, MAX(indexed_time) AS last_indexed FROM files"
        ).fetchone()
        by_drive = conn.execute(
            "SELECT drive_root, COUNT(*) AS cnt FROM files GROUP BY drive_root"
        ).fetchall()
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        return {
            "total_files":   row["total"] or 0,
            "last_indexed":  row["last_indexed"] or 0,
            "db_size_bytes": db_size,
            "by_drive":      {r["drive_root"]: r["cnt"] for r in by_drive},
        }
    finally:
        conn.close()


def _apply_filters(results: list[dict], filters: dict) -> list[dict]:
    """Post-filter search results by extension, size, modification date, and regex."""
    import time as _t
    import datetime as _dt
    import re as _re
    now = _t.time()
    day = 86_400

    _before_ts: float | None = None
    _after_ts:  float | None = None
    _regex_pat = None

    if "before" in filters:
        try:
            _before_ts = _dt.datetime.strptime(filters["before"], "%Y-%m-%d").timestamp()
        except ValueError:
            pass
    if "after" in filters:
        try:
            _after_ts = _dt.datetime.strptime(filters["after"], "%Y-%m-%d").timestamp()
        except ValueError:
            pass
    if "regex" in filters:
        try:
            _regex_pat = _re.compile(filters["regex"], _re.IGNORECASE)
        except _re.error:
            pass

    out = []
    for r in results:
        path = r.get("path", "")

        if "extensions" in filters:
            ext = Path(path).suffix.lower()
            if ext not in filters["extensions"]:
                continue

        if "size_op" in filters:
            sz  = r.get("file_size") or 0
            op  = filters["size_op"]
            ref = filters["size_bytes"]
            if op == ">" and sz <= ref:
                continue
            elif op == "<" and sz >= ref:
                continue
            elif op == "=" and abs(sz - ref) > max(ref * 0.1, 1):
                continue

        if "modified" in filters:
            age = now - (r.get("modified_time") or 0)
            mod = filters["modified"]
            if mod == "today" and age > day:
                continue
            elif mod == "yesterday" and not (day <= age <= 2 * day):
                continue
            elif mod == "week" and age > 7 * day:
                continue
            elif mod == "month" and age > 30 * day:
                continue

        if _before_ts is not None:
            if (r.get("modified_time") or 0) >= _before_ts:
                continue

        if _after_ts is not None:
            if (r.get("modified_time") or 0) <= _after_ts:
                continue

        if _regex_pat is not None:
            if not _regex_pat.search(path):
                continue

        out.append(r)
    return out


def _should_skip_dir(path: Path) -> bool:
    """Return True when *path* should be excluded from indexing."""
    name_lower = path.name.lower()

    # Fast name-only check (no string contains needed)
    _NAME_SKIP = frozenset({
        "node_modules", ".git", "__pycache__",
        "venv", ".venv", "env", ".env",
        "$recycle.bin", "system volume information",
    })
    if name_lower in _NAME_SKIP:
        return True

    # Substring check against the full lowercased path
    return is_excluded(path)
