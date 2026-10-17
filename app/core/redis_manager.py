from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .config import Settings
from .consistent_hash import ConsistentHash
from .metrics import REDIS_OP_ERRORS, REDIS_OP_LATENCY, REDIS_SHARD_UP

logger = logging.getLogger(__name__)

# Kept under the readiness probe's timeout so a dead shard shows up as
# "degraded" rather than the probe itself timing out.
PING_TIMEOUT_SECONDS = 1.0


def shard_label(node_url: str) -> str:
    """Stable, low-cardinality label for a shard: host:port."""
    parsed = urlparse(node_url)
    if parsed.port:
        return f"{parsed.hostname}:{parsed.port}"
    return str(parsed.hostname)


def shard_port(node_url: str) -> str:
    parsed = urlparse(node_url)
    return str(parsed.port) if parsed.port else "6379"


class RedisManager:
    """Owns one connection pool per Redis shard and routes keys between them.

    Keys are never moved between shards here. Every replica builds one of
    these, so a rebalance on startup would have them all racing each other.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str, aioredis.Redis] = {}
        self.ring = ConsistentHash(virtual_nodes=settings.VIRTUAL_NODES)

        for node_url in settings.redis_nodes:
            self._register(node_url)

        if not self._clients:
            raise ValueError("REDIS_NODES is empty; at least one shard is required")

    def _register(self, node_url: str) -> None:
        if node_url in self._clients:
            return

        self._clients[node_url] = aioredis.from_url(
            node_url,
            password=self._settings.REDIS_PASSWORD or None,
            db=self._settings.REDIS_DB,
            decode_responses=True,
            max_connections=self._settings.REDIS_MAX_CONNECTIONS,
            socket_timeout=self._settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=self._settings.REDIS_CONNECT_TIMEOUT_SECONDS,
            retry=Retry(ExponentialBackoff(base=0.05, cap=0.5), retries=2),
            retry_on_error=[RedisConnectionError, RedisTimeoutError],
            health_check_interval=30,
        )
        self.ring.add_node(node_url)
        logger.info(
            "redis shard registered",
            extra={"extra_fields": {"shard": shard_label(node_url)}},
        )

    def node_for(self, key: str) -> str:
        node = self.ring.get_node(key)
        if node is None:
            raise RuntimeError("no Redis shards available")
        return node

    def shard_label_for(self, key: str) -> str:
        return shard_label(self.node_for(key))

    def shard_port_for(self, key: str) -> str:
        # The API reports served_via as redis_<port>.
        return shard_port(self.node_for(key))

    def client_for(self, key: str) -> aioredis.Redis:
        return self._clients[self.node_for(key)]

    @property
    def shards(self) -> list[str]:
        return list(self._clients)

    async def increment(self, key: str, amount: int = 1) -> int:
        node_url = self.node_for(key)
        label = shard_label(node_url)
        started = time.perf_counter()
        try:
            result = await self._clients[node_url].incrby(key, amount)
        except Exception:
            REDIS_OP_ERRORS.labels(operation="incrby", shard=label).inc()
            raise
        REDIS_OP_LATENCY.labels(operation="incrby", shard=label).observe(
            time.perf_counter() - started
        )
        return int(result)

    async def get_count(self, key: str) -> int | None:
        node_url = self.node_for(key)
        label = shard_label(node_url)
        started = time.perf_counter()
        try:
            value = await self._clients[node_url].get(key)
        except Exception:
            REDIS_OP_ERRORS.labels(operation="get", shard=label).inc()
            raise
        REDIS_OP_LATENCY.labels(operation="get", shard=label).observe(time.perf_counter() - started)
        return int(value) if value is not None else None

    async def ping_all(self) -> dict[str, bool]:
        """Ping every shard concurrently. Used by readiness, never liveness."""
        labels = [shard_label(node_url) for node_url in self._clients]
        results = await asyncio.gather(
            *(
                asyncio.wait_for(client.ping(), timeout=PING_TIMEOUT_SECONDS)
                for client in self._clients.values()
            ),
            return_exceptions=True,
        )

        status: dict[str, bool] = {}
        for label, result in zip(labels, results, strict=True):
            up = not isinstance(result, BaseException)
            status[label] = up
            REDIS_SHARD_UP.labels(shard=label).set(1 if up else 0)
            if not up:
                logger.warning(
                    "redis shard ping failed",
                    extra={"extra_fields": {"shard": label, "error": repr(result)}},
                )
        return status

    async def aclose(self) -> None:
        for node_url, client in self._clients.items():
            try:
                await client.aclose()
            except Exception:
                logger.warning(
                    "error closing redis client",
                    extra={"extra_fields": {"shard": shard_label(node_url)}},
                )
