from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads a local .env when present (plan 8.3: run a service outside Docker); real env vars win.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DB_PATH: str = "fare.db"
    REDIS_URL: str
    RABBITMQ_URL: str
    INTERNAL_TOKEN: str
    MATCHING_URL: str = "http://matching:8002"
    QUOTE_TTL_SECONDS: int = 600  # plan 6.5: a quote "expires in 10 min"
    LOG_LEVEL: str = "INFO"


settings = Settings()
