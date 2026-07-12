"""El canal de WhatsApp: un webhook público y tres endpoints autenticados.

El webhook es la ÚNICA ruta de toda la API sin JWT — Twilio no tiene forma de mandar
uno. Lo que ocupa su lugar es la firma HMAC del header `X-Twilio-Signature`, que se
valida antes de tocar la base. Si esa validación se apaga (TWILIO_VALIDAR_FIRMA=false),
cualquiera puede hacer POST fingiendo ser un número ya vinculado y leerá la cartera de
esa persona: es un interruptor de desarrollo, no una opción de despliegue.
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, Header, Request, Response

from src.config.settings import settings
from src.controllers import whatsapp_controller
from src.dependencies.auth import get_current_user
from src.models.auth import CurrentUser
from src.models.whatsapp import LinkCodeResponse, WhatsAppStatus
from src.services import whatsapp

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


@router.post(
    "/webhook",
    include_in_schema=False,  # no es para humanos: lo llama Twilio
    summary="Mensaje entrante de WhatsApp (Twilio)",
)
async def webhook(
    request: Request,
    x_twilio_signature: str = Header(default=""),
) -> Response:
    """Recibe un mensaje y contesta con TwiML.

    Se responde SIEMPRE 200 con un XML, incluso cuando algo falla: un 500 hace que
    Twilio reintente y luego marque el número como caído, y el usuario no ve nada. El
    único caso que corta seco es la firma inválida — ahí sí, 403 y sin cuerpo, porque
    quien manda ese request no es un usuario esperando respuesta.
    """
    form = await request.form()
    # dict crudo: la firma se calcula sobre EXACTAMENTE los campos que Twilio envió.
    # Filtrar o renombrar alguno acá rompería el HMAC de forma silenciosa.
    params = {k: str(v) for k, v in form.items()}

    if settings.TWILIO_VALIDAR_FIRMA and not whatsapp.firma_valida(
        settings.TWILIO_WEBHOOK_URL,
        params,
        x_twilio_signature,
        settings.TWILIO_AUTH_TOKEN,
    ):
        return Response(status_code=403)

    texto = await whatsapp_controller.procesar_mensaje(
        desde=params.get("From", ""), cuerpo=params.get("Body", "")
    )
    return Response(content=whatsapp.twiml(texto), media_type="application/xml")


@router.post(
    "/link-code",
    response_model=LinkCodeResponse,
    summary="Genera el código de un solo uso para vincular WhatsApp",
)
async def link_code(
    usuario: CurrentUser = Depends(get_current_user),
) -> LinkCodeResponse:
    return whatsapp_controller.crear_codigo(usuario)


@router.get(
    "/status",
    response_model=WhatsAppStatus,
    summary="¿Esta cuenta tiene un WhatsApp vinculado?",
)
async def status(
    usuario: CurrentUser = Depends(get_current_user),
) -> WhatsAppStatus:
    return whatsapp_controller.estado(usuario)


@router.delete(
    "/link",
    response_model=WhatsAppStatus,
    summary="Desvincula el WhatsApp de esta cuenta",
)
async def unlink(
    usuario: CurrentUser = Depends(get_current_user),
) -> WhatsAppStatus:
    return whatsapp_controller.desvincular(usuario)
