"""La capa de transporte de WhatsApp: firma, formato de salida y teléfonos.

Todo lo de acá es mecánico y sin estado — se puede probar sin base y sin red, que es
justo lo que se quiere de la única puerta pública de la API. La conversación en sí
(quién pregunta, qué contesta) vive en `controllers/whatsapp_controller.py`.

No se usa el SDK de Twilio a propósito: para *contestar* un webhook basta devolver
TwiML (un XML de tres líneas), y para *verificar* que el POST viene de Twilio basta un
HMAC. Meter una dependencia para eso sería pagar peso de despliegue por nada.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from xml.sax.saxutils import escape

# WhatsApp corta los mensajes largos. Twilio acepta hasta 1600 caracteres por
# <Message>; partimos nosotros para elegir DÓNDE se corta (en un salto de línea, no a
# mitad de una cifra), y así una viñeta nunca queda mutilada entre dos globos.
LIMITE_MENSAJE = 1500


# ===========================================================================
# 1. ¿Este POST lo mandó Twilio?
# ===========================================================================


def firma_esperada(url: str, params: dict[str, str], auth_token: str) -> str:
    """La firma que Twilio habría calculado para este request.

    El algoritmo es de ellos y no admite interpretación: a la URL del webhook se le
    concatenan los parámetros del POST ORDENADOS POR NOMBRE, cada uno como
    `clave` + `valor` sin separador; a esa cadena se le saca un HMAC-SHA1 con el auth
    token y se codifica en base64.
    """
    firmable = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    mac = hmac.new(auth_token.encode(), firmable.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()


def firma_valida(url: str, params: dict[str, str], firma: str, auth_token: str) -> bool:
    """True si `firma` (header X-Twilio-Signature) corresponde a este request.

    La comparación es `compare_digest`, no `==`: comparar firmas byte a byte con corte
    temprano filtra información sobre la firma correcta a quien mida los tiempos.
    """
    if not auth_token or not firma:
        return False
    return hmac.compare_digest(firma_esperada(url, params, auth_token), firma)


# ===========================================================================
# 2. Teléfonos
# ===========================================================================

# Twilio manda "whatsapp:+593999999999". La base guarda "+593999999999": el canal es un
# detalle del transporte y no tiene por qué contaminar la identidad del usuario.
_PREFIJO = re.compile(r"^whatsapp:", re.IGNORECASE)
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalizar_telefono(crudo: str) -> str | None:
    """'whatsapp:+593 99 999 9999' → '+593999999999'. None si no es un E.164 creíble."""
    sin_canal = _PREFIJO.sub("", (crudo or "").strip())
    compacto = re.sub(r"[\s\-().]", "", sin_canal)
    return compacto if _E164.match(compacto) else None


def enmascarar(telefono: str) -> str:
    """'+593999999999' → '+593•••9999'. Para los logs: un teléfono es dato personal."""
    return f"{telefono[:4]}•••{telefono[-4:]}" if len(telefono) > 8 else "•••"


# ===========================================================================
# 3. La respuesta: TwiML
# ===========================================================================


def _partir(texto: str, limite: int = LIMITE_MENSAJE) -> list[str]:
    """Parte el texto en globos, cortando en saltos de línea antes que a media palabra.

    El agente responde listas con viñetas «• » (así lo pide su prompt). Cortar a ciegas
    cada 1500 caracteres partiría una viñeta en dos mensajes y el usuario leería medio
    producto y medio monto — exactamente el tipo de confusión que el guardarraíl trata
    de evitar aguas arriba.
    """
    texto = texto.strip()
    if len(texto) <= limite:
        return [texto]

    partes: list[str] = []
    resto = texto
    while len(resto) > limite:
        ventana = resto[:limite]
        # Mejor punto de corte: fin de párrafo > fin de línea > espacio > el límite seco.
        corte = max(ventana.rfind("\n\n"), ventana.rfind("\n"), ventana.rfind(" "))
        if corte <= 0:
            corte = limite
        partes.append(resto[:corte].strip())
        resto = resto[corte:].strip()

    if resto:
        partes.append(resto)
    return partes


def twiml(texto: str) -> str:
    """El XML que Twilio espera como respuesta al webhook.

    `escape` no es decorativo: el texto lo escribió un LLM y puede traer un `&` o un
    `<`. Sin escapar, un ampersand rompe el XML y Twilio no entrega NADA — el usuario
    ve silencio y no hay error en ningún lado.
    """
    globos = "".join(f"<Message>{escape(parte)}</Message>" for parte in _partir(texto))
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{globos}</Response>'
