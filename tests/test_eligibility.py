"""⭐ La recomendación respeta un criterio objetivo de solidez del emisor.

"Ningún producto de la plantilla conservadora viene de una institución peor que AAA-."
Eso no es una opinión del modelo: es una comparación de enteros (`rating_tier`) contra una
regla versionada (`profile_institution_rules`), y se verifica con un assert.

El segundo test es el interesante: prueba que la regla **muerde de verdad**. Entre los
depósitos a plazo, el de Banco Loja paga la mejor tasa (9,4%) y viene de la institución peor
calificada (AA): es exactamente el trade-off entre rentabilidad y riesgo de contraparte. Si
la regla fuera un adorno, el perfil conservador podría llevárselo. No puede; el agresivo sí.
"""

from src.config.database import fetch_all, fetch_one


def test_todo_producto_recomendado_cumple_la_calificacion_del_perfil() -> None:
    """⭐ El assert que se enseña en el documento explicativo."""
    filas = fetch_all("select * from public.v_institution_eligibility")

    assert filas, "No hay plantillas con instituciones: ¿corriste seed.sql?"
    inelegibles = [
        f"{f['template_name']}: {f['instrument_code']} viene de {f['institution_name']} "
        f"({f['credit_rating']}, tier {f['rating_tier']}) y el perfil admite hasta tier {f['max_rating_tier']}"
        for f in filas
        if not f["is_eligible"]
    ]
    assert not inelegibles, f"Productos que violan la regla de calificación: {inelegibles}"


def test_la_regla_muerde_el_conservador_no_alcanza_la_mejor_tasa() -> None:
    """El trade-off, hecho test: la mejor tasa del catálogo está fuera del alcance del conservador."""
    loja = fetch_one(
        """
        select i.code, i.expected_return, inst.credit_rating, inst.rating_tier
        from public.instruments i
        join public.institutions inst on inst.id = i.institution_id
        where i.code = 'DPF_LOJA_360'
        """
    )
    assert loja, "El seed debe traer el DPF de Loja: es el caso que hace visible la regla."

    # Entre los DPF (producto comparable), el de Loja paga más que cualquier otro. Es la
    # tentación: mejor tasa, peor emisor. Comparar contra TODO el catálogo no diría nada,
    # porque un fondo de crecimiento rinde más por asumir riesgo de mercado, no de contraparte.
    mejor_dpf = fetch_one(
        """
        select max(expected_return) as tasa
        from public.instruments
        where product_type = 'deposito_plazo'
        """
    )
    assert loja["expected_return"] == mejor_dpf["tasa"], (
        "El DPF de Loja debe seguir siendo el depósito de mejor tasa: si no, este test dejó "
        "de demostrar el trade-off entre rentabilidad y riesgo de contraparte."
    )
    assert loja["credit_rating"] == "AA", "…y debe seguir siendo el de peor calificación."

    for perfil, alcanza in (("conservador", False), ("agresivo", True)):
        regla = fetch_one(
            """
            select pir.max_rating_tier
            from public.profile_institution_rules pir
            join public.risk_profiles rp  on rp.id = pir.risk_profile_id
            join public.rules_versions rv on rv.id = pir.rules_version_id
            where rv.is_active and rp.code = %s
            """,
            (perfil,),
        )
        admitido = loja["rating_tier"] <= regla["max_rating_tier"]
        assert admitido is alcanza, (
            f"El perfil {perfil} {'debería' if alcanza else 'NO debería'} admitir a "
            f"Banco Loja ({loja['credit_rating']})."
        )
