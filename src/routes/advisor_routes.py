"""Endpoints del asesor (HU3). Solo I/O HTTP: la lógica vive en el controller.

Todo el router está detrás de `require_role(Rol.ADVISOR)`: un inversionista con un
token válido llamando a cualquiera de estas rutas recibe **403** (test_roles.py).
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends

from src.controllers import advisor_controller
from src.dependencies.auth import require_role
from src.models.advisor import (
    ColaItem,
    PropuestaDetalle,
    RevisionRequest,
    RevisionResultado,
)
from src.models.auth import CurrentUser, Rol

router = APIRouter(
    prefix="/api/advisor",
    tags=["advisor"],
    dependencies=[Depends(require_role(Rol.ADVISOR))],
)


@router.get(
    "/queue",
    response_model=list[ColaItem],
    summary="Cola de propuestas pendientes de revisión",
)
async def get_queue() -> list[ColaItem]:
    return await advisor_controller.listar_cola()


@router.get(
    "/proposals/{proposal_id}",
    response_model=PropuestaDetalle,
    summary="Detalle de una propuesta, con banderas deterministas para el asesor",
)
async def get_proposal(proposal_id: str) -> PropuestaDetalle:
    return await advisor_controller.obtener_detalle(proposal_id)


@router.post(
    "/proposals/{proposal_id}/review",
    response_model=RevisionResultado,
    summary="Aprueba, edita o rechaza una propuesta (queda fecha, versión y responsable)",
)
async def review_proposal(
    proposal_id: str,
    payload: RevisionRequest,
    asesor: CurrentUser = Depends(require_role(Rol.ADVISOR)),
) -> RevisionResultado:
    return await advisor_controller.revisar_propuesta(proposal_id, payload, asesor)
