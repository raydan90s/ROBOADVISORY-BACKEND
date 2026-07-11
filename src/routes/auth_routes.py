"""Endpoints de autenticación. Solo I/O HTTP: la lógica vive en el controller."""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, status

from src.controllers import auth_controller
from src.dependencies.auth import get_current_user
from src.models.auth import CurrentUser, LoginRequest, RegisterRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registra un inversionista (el rol no es negociable desde el cliente)",
)
async def register(payload: RegisterRequest) -> TokenResponse:
    return await auth_controller.register(payload)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Devuelve el JWT con el rol adentro",
)
async def login(payload: LoginRequest) -> TokenResponse:
    return await auth_controller.login(payload)


@router.get(
    "/me",
    response_model=CurrentUser,
    summary="El usuario del token — sirve al front para revalidar la sesión guardada",
)
async def me(usuario: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return usuario
