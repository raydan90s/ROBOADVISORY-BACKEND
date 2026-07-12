"""El agente conversacional como grafo de estados (LangGraph).

Responde al criterio de evaluación #1 (arquitectura agéntica) y refuerza el #3
(antialucinación). El router clasifica cada mensaje en una de 3 rutas (más el
rechazo), y el grafo es corto y auditable:

    entrada → router ─┬─(A: bancario)→ qa ──────┐
                       ├─(B: mixto)  → mixto ────┤
                       ├─(C: externo)→ mercado ──┼→ guardrail ─┬─(ok)──────────→ FIN
                       │                         │             ├─(falla,1 vez)→ (misma ruta)
                       └─(fuera de alcance)───────────────────→│             └─(reincide)───→ fallback → FIN
                                                                (refuse) ──────────────────────────────→ FIN

- **Ruta A (bancario)**: usa exclusivamente los DATOS del inversionista que salen de
  Postgres (perfil, propuesta, catálogo del banco, subcuentas). Es el flujo original.
- **Ruta B (mixto)**: A + cotizaciones de Alpha Vantage (`market_data.py`), para
  preguntas que comparan el banco con mercados externos.
- **Ruta C (externo)**: 100% Alpha Vantage — acciones, forex, cripto, índices. NO usa
  el catálogo del banco.
- **Rechazo**: predicciones de mercado, órdenes de compra/venta y tareas ajenas
  (traducir, programar, etc.) siguen fuera de alcance en las tres rutas.

REGLA DE CONTENCIÓN: ningún nodo de B ni de C escribe en `proposals` /
`proposal_items` — solo leen (Alpha Vantage, y en B también el contexto ya cargado
del banco) y devuelven texto. La única escritura que hace cualquier ruta es el
historial de chat (`llm_interactions`), en `agent_controller._guardar_turno`, igual
para las 3 rutas.

El prompt es **unificado**: un bloque estático (identidad + regla de oro + DATOS)
seguido de la conversación (historial + pregunta). Los principios:

- La **fuente de verdad NO es la memoria del modelo**: son los DATOS que salen de la
  base (Ruta A/B) o de Alpha Vantage (Ruta B/C), inyectados en el prompt. Todo número
  que el modelo escriba se compara contra `ContextoPermitido` con el mismo
  `guardrails.validar` que valida las propuestas.
- El modelo (proveedor y versión) se elige desde el `.env`; ver `llm_provider.py`.
- **No hay tools ni acciones.** El agente solo lee y explica; no ejecuta ni agenda
  nada. Si el guardarraíl no puede confirmar el texto, cae a una explicación
  determinista o a un rechazo — nunca muestra un dato inventado.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypedDict

from src.services import market_data
from src.services.ai_agent import (
    PLANTILLA,
    DatosExplicacion,
    _usd,
    contexto_permitido,
    explicacion_determinista,
)
from src.services.guardrails import ContextoPermitido, validar
from src.services.llm_provider import crear_llm, hay_api_key, modelo_activo
from src.services.market_data import MarketQuote

log = logging.getLogger(__name__)

REFUSE = "refuse"  # marca de modelo para los turnos fuera de alcance

RUTA_BANCARIO = "bancario"  # Ruta A: solo datos del banco
RUTA_MIXTO = "mixto"  # Ruta B: banco + Alpha Vantage
RUTA_EXTERNO = "externo"  # Ruta C: 100% Alpha Vantage
RUTA_RECHAZO = "rechazo"  # fuera de alcance en cualquier ruta

# Disclaimer breve para el chat (el de la propuesta es largo y aquí lo haría pesado en
# cada turno). Mantiene el criterio HU2-3: es propuesta, no orden ni promesa.
DISCLAIMER_CHAT = "Es una propuesta referencial y la revisa un asesor autorizado."

# El aviso NO negociable de las Rutas B y C (pedido explícito del reto): estos
# instrumentos no son del banco, y la respuesta tiene que decirlo siempre, la
# escriba el modelo o la plantilla determinista.
DISCLAIMER_SIMULACION = (
    "Esta es una simulación educativa con datos de mercados externos (Alpha Vantage). "
    "Estos instrumentos NO están en el catálogo del banco ni forman parte de tu propuesta."
)

# Texto fijo de rechazo (ARQUITECTURA-IA §6). No es prompt engineering esperanzado:
# es un nodo del grafo, y por eso es un caso de prueba reproducible.
TEXTO_RECHAZO = (
    "Solo puedo ayudarte con TUS datos: explicarte cómo se calculó tu perfil o "
    "qué instrumentos tiene tu propuesta. No puedo predecir mercados, recomendar "
    "productos fuera de tu propuesta ni hacer otras tareas."
)


# ===========================================================================
# Router: ¿bancario, mixto, 100% externo, o fuera de alcance?
# ===========================================================================

# Fuera de alcance en CUALQUIER ruta: predecir movimientos de mercado (dar una
# cotización actual es Ruta C; predecir hacia dónde va es otra cosa, prohibida
# igual que en el catálogo del banco), órdenes de compra/venta, y tareas ajenas.
# Lo dudoso se deja pasar al nodo correspondiente, cuyo prompt también acota el
# alcance, y al guardarraíl. Es una primera línea determinista, no la única.
_FUERA_DE_ALCANCE = re.compile(
    r"""
    \b(?:
        va\s+a\s+(?:subir|bajar|caer|crecer|rendir) | subir[áa] | bajar[áa] |
        predic\w* | pron[oó]stic\w* | proyecci[oó]n | qu[eé]\s+va\s+a\s+pasar |
        c[oó]mprame | v[eé]ndeme | ejecuta\w* | invierte\s+por\s+m[ií] |
        trad[uú]ce\w* | traducci[oó]n | (?:escribe|dame|genera)\s+(?:un\s+)?c[oó]digo |
        program[ae]\w* | receta | chiste | poema |
        clima | noticias? | deporte\w* | f[uú]tbol | pel[ií]cula\w* | hor[oó]scopo
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Mercados externos: cripto, forex, índices, acciones — el vocabulario que el router
# original rechazaba de plano y que ahora abre la Ruta B o C en vez de un rechazo.
_MERCADO_EXTERNO = re.compile(
    r"""
    \b(?:
        bitcoin | btc | cripto\w* | ethereum | eth |
        forex | eur\s*/?\s*usd | euro | d[oó]lar |
        oro | xau | plata | xag |
        nasdaq | s\s*&\s*p\s*500? | spy | acci[oó]n\w* | bolsa | índice\w* | indice\w* |
        nikkei | jpn\s*225 | jap[oó]n | mercados?\s+(?:externos?|internacionales?) |
        cotizaci[oó]n\w*
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Señales de que, ADEMÁS de preguntar por mercados, el cliente quiere comparar/mezclar
# con el banco (Ruta B). Sin esta señal, una pregunta de mercado es 100% externa (C).
_MENCION_BANCO = re.compile(
    r"""
    \b(?:
        mi\s+perfil | mi\s+propuesta | mi\s+cartera | mi\s+subcuenta | mis\s+subcuentas |
        banco\w* | dep[oó]sito\w* | dpf | fondo\w* | elegib\w* | cat[aá]logo |
        compar\w* | versus | \bvs\b | tasa\w*
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Palabra clave del mensaje → símbolo de `market_data.py`. Si el mensaje no nombra
# ninguno en particular ("¿cómo están los mercados?"), se piden los 5 del ticker.
_SIMBOLO_POR_PALABRA: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"bitcoin|btc|cripto\w*", re.IGNORECASE), "BTCUSD"),
    (re.compile(r"oro\b|xau", re.IGNORECASE), "XAUUSD"),
    (re.compile(r"nikkei|jpn\s*225|jap[oó]n", re.IGNORECASE), "JPN225"),
    (re.compile(r"s\s*&\s*p\s*500?|spy|nasdaq|acci[oó]n\w*|bolsa", re.IGNORECASE), "SPY"),
    (re.compile(r"eur\s*/?\s*usd|euro|d[oó]lar", re.IGNORECASE), "EURUSD"),
]


def _clasificar_ruta(mensaje: str) -> str:
    """El router determinista de las 3 rutas + rechazo. Ver el diagrama del módulo."""
    if _FUERA_DE_ALCANCE.search(mensaje):
        return RUTA_RECHAZO
    if not _MERCADO_EXTERNO.search(mensaje):
        return RUTA_BANCARIO
    return RUTA_MIXTO if _MENCION_BANCO.search(mensaje) else RUTA_EXTERNO


def _simbolos_de(mensaje: str) -> list[str]:
    """Los símbolos que el mensaje nombra explícitamente, o los 5 del ticker si no nombra ninguno."""
    encontrados = [simbolo for patron, simbolo in _SIMBOLO_POR_PALABRA if patron.search(mensaje)]
    # sin duplicados, conservando el orden de aparición
    return list(dict.fromkeys(encontrados)) or list(market_data.SIMBOLOS_DEFAULT)


# ===========================================================================
# El prompt unificado: identidad + regla de oro + DATOS del inversionista
# ===========================================================================

# ===========================================================================
# El contexto que el agente conoce del inversionista (todo sale de la base)
# ===========================================================================


@dataclass(frozen=True)
class ItemCatalogo:
    """Un producto del catálogo, con si el perfil del inversionista puede tomarlo."""

    nombre: str
    institucion: str
    calificacion: str
    rating_tier: int
    clase_activo: str
    retorno: float | None
    plazo_dias: int | None
    min_amount: float | None
    elegible: bool


@dataclass(frozen=True)
class Subcuenta:
    """Una sesión de inversión del mismo inversionista (para comparar)."""

    nombre: str | None
    monto: float | None
    perfil: str
    instrumento_principal: str | None
    es_actual: bool


@dataclass(frozen=True)
class ContextoAgente:
    """TODO lo que el agente conoce del inversionista para analizar, no solo describir.

    `datos` es la propuesta de la sesión actual (lo mismo que redacta la explicación);
    el resto le permite razonar: la regla de elegibilidad de su perfil, el catálogo
    marcando qué puede o no tomar, y sus otras subcuentas. Todo sale de Postgres.
    """

    datos: DatosExplicacion
    max_rating_tier: int | None
    regla_elegibilidad: str | None
    catalogo: list[ItemCatalogo]
    subcuentas: list[Subcuenta]
    # Todas las calificaciones que existen en `institutions` (AAA, AAA-, AA+, AA…). El
    # agente las nombra al explicar la regla de elegibilidad; son reales, no inventadas.
    calificaciones_validas: list[str]
    # Capital del inversionista (subcuentas): techo declarado, lo ya repartido y lo que
    # queda libre. None si no declaró un capital total. Todo sale de Postgres.
    capital_total: float | None
    asignado: float | None
    sin_asignar: float | None


def contexto_permitido_agente(ctx: ContextoAgente) -> ContextoPermitido:
    """Extiende el conjunto citable de la propuesta con el catálogo y las subcuentas.

    Sin esto, si el agente menciona un producto del catálogo (para explicar por qué NO
    es elegible, p. ej.), el guardarraíl lo rechazaría por "inventado". Acá le damos
    permiso de nombrar exactamente lo que la base dice — y nada más.
    """
    base = contexto_permitido(ctx.datos)
    numeros = set(base.numeros)
    instrumentos = set(base.instrumentos)
    instituciones = set(base.instituciones)
    calificaciones = set(base.calificaciones)

    for it in ctx.catalogo:
        instrumentos.add(it.nombre)
        instituciones.add(it.institucion)
        calificaciones.add(it.calificacion)
        if it.retorno is not None:
            numeros.add(Decimal(str(it.retorno)))
        if it.plazo_dias is not None:
            numeros.add(Decimal(it.plazo_dias))
        if it.min_amount is not None:
            numeros.add(Decimal(str(it.min_amount)))

    for s in ctx.subcuentas:
        if s.monto is not None:
            numeros.add(Decimal(str(s.monto)))
        if s.instrumento_principal:
            instrumentos.add(s.instrumento_principal)

    # Las calificaciones reales del sistema: el agente las cita al explicar la regla
    # ("tu perfil admite hasta AA"), y son legítimas aunque no estén en su cartera.
    calificaciones.update(ctx.calificaciones_validas)

    for monto in (ctx.capital_total, ctx.asignado, ctx.sin_asignar):
        if monto is not None:
            numeros.add(Decimal(str(monto)))

    if ctx.max_rating_tier is not None:
        numeros.add(Decimal(ctx.max_rating_tier))

    return ContextoPermitido(
        numeros=numeros,
        instrumentos=instrumentos,
        instituciones=instituciones,
        calificaciones=calificaciones,
    )


def _norm(s: str) -> str:
    """Sin tildes y en minúsculas, para comparar menciones sin depender de la ortografía."""
    plano = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(plano.lower().split())


def fuentes_citadas(ctx: ContextoAgente, texto: str) -> list[dict[str, Any]]:
    """Source chips DINÁMICOS: solo las fuentes que ESTA respuesta realmente mencionó.

    Antes se pegaban siempre los mismos chips de la propuesta (parecía quemado). Ahora se
    revisa el texto ya generado: un producto se cita si aparecen SU nombre Y su banco
    (el banco desambigua los depósitos con el mismo nombre); la regla/puntaje se cita si
    el texto habla del perfil, la elegibilidad o una calificación. Si no cita nada
    concreto, no hay chips — que es lo honesto.
    """
    t = _norm(texto)
    chips: list[dict[str, Any]] = []
    vistos: set[tuple[str, str]] = set()

    # Candidatos: primero los de la propuesta, luego el resto del catálogo.
    candidatos: list[tuple[str, str, str, str, str]] = []
    for a in ctx.datos.allocations:
        etiqueta = f"{a.nombre} · {a.porcentaje:g}%" + (
            f" · {_usd(a.monto_asignado)}" if a.monto_asignado is not None else ""
        )
        candidatos.append((a.nombre, a.institucion or "", "proposal_items", a.instrumento_code, etiqueta))
    for it in ctx.catalogo:
        etiqueta = f"{it.nombre} · {it.institucion} · {it.calificacion}" + (
            f" · {it.retorno:g}%" if it.retorno is not None else ""
        )
        candidatos.append((it.nombre, it.institucion, "instruments", f"{it.nombre} — {it.institucion}", etiqueta))

    for nombre, institucion, tabla, record_id, etiqueta in candidatos:
        if not nombre or not institucion:
            continue
        par = (_norm(nombre), _norm(institucion))
        if par in vistos:
            continue
        if par[0] in t and par[1] in t:
            vistos.add(par)
            chips.append({"table": tabla, "record_id": record_id, "label": etiqueta})

    d = ctx.datos
    if any(k in t for k in ("perfil", "puntaje", "regla", "calific", "elegib", "umbral")):
        chips.append(
            {
                "table": "scoring_rules",
                "record_id": d.rules_version,
                "label": f"Puntaje {d.investor.puntaje}/{d.puntaje_max} · reglas {d.rules_version}",
            }
        )

    return chips


# ===========================================================================
# Rutas B/C: contexto y fuentes de Alpha Vantage
# ===========================================================================


def contexto_permitido_mercado(
    cotizaciones: list[MarketQuote], base: ContextoPermitido | None = None
) -> ContextoPermitido:
    """El conjunto citable de las Rutas B/C: los precios y símbolos que trajo Alpha Vantage.

    Ruta C parte de un `ContextoPermitido` vacío (no debe citar NADA del banco); Ruta B
    parte del `contexto_permitido_agente` normal y le SUMA esto — así el modelo puede
    comparar "tu depósito rinde X% vs. Bitcoin varió Y% hoy" sin que el guardarraíl
    trate el precio de Bitcoin como un número inventado.
    """
    numeros = set(base.numeros) if base else set()
    instrumentos = set(base.instrumentos) if base else set()
    instituciones = set(base.instituciones) if base else set()
    calificaciones = set(base.calificaciones) if base else set()

    for q in cotizaciones:
        numeros.add(Decimal(str(q.price)))
        if q.change_percent:
            # el signo no sobrevive la extracción de dígitos del guardarraíl: se permite
            # el valor absoluto también, para que "-0.32%" y "0.32%" ambos calcen.
            numeros.add(Decimal(str(q.change_percent)))
            numeros.add(Decimal(str(abs(q.change_percent))))
        instrumentos.add(q.symbol)

    return ContextoPermitido(
        numeros=numeros,
        instrumentos=instrumentos,
        instituciones=instituciones,
        calificaciones=calificaciones,
    )


def _texto_cotizaciones(cotizaciones: list[MarketQuote]) -> str:
    return "; ".join(
        f"{q.symbol}: USD {q.price:,.2f}"
        + (f" ({q.change_percent:+.2f}% hoy)" if q.change_percent else "")
        for q in cotizaciones
    )


def fuentes_citadas_mercado(cotizaciones: list[MarketQuote], texto: str) -> list[dict[str, Any]]:
    """Source chips de Alpha Vantage: solo los símbolos que la respuesta realmente citó."""
    t = _norm(texto)
    chips: list[dict[str, Any]] = []
    for q in cotizaciones:
        if _norm(q.symbol) not in t:
            continue
        fuente = "Alpha Vantage" if q.source == "alpha_vantage" else "Alpha Vantage (simulado)"
        chips.append(
            {
                "table": "alpha_vantage",
                "record_id": q.symbol,
                "label": f"{q.symbol} · USD {q.price:,.2f} · {fuente}",
            }
        )
    return chips


_REGLA_DE_ORO = """Eres el asistente virtual de un robo-advisor (sin nombre propio; no te
presentes con uno). Conoces a ESTE inversionista: su perfil, puntaje, propuesta(s) y qué
del catálogo puede tomar. Explicas y analizas SUS datos con los DATOS de abajo (salen de
la base de datos).

REGLA DE ORO (si rompes una, tu respuesta se descarta):
1. FUENTE DE VERDAD = los DATOS de abajo. NO inventes ni recalcules ningún número, %,
   monto, banco ni calificación. Si algo no está en los DATOS, dilo; NUNCA lo supongas.
2. SÉ BREVE Y CONTUNDENTE. Ve directo a la respuesta, sin rodeos ni relleno. Una
   explicación: máx. 60 palabras. Si el usuario pide una LISTA o comparación (sus
   productos, propuestas, subcuentas, opciones), responde así: una línea corta de intro y
   luego cada ítem en SU PROPIA LÍNEA empezando con «• ». No uses markdown (**negritas**,
   #, tablas): solo texto con viñetas «• » y saltos de línea.
3. Puedes ANALIZAR y COMPARAR los DATOS (por qué tu perfil no admite un banco, qué
   subcuenta es más conservadora, el trade-off tasa/calificación), pero NO predigas
   mercados, NO recomiendes comprar/vender productos nuevos y NO prometas rentabilidad
   ("garantizado", "seguro", "sin riesgo", "vas a ganar" están prohibidos). Los retornos
   son referenciales.
4. Fuera de los DATOS (otros activos, predicciones, tareas ajenas como traducir o
   programar): di en una frase que solo explicas y analizas su perfil, propuestas y catálogo.
5. Cuenta con letras ("los dos productos"), nunca con dígitos.
6. Cita cada producto por su nombre COMPLETO y EXACTO con banco (ej. «Depósito a Plazo
   Fijo 360 días de Banco Loja» — NUNCA «el DPF» ni abreviado). No uses «Fondo» o
   «Depósito» sueltos como palabra genérica; si no nombras uno puntual, di «ese producto»."""


def _bloque_datos(ctx: ContextoAgente) -> str:
    """El bloque con TODO lo que el agente tiene permitido citar. Sale de Postgres."""
    d = ctx.datos
    inv = d.investor
    respuestas = " ".join(
        f"[{r.pregunta_text} → «{r.opcion_label}», {r.puntos} pts]"
        for r in inv.respuestas
    )
    productos = "\n".join(
        f"- {a.nombre} ({a.institucion}, calificación {a.calificacion}): {a.porcentaje:g}%"
        + (f", {_usd(a.monto_asignado)}" if a.monto_asignado is not None else "")
        + (f", plazo {a.plazo_dias} días" if a.plazo_dias is not None else ", sin plazo fijo")
        + (f", retorno referencial {a.retorno_esperado:g}%" if a.retorno_esperado is not None else "")
        for a in d.allocations
    )
    monto = _usd(d.monto_total) if d.monto_total is not None else "no declarado"

    elegibilidad = (
        f"\nRegla de elegibilidad de tu perfil: {ctx.regla_elegibilidad}"
        if ctx.regla_elegibilidad
        else ""
    )

    catalogo = ""
    if ctx.catalogo:
        lineas = "\n".join(
            f"- {it.nombre} ({it.institucion}, {it.calificacion}): "
            + (f"tasa {it.retorno:g}%" if it.retorno is not None else "sin tasa fija")
            + (f", plazo {it.plazo_dias} días" if it.plazo_dias is not None else "")
            + (f", mínimo {_usd(it.min_amount)}" if it.min_amount is not None else "")
            + (" — ELEGIBLE para tu perfil" if it.elegible else " — NO elegible para tu perfil")
            for it in ctx.catalogo
        )
        catalogo = f"\n\nCatálogo del banco (lo que tu perfil PUEDE o NO tomar):\n{lineas}"

    subcuentas = ""
    if len(ctx.subcuentas) > 1:
        lineas = "\n".join(
            f"- {'[esta] ' if s.es_actual else ''}{s.nombre or 'Subcuenta'} · perfil {s.perfil}"
            + (f" · {_usd(s.monto)}" if s.monto is not None else "")
            + (f" · principal: {s.instrumento_principal}" if s.instrumento_principal else "")
            for s in ctx.subcuentas
        )
        subcuentas = f"\n\nTus subcuentas (sesiones de inversión, para comparar):\n{lineas}"

    capital = ""
    if ctx.capital_total is not None:
        detalle = f"capital total {_usd(ctx.capital_total)}"
        if ctx.asignado is not None:
            detalle += f", asignado {_usd(ctx.asignado)}"
        if ctx.sin_asignar is not None:
            detalle += f", sin asignar {_usd(ctx.sin_asignar)}"
        capital = f"\n\nTu capital (para tus subcuentas): {detalle}."

    return f"""DATOS DEL INVERSIONISTA (son los ÚNICOS números y nombres que puedes usar):
Cliente: {inv.nombre}
Monto de esta subcuenta: {monto}
Puntaje: {inv.puntaje} de {d.puntaje_max} → perfil {inv.perfil_riesgo.value}
Rango del perfil: {d.umbral_min} a {d.umbral_max} puntos (reglas {d.rules_version})
Riesgo de la cartera: {d.riesgo.value}
Respuestas del cuestionario: {respuestas}
Cartera asignada por el motor de reglas:
{productos}{elegibilidad}{catalogo}{subcuentas}{capital}"""


def build_system_prompt(ctx: ContextoAgente) -> str:
    """Prompt de sistema unificado: regla de oro + todo el contexto del inversionista."""
    return f"{_REGLA_DE_ORO}\n\n{_bloque_datos(ctx)}"


# ===========================================================================
# Prompts de las Rutas B (mixto) y C (externo)
# ===========================================================================

_REGLA_DE_ORO_MERCADO = """Eres un asistente educativo de mercados externos (fuera del
catálogo del banco): acciones, forex, cripto e índices.

REGLA DE ORO (si rompes una, tu respuesta se descarta):
1. FUENTE DE VERDAD = las COTIZACIONES de abajo (Alpha Vantage). NO inventes ni
   recalcules ningún precio ni variación porcentual.
2. NO prediges hacia dónde va un precio, NO recomiendas comprar ni vender, y NO
   prometes rentabilidad ("garantizado", "seguro", "sin riesgo", "vas a ganar"
   prohibidos). Son cotizaciones de referencia, no una recomendación.
3. SÉ BREVE: máx. 60 palabras, tono cercano, tuteando, sin markdown.
4. SIEMPRE deja claro que es una simulación educativa y que estos instrumentos no
   están en el catálogo del banco (ver el AVISO abajo — inclúyelo o parafraséalo)."""


def _bloque_cotizaciones(cotizaciones: list[MarketQuote]) -> str:
    lineas = "\n".join(
        f"- {q.symbol}: USD {q.price:,.2f}"
        + (f", variación {q.change_percent:+.2f}% hoy" if q.change_percent else "")
        + f" [{'Alpha Vantage' if q.source == 'alpha_vantage' else 'simulado'}]"
        for q in cotizaciones
    )
    return f"""COTIZACIONES DE MERCADOS EXTERNOS (los ÚNICOS números que puedes usar):
{lineas}

AVISO OBLIGATORIO: {DISCLAIMER_SIMULACION}"""


def build_system_prompt_externo(cotizaciones: list[MarketQuote]) -> str:
    """Ruta C: 100% Alpha Vantage. El prompt NO incluye nada del catálogo del banco."""
    return f"{_REGLA_DE_ORO_MERCADO}\n\n{_bloque_cotizaciones(cotizaciones)}"


def build_system_prompt_mixto(ctx: ContextoAgente, cotizaciones: list[MarketQuote]) -> str:
    """Ruta B: los DATOS del banco (regla de oro normal) + las cotizaciones externas.

    Reutiliza `build_system_prompt` tal cual —el cliente sigue sin poder inventarse un
    producto del banco— y le añade el bloque de mercados con su propia regla de oro
    (no predecir, no prometer) y el aviso de simulación.
    """
    return (
        f"{build_system_prompt(ctx)}\n\n"
        "Además de tus datos del banco, también puedes comparar con mercados externos "
        "usando SOLO estas cotizaciones (mismas reglas: no inventes precios, no "
        "predigas, no prometas rentabilidad):\n\n"
        f"{_bloque_cotizaciones(cotizaciones)}"
    )


def _explicacion_mercado_determinista(cotizaciones: list[MarketQuote]) -> str:
    """Fallback de la Ruta C: pasa el guardarraíl por construcción (números de `cotizaciones`)."""
    return f"Cotizaciones de referencia: {_texto_cotizaciones(cotizaciones)}.\n\n{DISCLAIMER_SIMULACION}"


def _explicacion_mixta_determinista(ctx: ContextoAgente, cotizaciones: list[MarketQuote]) -> str:
    """Fallback de la Ruta B: la explicación de la propuesta + las cotizaciones, ambas seguras."""
    return (
        f"{explicacion_determinista(ctx.datos)}\n\n"
        f"Cotizaciones de mercados externos de referencia: {_texto_cotizaciones(cotizaciones)}.\n\n"
        f"{DISCLAIMER_SIMULACION}"
    )


# ===========================================================================
# El LLM (el proveedor lo elige el .env; ver llm_provider.py)
# ===========================================================================


async def _llamar_llm(
    system: str,
    historial: list[tuple[str, str]],
    mensaje: str,
    correccion: str = "",
    provider: str | None = None,
) -> str:
    """Un turno del chat. `historial` son los turnos previos como (rol, texto)."""
    llm = crear_llm(provider=provider)  # el proveedor lo elige el front; si no, el .env
    mensajes: list[tuple[str, str]] = [("system", system), *historial, ("human", mensaje)]
    if correccion:
        mensajes.append(("human", correccion))

    respuesta = await llm.ainvoke(mensajes)
    return str(respuesta.content).strip()


# ===========================================================================
# El estado del grafo
# ===========================================================================


class AgentState(TypedDict, total=False):
    """Lo que fluye entre nodos. LangGraph fusiona los dicts que devuelve cada nodo."""

    mensaje: str
    contexto: ContextoAgente
    ctx: ContextoPermitido
    historial: list[tuple[str, str]]
    # Proveedor elegido en el front para ESTE turno (None = el default del .env).
    provider: str | None

    # Ruta elegida por el router: "bancario" | "mixto" | "externo" | "rechazo".
    ruta: str
    # Cotizaciones de Alpha Vantage pedidas para este turno (vacío en Ruta A).
    cotizaciones: list[MarketQuote]
    correccion: str

    texto: str
    modelo: str
    guardrail_passed: bool
    retry_count: int
    motivos: list[str]
    sources: list[dict[str, Any]]


# ===========================================================================
# Nodos
# ===========================================================================


def router_node(state: AgentState) -> AgentState:
    """Clasifica el mensaje en una de las 3 rutas (o rechazo). Ver el diagrama del módulo."""
    return {"ruta": _clasificar_ruta(state["mensaje"])}


async def qa_node(state: AgentState) -> AgentState:
    """Ruta A (bancario): genera con el LLM usando SOLO los datos del banco.

    El fallback determinista NO responde la pregunta literal, pero es un texto veraz
    y con fuentes que jamás inventa un número — el piso de calidad de la demo.
    """
    ctx = state["contexto"]
    system = build_system_prompt(ctx)
    provider = state.get("provider")

    if not hay_api_key(provider):
        log.warning("Sin API key del proveedor de IA: el agente usa la explicación determinista.")
        return {"texto": explicacion_determinista(ctx.datos), "modelo": PLANTILLA}

    try:
        texto = await _llamar_llm(
            system,
            state.get("historial", []),
            state["mensaje"],
            state.get("correccion", ""),
            provider=provider,
        )
    except Exception as exc:  # API caída, cuota agotada, timeout…
        log.warning("El proveedor de IA falló en el agente: %s", exc)
        return {"texto": explicacion_determinista(ctx.datos), "modelo": PLANTILLA}

    # Disclaimer breve, no depende de que el modelo se acuerde de escribirlo.
    if "revisa un asesor" not in texto:
        texto = f"{texto}\n\n{DISCLAIMER_CHAT}"
    return {"texto": texto, "modelo": modelo_activo(provider)}


async def mercado_node(state: AgentState) -> AgentState:
    """Ruta C: 100% Alpha Vantage. NUNCA lee ni cita el catálogo del banco.

    Contención: este nodo solo llama a `market_data` (lectura) y al LLM; no ejecuta
    ningún INSERT/UPDATE — ni aquí ni en ninguna tabla de `proposals`.
    """
    cotizaciones = await market_data.obtener_cotizaciones(_simbolos_de(state["mensaje"]))
    system = build_system_prompt_externo(cotizaciones)
    provider = state.get("provider")
    # El guardarraíl de esta ruta solo permite los números de Alpha Vantage: CERO
    # contexto del banco, aunque `state["contexto"]` (el del banco) siga cargado.
    ctx_permitido = contexto_permitido_mercado(cotizaciones)

    if not hay_api_key(provider):
        log.warning("Sin API key del proveedor de IA: Ruta C usa la cotización sin redactar.")
        return {
            "texto": _explicacion_mercado_determinista(cotizaciones),
            "modelo": PLANTILLA,
            "cotizaciones": cotizaciones,
            "ctx": ctx_permitido,
        }

    try:
        texto = await _llamar_llm(
            system,
            state.get("historial", []),
            state["mensaje"],
            state.get("correccion", ""),
            provider=provider,
        )
    except Exception as exc:
        log.warning("El proveedor de IA falló en la Ruta C: %s", exc)
        return {
            "texto": _explicacion_mercado_determinista(cotizaciones),
            "modelo": PLANTILLA,
            "cotizaciones": cotizaciones,
            "ctx": ctx_permitido,
        }

    if "simulación educativa" not in texto.lower():
        texto = f"{texto}\n\n{DISCLAIMER_SIMULACION}"
    return {
        "texto": texto,
        "modelo": modelo_activo(provider),
        "cotizaciones": cotizaciones,
        "ctx": ctx_permitido,
    }


async def mixto_node(state: AgentState) -> AgentState:
    """Ruta B: datos del banco + Alpha Vantage. Solo lectura de ambos, igual que Ruta A/C."""
    ctx = state["contexto"]
    cotizaciones = await market_data.obtener_cotizaciones(_simbolos_de(state["mensaje"]))
    system = build_system_prompt_mixto(ctx, cotizaciones)
    provider = state.get("provider")
    ctx_permitido = contexto_permitido_mercado(cotizaciones, base=contexto_permitido_agente(ctx))

    if not hay_api_key(provider):
        log.warning("Sin API key del proveedor de IA: Ruta B usa la explicación determinista.")
        return {
            "texto": _explicacion_mixta_determinista(ctx, cotizaciones),
            "modelo": PLANTILLA,
            "cotizaciones": cotizaciones,
            "ctx": ctx_permitido,
        }

    try:
        texto = await _llamar_llm(
            system,
            state.get("historial", []),
            state["mensaje"],
            state.get("correccion", ""),
            provider=provider,
        )
    except Exception as exc:
        log.warning("El proveedor de IA falló en la Ruta B: %s", exc)
        return {
            "texto": _explicacion_mixta_determinista(ctx, cotizaciones),
            "modelo": PLANTILLA,
            "cotizaciones": cotizaciones,
            "ctx": ctx_permitido,
        }

    if "simulación educativa" not in texto.lower():
        texto = f"{texto}\n\n{DISCLAIMER_SIMULACION}"
    return {
        "texto": texto,
        "modelo": modelo_activo(provider),
        "cotizaciones": cotizaciones,
        "ctx": ctx_permitido,
    }


def guardrail_node(state: AgentState) -> AgentState:
    """Valida el texto contra el conjunto permitido de la ruta. Si falla, prepara el reintento."""
    # Si la respuesta ya vino de la plantilla (LLM caído/ausente), pasa por construcción.
    if state["modelo"] == PLANTILLA:
        return {"guardrail_passed": True, "motivos": []}

    veredicto = validar(state["texto"], state["ctx"])
    if veredicto.ok:
        return {"guardrail_passed": True, "motivos": []}

    intentos = state.get("retry_count", 0) + 1
    log.warning("Guardarraíl rechazó al agente (intento %s, ruta %s): %s", intentos, state.get("ruta"), veredicto.motivos)
    correccion = (
        "Tu respuesta anterior fue RECHAZADA por el validador:\n"
        + "\n".join(f"- {m}" for m in veredicto.motivos)
        + "\nReescríbela usando EXCLUSIVAMENTE los números y nombres de los DATOS/COTIZACIONES."
    )
    return {
        "guardrail_passed": False,
        "motivos": veredicto.motivos,
        "retry_count": intentos,
        "correccion": correccion,
    }


def refuse_node(state: AgentState) -> AgentState:
    """Fuera de alcance: texto fijo, sin fuentes, sin LLM."""
    return {
        "texto": TEXTO_RECHAZO,
        "modelo": REFUSE,
        "guardrail_passed": True,
        "sources": [],
        "motivos": [],
    }


def fallback_node(state: AgentState) -> AgentState:
    """Dos rechazos del guardarraíl: se cae a una explicación determinista, según la ruta.

    Nunca se muestra nada inventado, y una pregunta 100% de mercado (Ruta C) no debe
    caer en el fallback del banco (mostraría la propuesta, que no viene al caso).
    """
    ruta = state.get("ruta", RUTA_BANCARIO)
    cotizaciones = state.get("cotizaciones", [])
    if ruta == RUTA_EXTERNO:
        texto = _explicacion_mercado_determinista(cotizaciones)
    elif ruta == RUTA_MIXTO:
        texto = _explicacion_mixta_determinista(state["contexto"], cotizaciones)
    else:
        texto = explicacion_determinista(state["contexto"].datos)
    return {"texto": texto, "modelo": PLANTILLA, "guardrail_passed": True}


# ===========================================================================
# Aristas condicionales
# ===========================================================================

_NODO_DE_RUTA = {RUTA_BANCARIO: "qa", RUTA_MIXTO: "mixto", RUTA_EXTERNO: "mercado"}


def _tras_router(state: AgentState) -> str:
    ruta = state["ruta"]
    return "refuse" if ruta == RUTA_RECHAZO else _NODO_DE_RUTA[ruta]


def _tras_guardrail(state: AgentState) -> str:
    if state["guardrail_passed"]:
        return "fin"
    # Un solo reintento, y a la MISMA ruta que generó el texto rechazado: retry_count
    # llega a 1 en el primer fallo y a 2 en el segundo. Nunca se le muestra al usuario
    # un texto sin validar.
    if state.get("retry_count", 0) >= 2:
        return "fallback"
    return _NODO_DE_RUTA[state["ruta"]]


# ===========================================================================
# Compilación del grafo (una sola vez por proceso)
# ===========================================================================


def _construir_grafo():
    g = StateGraph(AgentState)
    g.add_node("router", router_node)
    g.add_node("qa", qa_node)
    g.add_node("mixto", mixto_node)
    g.add_node("mercado", mercado_node)
    g.add_node("guardrail", guardrail_node)
    g.add_node("refuse", refuse_node)
    g.add_node("fallback", fallback_node)

    g.set_entry_point("router")
    g.add_conditional_edges(
        "router", _tras_router, {"qa": "qa", "mixto": "mixto", "mercado": "mercado", "refuse": "refuse"}
    )
    g.add_edge("qa", "guardrail")
    g.add_edge("mixto", "guardrail")
    g.add_edge("mercado", "guardrail")
    g.add_conditional_edges(
        "guardrail",
        _tras_guardrail,
        {"qa": "qa", "mixto": "mixto", "mercado": "mercado", "fallback": "fallback", "fin": END},
    )
    g.add_edge("refuse", END)
    g.add_edge("fallback", END)
    return g.compile()


# LangGraph se importa acá abajo para que el módulo se pueda importar aunque la lib
# no esté instalada (los tests que no tocan el grafo no la necesitan).
from langgraph.graph import END, StateGraph  # noqa: E402

_GRAFO = _construir_grafo()


async def responder(
    contexto: ContextoAgente,
    mensaje: str,
    historial: list[tuple[str, str]] | None = None,
    provider: str | None = None,
) -> AgentState:
    """Corre el grafo para un turno y devuelve el estado final.

    `contexto` es todo lo que el agente conoce del inversionista (de la base); `mensaje`
    la pregunta; `historial` los turnos previos (para continuidad); `provider` el modelo
    elegido en el front (None = el default del .env). El router decide adentro del
    grafo si el turno usa `contexto`, Alpha Vantage, o ambos (ver el diagrama arriba).
    """
    estado: AgentState = {
        "mensaje": mensaje,
        "contexto": contexto,
        "ctx": contexto_permitido_agente(contexto),  # override si la ruta es B/C
        "historial": historial or [],
        "provider": provider,
        "retry_count": 0,
    }
    final = await _GRAFO.ainvoke(estado)
    ruta = final.get("ruta", RUTA_BANCARIO)
    cotizaciones = final.get("cotizaciones", [])
    texto = final.get("texto", "")

    # Los chips se calculan sobre el texto YA generado: solo las fuentes que citó.
    if final.get("modelo") == REFUSE:
        final["sources"] = []
    elif ruta == RUTA_EXTERNO:
        final["sources"] = fuentes_citadas_mercado(cotizaciones, texto)
    elif ruta == RUTA_MIXTO:
        final["sources"] = fuentes_citadas(contexto, texto) + fuentes_citadas_mercado(cotizaciones, texto)
    else:
        final["sources"] = fuentes_citadas(contexto, texto)
    return final
