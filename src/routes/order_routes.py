"""Endpoints de la orden de inversión. Solo I/O HTTP: la lógica vive en el controller.

Quién puede cursar una orden, y por qué está escrito así
--------------------------------------------------------
`POST /invest` exige el rol `investor` sobre un JWT. Eso no es burocracia: es lo que
mantiene en pie la promesa central del producto —«la IA nunca ejecuta»— sin depender de
que nadie se acuerde de cumplirla.

- El **agente** (`/api/agent/chat`) no tiene forma de llamar acá: `agent_graph` devuelve
  texto, no invoca endpoints. Sus rutas B y C ni siquiera pueden escribir en `proposals`.
- El **bot de WhatsApp** tampoco: su webhook es público y se autentica con la firma de
  Twilio, no con un JWT de inversionista. Sin token no hay `require_role(Rol.INVESTOR)`
  que pase. Que mover plata por WhatsApp sea imposible es una propiedad de la
  arquitectura, no una decisión de producto que alguien pueda revertir por descuido.

Lo único que puede cursar una orden es una persona autenticada tocando un botón, sobre una
propuesta que otra persona firmó.
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, status

from src.controllers import orders_controller
from src.dependencies.auth import get_current_user, require_role
from src.models.auth import CurrentUser, Rol
from src.models.orders import CatalogoConvenios, Orden, OrdenFeedItem, ResumenComisiones

# ---------------------------------------------------------------------------
# El inversionista
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/investor", tags=["orders"])


@router.post(
    "/proposals/{proposal_id}/invest",
    response_model=Orden,
    status_code=status.HTTP_201_CREATED,
    summary="«Invertir ahora»: cursa la propuesta firmada como N instrucciones bancarias",
)
async def invest(
    proposal_id: str,
    usuario: CurrentUser = Depends(require_role(Rol.INVESTOR)),
) -> Orden:
    # Solo el dueño, solo una propuesta que un asesor firmó, solo hacia bancos con
    # convenio, y solo una vez. Los cuatro candados están además en la base.
    return await orders_controller.cursar_orden(proposal_id, usuario)


@router.post(
    "/orders/{order_id}/confirm",
    response_model=Orden,
    summary="Acuse del banco: cada línea recibe su referencia (idempotente)",
)
async def confirm_order(
    order_id: str,
    usuario: CurrentUser = Depends(require_role(Rol.INVESTOR)),
) -> Orden:
    # Hoy lo llama la app al terminar la animación de conexión. En una integración real
    # sería un webhook firmado del banco — ver el docstring de `confirmar_orden`.
    return await orders_controller.confirmar_orden(order_id, usuario)


@router.get(
    "/orders/{order_id}",
    response_model=Orden,
    summary="El comprobante de una orden",
)
async def get_order(
    order_id: str,
    usuario: CurrentUser = Depends(get_current_user),
) -> Orden:
    return await orders_controller.obtener_orden(order_id, usuario)


@router.get(
    "/proposals/{proposal_id}/order",
    response_model=Orden | None,
    summary="La orden de una propuesta, o null si todavía no se ha invertido",
)
async def get_order_of_proposal(
    proposal_id: str,
    usuario: CurrentUser = Depends(get_current_user),
) -> Orden | None:
    # `null` y no 404: "todavía no se invirtió" es una respuesta, no un error. Es lo que
    # decide si la pantalla pinta el botón o el comprobante.
    return await orders_controller.orden_de_propuesta(proposal_id, usuario)


# ---------------------------------------------------------------------------
# El convenio, a la vista de cualquiera que tenga cuenta
# ---------------------------------------------------------------------------

catalog_router = APIRouter(prefix="/api/catalog", tags=["orders"])


@catalog_router.get(
    "/convenios",
    response_model=CatalogoConvenios,
    summary="Con qué instituciones hay convenio y cuánto cobra Brokeate por intermediar",
)
async def get_convenios(
    _usuario: CurrentUser = Depends(get_current_user),
) -> CatalogoConvenios:
    return await orders_controller.catalogo_convenios()


# ---------------------------------------------------------------------------
# El asesor
# ---------------------------------------------------------------------------

advisor_router = APIRouter(
    prefix="/api/advisor",
    tags=["orders"],
    dependencies=[Depends(require_role(Rol.ADVISOR))],
)


@advisor_router.get(
    "/orders",
    response_model=list[OrdenFeedItem],
    summary="Quién acaba de invertir: el aviso que dispara la llamada del asesor",
)
async def get_order_feed(limite: int = 50) -> list[OrdenFeedItem]:
    return await orders_controller.listar_feed_ordenes(limite)


@advisor_router.get(
    "/commissions",
    response_model=list[ResumenComisiones],
    summary="Lo intermediado y lo facturado, por asesor",
)
async def get_commissions() -> list[ResumenComisiones]:
    return await orders_controller.resumen_comisiones()
