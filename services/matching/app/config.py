from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads a local .env when present (plan 8.3: run a service outside Docker); real env vars win.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DB_PATH: str = "matching.db"
    REDIS_URL: str
    RABBITMQ_URL: str
    INTERNAL_TOKEN: str
    POOL_MAX_DETOUR_PCT: int = 140
    MATCH_RADIUS_M: int = 3000
    LOG_LEVEL: str = "INFO"


settings = Settings()
