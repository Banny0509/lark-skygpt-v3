from __future__ import annotations
from pydantic import BaseSettings, Field

class Settings(BaseSettings):
    # Lark
    APP_ID: str = Field("", env="APP_ID")
    APP_SECRET: str = Field("", env="APP_SECRET")
    VERIFICATION_TOKEN: str = Field("", env="VERIFICATION_TOKEN")
    BOT_NAME: str = Field("Skygpt", env="BOT_NAME")
    LARK_BASE: str = Field("https://open.larksuite.com", env="LARK_BASE")  # 中國區改 https://open.feishu.cn

    # DB / Cache
    DATABASE_URL: str = Field(..., env="DATABASE_URL")
    REDIS_URL: str = Field("", env="REDIS_URL")

    # OpenAI
    OPENAI_API_KEY: str = Field("", env="OPENAI_API_KEY")
    OPENAI_MODEL: str = Field("gpt-4o-mini", env="OPENAI_MODEL")

    # App
    REQUIRE_MENTION: bool = Field(False, env="REQUIRE_MENTION")
    TIMEZONE: str = Field("Asia/Taipei", env="TIMEZONE")
    LOG_LEVEL: str = Field("INFO", env="LOG_LEVEL")

    class Config:
        case_sensitive = True

settings = Settings()

    LOG_LEVEL: str = "INFO"
    LARK_BASE: str = "https://open.larksuite.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
