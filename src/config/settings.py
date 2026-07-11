"""Carga y valida las variables de entorno una sola vez para toda la app."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Supabase
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # IA
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"

    # App
    APP_ENV: str = "development"
    CORS_ORIGINS: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cacheado: el .env se lee una única vez por proceso."""
    return Settings()


settings = get_settings()
