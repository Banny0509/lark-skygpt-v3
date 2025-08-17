from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # Lark
    APP_ID: str
    APP_SECRET: str
    BOT_NAME: str = "Skygpt"

    # OpenAI
    OPENAI_API_KEY: Optional[str] = None

    # External services
    DATABASE_URL: str
    REDIS_URL: str

    # App
    TIMEZONE: str = "Asia/Taipei"
    LOG_LEVEL: str = "INFO"
    LARK_BASE: str = "https://open.larksuite.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
