"""Endpoints del inversionista. Solo I/O HTTP: la lógica vive en el controller.

Salvo `/questions` (el cuestionario es catálogo público, no dato de nadie), todo exige
token. Los datos de un cliente —su perfilamiento y su propuesta— solo los lee él mismo o
un asesor: `exige_dueno_o_asesor`.
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, Query, status

from src.controllers import investor_controller
from src.dependencies.auth import exige_dueno_o_asesor, get_current_user, require_role
from src.models.auth import CurrentUser, Rol
from src.models.investor import (
    CapitalAsignar,
    CapitalResponse,
    Investor,
    InvestorProfileCreate,
    PortfolioProposal,
    Pregunta,
    ProfilingBreakdown,
    Subcuenta,
)

router = APIRouter(prefix="/api/investor", tags=["investor"])


@router.get(
    "/questions",
    response_model=list[Pregunta],
    summary="Cuestionario de perfilamiento (preguntas y opciones válidas)",
)
async def get_questions() -> list[Pregunta]:
    return await investor_controller.listar_preguntas()


@router.post(
    "/profile",
    response_model=Investor,
    status_code=status.HTTP_201_CREATED,
    summary="Perfila al usuario del token: calcula su puntaje y su perfil de riesgo",
)
async def create_profile(
    payload: InvestorProfileCreate,
    usuario: CurrentUser = Depends(require_role(Rol.INVESTOR)),
) -> Investor:
    # El perfilamiento se le adjunta al usuario autenticado. Nadie perfila a nombre de otro.
    return await investor_controller.create_investor_profile(payload, usuario)


@router.post(
    "/capital",
    response_model=CapitalResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Declara el capital total del usuario del token",
)
async def set_capital(
    payload: CapitalAsignar,
    usuario: CurrentUser = Depends(require_role(Rol.INVESTOR)),
) -> CapitalResponse:
    return await investor_controller.declarar_capital(payload, usuario)


@router.get(
    "/{investor_id}/portfolio",
    response_model=PortfolioProposal,
    summary="Devuelve la propuesta de portafolio (la genera la primera vez)",
)
async def get_portfolio(
    investor_id: str,
    session_id: str | None = Query(
        None,
        description=(
            "Subcuenta concreta. Sin este parámetro se devuelve la propuesta de la "
            "última sesión del inversionista."
        ),
    ),
    usuario: CurrentUser = Depends(get_current_user),
) -> PortfolioProposal:
    exige_dueno_o_asesor(investor_id, usuario)
    return await investor_controller.get_portfolio_proposal(investor_id, session_id)


@router.get(
    "/{investor_id}/subaccounts",
    response_model=list[Subcuenta],
    summary="Lista las subcuentas del inversionista",
)
async def get_subaccounts(
    investor_id: str,
    usuario: CurrentUser = Depends(get_current_user),
) -> list[Subcuenta]:
    exige_dueno_o_asesor(investor_id, usuario)
    return await investor_controller.listar_subcuentas(investor_id)


@router.get(
    "/{investor_id}/breakdown",
    response_model=ProfilingBreakdown,
    summary="Desglose respuesta → puntos → umbral (la pantalla 'cómo se calculó')",
)
async def get_breakdown(
    investor_id: str,
    session_id: str | None = Query(
        None,
        description=(
            "Sesión concreta. El asesor pasa la que trae la cola; sin este parámetro "
            "se devuelve la última sesión completada del inversionista."
        ),
    ),
    usuario: CurrentUser = Depends(get_current_user),
) -> ProfilingBreakdown:
    exige_dueno_o_asesor(investor_id, usuario)
    return await investor_controller.obtener_breakdown(investor_id, session_id)


@router.get(
    "/{investor_id}",
    response_model=Investor,
    summary="Devuelve el perfil del inversionista",
)
async def get_profile(
    investor_id: str,
    usuario: CurrentUser = Depends(get_current_user),
) -> Investor:
    exige_dueno_o_asesor(investor_id, usuario)
    return await investor_controller.get_investor(investor_id)
