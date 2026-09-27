from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads a local .env when present (plan 8.3: run a service outside Docker); real env vars win.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DB_PATH: str = "notification.db"
    REDIS_URL: str
    RABBITMQ_URL: str
    INTERNAL_TOKEN: str
    JWT_PUBLIC_KEY_PATH: str  # WebSockets come straight here (port 8005), not via the gateway, so the JWT is checked here
    LOG_LEVEL: str = "INFO"


settings = Settings()
