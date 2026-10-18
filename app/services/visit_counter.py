from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from ..core.config import Settings
from ..core.metrics import (
    BUFFER_DEPTH,
    BUFFER_FLUSHES,
    BUFFER_PAGES,
    CACHE_ENTRIES,
    READS_TOTAL,
    VISITS_FLUSHED,
    VISITS_LOST,
    VISITS_RECORDED,
)
from ..core.redis_manager import RedisManager

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    count: int
    fetched_at: float


class VisitCounterService:
    """Buffers visits in memory and flushes them to Redis periodically.

    There are no locks because none are needed: buffer updates never cross an
    await. A flush swaps in a fresh buffer before writing, so visits that
    arrive while it runs go into the new one.
    """

    def __init__(self, redis_manager: RedisManager, settings: Settings) -> None:
        self._redis = redis_manager
        self._settings = settings

        self._buffer: dict[str, int] = defaultdict(int)
        # sum(self._buffer.values()), kept up to date so the depth gauge
        # doesn't have to walk the dict on every visit.
        self._buffered = 0
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()

        self._flush_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._flush_task is None:
            self._stopping.clear()
            self._flush_task = asyncio.create_task(self._flush_loop(), name="visit-counter-flush")
            logger.info(
                "flush loop started",
                extra={"extra_fields": {"interval_s": self._settings.FLUSH_INTERVAL_SECONDS}},
            )

    async def aclose(self) -> None:
        """Stop the loop and flush whatever is still buffered."""
        self._stopping.set()

        # Let the loop exit on its own rather than cancelling it. If it is in
        # the middle of a flush, cancelling would drop the snapshot it holds.
        task, self._flush_task = self._flush_task, None
        if task is not None:
            await task

        if not self._buffered:
            logger.info("shutdown flush: nothing buffered")
            return

        try:
            flushed = await asyncio.wait_for(
                self._flush_all("shutdown"),
                timeout=self._settings.SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
            )
            logger.info(
                "shutdown flush complete",
                extra={"extra_fields": {"visits_flushed": flushed}},
            )
        except TimeoutError:
            VISITS_LOST.inc(self._buffered)
            logger.error(
                "shutdown flush timed out; buffered visits lost",
                extra={
                    "extra_fields": {
                        "visits_lost": self._buffered,
                        "timeout_s": self._settings.SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
                    }
                },
            )

    async def increment_visit(self, page_id: str) -> None:
        self._buffer[page_id] += 1
        self._buffered += 1
        VISITS_RECORDED.inc()
        self._publish_gauges()

    async def get_visit_count(self, page_id: str) -> tuple[int, str]:
        """Return (count, served_via), including visits not yet flushed."""
        now = time.monotonic()
        entry = self._cache.get(page_id)

        if entry is not None and (now - entry.fetched_at) < self._settings.CACHE_TTL_SECONDS:
            count = entry.count
            served_via = "in-memory"
            READS_TOTAL.labels(source="memory").inc()
        else:
            await self._flush_page(page_id, trigger="read")
            stored = await self._redis.get_count(page_id)
            count = stored if stored is not None else 0
            served_via = f"redis_{self._redis.shard_port_for(page_id)}"
            self._remember(page_id, count, now)
            READS_TOTAL.labels(source="redis").inc()

        return count + self._buffer.get(page_id, 0), served_via

    async def _flush_loop(self) -> None:
        interval = self._settings.FLUSH_INTERVAL_SECONDS
        while True:
            # Wait on the stop event instead of sleeping so shutdown doesn't
            # have to sit out the rest of the interval.
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                return
            except TimeoutError:
                pass

            try:
                await self._flush_all("interval")
            except Exception:
                logger.exception("scheduled flush failed")

    async def _flush_all(self, trigger: str) -> int:
        if not self._buffer:
            return 0

        pending, self._buffer = self._buffer, defaultdict(int)
        self._buffered = 0

        flushed = 0
        try:
            while pending:
                page_id, count = pending.popitem()
                flushed += await self._write(page_id, count, trigger)
        finally:
            # Only non-empty if we were cancelled part way through (e.g. the
            # shutdown flush timed out). Put the rest back so it is counted.
            for page_id, count in pending.items():
                self._buffer[page_id] += count
                self._buffered += count
            self._publish_gauges()

        return flushed

    async def _flush_page(self, page_id: str, trigger: str) -> int:
        count = self._buffer.pop(page_id, 0)
        if count <= 0:
            return 0
        self._buffered -= count
        flushed = await self._write(page_id, count, trigger)
        self._publish_gauges()
        return flushed

    async def _write(self, page_id: str, count: int, trigger: str) -> int:
        try:
            await self._redis.increment(page_id, count)
        except Exception:
            # Put them back so the next flush retries them.
            self._buffer[page_id] += count
            self._buffered += count
            BUFFER_FLUSHES.labels(trigger=trigger, outcome="error").inc()
            logger.exception(
                "flush failed; visits returned to buffer",
                extra={"extra_fields": {"page_id": page_id, "pending": count}},
            )
            return 0

        BUFFER_FLUSHES.labels(trigger=trigger, outcome="success").inc()
        VISITS_FLUSHED.labels(shard=self._redis.shard_label_for(page_id)).inc(count)
        self._cache.pop(page_id, None)
        return count

    def _remember(self, page_id: str, count: int, at: float) -> None:
        self._cache[page_id] = _CacheEntry(count=count, fetched_at=at)
        self._cache.move_to_end(page_id)
        while len(self._cache) > self._settings.CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)
        CACHE_ENTRIES.set(len(self._cache))

    def _publish_gauges(self) -> None:
        BUFFER_DEPTH.set(self._buffered)
        BUFFER_PAGES.set(len(self._buffer))
