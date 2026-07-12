"""Esquemas Pydantic de autenticación.

`Rol` espeja el enum `user_role` de schema.sql ('investor' | 'advisor').

`EmailStr` (no `str`) en todo lo que recibe un correo: valida el formato y normaliza el
dominio ANTES de que el controller toque la base, así "asdf" o "juan@" ni siquiera
llegan al INSERT — vuelven como 422 con el campo señalado. Ojo: probar que la cadena
*parece* un correo no prueba que alguien lo lea; de eso se encarga el código de seis
dígitos de `auth_codes`. Requiere el paquete `email-validator`.
"""

from enum import Enum

from pydantic import BaseModel, EmailStr, Field


class Rol(str, Enum):
    """enum user_role"""

    INVESTOR = "investor"
    ADVISOR = "advisor"


# El código de seis dígitos que viaja por correo. El patrón está acá y en el CHECK de la
# tabla: el 422 le ahorra a la base una consulta por cada dedazo.
CODIGO = Field(..., pattern=r"^\d{6}$", description="Código de 6 dígitos enviado por correo")


class RegisterRequest(BaseModel):
    """Body del POST /api/auth/register — solo self-signup de inversionistas."""

    nombre: str = Field(..., min_length=2, max_length=120)
    email: EmailStr = Field(..., max_length=160)
    # 72 es el límite real de bcrypt: más allá de eso los caracteres se ignoran.
    password: str = Field(..., min_length=8, max_length=72)
    cedula_ruc: str | None = None


class LoginRequest(BaseModel):
    """Body del POST /api/auth/login."""

    # Acá NO se usa EmailStr: un correo viejo con formato raro debe poder seguir
    # entrando, y de todos modos un login con formato inválido simplemente no encuentra
    # fila. Rechazarlo con 422 solo le diría al atacante qué cadenas no vale la pena probar.
    email: str = Field(..., min_length=5, max_length=160)
    password: str = Field(..., min_length=1, max_length=72)


class SolicitarCodigoRequest(BaseModel):
    """Body de /resend-code y /forgot-password: solo el correo."""

    email: EmailStr = Field(..., max_length=160)


class VerificarCorreoRequest(BaseModel):
    """Body del POST /api/auth/verify-email — canjea el código por el token."""

    email: EmailStr = Field(..., max_length=160)
    codigo: str = CODIGO


class ResetPasswordRequest(BaseModel):
    """Body del POST /api/auth/reset-password.

    Tener el código ES la autorización: por eso no se pide la contraseña anterior (quien
    la olvidó no la tiene) pero sí se exige el secreto que solo llegó a ese buzón.
    """

    email: EmailStr = Field(..., max_length=160)
    codigo: str = CODIGO
    password: str = Field(..., min_length=8, max_length=72)


class TokenResponse(BaseModel):
    """Lo que el front necesita para rutear: el token y, ya legible, el rol."""

    access_token: str
    token_type: str = "bearer"
    user_id: str
    full_name: str
    email: str | None = None
    role: Rol


class RegistroResponse(BaseModel):
    """La respuesta del registro: NO trae token.

    El token nace en /verify-email. Si el registro lo entregara de una, la verificación
    no bloquearía nada y el correo podría seguir siendo inventado: la cuenta ya estaría
    en uso antes de que el código llegara (o no llegara) a ningún buzón.
    """

    email: str
    # Sirve para que el front sepa que hay un código en el aire y salte a la pantalla de
    # verificación; el mensaje es para mostrarlo tal cual.
    requiere_verificacion: bool = True
    mensaje: str


class MensajeResponse(BaseModel):
    """Respuesta genérica de /resend-code y /forgot-password.

    Dice lo mismo exista o no la cuenta: si dijera "ese correo no está registrado",
    cualquiera podría usar el endpoint para descubrir quién tiene cuenta acá.
    """

    mensaje: str


class CurrentUser(BaseModel):
    """El usuario reconstruido desde el JWT. Es lo que inyecta `get_current_user`."""

    id: str
    full_name: str
    role: Rol
