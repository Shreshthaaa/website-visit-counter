from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from the environment, falling back to a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Redis
    REDIS_NODES: str = "redis://redis1:7070,redis://redis2:7071"
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0
    REDIS_MAX_CONNECTIONS: int = 200
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 2.0
    REDIS_CONNECT_TIMEOUT_SECONDS: float = 2.0

    # Consistent hashing
    VIRTUAL_NODES: int = 100

    # Write buffer and read cache. FLUSH_INTERVAL_SECONDS is also how much
    # traffic a crash can lose.
    FLUSH_INTERVAL_SECONDS: float = 30.0
    SHUTDOWN_FLUSH_TIMEOUT_SECONDS: float = 10.0
    CACHE_TTL_SECONDS: float = 5.0
    CACHE_MAX_ENTRIES: int = 10_000

    # Application
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # Baked into the image at build time.
    APP_VERSION: str = "0.0.0-dev"
    GIT_SHA: str = "unknown"

    # Comma-separated. Empty disables CORS entirely.
    CORS_ALLOW_ORIGINS: str = ""

    @property
    def redis_nodes(self) -> list[str]:
        return [node.strip() for node in self.REDIS_NODES.split(",") if node.strip()]

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ALLOW_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
