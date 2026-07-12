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

from src.services import feed_service, market_data
from src.services.ai_agent import (
    PLANTILLA,
    DatosExplicacion,
    _usd,
    contexto_permitido,
    explicacion_determinista,
)
from src.services.guardrails import (
    ContextoPermitido,
    extraer_numeros,
    validar,
    validar_noticias,
)
from src.services.llm_provider import crear_llm, hay_api_key, modelo_activo
from src.services.market_data import MarketQuote
from src.models.feed import FeedResponse

log = logging.getLogger(__name__)

REFUSE = "refuse"  # marca de modelo para los turnos fuera de alcance

RUTA_BANCARIO = "bancario"  # Ruta A: solo datos del banco
RUTA_MIXTO = "mixto"  # Ruta B: banco + Alpha Vantage
RUTA_EXTERNO = "externo"  # Ruta C: 100% Alpha Vantage
RUTA_NOTICIAS = "noticias"  # Ruta D: titulares reales de GNews (feed_service)
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
    "Puedo ayudarte con TU cuenta (perfil, puntaje, propuesta, subcuentas) y con los "
    "productos del catálogo del banco: cuál te conviene, qué tasa y qué plazo tiene. "
    "Lo que no hago es predecir precios ni mercados, ejecutar órdenes de compra o "
    "venta, ni tareas ajenas a la inversión."
)


# ===========================================================================
# Router: ¿bancario, mixto, 100% externo, o fuera de alcance?
# ===========================================================================

# Fuera de alcance en CUALQUIER ruta — los tres casos que el reto pide rechazar SIEMPRE:
#
#   1. Predecir el futuro (un precio, un mercado). Dar una cotización actual es Ruta C;
#      predecir hacia dónde va es otra cosa, prohibida igual que en el catálogo del
#      banco — es donde un LLM alucina con más aplomo y una cifra inventada hace más daño.
#   2. Ejecutar órdenes. El robo-advisor propone; comprar y vender lo hace un humano.
#   3. Tareas ajenas (traducir, programar, el clima).
#
# Lo que ya NO se ataja acá: preguntar por un activo que el banco no ofrece (cripto,
# forex, acciones, índices) — eso ahora abre la Ruta B o C en vez de un rechazo. Lo
# dudoso se deja pasar al nodo correspondiente, cuyo prompt también acota el alcance,
# y al guardarraíl. Es una primera línea determinista, no la única.
_FUERA_DE_ALCANCE = re.compile(
    r"""
    \b(?:
        va\s+a\s+(?:subir|bajar|caer|crecer|rendir|valer) | subir[áa] | bajar[áa] |
        predic\w* | pron[oó]stic\w* | proyecci[oó]n | qu[eé]\s+va\s+a\s+pasar |
        cu[aá]nto\s+(?:valdr[áa]|estar[áa]) | precio\s+(?:futuro|de\s+ma[ñn]ana) |
        c[oó]mprame | v[eé]ndeme | ejecuta\w* | invierte\s+por\s+m[ií] |
        (?:compra|vende)\s+(?:por\s+m[ií]|mis?\b) |
        trad[uú]ce\w* | traducci[oó]n | (?:escribe|dame|genera)\s+(?:un\s+)?c[oó]digo |
        program[ae]\w* | receta | chiste | poema |
        clima | deporte\w* | f[uú]tbol | pel[ií]cula\w* | hor[oó]scopo
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Intención de noticias/actualidad: ahora el chatbot SÍ contesta, citando titulares
# reales de GNews (`feed_service`). Antes "noticias" caía en `_FUERA_DE_ALCANCE`; se sacó
# de ahí y se atiende con la Ruta D. Ojo: "qué VA A pasar" sigue siendo predicción
# (fuera de alcance) y se ataja antes; "qué ESTÁ pasando" es actualidad y entra aquí.
_NOTICIAS = re.compile(
    r"""
    \b(?:
        noticias? | novedades? | titulares? | prensa | actualidad |
        qu[eé]\s+est[áa]\s+pasando | qu[eé]\s+pasa\s+(?:con|en) | \bpas[óo]\b |
        \b[uú]ltim[oa]\s+hora
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
        oro\w* | orit[oa]s? | xau | plata\w* | xag |
        nasdaq | s\s*&\s*p\s*500? | spy | acci[oó]n\w* | bolsa | índice\w* | indice\w* |
        nikkei | jpn\s*225 | jap[oó]n | mercados?\s+(?:externos?|internacionales?) |
        cotizaci[oó]n\w*
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Un follow-up ANAFÓRICO: un mensaje que NO trae tema propio y se apoya en el turno
# anterior ("y eso?", "cómo lo ves?", "sí, el que me diste"). Es la ÚNICA señal que hace
# que una charla de mercados continúe en mercados sin repetir la palabra clave. Sin esto,
# la "memoria de ruta" se quedaba pegada en amarillo para TODA pregunta siguiente.
_ES_FOLLOWUP = re.compile(
    r"""
    ^\s*(?: y | e | pero | entonces | ah | ok | okay | vale | s[ií] )\b |
    \b(?:
        eso | es[eoa]s? | aquell[oa]s? | reci[eé]n |
        el\s+que\s+me\s+diste | lo\s+que\s+me\s+diste |
        (?:me\s+)?acabas\s+de\s+dar | c[oó]mo\s+lo\s+ves | qu[eé]\s+tal | m[aá]s\s+detalle
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
    (re.compile(r"oro\w*|orit[oa]s?|xau", re.IGNORECASE), "XAUUSD"),
    (re.compile(r"nikkei|jpn\s*225|jap[oó]n", re.IGNORECASE), "JPN225"),
    (re.compile(r"s\s*&\s*p\s*500?|spy|nasdaq|acci[oó]n\w*|bolsa", re.IGNORECASE), "SPY"),
    (re.compile(r"eur\s*/?\s*usd|euro|d[oó]lar", re.IGNORECASE), "EURUSD"),
]


def _clasificar_ruta(mensaje: str, ruta_previa: str | None = None) -> str:
    """El router determinista de las 4 rutas + rechazo. Ver el diagrama del módulo.

    `ruta_previa` es la ruta del ÚLTIMO turno del asistente (de `llm_interactions`). Sirve
    para los follow-ups sin palabra clave: "¿y cómo lo ves?" / "sí, el que me diste"
    después de una respuesta de mercados no traen ninguna palabra que el router reconozca,
    así que sin memoria caerían a la Ruta A (banco) y el agente diría "no tengo ese dato"
    aunque lo acabe de dar. Con memoria, un follow-up de una charla de mercados sigue en
    mercados; si además menciona el banco, pasa a mixto (que tiene ambos contextos).
    """
    if _FUERA_DE_ALCANCE.search(mensaje):
        return RUTA_RECHAZO
    # Noticias antes que mercados: "noticias del bitcoin" trae ambas palabras y la
    # intención manda (titulares, no cotización).
    if _NOTICIAS.search(mensaje):
        return RUTA_NOTICIAS
    if _MERCADO_EXTERNO.search(mensaje):
        return RUTA_MIXTO if _MENCION_BANCO.search(mensaje) else RUTA_EXTERNO
    # Sin palabra de mercado, el DEFAULT es banco. La charla de mercados solo CONTINÚA si el
    # mensaje es un follow-up anafórico (sin tema propio) Y no menciona el banco — así una
    # pregunta nueva cualquiera vuelve a banco en vez de quedarse pegada en amarillo.
    if (
        ruta_previa in (RUTA_MIXTO, RUTA_EXTERNO)
        and _ES_FOLLOWUP.search(mensaje)
        and not _MENCION_BANCO.search(mensaje)
    ):
        return ruta_previa
    return RUTA_BANCARIO


def _fuera_de_alcance(mensaje: str) -> bool:
    """Compat: el chequeo de alcance como booleano, para quien solo necesita eso
    (`whatsapp_controller.py`/`test_whatsapp.py`) sin pasar por las rutas."""
    return _clasificar_ruta(mensaje) == RUTA_RECHAZO


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
2. RESPONDE LA PREGUNTA QUE TE HACEN, y solo esa. Si te preguntan un concepto, explica el
   concepto; si preguntan por una tasa, da esa tasa. NUNCA recites la propuesta completa
   cuando te preguntaron otra cosa: los DATOS son el material del que sacas la respuesta,
   no un guion que se repite. Si la pregunta es ambigua, responde lo más probable en una
   frase y ofrece precisar.
3. SÉ BREVE Y CONTUNDENTE. Ve directo a la respuesta, sin rodeos ni relleno. Una
   explicación: máx. 60 palabras. Si el usuario pide una LISTA o comparación (sus
   productos, propuestas, subcuentas, opciones), responde así: una línea corta de intro y
   luego cada ítem en SU PROPIA LÍNEA empezando con "• ". Escribe en texto plano: nada de
   markdown (**negritas**, #, tablas) ni comillas angulares (« »).
4. Puedes ANALIZAR y COMPARAR los DATOS (por qué tu perfil no admite un banco, qué
   subcuenta es más conservadora, el trade-off tasa/calificación) y puedes RECOMENDAR
   DÓNDE INVERTIR, pero SOLO entre los productos del catálogo marcados como ELEGIBLES
   para su perfil. Al recomendar, di por qué: tasa, plazo, calificación del emisor y
   encaje con su perfil. Nunca recomiendes un producto NO elegible: si te lo piden,
   explica por qué su perfil no lo admite y ofrece la alternativa elegible más parecida.
5. Puedes explicar conceptos de inversión en términos CUALITATIVOS y generales (qué es
   renta fija vs. renta variable, por qué a más plazo suele pedirse más tasa, qué
   significa diversificar, qué implica una calificación de riesgo). En esas
   explicaciones NO escribas NINGUNA cifra: ni tasas de mercado, ni rendimientos
   históricos, ni fechas, ni porcentajes que no estén en los DATOS. Concepto sí,
   número no.
6. Lo que NO haces, y lo dices en una frase sin rodeos: predecir precios o mercados
   (cuánto valdrá algo, si algo va a subir), ejecutar órdenes de compra o venta, y
   tareas ajenas a la inversión. Si te preguntan por un activo que el banco no ofrece
   (cripto, acciones, forex): puedes decir en una frase qué es, aclarar que no está en
   el catálogo, y llevar la conversación a lo que sí puede tomar.
7. NUNCA prometas rentabilidad ni niegues el riesgo ("garantizado", "seguro", "sin
   riesgo", "vas a ganar" están prohibidos). Los retornos son referenciales.
8. Cuenta con letras ("los dos productos"), nunca con dígitos.
9. Cita cada producto por su nombre COMPLETO y EXACTO con banco (ej. "Depósito a Plazo
   Fijo 360 días de Banco Loja" — NUNCA "el DPF" ni abreviado). No uses "Fondo" o
   "Depósito" sueltos como palabra genérica; si no nombras uno puntual, di "ese producto"."""


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
catálogo del banco): acciones, forex, cripto e índices. Ya no solo recitas la cotización:
la LEES y la COMENTAS con criterio, siempre sobre el dato de HOY.

REGLA DE ORO (si rompes una, tu respuesta se descarta):
1. FUENTE DE VERDAD = las COTIZACIONES de abajo (Alpha Vantage). NO inventes ni
   recalcules ningún precio ni variación porcentual: todo número que escribas tiene que
   estar en las COTIZACIONES.
2. SÍ PUEDES OPINAR sobre los datos ACTUALES: leer el precio y la variación de hoy, decir
   si se mueve mucho o poco, y explicar en términos CUALITATIVOS qué tipo de activo es
   (cripto muy volátil, el oro como refugio, un índice de renta variable). Si abajo
   aparece el PERFIL del inversionista, relaciónalo cualitativamente ("tan volátil no
   calza con tu perfil conservador") — SIN citar cifras, productos ni bancos del catálogo.
3. HABLAS DEL PRESENTE, NO DEL FUTURO. PROHIBIDO: predecir hacia dónde va un precio ("va a
   subir", "conviene entrar ahora"), recomendar comprar o vender un activo externo, y
   prometer rentabilidad ("garantizado", "seguro", "sin riesgo", "vas a ganar"). El
   movimiento de hoy SÍ; el de mañana NO. Recomendar DÓNDE invertir es solo del banco.
4. AL GRANO Y CORTO: máx. 55 palabras, tono cercano, tuteando, texto plano (sin markdown,
   sin negritas). Si comentas VARIAS cotizaciones, una por línea empezando con "• "
   (símbolo, precio y variación de hoy, y una nota cualitativa breve). Si es un solo
   activo, 2 o 3 frases directas. Nada de introducciones ni párrafos de relleno.
5. Cierra con UNA frase avisando que es una simulación educativa fuera del catálogo del
   banco (ver el AVISO abajo — inclúyelo o parafraséalo, sin repetirlo dos veces)."""


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


def build_system_prompt_externo(
    cotizaciones: list[MarketQuote], perfil: str | None = None
) -> str:
    """Ruta C: 100% Alpha Vantage. El prompt NO incluye ninguna cifra del catálogo del
    banco, pero SÍ el nombre del perfil de riesgo (dato cualitativo) para que el agente
    pueda relacionar el activo externo con el inversionista sin citar números del banco."""
    contexto_perfil = (
        f"\n\nEl inversionista tiene perfil de riesgo: {perfil}. Es un dato CUALITATIVO "
        "para que puedas relacionar el activo con él; NO cites números, productos ni "
        "bancos del catálogo (aquí no los tienes)."
        if perfil
        else ""
    )
    return f"{_REGLA_DE_ORO_MERCADO}\n\n{_bloque_cotizaciones(cotizaciones)}{contexto_perfil}"


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


# ===========================================================================
# Ruta D (noticias): titulares reales de GNews (feed_service)
# ===========================================================================

# Cuántas noticias entran (al prompt y a la respuesta): pocas y claras. GNews devuelve
# hasta 10; 3 titulares es lo que cabe en un globo de chat sin volverlo un muro de texto.
_MAX_NOTICIAS = 3

# El aviso de la Ruta D: los titulares son de terceros, no del banco. Mismo espíritu que
# el DISCLAIMER_SIMULACION de mercados: la app CITA la noticia, no la avala. Corto a
# propósito: el globo ya trae los titulares, no hace falta un párrafo de descargo.
DISCLAIMER_NOTICIAS = "Noticias de terceros (GNews). No son una recomendación del banco."

# Palabra clave del mensaje → tema de `feed_service`. Si no matchea ninguno, el tema
# general de mercados. Los temas son los que ya sirve el feed (mercados/cripto/materias/
# ecuador), no se inventan aquí.
_TEMA_POR_PALABRA: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"bitcoin|btc|cripto\w*|ethereum|eth", re.IGNORECASE), "cripto"),
    (re.compile(r"oro\w*|orit[oa]s?|plata\w*|petr[oó]leo|materias?\s+primas?|commodit\w*", re.IGNORECASE), "materias"),
    (re.compile(r"ecuador|nacional(?:es)?|local(?:es)?|del\s+pa[ií]s", re.IGNORECASE), "ecuador"),
]


def _tema_noticias(mensaje: str) -> str:
    """El tema del feed que mejor calza con la pregunta (o el general si ninguno)."""
    for patron, tema in _TEMA_POR_PALABRA:
        if patron.search(mensaje):
            return tema
    return feed_service.TEMA_DEFAULT


def contexto_permitido_noticias(feed: FeedResponse) -> ContextoPermitido:
    """El conjunto citable de la Ruta D: los números que aparecen en los titulares REALES.

    A diferencia del banco y de mercados, aquí no hay catálogo cerrado: la noticia trae
    bancos y cifras del mundo real. Se permiten exactamente los números que la fuente
    escribió (título + descripción), para que el modelo pueda restarlos sin inventar uno
    nuevo. `validar_noticias` no revisa productos/emisores (serían falsos positivos)."""
    numeros = set()
    for n in feed.noticias:
        numeros.update(extraer_numeros(n.titulo))
        if n.descripcion:
            numeros.update(extraer_numeros(n.descripcion))
    return ContextoPermitido(numeros=numeros, instrumentos=set(), instituciones=set(), calificaciones=set())


def _resumen_titulo(titulo: str, limite: int = 52) -> str:
    """Título recortado para el chip: una cita corta, no el titular entero."""
    titulo = titulo.strip()
    return titulo if len(titulo) <= limite else titulo[: limite - 1].rstrip() + "…"


def _bloque_titulares_prompt(feed: FeedResponse) -> str:
    """Los titulares para el PROMPT: solo el título (sin descripción, para que el modelo no
    la copie y alargue la respuesta)."""
    lineas = "\n".join(f"- {n.titulo}" for n in feed.noticias[:_MAX_NOTICIAS])
    return f"TITULARES REALES (tema {feed.tema}):\n{lineas}"


_REGLA_DE_ORO_NOTICIAS = """Eres un asistente que ayuda a un inversionista a ubicarse en la
actualidad financiera. Abajo tienes TITULARES REALES (de GNews, cada uno con su fuente).

REGLA DE ORO (si rompes una, tu respuesta se descarta):
1. NO inventes noticias, datos ni cifras. Tu único material son los TITULARES de abajo.
2. Escribe UNA sola frase MUY corta (máx. 15 palabras), como ORACIÓN COMPLETA (sin dos
   puntos al final), que resuma de qué van los titulares en términos CUALITATIVOS. El
   usuario verá las noticias citadas como ENLACES debajo de tu frase, así que NO las
   enumeres ni las repitas. NO uses cifras ni afirmes hechos que no estén en los
   titulares. Ejemplo del tono: "El bitcoin y la cautela del mercado dominan hoy."
3. NO predices precios ni recomiendas comprar o vender, y NO prometes rentabilidad. Una
   noticia no es una recomendación de inversión.
4. Tono cercano, tuteando, texto plano (sin markdown). SOLO esa frase, sin saludo,
   sin cierre, sin relleno."""


def build_system_prompt_noticias(feed: FeedResponse) -> str:
    """Ruta D: la regla de oro de noticias + los titulares reales del tema."""
    return f"{_REGLA_DE_ORO_NOTICIAS}\n\n{_bloque_titulares_prompt(feed)}"


def _una_frase(texto: str, limite: int = 160) -> str:
    """Recorta la salida del LLM a UNA sola frase corta: la primera línea con contenido,
    sin viñeta. Es el blindaje contra un modelo que enumera los titulares aunque el prompt
    pida una sola frase — el cuerpo de noticias tiene que quedarse liviano SÍ O SÍ, porque
    la lista real vive en los source chips, no en el texto."""
    for linea in texto.splitlines():
        limpia = linea.strip().lstrip("•-*·–—").strip()
        if limpia:
            return limpia if len(limpia) <= limite else limpia[: limite - 1].rstrip() + "…"
    return "Esto es lo más reciente que encontré sobre el tema."


def _explicacion_noticias_determinista(feed: FeedResponse) -> str:
    """Fallback de la Ruta D (LLM caído/ausente): una frase que apunta a las citas, sin
    listar nada en el cuerpo. Pasa el guardarraíl por construcción (no tiene cifras)."""
    if not feed.noticias:
        return f"No encontré noticias disponibles ahora mismo.\n\n{DISCLAIMER_NOTICIAS}"
    return (
        "Aquí tienes los titulares más recientes; tócalos para abrir la noticia.\n\n"
        f"{DISCLAIMER_NOTICIAS}"
    )


def fuentes_citadas_noticias(feed: FeedResponse) -> list[dict[str, Any]]:
    """Source chips de la Ruta D: cada noticia como una CITA corta (título resumido +
    fuente) cuyo `record_id` es el link real — el front lo abre al tocarla."""
    return [
        {
            "table": "gnews",
            "record_id": n.url or n.titulo,
            "label": f"{_resumen_titulo(n.titulo)} · {n.fuente}",
        }
        for n in feed.noticias[:_MAX_NOTICIAS]
    ]


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

    # Señal explícita del botón "Recomendación de Mercados (IA)": si viene, el router
    # NO clasifica el mensaje — fuerza la Ruta C con exactamente estos símbolos.
    simbolos_forzados: list[str] | None

    # Ruta del ÚLTIMO turno del asistente (para los follow-ups sin palabra clave).
    ruta_previa: str | None

    # Ruta elegida por el router: "bancario" | "mixto" | "externo" | "noticias" | "rechazo".
    ruta: str
    # Cotizaciones de Alpha Vantage pedidas para este turno (vacío salvo en Ruta B/C).
    cotizaciones: list[MarketQuote]
    # Feed de noticias pedido para este turno (solo en Ruta D).
    feed: FeedResponse | None
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
    """Clasifica el mensaje en una de las 3 rutas (o rechazo). Ver el diagrama del módulo.

    Con `simbolos_forzados` (el botón "Recomendación de Mercados (IA)"), el router no
    clasifica nada: la ruta es C por señal explícita del cliente, no por adivinar el
    texto. Sigue pasando por el guardarraíl igual que cualquier otra ruta.
    """
    if state.get("simbolos_forzados"):
        return {"ruta": RUTA_EXTERNO}
    return {"ruta": _clasificar_ruta(state["mensaje"], state.get("ruta_previa"))}


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
    except Exception as exc:  # API caída, cuota agotada, timeout, paquete sin instalar…
        # El proveedor va en el mensaje a propósito: cuando este camino se dispara SIEMPRE
        # (una key vacía, un `pip install` que faltó), el usuario ve la misma explicación
        # determinista en cada turno y parece que el agente ignora sus preguntas. Sin saber
        # QUÉ proveedor falló, ese síntoma se confunde con un problema de prompt.
        log.warning(
            "El proveedor de IA (%s) falló en el agente: %s",
            provider or "default del .env",
            exc,
        )
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
    simbolos = state.get("simbolos_forzados") or _simbolos_de(state["mensaje"])
    cotizaciones = await market_data.obtener_cotizaciones(simbolos)
    # El nombre del perfil (dato cualitativo) para que pueda relacionar el activo con el
    # inversionista. NO entra ningún número del banco: el guardarraíl de esta ruta sigue
    # siendo solo Alpha Vantage.
    perfil = state["contexto"].datos.investor.perfil_riesgo.value
    system = build_system_prompt_externo(cotizaciones, perfil=perfil)
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


async def noticias_node(state: AgentState) -> AgentState:
    """Ruta D: titulares reales de GNews (feed_service). El cuerpo del mensaje es UNA sola
    frase (la del LLM, recortada por `_una_frase` para que no enumere aunque quiera); los
    titulares NO van en el texto, viven en los source chips (cita corta + link real).

    Contención: este nodo solo LEE (GNews) y devuelve texto. No escribe en `proposals`
    ni en ninguna otra tabla — igual que las Rutas B/C."""
    feed = await feed_service.obtener_feed(_tema_noticias(state["mensaje"]))
    ctx_permitido = contexto_permitido_noticias(feed)
    provider = state.get("provider")

    # Sin noticias o sin key: se muestran los titulares tal cual (o el respaldo del feed).
    if not feed.noticias or not hay_api_key(provider):
        return {
            "texto": _explicacion_noticias_determinista(feed),
            "modelo": PLANTILLA,
            "feed": feed,
            "ctx": ctx_permitido,
        }

    system = build_system_prompt_noticias(feed)
    try:
        intro = await _llamar_llm(
            system,
            state.get("historial", []),
            state["mensaje"],
            state.get("correccion", ""),
            provider=provider,
        )
    except Exception as exc:
        log.warning("El proveedor de IA falló en la Ruta D (noticias): %s", exc)
        return {
            "texto": _explicacion_noticias_determinista(feed),
            "modelo": PLANTILLA,
            "feed": feed,
            "ctx": ctx_permitido,
        }

    # Solo UNA frase (recortada) + el aviso. Los titulares NO van en el cuerpo: viven en
    # los source chips (cita corta + link real), para que el mensaje sea liviano. El
    # recorte es lo que garantiza que no aparezca una lista aunque el modelo la escriba.
    texto = f"{_una_frase(intro)}\n\n{DISCLAIMER_NOTICIAS}"
    return {"texto": texto, "modelo": modelo_activo(provider), "feed": feed, "ctx": ctx_permitido}


def guardrail_node(state: AgentState) -> AgentState:
    """Valida el texto contra el conjunto permitido de la ruta. Si falla, prepara el reintento."""
    # Si la respuesta ya vino de la plantilla (LLM caído/ausente), pasa por construcción.
    if state["modelo"] == PLANTILLA:
        return {"guardrail_passed": True, "motivos": []}

    # Noticias tienen su propio contrato: números solo de los titulares reales, sin
    # catálogo cerrado (la fuente cita bancos y cifras del mundo real que no inventamos).
    if state.get("ruta") == RUTA_NOTICIAS:
        veredicto = validar_noticias(state["texto"], state["ctx"])
    else:
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
    elif ruta == RUTA_NOTICIAS:
        feed = state.get("feed")
        texto = _explicacion_noticias_determinista(feed) if feed else TEXTO_RECHAZO
    else:
        texto = explicacion_determinista(state["contexto"].datos)
    return {"texto": texto, "modelo": PLANTILLA, "guardrail_passed": True}


# ===========================================================================
# Aristas condicionales
# ===========================================================================

_NODO_DE_RUTA = {
    RUTA_BANCARIO: "qa",
    RUTA_MIXTO: "mixto",
    RUTA_EXTERNO: "mercado",
    RUTA_NOTICIAS: "noticias",
}


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
    g.add_node("noticias", noticias_node)
    g.add_node("guardrail", guardrail_node)
    g.add_node("refuse", refuse_node)
    g.add_node("fallback", fallback_node)

    g.set_entry_point("router")
    g.add_conditional_edges(
        "router",
        _tras_router,
        {"qa": "qa", "mixto": "mixto", "mercado": "mercado", "noticias": "noticias", "refuse": "refuse"},
    )
    g.add_edge("qa", "guardrail")
    g.add_edge("mixto", "guardrail")
    g.add_edge("mercado", "guardrail")
    g.add_edge("noticias", "guardrail")
    g.add_conditional_edges(
        "guardrail",
        _tras_guardrail,
        {
            "qa": "qa",
            "mixto": "mixto",
            "mercado": "mercado",
            "noticias": "noticias",
            "fallback": "fallback",
            "fin": END,
        },
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
    simbolos_forzados: list[str] | None = None,
    ruta_previa: str | None = None,
) -> AgentState:
    """Corre el grafo para un turno y devuelve el estado final.

    `contexto` es todo lo que el agente conoce del inversionista (de la base); `mensaje`
    la pregunta; `historial` los turnos previos (para continuidad); `provider` el modelo
    elegido en el front (None = el default del .env). El router decide adentro del
    grafo si el turno usa `contexto`, Alpha Vantage, o ambos (ver el diagrama arriba) —
    salvo que `simbolos_forzados` venga con algo: ahí la Ruta C es obligatoria (botón
    "Recomendación de Mercados (IA)"), sin pasar por el clasificador de texto.
    """
    estado: AgentState = {
        "mensaje": mensaje,
        "contexto": contexto,
        "ctx": contexto_permitido_agente(contexto),  # override si la ruta es B/C/D
        "historial": historial or [],
        "provider": provider,
        "simbolos_forzados": simbolos_forzados,
        "ruta_previa": ruta_previa,
        "retry_count": 0,
    }
    final = await _GRAFO.ainvoke(estado)
    ruta = final.get("ruta", RUTA_BANCARIO)
    cotizaciones = final.get("cotizaciones", [])
    texto = final.get("texto", "")

    # Los chips se calculan sobre el texto YA generado: solo las fuentes que citó.
    if final.get("modelo") == REFUSE:
        final["sources"] = []
    elif ruta == RUTA_NOTICIAS:
        feed = final.get("feed")
        final["sources"] = fuentes_citadas_noticias(feed) if feed else []
    elif ruta == RUTA_EXTERNO:
        final["sources"] = fuentes_citadas_mercado(cotizaciones, texto)
    elif ruta == RUTA_MIXTO:
        final["sources"] = fuentes_citadas(contexto, texto) + fuentes_citadas_mercado(cotizaciones, texto)
    else:
        final["sources"] = fuentes_citadas(contexto, texto)
    return final
