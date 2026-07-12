"""Catálogo de tasas: lectura pura sobre instruments + institutions.

No toca el schema ni el motor de scoring. La elegibilidad reutiliza la misma regla
que valida `v_institution_eligibility` (rating_tier <= max_rating_tier del perfil),
así el comparador nunca puede contradecir a los tests estrella.
"""

from src.config.database import fetch_all, fetch_one
from src.models.catalog import CatalogoTasas, TasaInstrumento

# El perfil vigente de quien consulta: su última sesión completada, con la regla de
# elegibilidad de la MISMA versión de reglas con la que se perfiló.
_SQL_PERFIL = """
select rp.code            as perfil_code,
       pir.max_rating_tier,
       pir.rationale
from public.profiling_sessions s
join public.risk_profiles rp              on rp.id = s.risk_profile_id
join public.profile_institution_rules pir on pir.rules_version_id = s.rules_version_id
                                         and pir.risk_profile_id  = s.risk_profile_id
where s.investor_id = %(investor_id)s
  and s.completed_at is not null
order by s.created_at desc
limit 1
"""

# Los USD los calcula Postgres (regla del equipo: nada se multiplica en React).
# Interés simple sobre el plazo del producto; si es un fondo sin plazo fijo se usa
# el ?plazo_dias= del request como horizonte, y si tampoco viene, no se estima nada.
_SQL_TASAS = """
select
    i.code,
    i.name                       as producto,
    i.product_type,
    inst.name                    as institucion,
    inst.credit_rating           as calificacion,
    inst.rating_tier,
    inst.rating_source           as fuente_calificacion,
    inst.rating_date             as fecha_calificacion,
    i.expected_return::float     as tasa_anual,
    i.term_days                  as plazo_dias,
    i.min_amount::float          as monto_minimo,

    case when %(max_tier)s::int is null then null
         else inst.rating_tier <= %(max_tier)s::int end                as elegible,
    case when %(max_tier)s::int is not null
          and inst.rating_tier > %(max_tier)s::int
         then %(rationale)s end                                        as motivo_no_elegible,

    case when %(monto)s::numeric is null or i.min_amount is null then null
         else %(monto)s::numeric >= i.min_amount end                   as cumple_monto_minimo,
    round(
        %(monto)s::numeric * i.expected_return / 100
        * coalesce(i.term_days, %(plazo)s::int) / 365.0
    , 2)::float                                                        as interes_estimado,
    round(
        %(monto)s::numeric
        + %(monto)s::numeric * i.expected_return / 100
        * coalesce(i.term_days, %(plazo)s::int) / 365.0
    , 2)::float                                                        as monto_final
from public.instruments i
join public.institutions inst on inst.id = i.institution_id
where i.is_active
  and inst.is_active
  and i.expected_return is not null
  and (%(todos)s::bool
       or %(plazo)s::int is null
       or i.term_days = %(plazo)s::int
       or i.term_days is null)
order by inst.rating_tier asc, i.expected_return desc
"""


async def listar_tasas(
    investor_id: str,
    monto: float | None,
    plazo_dias: int | None,
    todos_los_plazos: bool = False,
) -> CatalogoTasas:
    """Las tasas del catálogo, con la elegibilidad del perfil de quien consulta.

    `plazo_dias` hace dos cosas distintas y conviene no confundirlas: **filtra** los
    depósitos a ese plazo (lo que quiere el comparador) y, a la vez, es el **horizonte**
    con el que se estima el interés de los productos sin plazo fijo (los fondos).

    `todos_los_plazos` apaga solo el filtro: el simulador quiere ver TODAS las opciones
    del catálogo para poder cambiar de banco o de fondo, y que el plazo elegido siga
    sirviendo de horizonte para los fondos. Cada depósito se estima con SU propio plazo.
    """
    perfil = fetch_one(_SQL_PERFIL, {"investor_id": investor_id})

    filas = fetch_all(
        _SQL_TASAS,
        {
            "max_tier": perfil["max_rating_tier"] if perfil else None,
            "rationale": perfil["rationale"] if perfil else None,
            "monto": monto,
            "plazo": plazo_dias,
            "todos": todos_los_plazos,
        },
    )

    tasas = [TasaInstrumento(**fila) for fila in filas]
    recomendado = elegir_recomendado(tasas)
    if recomendado is not None:
        recomendado.recomendado = True

    return CatalogoTasas(
        perfil=perfil["perfil_code"] if perfil else None,
        monto=monto,
        plazo_dias=plazo_dias,
        tasas=tasas,
    )


def _viable(t: TasaInstrumento) -> bool:
    """Elegible para el perfil (o sin perfil todavía) y con el monto mínimo cubierto."""
    return (
        t.elegible is not False
        and t.cumple_monto_minimo is not False
        and t.monto_final is not None
    )


def elegir_recomendado(tasas: list[TasaInstrumento]) -> TasaInstrumento | None:
    """La opción que el motor recomienda. **Decide Python sobre las filas, nunca el LLM.**

    El criterio es explícito y auditable: entre lo que el perfil SÍ admite y cuyo mínimo
    el monto alcanza, la mayor tasa anual; a igual tasa, la institución mejor calificada.

    La tasa anual —y no el monto final— es lo comparable: un depósito a 720 días paga más
    intereses que uno a 180 solo por durar el doble, así que elegir por monto final sería
    recomendar siempre el plazo más largo disfrazándolo de "mejor producto". El riesgo ya
    quedó acotado antes, por la regla de elegibilidad del perfil.

    Sin `?monto=` no hay `monto_final` y no se recomienda nada: el comparador muestra el
    catálogo, no un consejo.
    """
    viables = [t for t in tasas if _viable(t)]
    if not viables:
        return None
    return max(viables, key=lambda t: (t.tasa_anual, -t.rating_tier))
