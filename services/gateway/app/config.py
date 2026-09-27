from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    REDIS_URL: str
    INTERNAL_TOKEN: str
    JWT_PUBLIC_KEY_PATH: str
    IDENTITY_URL: str = "http://identity:8001"
    MATCHING_URL: str = "http://matching:8002"
    TRIP_URL: str = "http://trip:8003"
    FARE_URL: str = "http://fare:8004"
    NOTIFICATION_URL: str = "http://notification:8005"


settings = Settings()
