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
    # Proveedor de IA activo. Se cambia el modelo del asistente SOLO con esta variable:
    # "google" | "openai" | "anthropic". Cada uno lee su propia API key y su modelo de
    # abajo. Si el proveedor elegido no tiene key, el agente cae a la explicación
    # determinista (la demo nunca se rompe). Ver src/services/llm_provider.py.
    AI_PROVIDER: str = "google"
    # Temperatura común a todos los proveedores. Baja: fidelidad a los datos, no creatividad.
    AI_TEMPERATURE: float = 0.2

    # --- Google Gemini ---
    # `gemini-flash-latest` resuelve hoy a `gemini-3.5-flash`, cuyo free tier es de
    # solo 20 requests/día y se agota rápido. `gemini-flash-lite-latest` tiene cuota
    # gratis aparte y responde bien para redactar (verificado 11-jul). Si cambias de
    # clave, revisa la cuota por modelo antes de grabar la demo.
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-flash-lite-latest"

    # --- OpenAI (opcional; requiere `pip install langchain-openai`) ---
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"

    # --- Anthropic (opcional; requiere `pip install langchain-anthropic`) ---
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-haiku-4-5"

    # --- Mercados externos (ticker + agente Rutas B/C) ---
    # Sin key, `market_data.py` sirve directamente las cotizaciones simuladas: el
    # ticker y el chat nunca se rompen por falta de configuración.
    ALPHA_VANTAGE_API_KEY: str = ""

    # --- Feed de noticias (gnews.io: 100 requests/día gratis) ---
    # Sin key, `feed_service.py` sirve las noticias de respaldo: el feed nunca se
    # queda en blanco. Con la caché de 1h por tema, la cuota alcanza de sobra.
    GNEWS_API_KEY: str = ""

    # --- WhatsApp (Twilio) ---
    # El webhook es público (Twilio no manda un JWT), así que lo que autentica el POST
    # es la FIRMA: Twilio firma cada request con el auth token y la manda en el header
    # X-Twilio-Signature. Sin validarla, cualquiera podría hacer POST al webhook
    # haciéndose pasar por un teléfono ya vinculado y leer la cartera de otro.
    TWILIO_AUTH_TOKEN: str = ""
    # La URL EXACTA que Twilio tiene configurada como webhook — entra en la firma, así
    # que si acá dice http y en Twilio https, toda firma se rechaza. Debe ser la misma
    # cadena, carácter por carácter, que aparece en la consola de Twilio.
    TWILIO_WEBHOOK_URL: str = "https://hackaton.api.alata-ec.com/api/whatsapp/webhook"
    # Escape hatch SOLO para probar el webhook con curl/ngrok en local. En producción
    # tiene que quedar en true: es la única puerta del bot.
    TWILIO_VALIDAR_FIRMA: bool = True

    # Proveedor de IA que atiende WhatsApp. Va aparte de AI_PROVIDER a propósito: el
    # front puede seguir demostrando el selector de modelos mientras el bot corre
    # siempre sobre el proveedor con cuota (OpenAI). Vacío = usa AI_PROVIDER.
    WHATSAPP_AI_PROVIDER: str = "openai"

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
