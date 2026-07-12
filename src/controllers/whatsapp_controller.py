"""El asistente por WhatsApp: quién escribe, qué se le contesta.

Reusa el MISMO agente que el chat de la app (`agent_graph.responder`) sobre el MISMO
contexto de base (`agent_controller._contexto_agente`). No hay un "cerebro de WhatsApp"
aparte, y eso es deliberado: dos agentes con dos prompts se desincronizan a la primera
semana, y el guardarraíl anti-alucinación tendría que probarse dos veces. Acá solo vive
lo que WhatsApp sí tiene de propio:

  1. **La identidad.** El chat de la app llega con un JWT; WhatsApp llega con un número
     de teléfono, que no prueba nada. La vinculación con código de un solo uso (ver
     migrations/003_whatsapp.sql) es lo que convierte ese número en un `profile_id`.
  2. **La degradación.** En la app un 404 es una pantalla de error; en WhatsApp tiene
     que ser una frase amable. Un usuario sin perfilamiento no puede quedarse mirando
     un mensaje que no llega.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import HTTPException
from psycopg import Connection
from psycopg.errors import UniqueViolation

from src.config.database import get_connection
from src.config.settings import settings
from src.controllers.agent_controller import (
    _cargar_historial,
    _contexto_agente,
    _guardar_turno,
)
from src.models.auth import CurrentUser
from src.models.whatsapp import LinkCodeResponse, WhatsAppStatus
from src.services.agent_graph import responder
from src.services.whatsapp import enmascarar, normalizar_telefono

log = logging.getLogger(__name__)

# Diez minutos: suficiente para ir de la app a WhatsApp, corto para que un código que
# se quedó en una captura de pantalla no siga sirviendo.
CODIGO_TTL_SEGUNDOS = 600

# El código viaja al canal por un mensaje del usuario, así que el prefijo es parte del
# protocolo: sin él, "123456" suelto sería una pregunta cualquiera para el agente.
_PREFIJOS_VINCULAR = ("vincular", "link", "vincula")
_PALABRAS_SALIR = {"salir", "desvincular", "desconectar", "stop", "baja"}
_PALABRAS_AYUDA = {"ayuda", "help", "menu", "menú", "hola", "start", "/start"}


# ===========================================================================
# Los textos del bot (fijos: no los escribe el LLM, no pueden alucinar)
# ===========================================================================

BIENVENIDA = (
    "👋 Soy el asistente de tu Brokeate.\n\n"
    "Para hablar de TU cuenta necesito saber que este número es tuyo. En la app, entra "
    "a *Perfil → Vincular WhatsApp*, copia el código de seis dígitos y escríbeme:\n\n"
    "*VINCULAR 123456*\n\n"
    "(cambiando 123456 por tu código)"
)

AYUDA = (
    "Puedo ayudarte con:\n\n"
    "• *Tu cuenta*: ¿qué inversiones tengo?, ¿cuál es mi perfil?, ¿cuánto tengo sin "
    "asignar?, ¿por qué me asignaron ese producto?\n"
    "• *Dónde invertir*: ¿qué me conviene a 180 días?, ¿cuál da mejor tasa?, "
    "compárame las dos opciones\n"
    "• *Conceptos*: ¿qué es renta fija?, ¿por qué importa la calificación?\n\n"
    "No predigo precios ni ejecuto compras: eso lo aprueba un asesor.\n"
    "Escribe *SALIR* para desvincular este número."
)

VINCULADO_OK = (
    "✅ Listo, {nombre}. Este número quedó vinculado a tu cuenta.\n\n"
    "Pregúntame lo que quieras sobre tus inversiones. Escribe *AYUDA* para ver ejemplos."
)

CODIGO_INVALIDO = (
    "❌ Ese código no es válido o ya expiró (duran diez minutos).\n\n"
    "Genera uno nuevo en la app, en *Perfil → Vincular WhatsApp*, y vuelve a escribirme "
    "*VINCULAR* seguido del código."
)

DESVINCULADO = (
    "🔓 Listo: este número ya no está vinculado a ninguna cuenta y no puedo ver tus "
    "datos. Cuando quieras volver, genera un código nuevo en la app."
)

SIN_PERFILAMIENTO = (
    "Todavía no tienes un perfilamiento completo, así que no tengo una cartera de la "
    "cual hablarte. Entra a la app, responde el cuestionario y genera tu propuesta — "
    "aquí te espero para explicártela."
)

ERROR_INTERNO = (
    "Uy, algo falló de mi lado y prefiero no contestarte con datos a medias. "
    "Intenta de nuevo en un momento."
)


# ===========================================================================
# Vinculación: lo que habla con la APP
# ===========================================================================


def _generar_codigo(conn: Connection, profile_id: str) -> str:
    """Un código de seis dígitos, único entre los que están vivos.

    `secrets`, no `random`: el código autoriza el acceso a una cartera, y el generador
    de Mersenne Twister es predecible si alguien observa suficientes salidas.

    La unicidad la impone el índice `whatsapp_link_codes_vivo` en la base, no un SELECT
    previo — entre el SELECT y el INSERT cabe otro request. Acá simplemente reintentamos
    cuando la base dice que ese código ya está tomado.
    """
    # Los códigos anteriores de este usuario mueren al pedir uno nuevo: si no, "genera
    # otro porque no me llegó" dejaría dos códigos válidos apuntando a la misma cuenta.
    conn.execute(
        "delete from public.whatsapp_link_codes where profile_id = %s and used_at is null",
        (profile_id,),
    )
    # Barrido barato de la basura ajena: códigos que nadie canjeó y ya expiraron. Sin
    # esto, el índice único parcial se iría llenando de códigos muertos y las colisiones
    # subirían con el tiempo.
    conn.execute(
        "delete from public.whatsapp_link_codes where used_at is null and expires_at < now()"
    )

    for _ in range(10):
        codigo = f"{secrets.randbelow(1_000_000):06d}"
        try:
            with conn.transaction():  # savepoint: una colisión no aborta la transacción
                conn.execute(
                    """
                    insert into public.whatsapp_link_codes (profile_id, code, expires_at)
                    values (%s, %s, now() + make_interval(secs => %s))
                    """,
                    (profile_id, codigo, CODIGO_TTL_SEGUNDOS),
                )
            return codigo
        except UniqueViolation:
            continue  # ese código ya está vivo para otro usuario: probamos con otro

    raise HTTPException(
        status_code=503,
        detail="No se pudo generar un código de vinculación. Intenta de nuevo.",
    )


def crear_codigo(usuario: CurrentUser) -> LinkCodeResponse:
    """La app pide un código para que el usuario lo escriba por WhatsApp."""
    with get_connection() as conn:
        codigo = _generar_codigo(conn, usuario.id)

    return LinkCodeResponse(
        code=codigo,
        expira_en_segundos=CODIGO_TTL_SEGUNDOS,
        instruccion=f"VINCULAR {codigo}",
    )


def estado(usuario: CurrentUser) -> WhatsAppStatus:
    """¿Esta cuenta tiene un WhatsApp vinculado? El teléfono vuelve enmascarado."""
    with get_connection() as conn:
        fila = conn.execute(
            """
            select phone_e164, linked_at
            from public.whatsapp_links
            where profile_id = %s and revoked_at is null
            """,
            (usuario.id,),
        ).fetchone()

    if not fila:
        return WhatsAppStatus(vinculado=False)
    return WhatsAppStatus(
        vinculado=True,
        telefono=enmascarar(fila["phone_e164"]),
        linked_at=fila["linked_at"].isoformat(),
    )


def desvincular(usuario: CurrentUser) -> WhatsAppStatus:
    """Corta el acceso desde la app (el equivalente del SALIR por WhatsApp)."""
    with get_connection() as conn:
        conn.execute(
            """
            update public.whatsapp_links set revoked_at = now()
            where profile_id = %s and revoked_at is null
            """,
            (usuario.id,),
        )
    return WhatsAppStatus(vinculado=False)


# ===========================================================================
# Vinculación: lo que habla con TWILIO
# ===========================================================================


def _link_activo(conn: Connection, telefono: str) -> dict[str, Any] | None:
    """El perfil dueño de este número, o None si el número no está vinculado.

    Esta consulta es el control de acceso de todo el canal: si devuelve None, el bot no
    toca ni una fila de datos financieros.
    """
    return conn.execute(
        """
        select l.id as link_id, p.id::text as profile_id, p.full_name, p.role::text as role
        from public.whatsapp_links l
        join public.profiles p on p.id = l.profile_id
        where l.phone_e164 = %s and l.revoked_at is null and p.is_active
        """,
        (telefono,),
    ).fetchone()


def _canjear_codigo(conn: Connection, telefono: str, codigo: str) -> str | None:
    """Canjea el código y vincula el teléfono. Devuelve el nombre, o None si no sirve.

    `for update` sobre el código: dos mensajes con el mismo código llegando a la vez
    (el usuario que toca "enviar" dos veces) tienen que canjearlo UNA sola vez. El
    segundo encuentra `used_at` ya escrito y cae en el mismo camino que un código falso.
    """
    fila = conn.execute(
        """
        select c.id, c.profile_id, p.full_name
        from public.whatsapp_link_codes c
        join public.profiles p on p.id = c.profile_id
        where c.code = %s and c.used_at is null and c.expires_at > now() and p.is_active
        for update of c
        """,
        (codigo,),
    ).fetchone()
    if not fila:
        return None

    conn.execute(
        "update public.whatsapp_link_codes set used_at = now(), used_by_phone = %s where id = %s",
        (telefono, fila["id"]),
    )

    # Revocamos lo viejo antes de insertar lo nuevo: los índices únicos parciales de la
    # migración prohíben dos vínculos activos, ya sea del mismo teléfono (que estaba
    # atado a otra cuenta) o de la misma cuenta (que tenía otro teléfono). Sin esto, el
    # INSERT de abajo violaría el índice — que es exactamente lo que queremos que pase
    # si alguien intenta saltarse este paso.
    conn.execute(
        """
        update public.whatsapp_links set revoked_at = now()
        where revoked_at is null and (phone_e164 = %s or profile_id = %s)
        """,
        (telefono, fila["profile_id"]),
    )
    conn.execute(
        "insert into public.whatsapp_links (profile_id, phone_e164) values (%s, %s)",
        (fila["profile_id"], telefono),
    )
    return fila["full_name"]


def _codigo_del_mensaje(texto: str) -> str | None:
    """Extrae el código de «VINCULAR 123456» (tolerando guiones, espacios y mayúsculas)."""
    palabras = texto.strip().lower().split()
    if not palabras or not palabras[0].startswith(_PREFIJOS_VINCULAR):
        return None
    digitos = "".join(c for c in " ".join(palabras[1:]) if c.isdigit())
    return digitos if len(digitos) == 6 else None


# ===========================================================================
# El turno de conversación
# ===========================================================================


async def _responder_agente(link: dict[str, Any], mensaje: str, telefono: str) -> str:
    """Corre el MISMO agente del chat de la app sobre la cuenta dueña de este teléfono."""
    usuario = CurrentUser(
        id=link["profile_id"], full_name=link["full_name"], role=link["role"]
    )
    # El hilo es el teléfono, no la sesión: en WhatsApp la conversación es continua
    # aunque el usuario tenga varias subcuentas, y no debe mezclarse con el chat de la app.
    hilo = f"wa:{telefono}"

    with get_connection() as conn:
        sesion = conn.execute(
            """
            select id::text as sid
            from public.profiling_sessions
            where investor_id = %s and completed_at is not null
            order by created_at desc
            limit 1
            """,
            (usuario.id,),
        ).fetchone()
        if not sesion:
            return SIN_PERFILAMIENTO

        try:
            contexto, proposal_id = _contexto_agente(conn, sesion["sid"])
        except HTTPException:
            # La sesión existe pero aún no tiene propuesta generada. En la app eso es un
            # 404; acá es una frase.
            return SIN_PERFILAMIENTO

        historial = _cargar_historial(conn, hilo)

    estado_final = await responder(
        contexto, mensaje, historial, provider=settings.WHATSAPP_AI_PROVIDER or None
    )

    with get_connection() as conn:
        _guardar_turno(
            conn,
            sesion["sid"],
            proposal_id,
            mensaje,
            estado_final,
            thread_id=hilo,
            platform="whatsapp",
        )

    log.warning(
        "[whatsapp] %s -> modelo=%s | guardrail=%s | en_alcance=%s",
        enmascarar(telefono),
        estado_final["modelo"],
        estado_final["guardrail_passed"],
        estado_final.get("en_alcance"),
    )
    return estado_final["texto"]


async def procesar_mensaje(desde: str, cuerpo: str) -> str:
    """Un mensaje entrante de WhatsApp → el texto con el que se le contesta.

    Devuelve texto plano SIEMPRE: nunca lanza. Una excepción que suba hasta el webhook
    haría que Twilio no entregue nada y el usuario se quede esperando en silencio, sin
    saber si el bot lo ignoró o se cayó.
    """
    telefono = normalizar_telefono(desde)
    if telefono is None:
        log.warning("[whatsapp] número irreconocible: %r", desde)
        return ERROR_INTERNO

    texto = (cuerpo or "").strip()
    if not texto:
        return AYUDA

    try:
        # 1. ¿Es un intento de vincular? Se atiende ANTES de mirar si ya está vinculado:
        #    así un usuario que cambió de cuenta puede re-vincular el mismo número.
        codigo = _codigo_del_mensaje(texto)
        if codigo:
            with get_connection() as conn:
                nombre = _canjear_codigo(conn, telefono, codigo)
            if nombre is None:
                log.warning("[whatsapp] código rechazado desde %s", enmascarar(telefono))
                return CODIGO_INVALIDO
            log.warning("[whatsapp] vinculado %s", enmascarar(telefono))
            return VINCULADO_OK.format(nombre=nombre.split()[0])

        # 2. ¿Quién es? Sin vínculo, el bot no lee ni un dato de nadie.
        with get_connection() as conn:
            link = _link_activo(conn, telefono)
        if link is None:
            return BIENVENIDA

        # 3. Comandos fijos (no pasan por el LLM: no hay nada que interpretar).
        palabra = texto.lower().strip(" .!¡?¿")
        if palabra in _PALABRAS_SALIR:
            with get_connection() as conn:
                conn.execute(
                    "update public.whatsapp_links set revoked_at = now() where id = %s",
                    (link["link_id"],),
                )
            return DESVINCULADO
        if palabra in _PALABRAS_AYUDA:
            return AYUDA

        # 4. Una pregunta de verdad: al agente.
        with get_connection() as conn:
            conn.execute(
                "update public.whatsapp_links set last_seen_at = now() where id = %s",
                (link["link_id"],),
            )
        return await _responder_agente(link, texto, telefono)

    except Exception:
        log.exception("[whatsapp] error atendiendo a %s", enmascarar(telefono))
        return ERROR_INTERNO
