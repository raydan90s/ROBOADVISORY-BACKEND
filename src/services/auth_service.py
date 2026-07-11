"""Primitivas de autenticación: hashing de contraseñas (bcrypt) y JWT.

Este módulo no toca la base ni HTTP: solo hashea, firma y verifica. Así se puede
testear sin levantar Postgres ni FastAPI.

El `role` viaja DENTRO del token firmado, no en un header que el cliente pueda
inventar. Es lo que permite que `require_role('advisor')` sea confiable.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

# pyrefly: ignore [missing-import]
import bcrypt

# pyrefly: ignore [missing-import]
import jwt

# pyrefly: ignore [missing-import]
from jwt import InvalidTokenError

from src.config.settings import settings

# bcrypt trunca silenciosamente en 72 bytes; truncamos nosotros para que el
# comportamiento sea explícito y no dependa de la versión de la librería.
_BCRYPT_MAX_BYTES = 72


def _to_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Hash bcrypt con salt aleatorio. Formato `$2b$12$...`, igual al del seed."""
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Compara en tiempo constante. Un usuario sin password_hash nunca puede entrar."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_to_bytes(password), password_hash.encode("utf-8"))
    except ValueError:
        # El hash guardado no tiene formato bcrypt (dato corrupto): no es un login válido.
        return False


def create_access_token(profile_id: str, role: str, full_name: str) -> str:
    """JWT de acceso. `sub` = profiles.id, `role` = investor | advisor."""
    ahora = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(profile_id),
        "role": role,
        "name": full_name,
        "iat": ahora,
        "exp": ahora + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """Devuelve el payload si la firma y el `exp` son válidos; None si no."""
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except InvalidTokenError:
        # Cubre firma inválida, token expirado y payload malformado.
        return None
