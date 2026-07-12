"""Esquemas del canal de WhatsApp.

Solo modelan lo que habla con la APP (pedir un código, consultar el estado, desvincular).
El webhook de Twilio no tiene esquema Pydantic: llega como `application/x-www-form-urlencoded`
y hay que leer el form crudo para poder verificar la firma sobre EXACTAMENTE los campos
que Twilio envió — un modelo que descarte campos desconocidos rompería el HMAC.
"""

from pydantic import BaseModel


class LinkCodeResponse(BaseModel):
    """El código que la app le muestra al usuario para vincular su WhatsApp."""

    code: str
    expira_en_segundos: int
    # El mensaje ya armado, listo para un botón "Abrir WhatsApp" o para copiar.
    instruccion: str


class WhatsAppStatus(BaseModel):
    """Si esta cuenta ya tiene un WhatsApp vinculado. El teléfono va enmascarado."""

    vinculado: bool
    telefono: str | None = None
    linked_at: str | None = None
