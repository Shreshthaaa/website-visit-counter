from fastapi import APIRouter

from .endpoints import counter

api_router = APIRouter()

api_router.include_router(counter.router, prefix="/counter", tags=["counter"])
