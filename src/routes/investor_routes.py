"""Endpoints del inversionista. Solo I/O HTTP: la lógica vive en el controller."""

from fastapi import APIRouter, status

from src.controllers import investor_controller
from src.models.investor import Investor, InvestorProfileCreate, PortfolioProposal

router = APIRouter(prefix="/api/investor", tags=["investor"])


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
    summary="Devuelve la propuesta de portafolio generada por el agente de IA",
)
async def get_portfolio(investor_id: str) -> PortfolioProposal:
    return await investor_controller.get_portfolio_proposal(investor_id)


@router.get(
    "/{investor_id}",
    response_model=Investor,
    summary="Devuelve el perfil del inversionista",
)
async def get_profile(investor_id: str) -> Investor:
    return await investor_controller.get_investor(investor_id)
