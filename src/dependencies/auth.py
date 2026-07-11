"""Dependencias de FastAPI para proteger endpoints.

    @router.get("/queue", dependencies=[Depends(require_role(Rol.ADVISOR))])

`require_role` es lo que hace que un inversionista llamando /api/advisor/* reciba
un 403 (test_roles.py). La comprobación se hace contra el `role` del JWT firmado:
el cliente no puede alterarlo sin romper la firma.

El token es autocontenido: no consultamos la base en cada request. El precio es que
desactivar una cuenta no invalida su token hasta que expire (12 h). Es aceptable
porque no hay refresh tokens en este alcance (ver "Cortes" en docs/PLAN.md).
"""

from collections.abc import Callable

# pyrefly: ignore [missing-import]
from fastapi import Depends, HTTPException, status

# pyrefly: ignore [missing-import]
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.models.auth import CurrentUser, Rol
from src.services.auth_service import decode_token

# auto_error=False: preferimos lanzar nosotros el 401 con un mensaje en español.
_bearer = HTTPBearer(auto_error=False)


def _no_autenticado(detalle: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detalle,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credenciales: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    """Extrae el usuario del `Authorization: Bearer <token>`."""
    if credenciales is None:
        raise _no_autenticado("Falta el token de acceso.")

    payload = decode_token(credenciales.credentials)
    if payload is None:
        raise _no_autenticado("Token inválido o expirado.")

    try:
        return CurrentUser(
            id=str(payload["sub"]),
            full_name=str(payload.get("name", "")),
            role=Rol(payload["role"]),
        )
    except (KeyError, ValueError) as exc:
        # Token bien firmado pero con un payload que no reconocemos (p. ej. un rol
        # que ya no existe). Se trata como no autenticado, no como error 500.
        raise _no_autenticado("Token con contenido inválido.") from exc


def require_role(*roles: Rol) -> Callable[[CurrentUser], CurrentUser]:
    """Dependencia que exige uno de los roles dados. 401 si no hay token, 403 si el rol no basta."""
    permitidos = set(roles)

    def _verificar(usuario: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if usuario.role not in permitidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "No tienes permiso para acceder a este recurso. "
                    f"Se requiere el rol: {', '.join(r.value for r in permitidos)}."
                ),
            )
        return usuario

    return _verificar
