"""Lógica de negocio del inversionista: scoring de riesgo + persistencia + IA."""

from fastapi import HTTPException, status

from src.config.database import INVESTORS_TABLE, get_supabase
from src.models.investor import (
    EstadoPropuesta,
    Investor,
    InvestorProfileCreate,
    PerfilRiesgo,
    PortfolioProposal,
)
from src.services.ai_agent import generate_portfolio_proposal

# ===========================================================================
# >>> REGLAS DURAS DEL TEST DE RIESGO — EDITAR AQUÍ <<<
# ===========================================================================
# Peso de cada pregunta del cuestionario. La clave debe coincidir con las que
# manda el frontend en `respuestas_riesgo`.
# Ej: si "pregunta_1" pesa 2 y el usuario responde opción 4 -> aporta 8 puntos.
PESOS_PREGUNTAS: dict[str, float] = {
    "pregunta_1": 1.0,  # ej. horizonte de inversión
    "pregunta_2": 1.5,  # ej. tolerancia a pérdidas (pesa más)
    "pregunta_3": 1.0,  # ej. experiencia previa
    "pregunta_4": 1.0,  # ej. estabilidad de ingresos
    "pregunta_5": 1.5,  # ej. reacción ante una caída del 20%
}

# Valor máximo que puede tener una respuesta (ej. escala Likert 1..5).
VALOR_MAX_RESPUESTA = 5

# Cortes del puntaje normalizado (0-100) -> perfil.
UMBRAL_CONSERVADOR = 40  # <= 40  -> conservador
UMBRAL_MODERADO = 70     # <= 70  -> moderado ; > 70 -> agresivo


def calcular_puntaje_riesgo(respuestas: dict[str, int]) -> int:
    """Convierte las respuestas del test en un puntaje normalizado 0-100.

    >>> REEMPLAZA ESTA FÓRMULA por las reglas reales del hackathon. <<<
    Si el reto define un scoring propio (o penalizaciones cruzadas entre
    preguntas), este es el único lugar que hay que tocar.
    """
    if not respuestas:
        return 0

    puntaje = 0.0
    peso_total = 0.0
    for pregunta, valor in respuestas.items():
        peso = PESOS_PREGUNTAS.get(pregunta, 1.0)
        puntaje += peso * valor
        peso_total += peso * VALOR_MAX_RESPUESTA

    if peso_total == 0:
        return 0
    return round((puntaje / peso_total) * 100)


def clasificar_perfil(puntaje: int) -> PerfilRiesgo:
    """Mapea el puntaje al perfil de riesgo. Ajusta los umbrales arriba."""
    if puntaje <= UMBRAL_CONSERVADOR:
        return PerfilRiesgo.CONSERVADOR
    if puntaje <= UMBRAL_MODERADO:
        return PerfilRiesgo.MODERADO
    return PerfilRiesgo.AGRESIVO


# ===========================================================================
# Casos de uso
# ===========================================================================


async def create_investor_profile(payload: InvestorProfileCreate) -> Investor:
    """Calcula el riesgo, guarda el perfil en Supabase y devuelve la fila creada."""
    puntaje = calcular_puntaje_riesgo(payload.respuestas_riesgo)
    perfil = clasificar_perfil(puntaje)

    fila = {
        **payload.model_dump(exclude_none=True),
        "puntaje_riesgo": puntaje,
        "perfil_riesgo": perfil.value,
        "estado_propuesta": EstadoPropuesta.PENDIENTE.value,
    }

    try:
        resp = get_supabase().table(INVESTORS_TABLE).insert(fila).execute()
    except Exception as exc:  # credenciales inválidas / red / RLS / constraint
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error al guardar en Supabase: {exc}",
        ) from exc

    if not resp.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supabase no devolvió la fila insertada.",
        )

    return Investor(**resp.data[0])


async def get_investor(investor_id: str) -> Investor:
    """Lee un inversionista por id. 404 si no existe."""
    try:
        resp = (
            get_supabase()
            .table(INVESTORS_TABLE)
            .select("*")
            .eq("id", investor_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error al consultar Supabase: {exc}",
        ) from exc

    if not resp.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe el inversionista {investor_id}",
        )

    return Investor(**resp.data[0])


async def get_portfolio_proposal(investor_id: str) -> PortfolioProposal:
    """Devuelve la propuesta del inversionista, generándola con la IA si hace falta.

    Flujo:
      1. lee el perfil (404 si no existe)
      2. llama al agente de IA  <-- src/services/ai_agent.py
      3. marca el estado como LISTA y devuelve la propuesta

    NOTA para el hackathon: hoy la propuesta se regenera en cada GET. Cuando el
    agente sea real (y por tanto lento/caro), guarda el resultado en una tabla
    `portfolios` y devuélvelo de caché si `estado_propuesta == LISTA`.
    """
    investor = await get_investor(investor_id)

    proposal = await generate_portfolio_proposal(investor)

    try:
        get_supabase().table(INVESTORS_TABLE).update(
            {"estado_propuesta": EstadoPropuesta.LISTA.value}
        ).eq("id", investor_id).execute()
    except Exception:
        # No rompemos la respuesta al usuario si solo falló el update de estado.
        pass

    return proposal
