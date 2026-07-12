"""Endpoints de autenticación. Solo I/O HTTP: la lógica vive en el controller.

El flujo completo, de arriba abajo:

    register  ──► (código al correo) ──► verify-email ──► TOKEN
                        ▲                                   │
                  resend-code                               │
                                                            ▼
    login ──► TOKEN   (403 si el correo no está verificado: el front salta a verify-email)

    forgot-password ──► (código al correo) ──► reset-password ──► TOKEN

`register` es el único que NO devuelve token: sin correo probado no hay sesión.
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, status

from src.controllers import auth_controller
from src.dependencies.auth import get_current_user
from src.models.auth import (
    CurrentUser,
    LoginRequest,
    MensajeResponse,
    RegisterRequest,
    RegistroResponse,
    ResetPasswordRequest,
    SolicitarCodigoRequest,
    TokenResponse,
    VerificarCorreoRequest,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=RegistroResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crea la cuenta y manda el código al correo (el rol no es negociable desde el cliente)",
)
async def register(payload: RegisterRequest) -> RegistroResponse:
    return await auth_controller.register(payload)


@router.post(
    "/verify-email",
    response_model=TokenResponse,
    summary="Canjea el código de 6 dígitos por el token: acá nace la sesión",
)
async def verify_email(payload: VerificarCorreoRequest) -> TokenResponse:
    return await auth_controller.verify_email(payload)


@router.post(
    "/resend-code",
    response_model=MensajeResponse,
    summary="Reenvía el código de verificación (responde igual exista o no la cuenta)",
)
async def resend_code(payload: SolicitarCodigoRequest) -> MensajeResponse:
    return await auth_controller.resend_code(payload)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Devuelve el JWT con el rol adentro; 403 si el correo no está verificado",
)
async def login(payload: LoginRequest) -> TokenResponse:
    return await auth_controller.login(payload)


@router.post(
    "/forgot-password",
    response_model=MensajeResponse,
    summary="Manda el código para cambiar la contraseña (responde igual exista o no la cuenta)",
)
async def forgot_password(payload: SolicitarCodigoRequest) -> MensajeResponse:
    return await auth_controller.forgot_password(payload)


@router.post(
    "/reset-password",
    response_model=TokenResponse,
    summary="Cambia la contraseña con el código del correo y deja logueado al usuario",
)
async def reset_password(payload: ResetPasswordRequest) -> TokenResponse:
    return await auth_controller.reset_password(payload)


@router.get(
    "/me",
    response_model=CurrentUser,
    summary="El usuario del token — sirve al front para revalidar la sesión guardada",
)
async def me(usuario: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return usuario
