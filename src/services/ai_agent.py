"""Agente asesor financiero.

MOCK por ahora. Aquí va la máquina de estados de LangGraph + Gemini.

CONTRATO IMPORTANTE: el agente recibe el portafolio YA CALCULADO por la base
(allocation_template_items) y solo redacta la explicación en lenguaje natural.
No inventa instrumentos ni porcentajes — eso es lo que hace auditable el sistema
y lo que evidencia la tabla llm_interactions del esquema.
"""

from src.models.investor import AssetAllocation, Investor, NivelRiesgo


def _describir_cartera(allocations: list[AssetAllocation]) -> str:
    return ", ".join(f"{a.porcentaje:g}% en {a.nombre}" for a in allocations)


async def redactar_explicacion(
    investor: Investor,
    allocations: list[AssetAllocation],
    riesgo_esperado: NivelRiesgo,
    retorno_esperado_anual: float | None,
) -> str:
    """Explica al inversionista por qué le tocó esta cartera.

    >>> AQUÍ SE INYECTA LA IA DEL HACKATHON <<<

    Implementación real (esqueleto):
        from langchain_google_genai import ChatGoogleGenerativeAI
        from src.config.settings import settings

        llm = ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            google_api_key=settings.GEMINI_API_KEY,
        )
        prompt = (
            "Eres un asesor financiero. Explica en lenguaje simple, sin prometer "
            "rentabilidad, por qué esta cartera encaja con el perfil del cliente.\\n"
            f"Perfil: {investor.perfil_riesgo.value} (puntaje {investor.puntaje})\\n"
            f"Respuestas: {[r.model_dump() for r in investor.respuestas]}\\n"
            f"Cartera asignada: {[a.model_dump() for a in allocations]}"
        )
        resp = await llm.ainvoke(prompt)
        return resp.content

    Los porcentajes van en el PROMPT, no los decide el modelo. Si algún día el
    LLM devuelve números propios, ignóralos: la fuente de verdad es la BD.
    """
    motivos = " ".join(
        f"Respondiste «{r.opcion_label}» ({r.puntos} pts)." for r in investor.respuestas
    )
    retorno = (
        f" El retorno esperado de referencia es {retorno_esperado_anual:.2f}% anual (cifra "
        "ilustrativa, no una promesa)."
        if retorno_esperado_anual is not None
        else ""
    )

    return (
        f"Hola {investor.nombre}: tu perfil es {investor.perfil_riesgo.value} "
        f"con {investor.puntaje} puntos. {motivos} "
        f"Por eso te proponemos una cartera de riesgo {riesgo_esperado.value}: "
        f"{_describir_cartera(allocations)}.{retorno} "
        "Un asesor humano revisará esta propuesta antes de que se considere final."
    )
