"""Carga y valida las variables de entorno una sola vez para toda la app."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base de datos: connection string de Postgres (Session pooler de Supabase).
    DATABASE_URL: str

    # IA. Ojo con el modelo: la clave del proyecto tiene cuota 0 en `gemini-2.0-flash`
    # (429 "limit: 0", que no es "te pasaste" sino "este modelo no está habilitado").
    # `gemini-flash-latest` sí responde. Si cambias de clave, verifica el modelo con:
    #   curl ".../v1beta/models/<modelo>:generateContent?key=$GEMINI_API_KEY" -d '{...}'
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-flash-latest"

    # Auth: firma de los JWT. En producción es obligatorio ponerlo en el entorno
    # (Render); si se queda el default, los tokens de todos los despliegues serían
    # falsificables con una llave pública.
    JWT_SECRET: str = "dev-insecure-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720  # 12 h: cubre la demo sin refresh tokens

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
