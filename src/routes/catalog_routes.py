"""Catálogo de tasas para el comparador y el simulador del inversionista."""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, Query

from src.controllers import catalog_controller
from src.dependencies.auth import get_current_user
from src.models.auth import CurrentUser
from src.models.catalog import CatalogoTasas

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get(
    "/rates",
    response_model=CatalogoTasas,
    summary="Tasas referenciales del catálogo, con la elegibilidad del perfil del usuario",
)
async def get_rates(
    monto: float | None = Query(
        None, gt=0, description="Si viene, Postgres calcula interés y monto final por producto."
    ),
    plazo_dias: int | None = Query(
        None, gt=0, description="Filtra los depósitos a ese plazo (los fondos no tienen plazo y siempre salen)."
    ),
    usuario: CurrentUser = Depends(get_current_user),
) -> CatalogoTasas:
    # Los no elegibles NO se filtran: van marcados con su motivo. Enseñar la regla
    # trabajando vale más que esconder la fila (criterio HU2 de la Guía).
    return await catalog_controller.listar_tasas(usuario.id, monto, plazo_dias)
