"""Configuración común de los tests.

Los tests de scoring/elegibilidad corren **contra la base real** (la sembrada por
`seed.sql`): validar el motor determinista contra un mock no probaría nada, porque el
motor *es* la base. Si no hay `DATABASE_URL`, se saltan en vez de fallar.
"""

import pytest

from src.config.settings import settings


@pytest.fixture(scope="session", autouse=True)
def _requiere_base() -> None:
    if not settings.DATABASE_URL:
        pytest.skip("Sin DATABASE_URL: los tests contra la base se saltan.", allow_module_level=True)
