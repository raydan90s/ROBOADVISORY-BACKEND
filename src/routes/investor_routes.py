"""Endpoints del inversionista. Solo I/O HTTP: la lógica vive en el controller."""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.controllers import investor_controller
from src.dependencies.auth import get_current_user
from src.models.auth import CurrentUser, Rol
from src.models.investor import (
    Investor,
    InvestorProfileCreate,
    PortfolioProposal,
    Pregunta,
    ProfilingBreakdown,
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
    summary="Guarda el perfil del inversionista y calcula su puntaje de riesgo",
)
async def create_profile(payload: InvestorProfileCreate) -> Investor:
    return await investor_controller.create_investor_profile(payload)


@router.get(
    "/{investor_id}/portfolio",
    response_model=PortfolioProposal,
    summary="Devuelve la propuesta de portafolio (la genera la primera vez)",
)
async def get_portfolio(investor_id: str) -> PortfolioProposal:
    return await investor_controller.get_portfolio_proposal(investor_id)


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
    # Un inversionista solo lee su propio desglose; el asesor lee el de cualquiera
    # (revisar propuestas ajenas es literalmente su trabajo).
    if usuario.role is Rol.INVESTOR and usuario.id != investor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo puedes consultar tu propio perfilamiento.",
        )
    return await investor_controller.obtener_breakdown(investor_id, session_id)


@router.get(
    "/{investor_id}",
    response_model=Investor,
    summary="Devuelve el perfil del inversionista",
)
async def get_profile(investor_id: str) -> Investor:
    return await investor_controller.get_investor(investor_id)
