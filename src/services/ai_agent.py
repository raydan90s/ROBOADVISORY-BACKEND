"""Agente asesor: Gemini redacta la explicación, la base pone los números.

CONTRATO: el agente recibe el portafolio YA CALCULADO (allocation_template_items) y solo
lo pone en palabras. Los porcentajes y los USD van **en el prompt**, no los decide el
modelo. Si el modelo devuelve un número propio, `guardrails.validar` lo caza y el texto
se descarta — no se "corrige", se descarta.

El ciclo es: **generar → validar → reintentar una vez → caer a la plantilla determinista.**
Esa última rama es la que hace que la demo no dependa de que Gemini esté de buen humor:
si la API se cae o el modelo alucina dos veces, el usuario igual recibe una explicación
correcta, escrita a partir de los mismos datos. Nunca se le muestra un número inventado.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from src.config.settings import settings
from src.models.investor import AssetAllocation, Investor, NivelRiesgo
from src.services.guardrails import ContextoPermitido, validar

log = logging.getLogger(__name__)

PLANTILLA = "plantilla-determinista"

# El disclaimer no es negociable (HU2, criterio 3): la propuesta no es una orden ni una
# promesa. Se anexa siempre, venga el texto de Gemini o de la plantilla.
DISCLAIMER = (
    "Esta propuesta no constituye una orden de compra ni una promesa de rentabilidad, "
    "y será revisada por un asesor autorizado antes de considerarse final."
)


def _usd(monto: Decimal | float) -> str:
    """Formato ecuatoriano: USD 12.000 (el punto separa miles).

    Importa más de lo que parece: `validar_numeros` lee '12,000' como doce mil solo por
    convención, y mezclar los dos formatos en un mismo texto es pedirle al guardarraíl que
    adivine. La app escribe un solo formato, siempre.
    """
    return f"USD {monto:,.0f}".replace(",", ".")


@dataclass(frozen=True)
class DatosExplicacion:
    """Todo lo que la base sabe de la propuesta. El LLM no ve nada más que esto."""

    investor: Investor
    allocations: list[AssetAllocation]
    riesgo: NivelRiesgo
    monto_total: Decimal | None
    retorno_anual: float | None
    rules_version: str
    umbral_min: int
    umbral_max: int
    puntaje_max: int


@dataclass
class Explicacion:
    """El texto y su expediente: qué modelo lo escribió y si pasó el guardarraíl."""

    texto: str
    modelo: str
    guardrail_passed: bool
    retry_count: int = 0
    motivos: list[str] = field(default_factory=list)
    prompt: str = ""
    sources: list[dict] = field(default_factory=list)


# ===========================================================================
# Lo que el texto tiene derecho a decir
# ===========================================================================


def contexto_permitido(d: DatosExplicacion) -> ContextoPermitido:
    """Construye el conjunto cerrado de números, productos y emisores citables.

    Es el corazón del criterio anti-alucinación: si un número no entra acá, el texto que
    lo mencione se rechaza, sin importar cuán convincente suene.
    """
    numeros: set[Decimal] = {
        Decimal(100),  # "el 100% de tu cartera": estructural, no es un dato inventado
        Decimal(d.investor.puntaje),
        Decimal(d.umbral_min),
        Decimal(d.umbral_max),
        Decimal(d.puntaje_max),
    }

    # "reglas v1" → el 1 es parte del nombre de la versión, no una cifra financiera.
    numeros.update(Decimal(t) for t in "".join(
        c if c.isdigit() else " " for c in d.rules_version
    ).split())

    if d.monto_total is not None:
        numeros.add(Decimal(d.monto_total))
    if d.retorno_anual is not None:
        numeros.add(Decimal(str(d.retorno_anual)))

    for a in d.allocations:
        numeros.add(Decimal(str(a.porcentaje)))
        if a.monto_asignado is not None:
            numeros.add(Decimal(str(a.monto_asignado)))
        if a.retorno_esperado is not None:
            numeros.add(Decimal(str(a.retorno_esperado)))
        if a.plazo_dias is not None:
            numeros.add(Decimal(a.plazo_dias))

    return ContextoPermitido(
        numeros=numeros,
        instrumentos={a.nombre for a in d.allocations},
        instituciones={a.institucion for a in d.allocations if a.institucion},
        calificaciones={a.calificacion for a in d.allocations if a.calificacion},
    )


def fuentes(d: DatosExplicacion) -> list[dict]:
    """Los "source chips": de dónde salió cada afirmación. Se guardan en llm_interactions."""
    chips = [
        {
            "table": "proposal_items",
            "record_id": a.instrumento_code,
            "label": f"{a.nombre} · {a.porcentaje:g}%"
            + (f" · {_usd(a.monto_asignado)}" if a.monto_asignado is not None else ""),
        }
        for a in d.allocations
    ]
    chips.append(
        {
            "table": "scoring_rules",
            "record_id": d.rules_version,
            "label": f"Puntaje {d.investor.puntaje}/{d.puntaje_max} · reglas {d.rules_version}",
        }
    )
    return chips


# ===========================================================================
# La explicación determinista: fallback y, a la vez, piso de calidad
# ===========================================================================


def _linea(a: AssetAllocation) -> str:
    monto = f" ({_usd(a.monto_asignado)})" if a.monto_asignado else ""
    emisor = f" de {a.institucion}" if a.institucion else ""
    rating = f" ({a.calificacion})" if a.calificacion else ""
    return f"{a.porcentaje:g}%{monto} en {a.nombre}{emisor}{rating}"


def explicacion_determinista(d: DatosExplicacion) -> str:
    """La misma información, escrita sin LLM. Es lo que se muestra si Gemini falla.

    Pasa el guardarraíl por construcción: cada número que escribe sale de `d`.
    """
    inv = d.investor
    monto = f"Sobre un monto de {_usd(d.monto_total)}, " if d.monto_total is not None else ""
    cartera = "; ".join(_linea(a) for a in d.allocations)

    return (
        f"Hola {inv.nombre}: tu perfil es {inv.perfil_riesgo.value} con {inv.puntaje} de "
        f"{d.puntaje_max} puntos, calculado con las reglas {d.rules_version} "
        f"(el rango de tu perfil va de {d.umbral_min} a {d.umbral_max} puntos). "
        f"{monto}te proponemos una cartera de riesgo {d.riesgo.value}: {cartera}. "
        f"Todos los emisores cumplen la calificación mínima que tu perfil admite. "
        f"{DISCLAIMER}"
    )


# ===========================================================================
# Gemini
# ===========================================================================

_SISTEMA = """Eres el asesor financiero de un banco ecuatoriano. Tu trabajo es EXPLICAR
en español claro una propuesta que YA fue calculada por el motor de reglas del banco.

REGLAS ABSOLUTAS (si rompes una, tu respuesta se descarta):
1. NO inventes ni recalcules NINGÚN número. Usa exclusivamente las cifras de los DATOS.
   No sumes, no estimes, no redondees a otra cifra, no cites fechas ni años.
2. NO menciones ningún producto, banco ni calificación que no esté en los DATOS.
3. NUNCA prometas rentabilidad ni niegues el riesgo. Prohibido: "garantizado", "seguro",
   "sin riesgo", "vas a ganar". Los retornos son referenciales, no promesas.
4. Escribe los montos en formato ecuatoriano: USD 12.000 (el punto separa miles).
5. Máximo 130 palabras, tono cercano, tuteando. No uses listas ni markdown: un solo párrafo.
6. Explica POR QUÉ esa cartera encaja con las respuestas del cliente, y menciona el emisor
   de cada producto con su calificación."""


def _prompt(d: DatosExplicacion) -> str:
    respuestas = " ".join(
        f"[{r.pregunta_text} → «{r.opcion_label}», {r.puntos} pts]" for r in d.investor.respuestas
    )
    productos = "\n".join(
        f"- {a.nombre} ({a.institucion}, calificación {a.calificacion}): "
        f"{a.porcentaje:g}%"
        + (f", {_usd(a.monto_asignado)}" if a.monto_asignado is not None else "")
        + (f", plazo {a.plazo_dias} días" if a.plazo_dias is not None else ", sin plazo fijo")
        + (f", retorno referencial {a.retorno_esperado:g}%" if a.retorno_esperado is not None else "")
        for a in d.allocations
    )
    monto = _usd(d.monto_total) if d.monto_total is not None else "no declarado"

    return f"""DATOS (son los ÚNICOS números que puedes usar):
Cliente: {d.investor.nombre}
Monto a invertir: {monto}
Puntaje: {d.investor.puntaje} de {d.puntaje_max} → perfil {d.investor.perfil_riesgo.value}
Rango del perfil: {d.umbral_min} a {d.umbral_max} puntos (reglas {d.rules_version})
Riesgo de la cartera: {d.riesgo.value}
Respuestas del cuestionario: {respuestas}
Cartera asignada por el motor de reglas:
{productos}

Redacta la explicación para el cliente."""


async def _generar_con_gemini(prompt: str, reintento: str = "") -> str:
    # Import perezoso: si la Fase 3 no está configurada, la app arranca igual.
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(
        model=settings.GEMINI_MODEL,
        google_api_key=settings.GEMINI_API_KEY,
        temperature=0.2,  # bajo: queremos fidelidad a los datos, no creatividad
    )
    mensajes = [("system", _SISTEMA), ("human", prompt)]
    if reintento:
        mensajes.append(("human", reintento))

    respuesta = await llm.ainvoke(mensajes)
    return str(respuesta.content).strip()


async def redactar_explicacion(d: DatosExplicacion) -> Explicacion:
    """Genera → valida → reintenta una vez → cae a la plantilla. Nunca devuelve un número inventado."""
    ctx = contexto_permitido(d)
    prompt = _prompt(d)
    chips = fuentes(d)
    determinista = Explicacion(
        texto=explicacion_determinista(d),
        modelo=PLANTILLA,
        guardrail_passed=True,
        prompt=prompt,
        sources=chips,
    )

    if not settings.GEMINI_API_KEY:
        log.warning("Sin GEMINI_API_KEY: se usa la explicación determinista.")
        return determinista

    correccion = ""
    ultimos_motivos: list[str] = []

    for intento in range(2):  # el original y UN reintento
        try:
            texto = await _generar_con_gemini(prompt, correccion)
        except Exception as exc:  # API caída, cuota agotada, timeout…
            log.warning("Gemini falló (intento %s): %s", intento + 1, exc)
            determinista.retry_count = intento
            determinista.motivos = [f"Gemini no respondió: {exc}"]
            return determinista

        # El disclaimer lo ponemos nosotros: no depende de que el modelo se acuerde.
        if "no constituye una orden" not in texto:
            texto = f"{texto} {DISCLAIMER}"

        veredicto = validar(texto, ctx)
        if veredicto.ok:
            return Explicacion(
                texto=texto,
                modelo=settings.GEMINI_MODEL,
                guardrail_passed=True,
                retry_count=intento,
                prompt=prompt,
                sources=chips,
            )

        ultimos_motivos = veredicto.motivos
        log.warning("Guardarraíl rechazó a Gemini (intento %s): %s", intento + 1, veredicto.motivos)
        correccion = (
            "Tu respuesta anterior fue RECHAZADA por el validador del banco:\n"
            + "\n".join(f"- {m}" for m in veredicto.motivos)
            + "\nReescríbela usando EXCLUSIVAMENTE los números, productos y bancos de los DATOS."
        )

    # Dos rechazos: no se le muestra al usuario nada que el modelo haya inventado.
    determinista.retry_count = 1
    determinista.motivos = ultimos_motivos
    return determinista
