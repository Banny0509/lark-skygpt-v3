
# app/config.py
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    # ----- Lark App -----
    LARK_APP_ID: str = Field(default="", description="Lark app_id")
    LARK_APP_SECRET: str = Field(default="", description="Lark app_secret")
    LARK_VERIFICATION_TOKEN: str = Field(default="", description="Lark verification token (for challenge)")
    LARK_ENCRYPT_KEY: str = Field(default="", description="Lark encrypt key (optional)")

    # ----- OpenAI -----
    OPENAI_API_KEY: str = Field(default="", description="OpenAI API key")
    OPENAI_BASE_URL: str = Field(default="", description="Optional: custom base url")
    OPENAI_CHAT_MODEL: str = Field(default="gpt-4o-mini", description="Default chat model")
    OPENAI_SUMMARY_MODEL: str = Field(default="gpt-4o-mini", description="Model for summaries")
    OPENAI_VISION_MODEL: str = Field(default="gpt-4o-mini", description="Model for image/pdf understanding")

    # ----- Redis / DB -----
    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    DATABASE_URL: str = Field(default="", description="Optional DB (Postgres). Leave empty to disable.")

    # ----- Feature Flags -----
    ENABLE_SUMMARY: bool = Field(default=True)
    MAX_REPLY_TOKENS: int = Field(default=1024)

    # ----- Misc -----
    ENV: str = Field(default="dev")

    class Config:
        env_file = ".env"
        extra = "allow"

settings = Settings()
