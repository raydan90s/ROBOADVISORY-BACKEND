"""Pool de conexiones a Postgres (Supabase) vía psycopg 3.

Nos conectamos por Postgres directo, no por la API REST de Supabase: entramos
como el rol `postgres`, que bypassea el RLS lock-down definido en schema.sql.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

# pyrefly: ignore [missing-import]
from psycopg import Connection
# pyrefly: ignore [missing-import]
from psycopg.rows import dict_row
# pyrefly: ignore [missing-import]
from psycopg_pool import ConnectionPool

from src.config.settings import settings


@lru_cache
def get_pool() -> ConnectionPool:
    """Pool único por proceso. Se abre perezosamente en el primer uso."""
    return ConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},  # cada fila llega como dict
        open=True,
    )


@contextmanager
def get_connection() -> Iterator[Connection]:
    """Presta una conexión del pool y hace commit/rollback automático.

        with get_connection() as conn:
            conn.execute("insert into ...")
    """
    with get_pool().connection() as conn:
        yield conn


def fetch_all(sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Ejecuta un SELECT y devuelve todas las filas como dicts."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Ejecuta un SELECT y devuelve la primera fila, o None si no hay ninguna."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Ejecuta INSERT/UPDATE/DELETE. Si la sentencia lleva RETURNING, devuelve esa fila."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:  # la sentencia no devuelve filas
            return None
        return cur.fetchone()
