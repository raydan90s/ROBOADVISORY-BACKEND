"""Correo saliente por SMTP (Gmail).

Sin SDK a propósito: mandar un correo es abrir un socket TLS, autenticarse y escupir un
MIME. `smtplib` de la stdlib hace exactamente eso, y meter una dependencia (o peor, un
servicio con API key) por dos plantillas sería pagar peso de despliegue por nada — el
mismo criterio que en `services/whatsapp.py`.

Este módulo no sabe qué es un usuario ni toca la base: recibe un destinatario y un
código, y lo entrega. Eso lo hace probable sin Postgres y sin FastAPI.

DEGRADACIÓN: si no hay credenciales SMTP, en `development` el código se **imprime en el
log** en vez de enviarse (se puede desarrollar el flujo completo sin tocar Gmail) y en
`production` se levanta `EmailNoConfigurado`. La asimetría es deliberada: en la demo un
código que nadie recibe es una cuenta que nadie puede abrir, y prefiero un 503 ruidoso
a un registro que se cuelga en silencio.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from src.config.settings import settings

log = logging.getLogger(__name__)

MARCA = "Brokeate"


class EmailNoConfigurado(RuntimeError):
    """Faltan SMTP_USER / SMTP_PASSWORD en el entorno."""


class EmailNoEnviado(RuntimeError):
    """El servidor SMTP rechazó el mensaje (credenciales malas, buzón inválido, red)."""


def esta_configurado() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD)


def _remitente() -> str:
    correo = settings.SMTP_FROM_EMAIL or settings.SMTP_USER
    return formataddr((settings.SMTP_FROM_NAME, correo))


def _enviar_sync(destinatario: str, asunto: str, texto: str, html: str) -> None:
    """Bloqueante: abre la conexión, autentica, manda y cierra.

    Nunca se llama directo desde un endpoint — `enviar()` lo saca del event loop.
    """
    mensaje = EmailMessage()
    mensaje["From"] = _remitente()
    mensaje["To"] = destinatario
    mensaje["Subject"] = asunto
    # El cuerpo de texto no es decorativo: es lo que ven los clientes que no renderizan
    # HTML, y lo que evita que Gmail marque el correo como sospechoso por ser solo-HTML.
    mensaje.set_content(texto)
    mensaje.add_alternative(html, subtype="html")

    contexto = ssl.create_default_context()
    try:
        if settings.SMTP_PORT == 465:
            # SSL implícito: el canal va cifrado desde el primer byte.
            with smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, context=contexto, timeout=15
            ) as smtp:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.send_message(mensaje)
        else:
            # STARTTLS (587, el puerto de Gmail): se abre en claro y se sube a TLS.
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
                smtp.starttls(context=contexto)
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.send_message(mensaje)
    except smtplib.SMTPAuthenticationError as exc:
        # El error más probable de todos: Gmail rechaza la contraseña normal de la cuenta.
        # Hay que usar una App Password de 16 caracteres (requiere 2FA activo).
        raise EmailNoEnviado(
            "Gmail rechazó las credenciales SMTP. SMTP_PASSWORD debe ser una "
            "'Contraseña de aplicación' de 16 caracteres, no la contraseña de la cuenta."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailNoEnviado(f"No se pudo entregar el correo: {exc}") from exc


async def enviar(destinatario: str, asunto: str, texto: str, html: str) -> None:
    """Manda el correo sin bloquear el event loop.

    `smtplib` es síncrono y un handshake TLS + login contra Gmail tarda cientos de ms:
    llamarlo directo desde un endpoint `async` congelaría TODA la API mientras dura.
    Por eso va a un hilo.
    """
    if not esta_configurado():
        if settings.APP_ENV == "production":
            raise EmailNoConfigurado(
                "SMTP_USER / SMTP_PASSWORD no están configurados: no se puede enviar correo."
            )
        log.warning(
            "SMTP sin configurar (APP_ENV=%s): el correo a %s NO se envió. "
            "Contenido:\n---\n%s\n---",
            settings.APP_ENV,
            destinatario,
            texto,
        )
        return

    await asyncio.to_thread(_enviar_sync, destinatario, asunto, texto, html)


# ===========================================================================
# Plantillas. Un código, un propósito, y ni un enlace en el que hacer clic:
# el usuario ya está en la app esperándolo, y un correo sin links es un correo
# que no se puede convertir en phishing.
# ===========================================================================


def _html(titulo: str, bajada: str, codigo: str, cierre: str) -> str:
    """Tabla + estilos inline: es lo único que renderiza igual en Gmail y en Outlook."""
    return f"""\
<!DOCTYPE html>
<html lang="es">
  <body style="margin:0;padding:24px;background:#F4F5F7;font-family:Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center">
        <table role="presentation" width="100%" style="max-width:480px;background:#FFFFFF;border-radius:16px;padding:32px;">
          <tr><td>
            <p style="margin:0 0 4px;font-size:13px;font-weight:bold;letter-spacing:1px;color:#1E5C9B;text-transform:uppercase;">{MARCA}</p>
            <h1 style="margin:0 0 12px;font-size:22px;color:#14375E;">{titulo}</h1>
            <p style="margin:0 0 24px;font-size:15px;line-height:1.5;color:#52525B;">{bajada}</p>
            <div style="margin:0 0 24px;padding:18px;background:#F4F5F7;border-radius:12px;text-align:center;">
              <span style="font-size:34px;font-weight:bold;letter-spacing:10px;color:#14375E;">{codigo}</span>
            </div>
            <p style="margin:0 0 8px;font-size:14px;line-height:1.5;color:#52525B;">
              El código vence en {settings.EMAIL_CODE_TTL_MINUTES} minutos y solo se puede usar una vez.
            </p>
            <p style="margin:0;font-size:13px;line-height:1.5;color:#A1A1AA;">{cierre}</p>
          </td></tr>
        </table>
        <p style="max-width:480px;margin:16px auto 0;font-size:12px;line-height:1.5;color:#A1A1AA;text-align:center;">
          {MARCA} no ejecuta órdenes ni maneja tu dinero. Las propuestas son referenciales y las revisa un asesor.
        </p>
      </td></tr>
    </table>
  </body>
</html>"""


async def enviar_codigo_verificacion(destinatario: str, nombre: str, codigo: str) -> None:
    minutos = settings.EMAIL_CODE_TTL_MINUTES
    texto = (
        f"Hola {nombre}:\n\n"
        f"Tu código para verificar tu correo en {MARCA} es: {codigo}\n\n"
        f"Vence en {minutos} minutos. Si no creaste esta cuenta, ignora este mensaje.\n"
    )
    html = _html(
        titulo="Verifica tu correo",
        bajada=f"Hola {nombre}, escribe este código en la app para activar tu cuenta.",
        codigo=codigo,
        cierre="Si no creaste esta cuenta, ignora este mensaje: sin el código, nadie puede activarla.",
    )
    await enviar(destinatario, f"{codigo} es tu código de verificación · {MARCA}", texto, html)


async def enviar_codigo_reset(destinatario: str, nombre: str, codigo: str) -> None:
    minutos = settings.EMAIL_CODE_TTL_MINUTES
    texto = (
        f"Hola {nombre}:\n\n"
        f"Tu código para cambiar la contraseña de {MARCA} es: {codigo}\n\n"
        f"Vence en {minutos} minutos. Si no lo pediste, no hagas nada: "
        f"tu contraseña actual sigue funcionando.\n"
    )
    html = _html(
        titulo="Cambia tu contraseña",
        bajada="Pediste recuperar el acceso. Escribe este código en la app para elegir una contraseña nueva.",
        codigo=codigo,
        cierre="Si no lo pediste, no hagas nada: tu contraseña actual sigue funcionando.",
    )
    await enviar(destinatario, f"{codigo} es tu código de recuperación · {MARCA}", texto, html)
