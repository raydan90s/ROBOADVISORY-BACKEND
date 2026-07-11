"""Registro y login. La única puerta por la que se crean credenciales.

Decisión de diseño: `register` fuerza `role='investor'`. Un asesor no se auto-registra
— se siembra desde la base (seed.sql). Si el rol viniera del body, cualquiera podría
crearse una cuenta de asesor y aprobar sus propias propuestas.
"""

# pyrefly: ignore [missing-import]
from fastapi import HTTPException, status

# pyrefly: ignore [missing-import]
from psycopg.errors import UniqueViolation

from src.config.database import fetch_one, get_connection
from src.models.auth import LoginRequest, RegisterRequest, Rol, TokenResponse
from src.services.auth_service import (
    create_access_token,
    hash_password,
    verify_password,
)


def _normalizar_email(email: str) -> str:
    """Sin esto, 'Juan@Demo.ec ' y 'juan@demo.ec' serían dos cuentas distintas."""
    return email.strip().lower()


def _token_de(fila: dict) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(
            profile_id=str(fila["id"]),
            role=fila["role"],
            full_name=fila["full_name"],
        ),
        user_id=str(fila["id"]),
        full_name=fila["full_name"],
        email=fila["email"],
        role=Rol(fila["role"]),
    )


async def register(payload: RegisterRequest) -> TokenResponse:
    """Crea el inversionista con su contraseña hasheada y lo deja logueado."""
    email = _normalizar_email(payload.email)

    try:
        with get_connection() as conn:
            fila = conn.execute(
                """
                insert into public.profiles (role, full_name, email, cedula_ruc, password_hash)
                values ('investor', %s, %s, %s, %s)
                returning id, role, full_name, email
                """,
                (payload.nombre, email, payload.cedula_ruc, hash_password(payload.password)),
            ).fetchone()
    except UniqueViolation as exc:
        # `email` y `cedula_ruc` son unique en profiles.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una cuenta con ese correo o esa cédula/RUC.",
        ) from exc

    return _token_de(fila)


async def login(payload: LoginRequest) -> TokenResponse:
    """Verifica credenciales y emite el JWT con el rol adentro."""
    fila = fetch_one(
        """
        select id, role, full_name, email, password_hash, is_active
        from public.profiles
        where email = %s
        """,
        (_normalizar_email(payload.email),),
    )

    # Mismo error para "no existe" y "contraseña incorrecta": no le decimos a un
    # atacante qué correos están registrados.
    if not fila or not verify_password(payload.password, fila["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Correo o contraseña incorrectos.",
        )

    if not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La cuenta está desactivada.",
        )

    return _token_de(fila)
