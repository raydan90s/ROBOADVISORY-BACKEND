"""Crear una cuenta usable desde un test, ahora que el registro no entrega el token.

Desde la verificación de correo, `POST /api/auth/register` devuelve 201 pero **sin
`access_token`**: la cuenta nace bloqueada y el token sale de `/verify-email`. Los tests
que solo querían "un inversionista logueado" no tienen buzón, así que leen el código
directamente de la base — que es exactamente lo que hace el usuario, solo que él lo lee
en Gmail.

Esto no debilita nada: el código está en claro en `auth_codes` (igual que en
`whatsapp_link_codes`), y quien corre los tests ya tiene el `DATABASE_URL`.

No se envía ningún correo: con SMTP_USER vacío y APP_ENV=development, el mailer imprime
el código en el log en vez de conectarse a Gmail. Por eso la suite corre sin credenciales.
"""

from __future__ import annotations

import uuid
from typing import Any

# pyrefly: ignore [missing-import]
from fastapi.testclient import TestClient

from src.config.database import fetch_one

# El dominio de los correos desechables de la suite. Vive acá, en un solo lugar, porque
# ya se rompió una vez repetido en cuatro archivos.
#
# NO puede ser `.local` (ni `.test`, `.invalid` o `.localhost`): son dominios especiales
# por RFC y `email-validator` —el que le da los dientes a `EmailStr`— los rechaza, así que
# el registro devolvía 422 y ni empezaban los tests. Uno inexistente pero de forma normal
# sirve igual: `EmailStr` valida el formato, no la entrega (`check_deliverability=False`),
# y la suite no manda correos (ver `_sin_correo_saliente` en conftest).
DOMINIO_DESECHABLE = "zz-tests.brokeate.ec"


def correo_desechable(prefijo: str) -> str:
    """`zz-<prefijo>-<aleatorio>@<dominio>`: el `zz-` marca "esto lo creó un test"."""
    return f"zz-{prefijo}-{uuid.uuid4().hex[:8]}@{DOMINIO_DESECHABLE}"


def codigo_pendiente(email: str, purpose: str = "email_verification") -> str:
    """El código de 6 dígitos que está vivo para ese correo. Falla si no hay ninguno."""
    fila = fetch_one(
        """
        select c.code
          from public.auth_codes c
          join public.profiles p on p.id = c.profile_id
         where p.email = %s and c.purpose = %s and c.used_at is null
        """,
        (email.lower(), purpose),
    )
    assert fila, f"No hay código '{purpose}' pendiente para {email}"
    return fila["code"]


def registrar_verificado(
    cliente: TestClient,
    prefijo: str,
    nombre: str,
    password: str = "demo1234",
) -> dict[str, Any]:
    """Registra un inversionista desechable, verifica su correo y devuelve el TokenResponse.

    El aleatorio del correo evita chocar con las cuentas que dejaron corridas anteriores.
    """
    email = correo_desechable(prefijo)

    r = cliente.post(
        "/api/auth/register",
        json={"nombre": nombre, "email": email, "password": password},
    )
    assert r.status_code == 201, f"No se pudo registrar {email}: {r.text}"

    r = cliente.post(
        "/api/auth/verify-email",
        json={"email": email, "codigo": codigo_pendiente(email)},
    )
    assert r.status_code == 200, f"No se pudo verificar {email}: {r.text}"

    return r.json()


def cabeceras_de(registro: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {registro['access_token']}"}
