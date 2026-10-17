import asyncio

import pytest

from app.core.redis_manager import RedisManager
from app.services.visit_counter import VisitCounterService

pytestmark = pytest.mark.asyncio


async def build_service(settings):
    manager = RedisManager(settings)
    return manager, VisitCounterService(manager, settings)


async def test_read_includes_unflushed_visits(settings):
    _, service = await build_service(settings)

    for _ in range(3):
        await service.increment_visit("page-a")

    count, _ = await service.get_visit_count("page-a")
    assert count == 3


async def test_served_via_reports_source(settings):
    _, service = await build_service(settings)

    await service.increment_visit("page-a")
    _, first = await service.get_visit_count("page-a")
    assert first.startswith("redis_")

    _, second = await service.get_visit_count("page-a")
    assert second == "in-memory"


async def test_cache_expires(settings):
    _, service = await build_service(settings)

    await service.get_visit_count("page-a")
    await asyncio.sleep(settings.CACHE_TTL_SECONDS + 0.05)
    _, served_via = await service.get_visit_count("page-a")
    assert served_via.startswith("redis_")


async def test_interval_flush_persists_to_redis(settings):
    manager, service = await build_service(settings)
    await service.start()
    try:
        for _ in range(5):
            await service.increment_visit("page-b")
        await asyncio.sleep(settings.FLUSH_INTERVAL_SECONDS * 3)
        assert await manager.get_count("page-b") == 5
    finally:
        await service.aclose()


async def test_shutdown_flushes_buffered_visits(settings):
    manager, service = await build_service(settings)
    await service.start()

    for _ in range(7):
        await service.increment_visit("page-c")
    assert await manager.get_count("page-c") is None  # nothing written yet

    await service.aclose()

    assert await manager.get_count("page-c") == 7


async def test_shutdown_during_interval_flush_loses_nothing(settings):
    manager, service = await build_service(settings)
    original = manager.increment

    async def slow_increment(key, amount=1):
        await asyncio.sleep(0.05)
        return await original(key, amount)

    manager.increment = slow_increment
    for page in ("a", "b", "c", "d"):
        await service.increment_visit(page)

    await service.start()
    # Close while the interval flush is part way through the four writes.
    await asyncio.sleep(settings.FLUSH_INTERVAL_SECONDS + 0.07)
    await service.aclose()

    manager.increment = original
    assert [await manager.get_count(page) for page in "abcd"] == [1, 1, 1, 1]


async def test_failed_flush_returns_visits_to_buffer(settings, monkeypatch):
    manager, service = await build_service(settings)

    async def boom(*args, **kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(manager, "increment", boom)

    await service.increment_visit("page-d")
    await service._flush_all("interval")

    assert service._buffer["page-d"] == 1


async def test_concurrent_increments_are_not_lost(settings):
    manager, service = await build_service(settings)

    await asyncio.gather(*(service.increment_visit("page-e") for _ in range(500)))
    await service._flush_all("interval")

    assert await manager.get_count("page-e") == 500


async def test_increments_during_flush_survive(settings):
    manager, service = await build_service(settings)

    await service.increment_visit("page-f")

    original = manager.increment

    async def slow_increment(key, amount=1):
        await asyncio.sleep(0.05)
        return await original(key, amount)

    manager.increment = slow_increment

    flush = asyncio.create_task(service._flush_all("interval"))
    await asyncio.sleep(0.01)
    await service.increment_visit("page-f")
    await flush

    manager.increment = original
    count, _ = await service.get_visit_count("page-f")
    assert count == 2


async def test_buffered_total_tracks_the_buffer(settings):
    manager, service = await build_service(settings)

    for page in ("a", "b", "a", "c"):
        await service.increment_visit(page)
    assert service._buffered == sum(service._buffer.values()) == 4

    await service._flush_page("a", trigger="read")
    assert service._buffered == sum(service._buffer.values()) == 2

    async def boom(*args, **kwargs):
        raise ConnectionError("redis unavailable")

    original = manager.increment
    manager.increment = boom
    await service._flush_all("interval")
    manager.increment = original

    assert service._buffered == sum(service._buffer.values()) == 2

    await service._flush_all("interval")
    assert service._buffered == sum(service._buffer.values()) == 0


async def test_cache_is_bounded(settings):
    _, service = await build_service(settings)

    for i in range(settings.CACHE_MAX_ENTRIES + 5):
        await service.get_visit_count(f"page-{i}")

    assert len(service._cache) <= settings.CACHE_MAX_ENTRIES


async def test_keys_route_to_their_own_shard(settings):
    manager, _ = await build_service(settings)

    await manager.increment("page-a", 1)
    owner = manager.node_for("page-a")
    other = next(node for node in manager.shards if node != owner)

    assert await manager._clients[owner].get("page-a") == "1"
    assert await manager._clients[other].get("page-a") is None
