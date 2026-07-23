"""Review result worker — polls DataTrust for completed review batches, applies
chunk decisions (re-tokenize + re-embed + update ES), and replays the outbox.

Step 3 of the DataTrust review-gate implementation (§7.5 in the design doc).

Two duties:
  Duty A — poll DataTrust for completed batches → apply per-chunk decisions
  Duty B — replay pending outbox records with exponential backoff
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type
from api.db.services.llm_service import LLMBundle
from common import settings
from common.constants import LLMType
from common.misc_utils import thread_pool_exec
from rag.nlp import rag_tokenizer
from rag.nlp.search import index_name
from rag.svr.data_trust_client import (
    ChunkDecision,
    CircuitBreakerOpenError,
    DataTrustRequestError,
    ReviewBatchSummary,
    SubmitBatchPayload,
    get_client,
)
from rag.svr.data_trust_client import ChunkPayload as Cp
from rag.svr.review_outbox import get_outbox

logger = logging.getLogger(__name__)

# ── Environment knobs ────────────────────────────────────────────────────────

POLL_INTERVAL: float = float(os.environ.get("REVIEW_WORKER_POLL_INTERVAL", "30"))
OUTBOX_BATCH: int = int(os.environ.get("REVIEW_WORKER_OUTBOX_BATCH", "50"))


class ReviewResultWorker:
    """Poll DataTrust for completed review batches and apply chunk decisions."""

    def __init__(self, poll_interval: float = POLL_INTERVAL):
        self.poll_interval = poll_interval
        self._client = get_client()
        self._outbox = get_outbox()
        self._shutdown = False
        # Cache embedding models per tenant_id
        self._embed_cache: dict[str, LLMBundle] = {}

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the worker loop until shutdown."""
        logger.info("ReviewResultWorker started (poll_interval=%.1fs)", self.poll_interval)
        while not self._shutdown:
            try:
                await self._duty_a_poll_results()
                await self._duty_b_replay_outbox()
            except Exception:
                logger.exception("Worker loop error — will retry after interval")
            await asyncio.sleep(self.poll_interval)

    def shutdown(self) -> None:
        self._shutdown = True

    # ── Duty A: Poll + apply review results ───────────────────────────────

    async def _duty_a_poll_results(self) -> None:
        """Poll DataTrust for completed batches, apply per-chunk decisions."""
        if not self._client.configured:
            return

        try:
            batches = await self._client.list_completed_batches()
        except (CircuitBreakerOpenError, DataTrustRequestError):
            return

        if not batches:
            return

        logger.info("Found %d completed review batch(es)", len(batches))

        for batch in batches:
            try:
                await self._apply_single_batch(batch)
            except Exception:
                logger.exception("Failed to apply batch %s", batch.batch_id)

    async def _apply_single_batch(self, batch: ReviewBatchSummary) -> None:
        """Fetch batch result and apply per-chunk decisions to ES."""
        batch_id = batch.batch_id
        logger.info("Processing batch %s (%d chunks)", batch_id, batch.chunk_count)

        # 1. Fetch detailed result for this batch
        try:
            result = await self._client.get_batch_result(batch_id)
        except (CircuitBreakerOpenError, DataTrustRequestError) as e:
            logger.warning("Cannot fetch batch %s result: %s", batch_id, e)
            return

        tenant_id = batch.tenant_id
        kb_id = batch.kb_id
        idx = index_name(tenant_id)

        approved = [c for c in result.chunks if c.action == "approved"]
        corrected = [c for c in result.chunks if c.action == "approved_with_changes"]
        rejected = [c for c in result.chunks if c.action == "rejected"]

        # 2. Approved chunks: set available_int=1
        for chunk in approved:
            try:
                settings.docStoreConn.update(
                    {"id": chunk.id}, {"available_int": 1}, idx, kb_id,
                )
            except Exception:
                logger.exception("Failed to activate chunk %s", chunk.id)

        # 3. Corrected chunks: re-tokenize + re-embed + update full doc
        if corrected:
            embed_model = await self._get_or_create_embed_model(tenant_id)
            for chunk in corrected:
                try:
                    await self._apply_corrected_chunk(chunk, tenant_id, kb_id, idx, embed_model)
                except Exception:
                    logger.exception("Failed to apply corrected chunk %s", chunk.id)

        # 4. Rejected chunks: keep available_int=0, log reason
        for chunk in rejected:
            logger.info(
                "Chunk %s rejected%s",
                chunk.id,
                f": {chunk.reason}" if chunk.reason else "",
            )

        # 5. Mark batch as applied
        try:
            await self._client.mark_batch_applied(batch_id)
            logger.info(
                "Batch %s applied: approved=%d corrected=%d rejected=%d",
                batch_id, len(approved), len(corrected), len(rejected),
            )
        except (CircuitBreakerOpenError, DataTrustRequestError) as e:
            logger.warning("Cannot mark batch %s applied: %s — retry next poll", batch_id, e)

    # ── Embedding model cache ─────────────────────────────────────────────

    async def _get_or_create_embed_model(self, tenant_id: str) -> LLMBundle | None:
        """Return a cached embedding model for *tenant_id*, creating if needed."""
        if tenant_id in self._embed_cache:
            return self._embed_cache[tenant_id]

        try:
            cfg = get_tenant_default_model_by_type(tenant_id, LLMType.EMBEDDING)
            model = await thread_pool_exec(LLMBundle, tenant_id, cfg, "English")
            self._embed_cache[tenant_id] = model
            logger.info("Embedding model bound for tenant %s", tenant_id)
            return model
        except Exception:
            logger.exception("Cannot bind embedding model for tenant %s", tenant_id)
            return None

    # ── Corrected chunk helpers ───────────────────────────────────────────

    async def _apply_corrected_chunk(
        self,
        chunk: ChunkDecision,
        tenant_id: str,
        kb_id: str,
        index_nm: str,
        embed_model: LLMBundle | None,
    ) -> None:
        """Re-tokenize corrected content, re-embed, and update ES."""
        if not chunk.corrected_content:
            logger.warning("Chunk %s without corrected_content, activating as-is", chunk.id)
            settings.docStoreConn.update(
                {"id": chunk.id}, {"available_int": 1}, index_nm, kb_id,
            )
            return

        corrected = chunk.corrected_content
        d: dict[str, Any] = {
            "content_with_weight": corrected,
            "content_ltks": rag_tokenizer.tokenize(corrected),
            "content_sm_ltks": rag_tokenizer.fine_grained_tokenize(
                rag_tokenizer.tokenize(corrected)
            ),
            "available_int": 1,
        }

        # Re-embed
        if embed_model:
            try:
                vts, _ = await thread_pool_exec(embed_model.encode, [corrected])
                d["q_1024_vec"] = vts[0].tolist() if hasattr(vts[0], "tolist") else vts[0]
            except Exception:
                logger.exception("Re-embedding failed for chunk %s", chunk.id)

        settings.docStoreConn.update({"id": chunk.id}, d, index_nm, kb_id)
        logger.info("Chunk %s corrected and re-embedded", chunk.id)

    # ── Duty B: Outbox replay ─────────────────────────────────────────────

    async def _duty_b_replay_outbox(self) -> None:
        """Replay pending outbox records (exponential-backoff retry)."""
        if not self._client.configured:
            return

        pending = self._outbox.peek_pending(OUTBOX_BATCH)
        if not pending:
            return

        logger.info("Replaying %d pending outbox record(s)", len(pending))

        for rec in pending:
            rec_id = rec["id"]
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]

            tasks = payload.get("tasks", payload.get("chunks", []))
            submit = SubmitBatchPayload(
                tenant_id=payload["tenant_id"],
                doc_id=payload["doc_id"],
                kb_id=payload["kb_id"],
                object_type=payload.get("object_type", "text_chunk"),
                chunks=[
                    Cp(
                        id=t.get("chunk_id", t.get("id", "")),
                        content_with_weight=t.get("content_with_weight", ""),
                    )
                    for t in tasks
                ],
            )

            try:
                r = await self._client.submit_review_batch(submit)
                self._outbox.mark_submitted(rec_id)
                logger.info("Outbox %d replayed → batch %s", rec_id, r.batch_id)
            except (CircuitBreakerOpenError, DataTrustRequestError) as e:
                self._outbox.mark_failed(rec_id)
                logger.warning("Outbox %d replay failed: %s", rec_id, e)


# ── Singleton & entry point ──────────────────────────────────────────────────

_worker: ReviewResultWorker | None = None


def get_worker() -> ReviewResultWorker:
    global _worker
    if _worker is None:
        _worker = ReviewResultWorker()
    return _worker


async def main() -> None:
    """Run the review result worker (standalone entry point)."""
    worker = get_worker()
    try:
        await worker.run()
    except KeyboardInterrupt:
        logger.info("Worker received shutdown signal")
        worker.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
