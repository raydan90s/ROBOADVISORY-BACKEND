"""Agente asesor financiero.

MOCK por ahora. Aquí va la máquina de estados de LangGraph + Gemini.
El resto del backend solo conoce `generate_portfolio_proposal`, así que puedes
reescribir todo el interior de este archivo sin tocar controllers ni routes.
"""

from src.models.investor import (
    AssetAllocation,
    EstadoPropuesta,
    Investor,
    PerfilRiesgo,
    PortfolioProposal,
)

# ---------------------------------------------------------------------------
# Portafolios de referencia (fallback y baseline del hackathon).
# La IA debería ajustar estos pesos, no inventarlos desde cero.
# ---------------------------------------------------------------------------
PORTAFOLIOS_BASE: dict[PerfilRiesgo, list[dict]] = {
    PerfilRiesgo.CONSERVADOR: [
        {"ticker": "AGG", "nombre": "Bonos agregados USA", "clase_activo": "renta_fija", "porcentaje": 60},
        {"ticker": "SPY", "nombre": "S&P 500", "clase_activo": "renta_variable", "porcentaje": 25},
        {"ticker": "CASH", "nombre": "Liquidez", "clase_activo": "cash", "porcentaje": 15},
    ],
    PerfilRiesgo.MODERADO: [
        {"ticker": "SPY", "nombre": "S&P 500", "clase_activo": "renta_variable", "porcentaje": 45},
        {"ticker": "AGG", "nombre": "Bonos agregados USA", "clase_activo": "renta_fija", "porcentaje": 35},
        {"ticker": "VXUS", "nombre": "Acciones internacionales", "clase_activo": "renta_variable", "porcentaje": 15},
        {"ticker": "CASH", "nombre": "Liquidez", "clase_activo": "cash", "porcentaje": 5},
    ],
    PerfilRiesgo.AGRESIVO: [
        {"ticker": "SPY", "nombre": "S&P 500", "clase_activo": "renta_variable", "porcentaje": 45},
        {"ticker": "QQQ", "nombre": "Nasdaq 100", "clase_activo": "renta_variable", "porcentaje": 25},
        {"ticker": "VXUS", "nombre": "Acciones internacionales", "clase_activo": "renta_variable", "porcentaje": 20},
        {"ticker": "BTC", "nombre": "Bitcoin", "clase_activo": "cripto", "porcentaje": 10},
    ],
}

METRICAS_BASE: dict[PerfilRiesgo, tuple[float, float]] = {
    # (retorno esperado anual, volatilidad esperada)
    PerfilRiesgo.CONSERVADOR: (0.05, 0.06),
    PerfilRiesgo.MODERADO: (0.08, 0.12),
    PerfilRiesgo.AGRESIVO: (0.13, 0.22),
}


async def generate_portfolio_proposal(investor: Investor) -> PortfolioProposal:
    """Genera la propuesta de portafolio para un inversionista.

    >>> AQUÍ SE INYECTA LA IA DEL HACKATHON <<<

    Plan sugerido para la máquina de estados (LangGraph):
      1. nodo `analizar_perfil`   -> interpreta puntaje/perfil + contexto del usuario
      2. nodo `buscar_mercado`    -> tool call: precios, noticias, tasas (opcional)
      3. nodo `construir_cartera` -> Gemini con structured output -> list[AssetAllocation]
      4. nodo `validar`           -> chequea que los pesos sumen 100 y respeten el perfil;
                                     si falla, hace loop de vuelta a `construir_cartera`
      5. nodo `explicar`          -> redacta `resumen_ia` en lenguaje simple

    Implementación real (esqueleto):
        from langgraph.graph import StateGraph, END
        from langchain_google_genai import ChatGoogleGenerativeAI
        from src.config.settings import settings

        llm = ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            google_api_key=settings.GEMINI_API_KEY,
        ).with_structured_output(PortfolioProposal)

        graph = StateGraph(...)  # nodos de arriba
        result = await graph.compile().ainvoke({"investor": investor.model_dump()})
        return PortfolioProposal(**result)

    Mantén la firma (Investor -> PortfolioProposal) para no romper el controller.
    """
    # --- MOCK: devuelve el portafolio base según el perfil de riesgo ---
    perfil = investor.perfil_riesgo
    allocations = [AssetAllocation(**a) for a in PORTAFOLIOS_BASE[perfil]]
    retorno, volatilidad = METRICAS_BASE[perfil]

    return PortfolioProposal(
        investor_id=investor.id,
        perfil_riesgo=perfil,
        puntaje_riesgo=investor.puntaje_riesgo,
        estado_propuesta=EstadoPropuesta.LISTA,
        allocations=allocations,
        retorno_esperado_anual=retorno,
        volatilidad_esperada=volatilidad,
        resumen_ia=(
            f"Hola {investor.nombre}, según tu test tienes un perfil {perfil.value} "
            f"(puntaje {investor.puntaje_riesgo}/100). Te proponemos una cartera "
            f"diversificada con un retorno esperado de {retorno:.0%} anual."
        ),
        metadata={"engine": "mock", "version": "0.1.0"},
    )
