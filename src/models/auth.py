"""Esquemas Pydantic de autenticación.

`Rol` espeja el enum `user_role` de schema.sql ('investor' | 'advisor').
"""

from enum import Enum

from pydantic import BaseModel, Field


class Rol(str, Enum):
    """enum user_role"""

    INVESTOR = "investor"
    ADVISOR = "advisor"


class RegisterRequest(BaseModel):
    """Body del POST /api/auth/register — solo self-signup de inversionistas."""

    nombre: str = Field(..., min_length=2, max_length=120)
    email: str = Field(..., min_length=5, max_length=160)
    # 72 es el límite real de bcrypt: más allá de eso los caracteres se ignoran.
    password: str = Field(..., min_length=8, max_length=72)
    cedula_ruc: str | None = None


class LoginRequest(BaseModel):
    """Body del POST /api/auth/login."""

    email: str = Field(..., min_length=5, max_length=160)
    password: str = Field(..., min_length=1, max_length=72)


class TokenResponse(BaseModel):
    """Lo que el front necesita para rutear: el token y, ya legible, el rol."""

    access_token: str
    token_type: str = "bearer"
    user_id: str
    full_name: str
    email: str | None = None
    role: Rol


class CurrentUser(BaseModel):
    """El usuario reconstruido desde el JWT. Es lo que inyecta `get_current_user`."""

    id: str
    full_name: str
    role: Rol
