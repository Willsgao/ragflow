"""Step 1b: SQLite outbox queue for review batch submission reliability.

When DataTrust is unreachable (network failure, circuit-breaker open, etc.),
batch submission payloads are persisted locally and replayed by the
ReviewResultWorker when connectivity is restored.

Key design decisions:
- Uses Python stdlib sqlite3 (zero extra dependencies).
- Thread-safe via threading.Lock.
- Idempotency via (doc_id + chunk_ids) hash — partial unique index only
  applies to 'pending' rows, so re-parsed docs can always enqueue a fresh batch.
- Exponential backoff: 30s → 1m → 2m → … capped at 10min.
- Failed records are kept for audit; never auto-deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Environment knobs ────────────────────────────────────────────────────────

REVIEW_OUTBOX_PATH: str = os.environ.get(
    "REVIEW_OUTBOX_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "outbox.db"),
)
REVIEW_OUTBOX_MAX_RETRIES: int = int(os.environ.get("REVIEW_OUTBOX_MAX_RETRIES", "10"))


class ReviewOutbox:
    """SQLite-backed outbox queue for review batch submissions."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or REVIEW_OUTBOX_PATH
        self._lock = threading.Lock()
        self._init_db()

    # ── internal helpers ──────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _compute_idempotency_key(self, payload: dict[str, Any]) -> str:
        """Deterministic key: doc_id || sorted chunk ids."""
        doc_id = payload.get("doc_id", "")
        tasks = payload.get("tasks", [])
        chunk_ids = sorted(t.get("chunk_id", "") for t in tasks if t.get("chunk_id"))
        raw = "|".join([doc_id] + chunk_ids)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    # ── schema ────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS review_outbox (
                        id               INTEGER PRIMARY KEY AUTOINCREMENT,
                        payload          TEXT    NOT NULL,
                        status           TEXT    NOT NULL DEFAULT 'pending',
                        retry_count      INTEGER NOT NULL DEFAULT 0,
                        max_retries      INTEGER NOT NULL DEFAULT 10,
                        next_retry_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                        doc_id           TEXT    NOT NULL,
                        idempotency_key  TEXT    NOT NULL,
                        created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
                        updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_idempotency
                        ON review_outbox(idempotency_key) WHERE status = 'pending';

                    CREATE INDEX IF NOT EXISTS idx_outbox_status_retry
                        ON review_outbox(status, next_retry_at);
                """)
                conn.commit()
            finally:
                conn.close()

    # ── public API ────────────────────────────────────────────────────────

    def enqueue(self, payload: dict[str, Any]) -> int | None:
        """Persist a batch submission payload.  Returns record id, or *None* if
        a pending batch with the same idempotency key already exists."""
        doc_id = payload.get("doc_id", "unknown")
        idempotency_key = self._compute_idempotency_key(payload)
        payload_json = json.dumps(payload, ensure_ascii=False)

        with self._lock:
            conn = self._get_conn()
            try:
                existing = conn.execute(
                    "SELECT id FROM review_outbox "
                    "WHERE idempotency_key = ? AND status = 'pending'",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    logger.debug(
                        "Outbox: skip duplicate batch doc_id=%s key=%s...",
                        doc_id, idempotency_key[:8],
                    )
                    return None

                cursor = conn.execute(
                    """INSERT INTO review_outbox
                       (payload, status, retry_count, max_retries,
                        next_retry_at, doc_id, idempotency_key)
                       VALUES (?, 'pending', 0, ?, datetime('now'), ?, ?)""",
                    (payload_json, REVIEW_OUTBOX_MAX_RETRIES, doc_id, idempotency_key),
                )
                conn.commit()
                record_id = cursor.lastrowid
                logger.info(
                    "Outbox: enqueued batch id=%s doc_id=%s", record_id, doc_id,
                )
                return record_id
            finally:
                conn.close()

    # ── peek / mark helpers ───────────────────────────────────────────────

    def peek_pending(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return pending records whose *next_retry_at* has elapsed."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT * FROM review_outbox
                       WHERE status = 'pending'
                         AND next_retry_at <= datetime('now')
                       ORDER BY next_retry_at ASC
                       LIMIT ?""",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def mark_submitted(self, record_id: int) -> None:
        """Mark a record as successfully submitted to DataTrust."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE review_outbox SET status = 'submitted', "
                    "updated_at = datetime('now') WHERE id = ?",
                    (record_id,),
                )
                conn.commit()
                logger.info("Outbox: record %s marked submitted", record_id)
            finally:
                conn.close()

    def mark_failed(self, record_id: int) -> None:
        """Increment retry counter and schedule next attempt with exponential
        backoff.  If max_retries is exceeded the record is permanently marked
        ``failed`` (kept for audit; never auto-deleted)."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT retry_count, max_retries FROM review_outbox WHERE id = ?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    return

                new_count: int = row["retry_count"] + 1
                max_retries: int = row["max_retries"] or REVIEW_OUTBOX_MAX_RETRIES

                if new_count >= max_retries:
                    conn.execute(
                        "UPDATE review_outbox SET status = 'failed', "
                        "retry_count = ?, updated_at = datetime('now') WHERE id = ?",
                        (new_count, record_id),
                    )
                    logger.warning(
                        "Outbox: record %s reached max retries (%s), marked failed",
                        record_id, max_retries,
                    )
                else:
                    delay_seconds = min(30 * (2 ** (new_count - 1)), 600)
                    next_retry = (
                        datetime.utcnow() + timedelta(seconds=delay_seconds)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "UPDATE review_outbox SET retry_count = ?, "
                        "next_retry_at = ?, updated_at = datetime('now') WHERE id = ?",
                        (new_count, next_retry, record_id),
                    )
                    logger.info(
                        "Outbox: record %s retry %s/%s, next at %s",
                        record_id, new_count, max_retries, next_retry,
                    )
                conn.commit()
            finally:
                conn.close()

    # ── monitoring helpers ────────────────────────────────────────────────

    def count_pending(self) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM review_outbox WHERE status = 'pending'"
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

    def count_failed(self) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM review_outbox WHERE status = 'failed'"
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()


# ── module-level singleton ───────────────────────────────────────────────────

_outbox: ReviewOutbox | None = None


def get_outbox() -> ReviewOutbox:
    """Return the process-wide outbox singleton (lazy init)."""
    global _outbox
    if _outbox is None:
        _outbox = ReviewOutbox()
    return _outbox
