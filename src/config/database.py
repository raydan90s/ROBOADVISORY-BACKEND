"""Cliente único de Supabase (patrón singleton vía lru_cache)."""

from functools import lru_cache

from supabase import Client, create_client

from src.config.settings import settings

# Nombre de la tabla en Supabase. Si la renombras, cámbialo aquí solamente.
INVESTORS_TABLE = "investors"


@lru_cache
def get_supabase() -> Client:
    """Devuelve el cliente de Supabase. Se instancia una sola vez por proceso.

    Úsalo como dependencia de FastAPI o llámalo directo desde los controllers:
        db = get_supabase()
        db.table(INVESTORS_TABLE).select("*").execute()
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        raise RuntimeError(
            "Faltan SUPABASE_URL o SUPABASE_KEY en el .env"
        )
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
