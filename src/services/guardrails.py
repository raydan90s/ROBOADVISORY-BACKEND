"""Guardarraíles anti-alucinación. Se escriben ANTES que el LLM porque son el contrato
que el LLM tiene que cumplir, no un parche posterior.

La idea es simple y verificable: el texto que genera el modelo **no puede contener nada
que no exista en la base**. Tres cierres, cada uno con su test:

1. `validar_numeros`  — todo número del texto tiene que estar en un conjunto permitido
   (los % de la propuesta, los USD de cada línea y el total, el puntaje, los umbrales,
   los retornos y los plazos de los productos citados). Un número de más → rechazo.
2. `validar_lexico`   — ninguna promesa de rentabilidad ("garantizado", "sin riesgo").
3. `validar_catalogo` — **dos catálogos cerrados**: ningún producto fuera de `instruments`
   y ningún banco ni calificación fuera de `institutions`. Que la IA se invente un
   "Banco XYZ con calificación AAA" es tan grave como que se invente un porcentaje.

El validador no corrige el texto: lo **rechaza**. Quien decide qué hacer con el rechazo
(reintentar, o caer a la explicación determinista) es `ai_agent.py`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Tolerancia al comparar: el modelo puede redondear 7,64% a 7,6%. Un centavo o una
# décima no es una alucinación; 8.000 donde debía decir 12.000 sí lo es.
TOLERANCIA = Decimal("0.05")


@dataclass(frozen=True)
class Veredicto:
    """Resultado del guardarraíl. `motivos` es lo que se guarda para auditar."""

    ok: bool
    motivos: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class ContextoPermitido:
    """Todo lo que el texto TIENE derecho a decir. Sale de la base, no de la imaginación."""

    numeros: set[Decimal]
    instrumentos: set[str]      # instruments.name
    instituciones: set[str]     # institutions.name
    calificaciones: set[str]    # institutions.credit_rating


# ===========================================================================
# 1. Números
# ===========================================================================

_NUMERO = re.compile(r"\d[\d.,]*")


def _normalizar_numero(token: str) -> Decimal | None:
    """Convierte '12.000', '9,4' o '1.234,56' al Decimal que representan.

    La ambigüedad entre el punto de miles (es-EC) y el punto decimal (en-US) se resuelve
    con reglas fijas, no adivinando: si hay ambos separadores, el ÚLTIMO es el decimal;
    si solo hay uno y agrupa de tres en tres, es separador de miles.

    Ser estricto acá es el punto: si aceptáramos las dos lecturas de '15.000' (quince mil
    y quince), un monto inventado se colaría por la puerta del puntaje.
    """
    token = token.strip(".,")
    if not token:
        return None

    tiene_punto, tiene_coma = "." in token, "," in token

    if tiene_punto and tiene_coma:
        decimal_sep = "." if token.rfind(".") > token.rfind(",") else ","
        miles_sep = "," if decimal_sep == "." else "."
        token = token.replace(miles_sep, "").replace(decimal_sep, ".")
    elif tiene_punto:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", token):  # 12.000 → doce mil
            token = token.replace(".", "")
    elif tiene_coma:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", token):  # 12,000 → doce mil
            token = token.replace(",", "")
        else:  # 9,4 → nueve coma cuatro
            token = token.replace(",", ".")

    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def extraer_numeros(texto: str) -> list[Decimal]:
    """Todos los números del texto, ya normalizados."""
    numeros = (_normalizar_numero(t) for t in _NUMERO.findall(texto))
    return [n for n in numeros if n is not None]


def validar_numeros(texto: str, valores_permitidos: set[Decimal]) -> Veredicto:
    """Rechaza el texto si cita un número que no salió de la base."""
    intrusos = [
        n
        for n in extraer_numeros(texto)
        if not any(abs(n - p) <= TOLERANCIA for p in valores_permitidos)
    ]
    if intrusos:
        return Veredicto(
            False,
            [
                f"Número inventado: {n} no está en el conjunto permitido."
                for n in dict.fromkeys(intrusos)  # sin repetir, conservando el orden
            ],
        )
    return Veredicto(True)


# ===========================================================================
# 2. Léxico prohibido
# ===========================================================================

# El robo-advisor recomienda; no ejecuta ni promete (HU2, criterio 3).
_PROHIBIDO: list[tuple[str, str]] = [
    (r"garantiz\w*", "promete una garantía"),
    (r"asegur(?!adora|amiento)\w*", "afirma que asegura un resultado"),
    (r"sin riesgo", "niega el riesgo"),
    (r"libre de riesgo", "niega el riesgo"),
    (r"riesgo cero", "niega el riesgo"),
    (r"vas? a ganar", "promete una ganancia"),
    (r"ganar[áa]s", "promete una ganancia"),
    (r"rendimiento seguro", "promete una ganancia"),
    (r"sin p[ée]rdidas?", "niega la posibilidad de pérdida"),
    (r"(te )?recomiendo comprar ya", "empuja a ejecutar una orden"),
]


def validar_lexico(texto: str) -> Veredicto:
    """Rechaza el texto si promete rentabilidad o niega el riesgo."""
    bajo = texto.lower()
    motivos = [
        f"Léxico prohibido ({razon}): «{m.group(0)}»"
        for patron, razon in _PROHIBIDO
        if (m := re.search(patron, bajo))
    ]
    return Veredicto(not motivos, motivos)


# ===========================================================================
# 3. Catálogos cerrados: productos, emisores y calificaciones
# ===========================================================================

# Un producto citado empieza por una de estas palabras. No intentamos entender el texto:
# capturamos el sintagma y exigimos que empiece por un nombre real del catálogo.
_PRODUCTO = re.compile(
    r"\b(?:Dep[óo]sito|DPF|Fondo)\b[^,.;:()\n]*", re.IGNORECASE
)
# Un emisor inventado casi siempre se llama "Banco ..." / "Cooperativa ...". El nombre
# puede llevar conectores en minúscula ("Banco del Pacífico"), así que hay que aceptarlos:
# sin ellos, "Banco del Banco Falso" se escaparía por no empezar en mayúscula.
# Los emisores reales que no siguen el patrón (Produbanco) se validan igual: si el nombre
# está en el catálogo no hay nada que reportar, y si no está, no lo capturamos como emisor.
_EMISOR = re.compile(
    r"\b(?:Banco|Cooperativa|Mutualista)"
    r"(?:\s+(?:de|del|la|los|el)\b|\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]*)+"
)
# Solo calificaciones de 2+ letras (AA, AAA, con signo). Una "A" suelta no se busca a
# propósito: en español una oración puede empezar con "A" y sería un falso positivo.
# El cierre es un lookahead, no un \b: tras un '+' el \b no existe y "AA+" se leería
# como "AA" — es decir, una calificación inventada se reportaría como otra distinta.
_CALIFICACION = re.compile(r"\b(?:AAA|AA)[+-]?(?![\w+-])")


def _plano(texto: str) -> str:
    """Sin tildes y en minúsculas: comparar catálogo no debe depender de la ortografía."""
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.lower().split())


def validar_catalogo(texto: str, ctx: ContextoPermitido) -> Veredicto:
    """Rechaza productos, emisores o calificaciones que no existan en la base."""
    motivos: list[str] = []

    instrumentos = {_plano(n) for n in ctx.instrumentos}
    for cita in _PRODUCTO.findall(texto):
        candidato = _plano(cita)
        # "Fondo Balanceado de Banco Guayaquil" es válido: empieza por un nombre real.
        # "Fondo Tecnológico Global" no empieza por ninguno.
        if not any(candidato.startswith(nombre) for nombre in instrumentos):
            motivos.append(f"Producto fuera del catálogo: «{cita.strip()}»")

    instituciones = {_plano(n) for n in ctx.instituciones}
    for cita in _EMISOR.findall(texto):
        candidato = _plano(cita)
        if not any(
            candidato == emisor or candidato.startswith(emisor + " ")
            for emisor in instituciones
        ):
            motivos.append(f"Emisor fuera del catálogo: «{cita.strip()}»")

    for cita in _CALIFICACION.findall(texto):
        if cita not in ctx.calificaciones:
            motivos.append(f"Calificación fuera del catálogo: «{cita}»")

    return Veredicto(not motivos, motivos)


# ===========================================================================
# El guardarraíl completo
# ===========================================================================


def validar(texto: str, ctx: ContextoPermitido) -> Veredicto:
    """Los tres cierres juntos. Basta que uno falle para rechazar el texto."""
    motivos: list[str] = []
    for veredicto in (
        validar_numeros(texto, ctx.numeros),
        validar_lexico(texto),
        validar_catalogo(texto, ctx),
    ):
        motivos.extend(veredicto.motivos)
    return Veredicto(not motivos, motivos)
