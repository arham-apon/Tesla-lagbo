from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads a local .env when present (plan 8.3: run a service outside Docker); real env vars win.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DB_PATH: str = "identity.db"
    REDIS_URL: str
    RABBITMQ_URL: str
    INTERNAL_TOKEN: str
    JWT_PRIVATE_KEY_PATH: str
    JWT_TTL_SECONDS: int = 3600
    TRIP_URL: str = "http://trip:8003"
    LOG_LEVEL: str = "INFO"


settings = Settings()
