"""Endpoint del agente conversacional. Solo I/O HTTP: la lógica vive en el controller.

Autenticado: el agente habla sobre los datos de UN inversionista, así que hace falta
saber quién pregunta. El inversionista conversa sobre lo suyo; el asesor, sobre lo de
cualquiera (la comprobación fina, contra el dueño de la sesión, la hace el controller).
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends

from src.controllers import agent_controller
from src.dependencies.auth import get_current_user
from src.models.agent import (
    AgentChatRequest,
    AgentChatResponse,
    ProviderInfo,
    SimuladorRequest,
    SimuladorResponse,
)
from src.models.auth import CurrentUser

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get(
    "/providers",
    response_model=list[ProviderInfo],
    summary="Proveedores de IA disponibles (para el selector del front)",
)
async def get_providers(
    _usuario: CurrentUser = Depends(get_current_user),
) -> list[ProviderInfo]:
    return agent_controller.proveedores()


@router.post(
    "/chat",
    response_model=AgentChatResponse,
    summary="Un turno de conversación con el asistente sobre el perfil y la propuesta",
)
async def chat(
    payload: AgentChatRequest,
    usuario: CurrentUser = Depends(get_current_user),
) -> AgentChatResponse:
    return await agent_controller.chat(payload, usuario)


@router.post(
    "/simulador",
    response_model=SimuladorResponse,
    summary="Recomendación de IA sobre la simulación en pantalla (el motor elige, la IA explica)",
)
async def simulador(
    payload: SimuladorRequest,
    usuario: CurrentUser = Depends(get_current_user),
) -> SimuladorResponse:
    # No hace falta tener una propuesta ni un perfilamiento: se puede simular antes. Sin
    # perfil, el catálogo viene sin regla de elegibilidad y la IA lo dice tal cual.
    return await agent_controller.recomendar_simulacion(payload, usuario)
