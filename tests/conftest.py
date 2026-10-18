import os

# Must run before app.* is imported: app.core.config builds settings on import.
os.environ.update(
    {
        "REDIS_NODES": "redis://redis1:7070,redis://redis2:7071",
        "REDIS_PASSWORD": "",
        "FLUSH_INTERVAL_SECONDS": "0.05",
        "CACHE_TTL_SECONDS": "0.2",
        "SHUTDOWN_FLUSH_TIMEOUT_SECONDS": "2.0",
        "LOG_LEVEL": "WARNING",
        "APP_VERSION": "test",
        "GIT_SHA": "testsha",
    }
)

import fakeredis  # noqa: E402
import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402

from app.core.config import Settings  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    return Settings(
        REDIS_NODES="redis://redis1:7070,redis://redis2:7071",
        REDIS_PASSWORD="",
        FLUSH_INTERVAL_SECONDS=0.05,
        CACHE_TTL_SECONDS=0.2,
        CACHE_MAX_ENTRIES=3,
        SHUTDOWN_FLUSH_TIMEOUT_SECONDS=2.0,
    )


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Give each shard URL its own fake server so routing is actually tested."""
    servers = {}

    def _from_url(url, **kwargs):
        server = servers.setdefault(url, fakeredis.FakeServer())
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)

    monkeypatch.setattr("app.core.redis_manager.aioredis.from_url", _from_url)
    return servers
