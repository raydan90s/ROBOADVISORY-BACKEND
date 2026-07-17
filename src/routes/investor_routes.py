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
    AsignacionUpdate,
    CapitalUpdate,
    Investor,
    InvestorProfileCreate,
    PerfilUpdate,
    PortfolioProposal,
    Pregunta,
    ProfilingBreakdown,
    RefutacionRequest,
    RefutacionResultado,
    ResumenCapital,
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
    response_model=ResumenCapital,
    summary="Fija el capital total del inversionista del token",
)
async def set_capital(
    payload: CapitalUpdate,
    usuario: CurrentUser = Depends(require_role(Rol.INVESTOR)),
) -> ResumenCapital:
    # El techo es del usuario del token: nadie fija el capital de otro.
    return await investor_controller.fijar_capital(usuario.id, payload)


@router.put(
    "/proposals/{proposal_id}/allocation",
    response_model=PortfolioProposal,
    summary="El inversionista arma su mezcla: agrega, quita o repondera fondos elegibles",
)
async def edit_allocation(
    proposal_id: str,
    payload: AsignacionUpdate,
    usuario: CurrentUser = Depends(require_role(Rol.INVESTOR)),
) -> PortfolioProposal:
    # Solo el dueño, solo pending_review, solo productos elegibles para su perfil.
    # La propuesta editada sigue esperando la revisión del asesor (HU3 intacta).
    return await investor_controller.editar_asignacion(proposal_id, payload, usuario)


@router.post(
    "/proposals/{proposal_id}/refute",
    response_model=RefutacionResultado,
    summary="El inversionista refuta la decisión firmada: la propuesta vuelve a la cola",
)
async def refute_proposal(
    proposal_id: str,
    payload: RefutacionRequest,
    usuario: CurrentUser = Depends(require_role(Rol.INVESTOR)),
) -> RefutacionResultado:
    # Solo el dueño, solo sobre 'approved'/'edited' y solo si aún no hay una orden
    # cursada. La decisión del asesor no se borra: la refutación queda al lado, en el
    # registro de auditoría, y la propuesta reaparece en la cola del asesor.
    return await investor_controller.refutar_propuesta(proposal_id, payload, usuario)


@router.put(
    "/sessions/{session_id}/profile",
    response_model=ProfilingBreakdown,
    summary="El inversionista corrige sus respuestas: se re-puntúa y vuelve a revisión",
)
async def edit_profile(
    session_id: str,
    payload: PerfilUpdate,
    usuario: CurrentUser = Depends(require_role(Rol.INVESTOR)),
) -> ProfilingBreakdown:
    # Solo el dueño de la sesión. A diferencia de la mezcla, esto SÍ se puede aunque el
    # asesor ya haya decidido: la propuesta se regenera y vuelve a `pending_review`, o
    # sea, a su cola. La decisión anterior no se borra: queda en el registro.
    return await investor_controller.editar_perfil(session_id, payload, usuario)


@router.get(
    "/{investor_id}/subaccounts",
    response_model=ResumenCapital,
    summary="Subcuentas del inversionista y el reparto de su capital",
)
async def get_subaccounts(
    investor_id: str,
    usuario: CurrentUser = Depends(get_current_user),
) -> ResumenCapital:
    exige_dueno_o_asesor(investor_id, usuario)
    return await investor_controller.listar_subcuentas(investor_id)


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
            "La subcuenta a mirar. Sin este parámetro se devuelve la sesión más "
            "reciente, que es lo que pedía la app de una sola cartera."
        ),
    ),
    usuario: CurrentUser = Depends(get_current_user),
) -> PortfolioProposal:
    exige_dueno_o_asesor(investor_id, usuario)
    return await investor_controller.get_portfolio_proposal(investor_id, session_id)


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
