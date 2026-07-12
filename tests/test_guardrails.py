"""⭐ El test estrella: el sistema es INCAPAZ de mostrar un número o un emisor inventado.

El criterio de evaluación #3 (mitigación de riesgos / antialucinación) no se promete: se
demuestra acá. Cada caso es una alucinación plausible —la clase de error que un LLM comete
de verdad— y el guardarraíl la rechaza.

No se necesita base de datos: el contexto permitido es un dato de entrada.
"""

from decimal import Decimal

import pytest

from src.services.guardrails import (
    ContextoPermitido,
    validar,
    validar_cantidades_en_letras,
    validar_catalogo,
    validar_lexico,
    validar_numeros,
)

# El caso de Juan Pérez: USD 20.000, 12/15 pts, 60/40 sobre dos productos reales.
CTX = ContextoPermitido(
    numeros={
        Decimal(n)
        for n in ("100", "12", "15", "9", "20000", "12000", "8000", "60", "40", "7.2", "8.3", "360", "1")
    },
    instrumentos={"Depósito a Plazo Fijo 360 días", "Fondo Balanceado"},
    instituciones={"Banco Pichincha", "Banco Guayaquil"},
    calificaciones={"AAA"},
)

TEXTO_BUENO = (
    "Hola Juan: tu perfil es moderado con 12 de 15 puntos (el rango va de 9 a 12). "
    "Sobre USD 20.000 te proponemos 60% (USD 12.000) en Depósito a Plazo Fijo 360 días "
    "de Banco Pichincha (AAA) y 40% (USD 8.000) en Fondo Balanceado de Banco Guayaquil "
    "(AAA). Los retornos referenciales son 7,2% y 8,3% anual. Esta propuesta no "
    "constituye una orden de compra ni una promesa de rentabilidad."
)


def test_texto_solo_con_datos_reales_es_aceptado() -> None:
    """El piso: un texto fiel a la base pasa. Si esto fallara, el guardarraíl sería inútil."""
    veredicto = validar(TEXTO_BUENO, CTX)
    assert veredicto.ok, veredicto.motivos


# ---------------------------------------------------------------------------
# Números inventados
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "intruso"),
    [
        ("Te proponemos 65% en Fondo Balanceado.", "65 no es el 60 de la plantilla"),
        ("Son USD 13.000 en Depósito a Plazo Fijo 360 días.", "13.000 ≠ 12.000"),
        ("Tu puntaje es 14 de 15.", "el puntaje es 12"),
        ("El retorno referencial es 9,8% anual.", "9,8 no es el retorno de ningún producto"),
        ("El plazo es de 720 días.", "el plazo del producto es 360"),
    ],
)
def test_numero_inventado_es_rechazado(texto: str, intruso: str) -> None:
    assert not validar_numeros(texto, CTX.numeros).ok, f"Debió rechazar: {intruso}"


def test_el_monto_total_no_puede_recalcularse() -> None:
    """La alucinación más peligrosa: el modelo "suma" y le da otra cifra al cliente."""
    texto = "Sobre USD 20.000 te asignamos USD 12.000 y USD 9.000."  # 9.000 no existe
    veredicto = validar_numeros(texto, CTX.numeros)
    assert not veredicto.ok
    assert any("9000" in m or "9.000" in m for m in veredicto.motivos)


def test_miles_y_decimales_no_se_confunden() -> None:
    """'12.000' es doce mil, no doce: si se leyeran igual, un monto falso pasaría colado."""
    assert validar_numeros("USD 12.000 en el DPF.", CTX.numeros).ok
    # 12.500 no está permitido aunque '12' sí lo esté (es el puntaje).
    assert not validar_numeros("USD 12.500 en el DPF.", CTX.numeros).ok


# ---------------------------------------------------------------------------
# Cantidades escritas en letras: el número que el validador no puede leer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "trampa"),
    [
        (
            "El plazo es de trescientos sesenta días.",
            "360 en letras esquiva al validador numérico",
        ),
        (
            "Rinde once coma cinco por ciento, unos mil cien dólares.",
            "una tasa y un monto inventados, escritos en palabras",
        ),
        (
            "Tu dinero queda comprometido por setecientos veinte días.",
            "720 en letras — el caso visto en producción",
        ),
    ],
)
def test_cantidad_en_letras_es_rechazada(texto: str, trampa: str) -> None:
    """En letras, una cifra no se puede comparar contra la base: se rechaza entera."""
    assert not validar_cantidades_en_letras(texto).ok, f"Debió rechazar: {trampa}"


def test_conteos_chicos_en_letras_si_pasan() -> None:
    """«las dos opciones» es conversación, no un dato: el prompt lo pide así a propósito."""
    assert validar_cantidades_en_letras(
        "Las dos opciones tienen la misma calificación y ambas admiten tu monto."
    ).ok


def test_el_texto_bueno_sigue_pasando_con_el_cierre_nuevo() -> None:
    """El cierre no puede romper el piso: el texto fiel a la base sigue siendo válido."""
    assert validar(TEXTO_BUENO, CTX).ok


# ---------------------------------------------------------------------------
# Promesas de rentabilidad
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto",
    [
        "Es una rentabilidad garantizada del 7,2%.",
        "Este producto es totalmente seguro y sin riesgo.",
        "Con esta cartera vas a ganar más que en el banco.",
        "Te aseguro que no vas a tener pérdidas.",
    ],
)
def test_promesa_de_rentabilidad_es_rechazada(texto: str) -> None:
    assert not validar_lexico(texto).ok


def test_hablar_de_riesgo_con_honestidad_no_es_rechazado() -> None:
    """El validador prohíbe prometer, no prohíbe la palabra 'riesgo'."""
    assert validar_lexico(
        "Esta cartera tiene riesgo medio: el valor puede subir o bajar."
    ).ok


# ---------------------------------------------------------------------------
# Catálogos cerrados: productos, emisores y calificaciones
# ---------------------------------------------------------------------------


def test_banco_inexistente_es_rechazado() -> None:
    """Un emisor inventado es tan grave como un porcentaje inventado."""
    texto = "Te proponemos un depósito en Banco Fantasma con calificación AAA."
    veredicto = validar_catalogo(texto, CTX)
    assert not veredicto.ok
    assert any("Banco Fantasma" in m for m in veredicto.motivos)


def test_producto_fuera_del_catalogo_es_rechazado() -> None:
    texto = "Te recomendamos el Fondo Tecnológico Global de Banco Pichincha."
    veredicto = validar_catalogo(texto, CTX)
    assert not veredicto.ok
    assert any("Tecnol" in m for m in veredicto.motivos)


def test_calificacion_inventada_es_rechazada() -> None:
    """Banco Pichincha existe y es AAA; decir que es AA+ es inventar el dato."""
    texto = "Depósito a Plazo Fijo 360 días de Banco Pichincha (AA+)."
    veredicto = validar_catalogo(texto, CTX)
    assert not veredicto.ok
    assert any("AA+" in m for m in veredicto.motivos)


def test_un_texto_perfecto_salvo_un_banco_falso_igual_se_rechaza() -> None:
    """Lo que hace peligrosa a una alucinación es venir envuelta en datos correctos."""
    texto = TEXTO_BUENO.replace("Banco Guayaquil", "Banco del Pacífico Andino")
    assert not validar(texto, CTX).ok
