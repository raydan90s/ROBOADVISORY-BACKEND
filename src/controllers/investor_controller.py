"""Lógica de negocio del inversionista.

Regla de oro del reto: el puntaje, el perfil y los porcentajes son DETERMINISTAS
y salen de la base (scoring_rules, profile_thresholds, allocation_template_items).
El LLM solo redacta la explicación en lenguaje natural.
"""

from typing import Any

from fastapi import HTTPException, status
from psycopg import Connection
from psycopg.types.json import Jsonb

from src.config.database import fetch_all, fetch_one, get_connection
from src.models.investor import (
    AssetAllocation,
    BreakdownRespuesta,
    EstadoPropuesta,
    Investor,
    InvestorProfileCreate,
    NivelRiesgo,
    PerfilRiesgo,
    Pregunta,
    OpcionPregunta,
    PortfolioProposal,
    ProfilingBreakdown,
    RespuestaDetalle,
)
from src.services.ai_agent import redactar_explicacion


def _rules_version_activa(conn: Connection) -> dict[str, Any]:
    """Versión de reglas vigente. Todo el cálculo se ancla a ella (auditable)."""
    row = conn.execute(
        """
        select id, version_label
        from public.rules_versions
        where is_active
        order by created_at desc
        limit 1
        """
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No hay una rules_version activa. Corre el seed de schema.sql.",
        )
    return row


# ===========================================================================
# Cuestionario
# ===========================================================================


async def listar_preguntas() -> list[Pregunta]:
    """Sirve el cuestionario desde la BD para que el front no lo duplique."""
    filas = fetch_all(
        """
        select q.code as q_code, q.text as q_text, o.code as o_code, o.label as o_label
        from public.questions q
        join public.question_options o on o.question_id = q.id
        where q.is_active
        order by q.order_index, o.order_index
        """
    )

    preguntas: dict[str, Pregunta] = {}
    for f in filas:
        p = preguntas.setdefault(
            f["q_code"], Pregunta(code=f["q_code"], text=f["q_text"], opciones=[])
        )
        p.opciones.append(OpcionPregunta(code=f["o_code"], label=f["o_label"]))
    return list(preguntas.values())


# ===========================================================================
# Perfilamiento (HU1)
# ===========================================================================


async def create_investor_profile(payload: InvestorProfileCreate) -> Investor:
    """Crea el inversionista, puntúa sus respuestas contra la BD y le asigna perfil.

    Todo ocurre en una transacción: si una respuesta es inválida, no queda ni el
    profile ni la sesión a medias.
    """
    with get_connection() as conn:
        rv = _rules_version_activa(conn)

        investor = conn.execute(
            """
            insert into public.profiles (role, full_name, email, cedula_ruc)
            values ('investor', %s, %s, %s)
            returning id, full_name, email, cedula_ruc, created_at
            """,
            (payload.nombre, payload.email, payload.cedula_ruc),
        ).fetchone()

        session = conn.execute(
            """
            insert into public.profiling_sessions (investor_id, rules_version_id)
            values (%s, %s)
            returning id
            """,
            (investor["id"], rv["id"]),
        ).fetchone()

        detalles: list[RespuestaDetalle] = []
        total = 0

        for q_code, o_code in payload.respuestas.items():
            regla = conn.execute(
                """
                select q.id   as question_id, q.text  as pregunta_text,
                       o.id   as option_id,   o.label as opcion_label,
                       sr.points
                from public.questions q
                join public.question_options o on o.question_id = q.id
                join public.scoring_rules sr   on sr.question_option_id = o.id
                where q.code = %s and o.code = %s and sr.rules_version_id = %s
                """,
                (q_code, o_code, rv["id"]),
            ).fetchone()

            if not regla:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Respuesta inválida: '{o_code}' no es una opción de '{q_code}' "
                        f"en las reglas {rv['version_label']}."
                    ),
                )

            conn.execute(
                """
                insert into public.profiling_answers
                    (session_id, question_id, option_id, points_awarded)
                values (%s, %s, %s, %s)
                """,
                (session["id"], regla["question_id"], regla["option_id"], regla["points"]),
            )

            total += regla["points"]
            detalles.append(
                RespuestaDetalle(
                    pregunta_code=q_code,
                    pregunta_text=regla["pregunta_text"],
                    opcion_code=o_code,
                    opcion_label=regla["opcion_label"],
                    puntos=regla["points"],
                )
            )

        perfil = conn.execute(
            """
            select rp.id, rp.code
            from public.profile_thresholds pt
            join public.risk_profiles rp on rp.id = pt.risk_profile_id
            where pt.rules_version_id = %s
              and %s between pt.min_score and pt.max_score
            """,
            (rv["id"], total),
        ).fetchone()

        if not perfil:
            # Pasa si el usuario contestó solo parte del cuestionario: el puntaje
            # cae fuera de todos los rangos de profile_thresholds.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"El puntaje {total} no cae en ningún rango de perfil. "
                    "¿Respondiste todas las preguntas?"
                ),
            )

        conn.execute(
            """
            update public.profiling_sessions
            set total_score = %s, risk_profile_id = %s, completed_at = now()
            where id = %s
            """,
            (total, perfil["id"], session["id"]),
        )

        return Investor(
            investor_id=str(investor["id"]),
            session_id=str(session["id"]),
            nombre=investor["full_name"],
            email=investor["email"],
            cedula_ruc=investor["cedula_ruc"],
            puntaje=total,
            perfil_riesgo=PerfilRiesgo(perfil["code"]),
            respuestas=detalles,
            created_at=investor["created_at"],
        )


async def get_investor(investor_id: str) -> Investor:
    """Lee el inversionista con su sesión de perfilamiento más reciente."""
    fila = fetch_one(
        """
        select p.id as investor_id, p.full_name, p.email, p.cedula_ruc, p.created_at,
               s.id as session_id, s.total_score, rp.code as perfil_code
        from public.profiles p
        join public.profiling_sessions s on s.investor_id = p.id
        left join public.risk_profiles rp on rp.id = s.risk_profile_id
        where p.id = %s and p.role = 'investor'
        order by s.created_at desc
        limit 1
        """,
        (investor_id,),
    )

    if not fila or fila["perfil_code"] is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe un perfilamiento completo para el inversionista {investor_id}",
        )

    respuestas = fetch_all(
        """
        select q.code as pregunta_code, q.text as pregunta_text,
               o.code as opcion_code,   o.label as opcion_label,
               a.points_awarded as puntos
        from public.profiling_answers a
        join public.questions q        on q.id = a.question_id
        join public.question_options o on o.id = a.option_id
        where a.session_id = %s
        order by q.order_index
        """,
        (fila["session_id"],),
    )

    return Investor(
        investor_id=str(fila["investor_id"]),
        session_id=str(fila["session_id"]),
        nombre=fila["full_name"],
        email=fila["email"],
        cedula_ruc=fila["cedula_ruc"],
        puntaje=fila["total_score"],
        perfil_riesgo=PerfilRiesgo(fila["perfil_code"]),
        respuestas=[RespuestaDetalle(**r) for r in respuestas],
        created_at=fila["created_at"],
    )


# ===========================================================================
# "¿Cómo se calculó?" (HU1, criterio 3)
# ===========================================================================


async def obtener_breakdown(
    investor_id: str, session_id: str | None = None
) -> ProfilingBreakdown:
    """El desglose respuesta → puntos → umbral, tal como lo devuelve la vista.

    Sin `session_id` toma la sesión completada más reciente (es lo que quiere el
    inversionista). El asesor sí pasa el `session_id` que trae la cola: revisa una
    propuesta concreta, y si el cliente se volvió a perfilar la última sesión ya no
    es la que originó esa propuesta.
    """
    if session_id is None:
        ultima = fetch_one(
            """
            select id::text as session_id
            from public.profiling_sessions
            where investor_id = %s and completed_at is not null
            order by created_at desc
            limit 1
            """,
            (investor_id,),
        )
        if not ultima:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No existe un perfilamiento completo para el inversionista {investor_id}",
            )
        session_id = ultima["session_id"]

    filas = fetch_all(
        """
        select session_id::text  as session_id,
               investor_id::text as investor_id,
               total_score,
               amount,
               rules_version,
               risk_profile_code,
               risk_profile_name,
               question_code,
               question_text,
               option_code,
               option_label,
               points_awarded,
               profile_min_score,
               profile_max_score,
               max_rating_tier,
               institution_rule
        from public.v_profiling_breakdown
        where session_id = %s and investor_id = %s
        order by order_index
        """,
        (session_id, investor_id),
    )

    if not filas:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No hay un desglose para la sesión {session_id} de {investor_id}.",
        )

    cabecera = filas[0]
    return ProfilingBreakdown(
        session_id=cabecera["session_id"],
        investor_id=cabecera["investor_id"],
        puntaje=cabecera["total_score"],
        monto=cabecera["amount"],
        rules_version=cabecera["rules_version"],
        perfil_code=cabecera["risk_profile_code"],
        perfil_nombre=cabecera["risk_profile_name"],
        umbral_min=cabecera["profile_min_score"],
        umbral_max=cabecera["profile_max_score"],
        regla_institucion=cabecera["institution_rule"],
        max_rating_tier=cabecera["max_rating_tier"],
        respuestas=[
            BreakdownRespuesta(
                question_code=f["question_code"],
                question_text=f["question_text"],
                option_code=f["option_code"],
                option_label=f["option_label"],
                puntos=f["points_awarded"],
            )
            for f in filas
        ],
    )


# ===========================================================================
# Propuesta de portafolio (HU2)
# ===========================================================================


def _allocations_de(conn: Connection, proposal_id: str) -> list[AssetAllocation]:
    filas = conn.execute(
        """
        select i.code as instrumento_code, i.name as nombre, i.asset_class as clase_activo,
               i.risk_class as riesgo, pi.percentage as porcentaje,
               i.expected_return as retorno_esperado
        from public.proposal_items pi
        join public.instruments i on i.id = pi.instrument_id
        where pi.proposal_id = %s
        order by pi.percentage desc
        """,
        (proposal_id,),
    ).fetchall()
    return [AssetAllocation(**f) for f in filas]


def _retorno_ponderado(allocations: list[AssetAllocation]) -> float | None:
    """Promedio ponderado de los retornos esperados. Ficticio, solo demo."""
    aportes = [
        a.porcentaje * a.retorno_esperado
        for a in allocations
        if a.retorno_esperado is not None
    ]
    if not aportes:
        return None
    return round(sum(aportes) / 100, 3)


async def get_portfolio_proposal(investor_id: str) -> PortfolioProposal:
    """Devuelve la propuesta del inversionista, generándola la primera vez.

    La propuesta se guarda (proposals + proposal_items): es un snapshot inmutable
    que el asesor revisará en la HU3, así que no se regenera en cada GET.
    """
    investor = await get_investor(investor_id)

    with get_connection() as conn:
        existente = conn.execute(
            "select id, expected_risk, explanation, status from public.proposals where session_id = %s",
            (investor.session_id,),
        ).fetchone()

        if existente:
            allocations = _allocations_de(conn, existente["id"])
            return PortfolioProposal(
                proposal_id=str(existente["id"]),
                investor_id=investor.investor_id,
                session_id=investor.session_id,
                perfil_riesgo=investor.perfil_riesgo,
                puntaje=investor.puntaje,
                riesgo_esperado=NivelRiesgo(existente["expected_risk"]),
                estado=EstadoPropuesta(existente["status"]),
                allocations=allocations,
                retorno_esperado_anual=_retorno_ponderado(allocations),
                explicacion=existente["explanation"],
            )

        # --- Primera vez: materializa la plantilla del perfil como propuesta ---
        plantilla = conn.execute(
            """
            select at.id, at.expected_risk
            from public.allocation_templates at
            join public.profiling_sessions s on s.rules_version_id = at.rules_version_id
                                            and s.risk_profile_id  = at.risk_profile_id
            where s.id = %s
            """,
            (investor.session_id,),
        ).fetchone()

        if not plantilla:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"No hay allocation_template para el perfil {investor.perfil_riesgo.value}.",
            )

        proposal = conn.execute(
            """
            insert into public.proposals (session_id, template_id, expected_risk)
            values (%s, %s, %s)
            returning id, status
            """,
            (investor.session_id, plantilla["id"], plantilla["expected_risk"]),
        ).fetchone()

        # Los porcentajes se copian tal cual de la plantilla: el LLM no los toca.
        conn.execute(
            """
            insert into public.proposal_items (proposal_id, instrument_id, percentage)
            select %s, ati.instrument_id, ati.percentage
            from public.allocation_template_items ati
            where ati.template_id = %s
            """,
            (proposal["id"], plantilla["id"]),
        )

        allocations = _allocations_de(conn, proposal["id"])
        riesgo = NivelRiesgo(plantilla["expected_risk"])
        retorno = _retorno_ponderado(allocations)

        explicacion = await redactar_explicacion(investor, allocations, riesgo, retorno)

        conn.execute(
            "update public.proposals set explanation = %s where id = %s",
            (explicacion, proposal["id"]),
        )
        conn.execute(
            """
            insert into public.audit_log (entity_type, entity_id, actor_id, action, metadata)
            values ('proposal', %s, %s, 'created', %s)
            """,
            (proposal["id"], investor.investor_id, Jsonb({"puntaje": investor.puntaje})),
        )

        return PortfolioProposal(
            proposal_id=str(proposal["id"]),
            investor_id=investor.investor_id,
            session_id=investor.session_id,
            perfil_riesgo=investor.perfil_riesgo,
            puntaje=investor.puntaje,
            riesgo_esperado=riesgo,
            estado=EstadoPropuesta(proposal["status"]),
            allocations=allocations,
            retorno_esperado_anual=retorno,
            explicacion=explicacion,
        )
