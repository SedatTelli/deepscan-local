"""
DeepScan Local - Hybrid Result Ranker
Designer: Sedat Telli | sedattelli.com

Combines FTS5 BM25 baseline with RapidFuzz similarity, a filename-match
boost, and a recency boost.  Returns a sorted list with 'final_score' added
to each candidate dict.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from rapidfuzz import fuzz as _fuzz
    _RAPIDFUZZ = True
except ImportError:          # Graceful degradation if not installed yet
    _RAPIDFUZZ = False

# ---------------------------------------------------------------------------
# Boost constants
# ---------------------------------------------------------------------------
FILENAME_BOOST  = 0.30   # Added when any query word appears in the filename
RECENCY_BOOST   = 0.20   # Added when file was modified within RECENCY_DAYS
RECENCY_DAYS    = 7
DISCONNECTED_PENALTY = 0.80   # Score multiplier for disconnected-drive results

# BM25 comes as negative floats from SQLite FTS5 (more negative = better match).
# We normalise to [0, 1] by dividing by a reference magnitude.
BM25_REFERENCE  = 10.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_results(
    query: str,
    candidates: list[dict],
    threshold: int = 70,
) -> list[dict]:
    """
    Re-rank *candidates* returned by the FTS5 query.

    Each candidate must contain:
        path          (str)   — absolute file path
        content       (str)   — raw text (used for fuzzy matching)
        bm25_score    (float) — FTS5 BM25 score (negative; lower = better)
        modified_time (float) — UNIX timestamp
        is_connected  (int)   — 1 = drive present, 0 = disconnected

    Returns the list sorted by ``final_score`` descending with that key added.
    Items below *threshold* fuzzy similarity AND negligible BM25 are dropped.
    """
    if not candidates:
        return []

    query_lower = query.lower()
    query_words = [w for w in query_lower.split() if len(w) > 1]
    recency_cutoff = datetime.now() - timedelta(days=RECENCY_DAYS)

    scored: list[dict] = []

    for row in candidates:
        path_str      = row.get("path", "")
        content       = row.get("content", "")
        bm25          = row.get("bm25_score", 0.0)
        modified_ts   = row.get("modified_time", 0.0)
        is_connected  = int(row.get("is_connected", 1))

        # ── BM25 component (normalised to 0-1) ──────────────────────────────
        bm25_norm = min(1.0, abs(bm25) / BM25_REFERENCE)

        # ── RapidFuzz component ──────────────────────────────────────────────
        fuzzy = 0.0
        if _RAPIDFUZZ and content:
            # Compare against first 3 000 chars for speed
            sample = content[:3_000].lower()
            fuzzy = _fuzz.partial_ratio(query_lower, sample) / 100.0

        # ── Filename boost ───────────────────────────────────────────────────
        filename_lower = Path(path_str).name.lower()
        fname_match = any(w in filename_lower for w in query_words)
        fname_boost = FILENAME_BOOST if fname_match else 0.0

        # Drop only when query is non-empty AND the file has no signal at all:
        # no FTS5 score, no fuzzy match, AND query words absent from filename.
        if query_lower and _RAPIDFUZZ and (fuzzy * 100) < threshold and bm25_norm < 0.05 and not fname_match:
            continue

        # ── Recency boost ────────────────────────────────────────────────────
        recency_boost = 0.0
        try:
            if datetime.fromtimestamp(modified_ts) > recency_cutoff:
                recency_boost = RECENCY_BOOST
        except (OSError, ValueError, OverflowError):
            pass

        # ── Final score ──────────────────────────────────────────────────────
        # BM25 and fuzzy are weighted equally; boosts are additive.
        final = (bm25_norm * 0.50) + (fuzzy * 0.50) + fname_boost + recency_boost

        if not is_connected:
            final *= DISCONNECTED_PENALTY

        scored.append({
            **row,
            "fuzzy_score": round(fuzzy, 3),
            "final_score": round(final, 4),
        })

    scored.sort(key=lambda r: r["final_score"], reverse=True)
    return scored
