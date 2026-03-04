from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(..., env="DATABASE_URL")

    # Redis
    redis_url: str = Field("redis://localhost:6379/0", env="REDIS_URL")

    # Anthropic
    anthropic_api_key: str = Field(..., env="ANTHROPIC_API_KEY")

    # JWT
    jwt_secret_key: str = Field(..., env="JWT_SECRET_KEY")
    jwt_algorithm: str = Field("HS256", env="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(10080, env="ACCESS_TOKEN_EXPIRE_MINUTES")

    # Google Custom Search
    google_cse_api_key: str = Field("", env="GOOGLE_CSE_API_KEY")
    google_cse_id: str = Field("", env="GOOGLE_CSE_ID")

    # Google OAuth
    google_client_id: str = Field("", env="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field("", env="GOOGLE_CLIENT_SECRET")

    # App
    app_env: str = Field("development", env="APP_ENV")
    # Stored as a plain string to avoid pydantic-settings JSON-decoding it.
    # main.py splits this on commas when building the CORS middleware.
    cors_origins_str: str = Field(
        default="chrome-extension://,http://localhost:3000",
        env="CORS_ORIGINS",
    )
    encryption_key: str = Field("", env="ENCRYPTION_KEY")

    # Cache TTL
    company_research_ttl_seconds: int = 60 * 60 * 48  # 48 hours
    google_search_max_queries: int = 4
    google_search_results_per_query: int = 5


settings = Settings()
