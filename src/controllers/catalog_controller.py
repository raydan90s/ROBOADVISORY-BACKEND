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
  and (%(plazo)s::int is null or i.term_days = %(plazo)s::int or i.term_days is null)
order by inst.rating_tier asc, i.expected_return desc
"""


async def listar_tasas(
    investor_id: str,
    monto: float | None,
    plazo_dias: int | None,
) -> CatalogoTasas:
    perfil = fetch_one(_SQL_PERFIL, {"investor_id": investor_id})

    filas = fetch_all(
        _SQL_TASAS,
        {
            "max_tier": perfil["max_rating_tier"] if perfil else None,
            "rationale": perfil["rationale"] if perfil else None,
            "monto": monto,
            "plazo": plazo_dias,
        },
    )

    return CatalogoTasas(
        perfil=perfil["perfil_code"] if perfil else None,
        monto=monto,
        plazo_dias=plazo_dias,
        tasas=[TasaInstrumento(**fila) for fila in filas],
    )
