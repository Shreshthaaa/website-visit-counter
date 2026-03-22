from pydantic_settings import BaseSettings
from pydantic import model_validator
import os

class Settings(BaseSettings):
    # Redis Configuration
    REDIS_NODES: str = ""
    
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0
    
    # Consistent Hashing Configuration
    VIRTUAL_NODES: int = 100
    
    # Batch Processing Configuration
    BATCH_INTERVAL_SECONDS: float = 5.0
    
    # Application Configuration
    DEBUG: bool = True
    API_PREFIX: str = "/api/v1"
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings() 