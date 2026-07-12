"""Registro, verificación del correo, login y recuperación de contraseña.

La única puerta por la que se crean credenciales.

Dos decisiones que ordenan todo el archivo:

1. `register` fuerza `role='investor'`. Un asesor no se auto-registra — se siembra desde
   la base (seed.sql). Si el rol viniera del body, cualquiera podría crearse una cuenta
   de asesor y aprobar sus propias propuestas.

2. **El registro no entrega el token.** La cuenta nace con `email_verified_at = NULL` y
   un código de seis dígitos viaja al buzón; el token sale de `verify_email`. Si el
   registro dejara logueado al usuario de una, el correo podría ser inventado y la
   verificación no bloquearía nada: sería un trámite, no un control. Como efecto
   colateral, el mismo mecanismo resuelve "olvidé mi contraseña" — probar que tienes el
   buzón ES la autorización para cambiarla.

Sobre no filtrar quién tiene cuenta: `forgot_password` y `resend_code` contestan lo
mismo exista o no el correo, y `login` da el mismo error para "no existe" y "contraseña
incorrecta". El único endpoint que sí distingue es `register` (409): tiene que hacerlo,
porque si no, el usuario no sabría por qué su cuenta nunca aparece.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

# pyrefly: ignore [missing-import]
from fastapi import HTTPException, status

# pyrefly: ignore [missing-import]
from psycopg import Connection

# pyrefly: ignore [missing-import]
from psycopg.errors import UniqueViolation

from src.config.database import fetch_one, get_connection
from src.config.settings import settings
from src.models.auth import (
    LoginRequest,
    MensajeResponse,
    RegisterRequest,
    RegistroResponse,
    ResetPasswordRequest,
    Rol,
    SolicitarCodigoRequest,
    TokenResponse,
    VerificarCorreoRequest,
)
from src.services.auth_service import (
    create_access_token,
    hash_password,
    verify_password,
)
from src.services.mailer import (
    EmailNoConfigurado,
    EmailNoEnviado,
    enviar_codigo_reset,
    enviar_codigo_verificacion,
)

log = logging.getLogger(__name__)

VERIFICACION = "email_verification"
RESET = "password_reset"

# Seis dígitos = un millón de combinaciones. Sin tope de intentos un script las agota en
# minutos; con cinco, acertar a ciegas es 5 en 1.000.000 antes de que el código muera.
MAX_INTENTOS = 5

# Lo que ve quien pide un código, exista o no la cuenta. Es la misma frase a propósito.
MENSAJE_GENERICO = (
    "Si ese correo tiene una cuenta, te enviamos un código de 6 dígitos. "
    "Revisa tu bandeja (y el spam)."
)

# El front detecta este 403 para saltar a la pantalla de verificación en vez de mostrar
# un error muerto. La frase es contrato: si cambia acá, cambia en LoginPage.tsx.
CORREO_SIN_VERIFICAR = (
    "Tu correo aún no está verificado. Te enviamos un código nuevo para activarlo."
)


def _normalizar_email(email: str) -> str:
    """Sin esto, 'Juan@Demo.ec ' y 'juan@demo.ec' serían dos cuentas distintas."""
    return email.strip().lower()


def _token_de(fila: dict[str, Any]) -> TokenResponse:
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


# ===========================================================================
# 1. El código: nacer, viajar, morir
# ===========================================================================


def _emitir_codigo(conn: Connection, profile_id: str, purpose: str) -> str:
    """Mata el código vivo anterior y crea uno nuevo. Devuelve el de seis dígitos.

    `secrets`, no `random`: este código activa una cuenta o cambia una contraseña, y el
    Mersenne Twister de `random` es predecible si alguien observa suficientes salidas.

    El reenvío invalida el anterior (`used_at = now()`) porque el índice único
    `auth_codes_vivo` solo tolera un código vivo por (cuenta, propósito). Que sea la base
    la que lo garantice y no un `if` es lo que hace que dos "reenviar" simultáneos no
    dejen dos códigos válidos.
    """
    conn.execute(
        """
        update public.auth_codes
           set used_at = now()
         where profile_id = %s and purpose = %s and used_at is null
        """,
        (profile_id, purpose),
    )

    codigo = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute(
        """
        insert into public.auth_codes (profile_id, purpose, code, expires_at)
        values (%s, %s, %s, now() + make_interval(mins => %s))
        """,
        (profile_id, purpose, codigo, settings.EMAIL_CODE_TTL_MINUTES),
    )
    return codigo


async def _mandar(purpose: str, email: str, nombre: str, codigo: str) -> None:
    """Entrega el código y traduce el fallo del SMTP a un error HTTP entendible."""
    try:
        if purpose == VERIFICACION:
            await enviar_codigo_verificacion(email, nombre, codigo)
        else:
            await enviar_codigo_reset(email, nombre, codigo)
    except EmailNoConfigurado as exc:
        # Solo pasa en producción: en development el mailer imprime el código en el log.
        log.error("Correo sin configurar: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El envío de correos no está configurado. Avisa al equipo.",
        ) from exc
    except EmailNoEnviado as exc:
        log.error("Fallo al enviar el correo a %s: %s", email, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No pudimos enviar el correo. Revisa la dirección e intenta de nuevo.",
        ) from exc


def _fallar(conn: Connection, codigo_http: int, detalle: str) -> HTTPException:
    """Confirma lo escrito y devuelve el error a levantar.

    El `commit` es la parte que importa. `get_connection()` hace ROLLBACK si la excepción
    sale del `with`, así que un `raise` directo después de sumar un intento fallido
    borraría ese +1: el contador nunca subiría y `MAX_INTENTOS` sería decorativo — un
    script podría probar el millón de códigos. Confirmamos ANTES de romper.
    """
    conn.commit()
    return HTTPException(status_code=codigo_http, detail=detalle)


def _consumir_codigo(conn: Connection, profile_id: str, purpose: str, codigo: str) -> None:
    """Valida el código y lo quema. Levanta 400 si no sirve.

    `for update` bloquea la fila: sin eso, dos peticiones simultáneas con el mismo código
    podrían pasar las dos (una carrera que convierte el "un solo uso" en una sugerencia).
    """
    fila = conn.execute(
        """
        select id, code, attempts, expires_at < now() as vencido
          from public.auth_codes
         where profile_id = %s and purpose = %s and used_at is null
           for update
        """,
        (profile_id, purpose),
    ).fetchone()

    if not fila:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No hay ningún código pendiente. Pide uno nuevo.",
        )

    if fila["vencido"]:
        conn.execute(
            "update public.auth_codes set used_at = now() where id = %s", (fila["id"],)
        )
        raise _fallar(conn, status.HTTP_400_BAD_REQUEST, "El código venció. Pide uno nuevo.")

    if fila["attempts"] >= MAX_INTENTOS:
        conn.execute(
            "update public.auth_codes set used_at = now() where id = %s", (fila["id"],)
        )
        raise _fallar(
            conn,
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Demasiados intentos fallidos. Pide un código nuevo.",
        )

    # `compare_digest` y no `!=`: comparar en tiempo constante evita que el tiempo de
    # respuesta le diga a un atacante cuántos dígitos acertó.
    if not secrets.compare_digest(fila["code"], codigo):
        conn.execute(
            "update public.auth_codes set attempts = attempts + 1 where id = %s",
            (fila["id"],),
        )
        restantes = MAX_INTENTOS - fila["attempts"] - 1
        raise _fallar(
            conn,
            status.HTTP_400_BAD_REQUEST,
            f"El código no es correcto. Te quedan {restantes} intentos."
            if restantes > 0
            else "El código no es correcto y se acabaron los intentos. Pide uno nuevo.",
        )

    conn.execute("update public.auth_codes set used_at = now() where id = %s", (fila["id"],))


# ===========================================================================
# 2. Registro y verificación
# ===========================================================================


async def register(payload: RegisterRequest) -> RegistroResponse:
    """Crea el inversionista SIN verificar y le manda el código al correo.

    No devuelve token: la cuenta todavía no sirve para entrar (ver `login`).

    Si el correo ya existe pero nunca se verificó, no se responde 409: se pisan nombre y
    contraseña y se reenvía el código. Esa cuenta no es de nadie —nadie probó tener ese
    buzón— y el 409 dejaría al usuario que abandonó a mitad del registro en un callejón
    sin salida, sin poder registrarse ni recuperar. Sigue siendo seguro: quien no lea el
    correo no la activa.
    """
    email = _normalizar_email(payload.email)
    password_hash = hash_password(payload.password)

    try:
        with get_connection() as conn:
            existente = conn.execute(
                """
                select id, full_name, email_verified_at is not null as verificado
                  from public.profiles
                 where email = %s
                   for update
                """,
                (email,),
            ).fetchone()

            if existente and existente["verificado"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Ya existe una cuenta con ese correo. Inicia sesión.",
                )

            if existente:
                conn.execute(
                    """
                    update public.profiles
                       set full_name = %s, password_hash = %s,
                           cedula_ruc = coalesce(%s, cedula_ruc), updated_at = now()
                     where id = %s
                    """,
                    (payload.nombre, password_hash, payload.cedula_ruc, existente["id"]),
                )
                fila = {"id": existente["id"], "full_name": payload.nombre}
            else:
                fila = conn.execute(
                    """
                    insert into public.profiles
                        (role, full_name, email, cedula_ruc, password_hash)
                    values ('investor', %s, %s, %s, %s)
                    returning id, full_name
                    """,
                    (payload.nombre, email, payload.cedula_ruc, password_hash),
                ).fetchone()

            codigo = _emitir_codigo(conn, str(fila["id"]), VERIFICACION)
    except UniqueViolation as exc:
        # Normalmente es la `cedula_ruc` (el correo ya lo resolvimos arriba), pero también
        # cae acá la carrera de dos registros simultáneos con el mismo correo: el SELECT
        # de arriba no vio nada en ninguno de los dos y el índice único desempata. Por eso
        # el mensaje nombra a los dos campos y no adivina cuál fue.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una cuenta con ese correo o esa cédula/RUC.",
        ) from exc

    # Fuera de la transacción: mandar el correo tarda cientos de ms y no vale la pena
    # tener una conexión del pool tomada mientras Gmail contesta.
    await _mandar(VERIFICACION, email, fila["full_name"], codigo)

    return RegistroResponse(
        email=email,
        mensaje=(
            f"Te enviamos un código de 6 dígitos a {email}. "
            "Escríbelo para activar tu cuenta."
        ),
    )


async def verify_email(payload: VerificarCorreoRequest) -> TokenResponse:
    """Canjea el código por el token: acá es donde la cuenta empieza a existir de verdad."""
    email = _normalizar_email(payload.email)

    with get_connection() as conn:
        fila = conn.execute(
            """
            select id, role, full_name, email, email_verified_at
              from public.profiles
             where email = %s
            """,
            (email,),
        ).fetchone()

        # Sin cuenta no hay código pendiente: el mismo 400 que un código equivocado, para
        # no convertir este endpoint en un detector de correos registrados.
        if not fila:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No hay ningún código pendiente. Pide uno nuevo.",
            )

        # Idempotente: si ya estaba verificado, el código sobra y devolvemos el token.
        # Verificar dos veces no es un error del usuario (doble tap, reintento de red).
        if fila["email_verified_at"] is None:
            _consumir_codigo(conn, str(fila["id"]), VERIFICACION, payload.codigo)
            conn.execute(
                """
                update public.profiles
                   set email_verified_at = now(), updated_at = now()
                 where id = %s
                """,
                (fila["id"],),
            )

    return _token_de(fila)


async def resend_code(payload: SolicitarCodigoRequest) -> MensajeResponse:
    """Reenvía el código de verificación. Contesta lo mismo exista o no la cuenta."""
    email = _normalizar_email(payload.email)

    with get_connection() as conn:
        fila = conn.execute(
            """
            select id, full_name, email_verified_at
              from public.profiles
             where email = %s
            """,
            (email,),
        ).fetchone()

        # Ya verificado o inexistente: no se manda nada, pero se contesta igual.
        if not fila or fila["email_verified_at"] is not None:
            return MensajeResponse(mensaje=MENSAJE_GENERICO)

        codigo = _emitir_codigo(conn, str(fila["id"]), VERIFICACION)

    await _mandar(VERIFICACION, email, fila["full_name"], codigo)
    return MensajeResponse(mensaje=MENSAJE_GENERICO)


# ===========================================================================
# 3. Login
# ===========================================================================


async def login(payload: LoginRequest) -> TokenResponse:
    """Verifica credenciales y emite el JWT con el rol adentro."""
    fila = fetch_one(
        """
        select id, role, full_name, email, password_hash, is_active, email_verified_at
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

    # El gate. Acá la verificación deja de ser un trámite: sin correo probado no se entra,
    # por más que la contraseña sea correcta. Se reenvía el código en el mismo golpe para
    # que el usuario no tenga que buscar dónde pedirlo (el front salta a /verify-email).
    if fila["email_verified_at"] is None:
        with get_connection() as conn:
            codigo = _emitir_codigo(conn, str(fila["id"]), VERIFICACION)
        await _mandar(VERIFICACION, fila["email"], fila["full_name"], codigo)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=CORREO_SIN_VERIFICAR,
        )

    return _token_de(fila)


# ===========================================================================
# 4. Olvidé mi contraseña
# ===========================================================================


async def forgot_password(payload: SolicitarCodigoRequest) -> MensajeResponse:
    """Manda el código de reseteo. Contesta lo mismo exista o no la cuenta.

    Este endpoint es público y sin token: si dijera "ese correo no existe", sería un
    oráculo gratis para enumerar quién tiene cuenta en el banco.
    """
    email = _normalizar_email(payload.email)

    with get_connection() as conn:
        fila = conn.execute(
            "select id, full_name from public.profiles where email = %s and is_active",
            (email,),
        ).fetchone()

        if not fila:
            return MensajeResponse(mensaje=MENSAJE_GENERICO)

        codigo = _emitir_codigo(conn, str(fila["id"]), RESET)

    await _mandar(RESET, email, fila["full_name"], codigo)
    return MensajeResponse(mensaje=MENSAJE_GENERICO)


async def reset_password(payload: ResetPasswordRequest) -> TokenResponse:
    """Cambia la contraseña con el código del correo y deja al usuario logueado.

    No se pide la contraseña anterior: quien la olvidó no la tiene. Lo que autoriza el
    cambio es haber leído el buzón — el mismo hecho que la cuenta usó para nacer.

    Efecto colateral querido: resetear la contraseña **verifica el correo**. Si el código
    llegó y volvió, ese buzón existe; no tiene sentido pedirle al usuario que lo pruebe
    dos veces.
    """
    email = _normalizar_email(payload.email)

    with get_connection() as conn:
        fila = conn.execute(
            """
            select id, role, full_name, email
              from public.profiles
             where email = %s and is_active
            """,
            (email,),
        ).fetchone()

        if not fila:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No hay ningún código pendiente. Pide uno nuevo.",
            )

        _consumir_codigo(conn, str(fila["id"]), RESET, payload.codigo)

        conn.execute(
            """
            update public.profiles
               set password_hash = %s,
                   email_verified_at = coalesce(email_verified_at, now()),
                   updated_at = now()
             where id = %s
            """,
            (hash_password(payload.password), fila["id"]),
        )

    return _token_de(fila)
