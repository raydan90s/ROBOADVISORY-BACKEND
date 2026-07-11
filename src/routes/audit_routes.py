"""Timeline de auditoría (HU3, criterio 3). Lo consume la AuditoriaPage del asesor.

Va en su propio router porque el prefijo es `/api/audit`, no `/api/advisor`, pero el
rol exigido es el mismo: la auditoría no es información del inversionista.
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, Query

from src.controllers import advisor_controller
from src.dependencies.auth import require_role
from src.models.advisor import EventoAuditoria
from src.models.auth import Rol

router = APIRouter(
    prefix="/api/audit",
    tags=["audit"],
    dependencies=[Depends(require_role(Rol.ADVISOR))],
)


@router.get(
    "",
    response_model=list[EventoAuditoria],
    summary="Eventos auditados, del más reciente al más antiguo",
)
async def get_audit(
    limite: int = Query(100, ge=1, le=500),
) -> list[EventoAuditoria]:
    return await advisor_controller.listar_auditoria(limite)
