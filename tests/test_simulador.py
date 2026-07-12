"""El simulador: **el motor elige, la IA explica** — y la IA no puede inventar una cifra.

Dos cosas que sostienen la pantalla y que no se pueden romper sin darse cuenta:

1. `elegir_recomendado` es DETERMINISTA. La opción destacada la decide Python sobre las
   filas de Postgres, no el LLM; la misma fila que el front resalta es la que el prompt le
   dice al modelo que explique. Si esto se moviera, la tarjeta y el texto se contradirían.
2. La recomendación determinista —la que se muestra cuando el proveedor de IA está caído o
   alucinó dos veces— **pasa el mismo guardarraíl** que se le exige al modelo. Es el piso
   de calidad de la demo: sin API key, el usuario sigue viendo números correctos.

No hace falta base de datos: las filas son un dato de entrada.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from src.controllers import agent_controller
from src.controllers.catalog_controller import elegir_recomendado
from src.models.agent import SimuladorRequest
from src.models.auth import CurrentUser, Rol
from src.models.catalog import CatalogoTasas, TasaInstrumento
from src.services import simulator_ai
from src.services.guardrails import validar
from src.services.simulator_ai import (
    Simulacion,
    contexto_permitido,
    fuentes_citadas,
    recomendacion_determinista,
)


@contextmanager
def _sin_base_de_datos() -> Iterator[None]:
    """Reemplaza a `get_connection`: estos tests no tocan Postgres."""
    yield None


def _tasa(
    code: str,
    producto: str,
    institucion: str,
    calificacion: str,
    rating_tier: int,
    tasa_anual: float,
    *,
    plazo_dias: int | None = 360,
    monto_minimo: float | None = 500.0,
    elegible: bool | None = True,
    motivo: str | None = None,
    cumple_monto_minimo: bool | None = True,
    interes_estimado: float | None = 100.0,
    monto_final: float | None = 10100.0,
) -> TasaInstrumento:
    """Una fila tal como la devuelve `/api/catalog/rates?monto=`."""
    return TasaInstrumento(
        code=code,
        producto=producto,
        product_type="deposito_plazo",
        institucion=institucion,
        calificacion=calificacion,
        rating_tier=rating_tier,
        fuente_calificacion="PCR",
        fecha_calificacion=None,
        tasa_anual=tasa_anual,
        plazo_dias=plazo_dias,
        monto_minimo=monto_minimo,
        elegible=elegible,
        motivo_no_elegible=motivo,
        cumple_monto_minimo=cumple_monto_minimo,
        interes_estimado=interes_estimado,
        monto_final=monto_final,
    )


REGLA = "Tu perfil moderado solo admite emisores con calificación AAA o AA."

# Un catálogo con las tres trampas: la tasa más alta está BLOQUEADA por el perfil, la
# siguiente pide un mínimo que el monto no alcanza, y hay un empate de tasa entre dos
# emisores con distinta calificación.
CATALOGO = [
    _tasa("MEJOR_BLOQUEADO", "Fondo de Crecimiento", "Banco Riesgoso", "A", 4, 12.0,
          elegible=False, motivo=REGLA),
    _tasa("CARO", "Fondo Balanceado", "Banco Guayaquil", "AAA", 1, 10.0,
          monto_minimo=25000.0, cumple_monto_minimo=False),
    _tasa("EMPATE_AA", "Depósito a Plazo Fijo 360 días", "Banco Loja", "AA", 2, 9.4,
          interes_estimado=940.0, monto_final=10940.0),
    _tasa("EMPATE_AAA", "Depósito a Plazo Fijo 720 días", "Produbanco", "AAA", 1, 9.4,
          plazo_dias=720, interes_estimado=1854.25, monto_final=11854.25),
    _tasa("PEOR", "Depósito a Plazo Fijo 180 días", "Banco Pichincha", "AAA", 1, 5.8,
          plazo_dias=180, interes_estimado=286.03, monto_final=10286.03),
]


def _simulacion(seleccionado: TasaInstrumento | None = None) -> Simulacion:
    tasas = list(CATALOGO)
    return Simulacion(
        monto=10000.0,
        plazo_dias=360,
        perfil="moderado",
        tasas=tasas,
        recomendado=elegir_recomendado(tasas),
        seleccionado=seleccionado,
    )


# ===========================================================================
# 1. El motor elige, y elige bien
# ===========================================================================


def test_no_recomienda_lo_que_el_perfil_no_admite() -> None:
    """La tasa más alta del catálogo está bloqueada: recomendarla sería saltarse la regla."""
    assert elegir_recomendado(CATALOGO).code != "MEJOR_BLOQUEADO"


def test_no_recomienda_lo_que_el_monto_no_alcanza() -> None:
    """Elegible, pero pide un mínimo de USD 25.000 sobre un monto de USD 10.000."""
    assert elegir_recomendado(CATALOGO).code != "CARO"


def test_a_igual_tasa_gana_el_emisor_mejor_calificado() -> None:
    """9,4% en los dos: el desempate es la calificación, no el orden de las filas."""
    assert elegir_recomendado(CATALOGO).code == "EMPATE_AAA"


def test_sin_monto_no_hay_recomendacion() -> None:
    """El comparador (sin `?monto=`) muestra el catálogo, no un consejo."""
    sin_monto = [_tasa("X", "Fondo Balanceado", "Banco Guayaquil", "AAA", 1, 8.0,
                       cumple_monto_minimo=None, interes_estimado=None, monto_final=None)]
    assert elegir_recomendado(sin_monto) is None


# ===========================================================================
# 2. La IA explica, y no puede inventar
# ===========================================================================


def test_la_recomendacion_determinista_pasa_su_propio_guardarrail() -> None:
    """El fallback es lo que se muestra cuando el LLM falla: tiene que ser válido SIEMPRE.

    Ojo, no es un test trivial: `validar_lexico` rechaza «garantiz*» sin entender la
    negación, así que un disclaimer mal redactado ("no garantiza rentabilidad") haría
    fallar este test — que es exactamente para lo que está.
    """
    sim = _simulacion()
    veredicto = validar(recomendacion_determinista(sim), contexto_permitido(sim))
    assert veredicto.ok, veredicto.motivos


def test_el_determinista_es_valido_tambien_al_comparar_con_lo_seleccionado() -> None:
    """El usuario se cambió a un emisor que su perfil no admite: se explica, sin inventar."""
    sim = _simulacion(seleccionado=CATALOGO[0])
    texto = recomendacion_determinista(sim)
    assert "Banco Riesgoso" in texto and REGLA in texto
    veredicto = validar(texto, contexto_permitido(sim))
    assert veredicto.ok, veredicto.motivos


def test_el_determinista_es_valido_cuando_nada_es_elegible() -> None:
    sim = Simulacion(
        monto=100.0, plazo_dias=180, perfil="conservador",
        tasas=[CATALOGO[0]], recomendado=None, seleccionado=None,
    )
    veredicto = validar(recomendacion_determinista(sim), contexto_permitido(sim))
    assert veredicto.ok, veredicto.motivos


def test_un_monto_final_inventado_se_rechaza() -> None:
    """La alucinación que importa: cifras plausibles que no están en ninguna fila."""
    sim = _simulacion()
    texto = (
        "Te recomiendo Depósito a Plazo Fijo 720 días de Produbanco: con USD 10.000 "
        "terminas con USD 13.500."  # 13.500 no existe en el catálogo
    )
    veredicto = validar(texto, contexto_permitido(sim))
    assert not veredicto.ok
    assert any("13500" in m for m in veredicto.motivos)


def test_un_banco_inventado_se_rechaza() -> None:
    sim = _simulacion()
    veredicto = validar(
        "Te recomiendo el Fondo Balanceado de Banco Inventado (AAA).", contexto_permitido(sim)
    )
    assert not veredicto.ok


def test_los_chips_citan_solo_lo_que_el_texto_nombro() -> None:
    """El source chip es la prueba de la afirmación: no se pegan fuentes de adorno."""
    sim = _simulacion()
    chips = fuentes_citadas(
        sim, "Te recomiendo Depósito a Plazo Fijo 720 días de Produbanco, al 9,4%."
    )
    assert [c["record_id"] for c in chips] == ["EMPATE_AAA"]


# ===========================================================================
# 4. La IA solo ve lo que el usuario ve
# ===========================================================================


async def test_el_comparador_le_oculta_a_la_ia_los_plazos_que_filtro(monkeypatch) -> None:
    """`todos_los_plazos` viaja en el body y el controlador lo REENVÍA, no lo fija.

    El simulador muestra todo el catálogo (el plazo es su horizonte) pero el comparador
    filtra por plazo: si acá se volviera a hardcodear `True`, la IA recomendaría un
    depósito a 720 días mientras la lista en pantalla solo tiene los de 360. El texto
    seguiría sin inventar ni un número y aun así mentiría.
    """
    visto: dict[str, object] = {}

    async def _listar_tasas(investor_id, monto, plazo_dias, todos_los_plazos=False):
        visto["todos_los_plazos"] = todos_los_plazos
        return CatalogoTasas(
            perfil="moderado", monto=monto, plazo_dias=plazo_dias, tasas=list(CATALOGO)
        )

    async def _recomendar(sim, provider=None):
        return simulator_ai.Recomendacion(
            texto=recomendacion_determinista(sim),
            modelo=simulator_ai.PLANTILLA,
            guardrail_passed=True,
        )

    # El archivado en `llm_interactions` necesita Postgres y no es lo que se prueba acá.
    monkeypatch.setattr(agent_controller, "get_connection", _sin_base_de_datos)
    monkeypatch.setattr(agent_controller, "_ultima_sesion", lambda conn, investor_id: None)
    monkeypatch.setattr(agent_controller, "_archivar_simulacion", lambda *a, **k: None)
    monkeypatch.setattr(agent_controller.catalog_controller, "listar_tasas", _listar_tasas)
    monkeypatch.setattr(agent_controller.simulator_ai, "recomendar", _recomendar)

    usuario = CurrentUser(id="inv-1", full_name="Ana", role=Rol.INVESTOR)

    await agent_controller.recomendar_simulacion(
        SimuladorRequest(monto=10000, plazo_dias=360, todos_los_plazos=False), usuario
    )
    assert visto["todos_los_plazos"] is False

    # Y el simulador, que sí quiere el catálogo entero, lo sigue recibiendo (es el default).
    await agent_controller.recomendar_simulacion(
        SimuladorRequest(monto=10000, plazo_dias=360), usuario
    )
    assert visto["todos_los_plazos"] is True
