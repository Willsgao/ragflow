"""RAGFlow → RAGTrust HTTP client with circuit breaker.

Step 1a of the DataTrust review-gate implementation.
Provides two interfaces:
  - get_review_policy(kb_id) -> ReviewPolicy
  - submit_review_batch(payload) -> batch_id

All calls are guarded by a circuit breaker. On OPEN the breaker raises
CircuitBreakerOpenError; the caller decides how to handle it based on
DATATRUST_FAIL_STRATEGY (fail_closed / fail_open).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from common.http_client import async_request

logger = logging.getLogger(__name__)

# ── Environment knobs ────────────────────────────────────────────────────────

DATATRUST_URL: str = os.environ.get("DATATRUST_URL", "")
DATATRUST_TIMEOUT: float = float(os.environ.get("DATATRUST_TIMEOUT", "15"))

DATATRUST_FAIL_STRATEGY: str = os.environ.get("DATATRUST_FAIL_STRATEGY", "fail_closed")
# fail_closed  — chunk stays available_int=0 until proven safe (secure default)
# fail_open    — chunk auto-passes review when DataTrust is unreachable

DATATRUST_CIRCUIT_THRESHOLD: int = int(os.environ.get("DATATRUST_CIRCUIT_THRESHOLD", "3"))
DATATRUST_CIRCUIT_RECOVERY: float = float(os.environ.get("DATATRUST_CIRCUIT_RECOVERY", "30"))


# ── Exceptions ───────────────────────────────────────────────────────────────

class DataTrustError(Exception):
    """Base exception for DataTrust client errors."""


class CircuitBreakerOpenError(DataTrustError):
    """Raised when the circuit breaker is OPEN — no attempt was made."""

    def __init__(self):
        super().__init__("Circuit breaker is OPEN — DataTrust is unreachable")


class DataTrustRequestError(DataTrustError):
    """A single HTTP request to DataTrust failed (non-2xx or connection error)."""


# ── Data models ──────────────────────────────────────────────────────────────

class FailStrategy(str, Enum):
    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"


@dataclass
class ReviewPolicy:
    kb_id: str
    mode: str  # auto_pass / full_review / sampled
    sample_rate: float = 0.0

    @classmethod
    def default(cls, kb_id: str) -> "ReviewPolicy":
        return cls(kb_id=kb_id, mode="auto_pass", sample_rate=0.0)


@dataclass
class ChunkPayload:
    id: str
    content_with_weight: str
    page_num_int: list[int] | None = None


@dataclass
class SubmitBatchPayload:
    tenant_id: str
    doc_id: str
    kb_id: str
    object_type: str = "text_chunk"
    chunks: list[ChunkPayload] = field(default_factory=list)


@dataclass
class SubmitBatchResult:
    batch_id: str
    status: str
    chunk_count: int


@dataclass
class ReviewBatchSummary:
    batch_id: str
    tenant_id: str
    doc_id: str
    kb_id: str
    status: str
    chunk_count: int
    completed_at: str = ""


@dataclass
class ChunkDecision:
    id: str
    action: str  # approved / approved_with_changes / rejected
    corrected_content: str | None = None
    changes: list[dict] | None = None
    reason: str | None = None


@dataclass
class BatchResult:
    batch_id: str
    tenant_id: str
    kb_id: str
    doc_id: str
    status: str
    chunks: list[ChunkDecision] = field(default_factory=list)


# ── Circuit breaker ──────────────────────────────────────────────────────────

class CircuitBreaker:
    """Simple three-state circuit breaker.

    States:
      CLOSED     — normal operation, calls go through
      OPEN       — too many failures, calls are rejected immediately
      HALF_OPEN  — recovery timeout expired, one probe call allowed
    """

    STATE_CLOSED = "closed"
    STATE_OPEN = "open"
    STATE_HALF_OPEN = "half_open"

    def __init__(self, threshold: int = 3, recovery_seconds: float = 30):
        self.threshold = threshold
        self.recovery_seconds = recovery_seconds
        self._state = self.STATE_CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._opened_at: float = 0

    @property
    def state(self) -> str:
        if self._state == self.STATE_OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_seconds:
                self._state = self.STATE_HALF_OPEN
                logger.info("Circuit breaker: OPEN → HALF_OPEN (recovery timeout elapsed)")
        return self._state

    def record_success(self) -> None:
        if self._state != self.STATE_CLOSED:
            logger.info("Circuit breaker: %s → CLOSED (probe succeeded)", self._state)
        self._state = self.STATE_CLOSED
        self._failure_count = 0

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == self.STATE_HALF_OPEN:
            # probe failed — go back to OPEN
            self._state = self.STATE_OPEN
            self._opened_at = time.monotonic()
            logger.warning("Circuit breaker: HALF_OPEN → OPEN (probe failed)")
        elif self._failure_count >= self.threshold:
            self._state = self.STATE_OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                "Circuit breaker: CLOSED → OPEN (%d consecutive failures)",
                self._failure_count,
            )

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(state={self.state}, failures={self._failure_count}, "
            f"threshold={self.threshold})"
        )


# ── Client ───────────────────────────────────────────────────────────────────

class DataTrustClient:
    """Async HTTP client for RAGTrust review-gate API.

    Usage::

        client = DataTrustClient("http://ragtrust:8000")
        policy = await client.get_review_policy("kb_001")
        result = await client.submit_review_batch(payload)
    """

    def __init__(self, base_url: str = ""):
        self.base_url = (base_url or DATATRUST_URL).rstrip("/")
        self.timeout = DATATRUST_TIMEOUT
        self.fail_strategy = FailStrategy(DATATRUST_FAIL_STRATEGY)
        self.circuit = CircuitBreaker(
            threshold=DATATRUST_CIRCUIT_THRESHOLD,
            recovery_seconds=DATATRUST_CIRCUIT_RECOVERY,
        )

    # -- helpers ---------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make an HTTP request to RAGTrust, guarded by the circuit breaker."""
        if not self.base_url:
            raise DataTrustError("DATATRUST_URL is not configured")

        if self.circuit.state == CircuitBreaker.STATE_OPEN:
            raise CircuitBreakerOpenError()

        try:
            url = f"{self.base_url}/api/v1{path}"
            response = await async_request(
                method,
                url,
                request_timeout=self.timeout,
                retries=0,  # retries are handled by the circuit breaker itself
                **kwargs,
            )
            if response.status_code >= 400:
                resp_text = response.text[:200]
                self.circuit.record_failure()
                raise DataTrustRequestError(
                    f"DataTrust returned {response.status_code}: {resp_text}"
                )
            self.circuit.record_success()
            return response.json()
        except DataTrustRequestError:
            raise
        except Exception as exc:
            self.circuit.record_failure()
            raise DataTrustRequestError(
                f"DataTrust request failed: {exc}"
            ) from exc

    # -- public API ------------------------------------------------------------

    async def get_review_policy(self, kb_id: str) -> ReviewPolicy:
        """§9.4: Fetch the review policy for a knowledge base.

        Returns a default auto_pass policy if RAGTrust is unreachable
        and fail_strategy is fail_open.
        """
        try:
            data = await self._request("GET", f"/policies/{kb_id}")
            return ReviewPolicy(
                kb_id=data["kb_id"],
                mode=data["mode"],
                sample_rate=data.get("sample_rate", 0.0),
            )
        except CircuitBreakerOpenError:
            if self.fail_strategy == FailStrategy.FAIL_OPEN:
                logger.warning("Circuit open + fail_open: defaulting to auto_pass for kb=%s", kb_id)
                return ReviewPolicy.default(kb_id)
            raise
        except DataTrustRequestError:
            if self.fail_strategy == FailStrategy.FAIL_OPEN:
                logger.warning("Request failed + fail_open: defaulting to auto_pass for kb=%s", kb_id)
                return ReviewPolicy.default(kb_id)
            raise

    async def submit_review_batch(self, payload: SubmitBatchPayload) -> SubmitBatchResult:
        """§9.1: Submit a batch of chunks for human review."""
        body = {
            "tenant_id": payload.tenant_id,
            "doc_id": payload.doc_id,
            "kb_id": payload.kb_id,
            "object_type": payload.object_type,
            "chunks": [
                {
                    "id": c.id,
                    "content_with_weight": c.content_with_weight,
                    "page_num_int": c.page_num_int,
                }
                for c in payload.chunks
            ],
        }
        data = await self._request("POST", "/review-batches", json=body)
        return SubmitBatchResult(
            batch_id=data["batch_id"],
            status=data["status"],
            chunk_count=data["chunk_count"],
        )

    async def list_completed_batches(self, kb_id: str = "") -> list[ReviewBatchSummary]:
        """§9.2: List batches that have completed review (status=completed)."""
        path = "/review-batches?status=completed"
        if kb_id:
            path += f"&kb_id={kb_id}"
        data = await self._request("GET", path)
        items = data if isinstance(data, list) else data.get("batches", [])
        return [
            ReviewBatchSummary(
                batch_id=item["batch_id"],
                tenant_id=item["tenant_id"],
                doc_id=item["doc_id"],
                kb_id=item["kb_id"],
                status=item["status"],
                chunk_count=item.get("chunk_count", 0),
                completed_at=item.get("completed_at", ""),
            )
            for item in items
        ]

    async def get_batch_result(self, batch_id: str) -> BatchResult:
        """§9.3: Fetch the per-chunk review decisions for a completed batch."""
        data = await self._request("GET", f"/review-batches/{batch_id}/result")
        chunks = [
            ChunkDecision(
                id=c["id"],
                action=c["action"],
                corrected_content=c.get("corrected_content"),
                changes=c.get("changes"),
                reason=c.get("reason"),
            )
            for c in data.get("chunks", [])
        ]
        return BatchResult(
            batch_id=data["batch_id"],
            tenant_id=data["tenant_id"],
            kb_id=data["kb_id"],
            doc_id=data["doc_id"],
            status=data["status"],
            chunks=chunks,
        )

    async def mark_batch_applied(self, batch_id: str) -> bool:
        """§9.4: Mark a batch as applied after chunk updates are committed."""
        data = await self._request("PATCH", f"/review-batches/{batch_id}", json={"status": "applied"})
        return data.get("status") == "applied"


# ── Module-level singleton ───────────────────────────────────────────────────

_client: DataTrustClient | None = None


def get_client() -> DataTrustClient:
    """Return the module-level DataTrustClient singleton, creating it on first access."""
    global _client
    if _client is None:
        _client = DataTrustClient()
    return _client
