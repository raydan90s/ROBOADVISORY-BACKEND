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


@pytest.fixture(scope="session", autouse=True)
def _sin_correo_saliente() -> None:
    """La suite no manda correos, aunque el `.env` tenga SMTP configurado.

    `ayudas_auth.registrar_verificado` crea una cuenta desechable por test, y cada alta
    dispara el código de verificación. Con `SMTP_USER`/`SMTP_PASSWORD` llenos —que es como
    está el `.env` de desarrollo— eso significa una conexión real a Gmail por test, hacia
    buzones que no existen: lento, ruidoso, y dependiente de que la red y la cuenta estén
    vivas para que pase un test que no tiene nada que ver con el correo.

    Vaciar `SMTP_USER` activa la degradación que `mailer.esta_configurado()` ya define: en
    `development` el código se imprime en el log en vez de enviarse. Los tests lo leen de
    `auth_codes`, así que el flujo se prueba igual — sin tocar Gmail.
    """
    settings.SMTP_USER = ""
    settings.SMTP_PASSWORD = ""
