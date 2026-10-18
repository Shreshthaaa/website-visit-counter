from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .api.v1.api import api_router
from .core.config import settings
from .core.logging import configure_logging, request_id_ctx
from .core.metrics import BUILD_INFO, HTTP_DURATION, HTTP_REQUESTS
from .core.redis_manager import RedisManager
from .services.visit_counter import VisitCounterService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.LOG_LEVEL)
    BUILD_INFO.labels(version=settings.APP_VERSION, git_sha=settings.GIT_SHA).set(1)

    redis_manager = RedisManager(settings)
    service = VisitCounterService(redis_manager, settings)
    await service.start()

    app.state.redis = redis_manager
    app.state.service = service
    app.state.ready = True

    logger.info(
        "startup complete",
        extra={
            "extra_fields": {
                "version": settings.APP_VERSION,
                "git_sha": settings.GIT_SHA,
                "shards": redis_manager.shards,
                "flush_interval_s": settings.FLUSH_INTERVAL_SECONDS,
            }
        },
    )

    try:
        yield
    finally:
        app.state.ready = False
        logger.info("shutting down")

        await service.aclose()
        await redis_manager.aclose()
        logger.info("shutdown complete")


app = FastAPI(
    title="Visit Counter Service",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


def _route_template(request: Request) -> str:
    """The matched route pattern, never the concrete path.

    page_id comes from the caller, so labelling by the real path would let
    anyone create unlimited series. Rebuilt from the matched path params
    because FastAPI's route objects change shape between versions.
    """
    if request.scope.get("route") is None:
        return "unmatched"

    params = request.path_params or {}
    if params:
        path = request.url.path
        for name, value in params.items():
            path = path.replace(str(value), "{" + name + "}", 1)
        return path

    for attr in ("path_format", "path"):
        value = getattr(request.scope["route"], attr, None)
        if isinstance(value, str) and value:
            return value

    return request.url.path


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Tag logs and the response with a request id, and write the access log."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = request_id_ctx.set(request_id)
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "unhandled error",
            extra={"extra_fields": {"path": request.url.path, "method": request.method}},
        )
        request_id_ctx.reset(token)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "request_id": request_id},
            headers={"x-request-id": request_id},
        )

    duration = time.perf_counter() - started
    response.headers["x-request-id"] = request_id

    route = _route_template(request)
    HTTP_REQUESTS.labels(method=request.method, route=route, status=str(response.status_code)).inc()
    HTTP_DURATION.labels(method=request.method, route=route).observe(duration)

    if request.url.path not in ("/metrics", "/healthz"):
        logger.info(
            "request",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                }
            },
        )

    request_id_ctx.reset(token)
    return response


@app.get("/", tags=["meta"])
async def root():
    return {"status": "healthy"}


@app.get("/healthz", tags=["meta"])
async def healthz():
    """Liveness. Doesn't touch Redis on purpose: a Redis outage shouldn't get
    every replica restarted."""
    return {"status": "alive"}


@app.get("/readyz", tags=["meta"])
async def readyz(request: Request):
    """Readiness. Fails only when no shard is reachable; with some shards up
    the pod can still serve their keys, so it reports "degraded"."""
    if not getattr(request.app.state, "ready", False):
        return JSONResponse(status_code=503, content={"status": "shutting-down"})

    shards = await request.app.state.redis.ping_all()
    if not any(shards.values()):
        return JSONResponse(
            status_code=503,
            content={"status": "no-redis-shards-reachable", "shards": shards},
        )

    status = "ready" if all(shards.values()) else "degraded"
    return {"status": status, "shards": shards}


@app.get("/version", tags=["meta"])
async def version():
    return {"version": settings.APP_VERSION, "git_sha": settings.GIT_SHA}


@app.get("/metrics", tags=["meta"])
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(api_router, prefix=settings.API_PREFIX)
