"""⭐ El sistema, validado contra el documento oficial del reto.

`test_caso_juan_perez` toma las respuestas que aparecen en el ejemplo del reto y verifica
que el motor —sin que nadie escriba un número a mano— llegue a las mismas cifras:
12/15 puntos → Moderado → 60/40 → USD 12.000 y USD 8.000.

Que esas cifras salgan solas de `scoring_rules` + `allocation_template_items` es la prueba
de que la fuente de verdad son las reglas de la base y no una constante en el código.
"""

from decimal import Decimal

import pytest

from src.config.database import fetch_all, fetch_one

# Las respuestas de Juan Pérez tal como están en el documento del reto.
RESPUESTAS_JUAN = {
    "objetivo": "crecer",
    "horizonte": "medio",
    "liquidez": "no",
    "tolerancia": "esperar",
    "preferencia": "seguridad_rentable",
}


def _puntaje_de(respuestas: dict[str, str]) -> int:
    """Puntúa contra `scoring_rules` de la versión activa. Cero aritmética hardcodeada."""
    filas = fetch_all(
        """
        select q.code as q, o.code as o, sr.points
        from public.scoring_rules sr
        join public.question_options o on o.id = sr.question_option_id
        join public.questions q        on q.id = o.question_id
        join public.rules_versions rv  on rv.id = sr.rules_version_id
        where rv.is_active
        """
    )
    puntos = {(f["q"], f["o"]): f["points"] for f in filas}
    return sum(puntos[(q, o)] for q, o in respuestas.items())


def _perfil_de(puntaje: int) -> str:
    fila = fetch_one(
        """
        select rp.code
        from public.profile_thresholds pt
        join public.risk_profiles rp  on rp.id = pt.risk_profile_id
        join public.rules_versions rv on rv.id = pt.rules_version_id
        where rv.is_active and %s between pt.min_score and pt.max_score
        """,
        (puntaje,),
    )
    assert fila, f"El puntaje {puntaje} no cae en ningún rango de perfil."
    return fila["code"]


def test_caso_juan_perez() -> None:
    """⭐ El caso del documento del reto, de punta a punta: 12 pts → Moderado → 60/40 → 12.000/8.000."""
    puntaje = _puntaje_de(RESPUESTAS_JUAN)
    assert puntaje == 12, f"El documento del reto da 12 puntos, el motor dio {puntaje}."

    perfil = _perfil_de(puntaje)
    assert perfil == "moderado"

    plantilla = fetch_all(
        """
        select i.code, ati.percentage
        from public.allocation_templates at
        join public.risk_profiles rp              on rp.id = at.risk_profile_id
        join public.rules_versions rv             on rv.id = at.rules_version_id
        join public.allocation_template_items ati on ati.template_id = at.id
        join public.instruments i                 on i.id = ati.instrument_id
        where rv.is_active and rp.code = 'moderado'
        order by ati.percentage desc
        """
    )
    porcentajes = [f["percentage"] for f in plantilla]
    assert porcentajes == [Decimal(60), Decimal(40)], f"La plantilla moderada no es 60/40: {porcentajes}"

    # Y sobre los USD 20.000 del ejemplo, los montos del documento.
    monto = Decimal(20000)
    montos = [round(monto * p / 100, 2) for p in porcentajes]
    assert montos == [Decimal("12000.00"), Decimal("8000.00")]


@pytest.mark.parametrize(
    ("puntaje", "perfil"),
    [
        (5, "conservador"),
        (8, "conservador"),   # borde alto del conservador
        (9, "moderado"),      # borde bajo del moderado
        (12, "moderado"),     # borde alto del moderado
        (13, "agresivo"),     # borde bajo del agresivo
        (15, "agresivo"),
    ],
)
def test_umbrales_de_borde(puntaje: int, perfil: str) -> None:
    """Los bordes 8/9 y 12/13: donde un off-by-one cambiaría el perfil de un cliente."""
    assert _perfil_de(puntaje) == perfil


def test_el_rango_de_puntajes_no_tiene_huecos() -> None:
    """Todo puntaje posible (5–15) cae en exactamente un perfil. Sin esto, un cliente
    real podría quedar sin perfil y sin propuesta."""
    for puntaje in range(5, 16):
        assert _perfil_de(puntaje)
