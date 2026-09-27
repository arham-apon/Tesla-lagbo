from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads a local .env when present (plan 8.3: run a service outside Docker); real env vars win.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DB_PATH: str = "trip.db"
    RABBITMQ_URL: str
    INTERNAL_TOKEN: str
    FARE_URL: str = "http://fare:8004"
    MATCHING_URL: str = "http://matching:8002"
    RIDE_REQUEST_TTL_SECONDS: int = 180
    LOG_LEVEL: str = "INFO"


settings = Settings()
