"""Lógica de negocio del inversionista.

Regla de oro del reto: el puntaje, el perfil y los porcentajes son DETERMINISTAS
y salen de la base (scoring_rules, profile_thresholds, allocation_template_items).
El LLM solo redacta la explicación en lenguaje natural.
"""

from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from psycopg import Connection
from psycopg.types.json import Jsonb

from src.config.database import fetch_all, fetch_one, get_connection
from src.models.auth import CurrentUser
from src.models.investor import (
    AssetAllocation,
    BreakdownRespuesta,
    CapitalUpdate,
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
    ResumenCapital,
    Subcuenta,
)
from src.services.ai_agent import DatosExplicacion, Explicacion, redactar_explicacion

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


async def create_investor_profile(
    payload: InvestorProfileCreate, usuario: CurrentUser
) -> Investor:
    """Perfila al usuario del token: puntúa sus respuestas contra la BD y le asigna perfil.

    El perfilamiento se adjunta a un `profiles` que YA existe (el que creó el registro).
    Crear acá una segunda fila —como se hacía antes— dejaba al cliente con dos identidades:
    la del login y la del cuestionario, que nunca coincidían.

    Todo ocurre en una transacción: si una respuesta es inválida, no queda la sesión a medias.
    """
    with get_connection() as conn:
        rv = _rules_version_activa(conn)

        # coalesce: la cédula que ya tenga el perfil manda. El cuestionario puede
        # completarla si falta, pero no reescribir un dato de identidad existente.
        investor = conn.execute(
            """
            update public.profiles
            set cedula_ruc = coalesce(cedula_ruc, %s)
            where id = %s and role = 'investor'
            returning id, full_name, email, cedula_ruc, created_at
            """,
            (payload.cedula_ruc, usuario.id),
        ).fetchone()

        if not investor:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="El usuario del token no existe como inversionista.",
            )

        # El monto de una subcuenta nunca puede pasarse del capital sin asignar. La
        # validación vive acá, en el servidor y dentro de la transacción: dos pestañas
        # abiertas no pueden repartirse el mismo dinero dos veces. En el front el aviso
        # es una cortesía; acá es la regla.
        _exige_capital_disponible(conn, str(investor["id"]), payload.monto)

        nombre_subcuenta = (payload.nombre_subcuenta or "").strip() or None

        session = conn.execute(
            """
            insert into public.profiling_sessions
                (investor_id, rules_version_id, amount, subaccount_name)
            values (%s, %s, %s, %s)
            returning id
            """,
            (investor["id"], rv["id"], payload.monto, nombre_subcuenta),
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

        maximo = conn.execute(
            """
            select max(max_score) as puntaje_max
            from public.profile_thresholds
            where rules_version_id = %s
            """,
            (rv["id"],),
        ).fetchone()

        return Investor(
            investor_id=str(investor["id"]),
            session_id=str(session["id"]),
            nombre=investor["full_name"],
            email=investor["email"],
            cedula_ruc=investor["cedula_ruc"],
            puntaje=total,
            puntaje_max=maximo["puntaje_max"],
            perfil_riesgo=PerfilRiesgo(perfil["code"]),
            respuestas=detalles,
            monto=float(payload.monto),
            created_at=investor["created_at"],
        )


async def get_investor(investor_id: str, session_id: str | None = None) -> Investor:
    """Lee el inversionista en una de sus sesiones de perfilamiento.

    Sin `session_id` devuelve la más reciente — que es lo que la app de una sola cartera
    siempre pidió, y por eso el parámetro es opcional: agregar subcuentas no rompe al
    cliente que no las conoce. Con `session_id` devuelve esa subcuenta concreta.
    """
    fila = fetch_one(
        """
        select p.id as investor_id, p.full_name, p.email, p.cedula_ruc, p.created_at,
               s.id as session_id, s.total_score, s.amount, rp.code as perfil_code,
               (select max(pt.max_score)
                  from public.profile_thresholds pt
                 where pt.rules_version_id = s.rules_version_id) as puntaje_max
        from public.profiles p
        join public.profiling_sessions s on s.investor_id = p.id
        left join public.risk_profiles rp on rp.id = s.risk_profile_id
        where p.id = %s and p.role = 'investor'
          and (%s::uuid is null or s.id = %s::uuid)
        order by s.created_at desc
        limit 1
        """,
        (investor_id, session_id, session_id),
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
        puntaje_max=fila["puntaje_max"],
        perfil_riesgo=PerfilRiesgo(fila["perfil_code"]),
        respuestas=[RespuestaDetalle(**r) for r in respuestas],
        monto=fila["amount"],
        created_at=fila["created_at"],
    )


# ===========================================================================
# Subcuentas y capital
# ===========================================================================


def _asignado(conn: Connection, investor_id: str) -> float:
    """Lo que el inversionista ya repartió: la suma de los montos de sus subcuentas."""
    fila = conn.execute(
        """
        select coalesce(sum(amount), 0)::float as asignado
        from public.profiling_sessions
        where investor_id = %s and completed_at is not null
        """,
        (investor_id,),
    ).fetchone()
    return fila["asignado"]


def _exige_capital_disponible(
    conn: Connection, investor_id: str, monto: Decimal
) -> None:
    """422 si la subcuenta nueva se pasa del capital sin asignar.

    El `for update` bloquea la fila del inversionista hasta el fin de la transacción: dos
    peticiones simultáneas del mismo cliente se serializan, así que no pueden leer las dos
    el mismo "sin asignar" y repartirse el mismo dinero dos veces.

    Si nunca declaró un capital total no hay techo contra el cual validar, y el
    perfilamiento pasa: es el caso de la app de una sola cartera.
    """
    fila = conn.execute(
        """
        select total_capital::float as capital
        from public.profiles
        where id = %s
        for update
        """,
        (investor_id,),
    ).fetchone()

    capital = fila["capital"] if fila else None
    if capital is None:
        return

    disponible = capital - _asignado(conn, investor_id)
    if float(monto) > disponible:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"El monto excede tu capital sin asignar: quedan USD {disponible:,.2f} "
                f"de un capital total de USD {capital:,.2f}."
            ),
        )


async def listar_subcuentas(investor_id: str) -> ResumenCapital:
    """Las subcuentas del inversionista y cómo reparten su capital.

    Cada fila es una `profiling_session` completada con su propuesta. El instrumento
    principal y el retorno esperado los calcula Postgres sobre `proposal_items`: son
    los mismos números que ya se ven en la propuesta, no una segunda versión de ellos.
    """
    filas = fetch_all(
        """
        select s.id::text          as session_id,
               s.subaccount_name   as nombre,
               s.amount::float     as monto,
               s.total_score       as puntaje,
               (select max(pt.max_score)
                  from public.profile_thresholds pt
                 where pt.rules_version_id = s.rules_version_id) as puntaje_max,
               rp.code             as perfil,
               pr.id::text         as proposal_id,
               pr.status           as estado,
               top.nombre          as instrumento_principal,
               ret.retorno         as retorno_esperado_anual
        from public.profiling_sessions s
        join public.risk_profiles rp on rp.id = s.risk_profile_id
        left join public.proposals pr on pr.session_id = s.id
        -- el de mayor porcentaje: el que define de qué se trata la subcuenta
        left join lateral (
            select i.name as nombre
            from public.proposal_items pi
            join public.instruments i on i.id = pi.instrument_id
            where pi.proposal_id = pr.id
            order by pi.percentage desc
            limit 1
        ) top on true
        left join lateral (
            select round(sum(pi.percentage * i.expected_return) / 100.0, 3)::float as retorno
            from public.proposal_items pi
            join public.instruments i on i.id = pi.instrument_id
            where pi.proposal_id = pr.id
        ) ret on true
        where s.investor_id = %s and s.completed_at is not null
        order by s.created_at
        """,
        (investor_id,),
    )

    subcuentas = [
        Subcuenta(
            session_id=f["session_id"],
            proposal_id=f["proposal_id"],
            # Las sesiones anteriores a las subcuentas no tienen nombre: se numeran por
            # orden de creación en vez de mostrarse en blanco.
            nombre=f["nombre"] or f"Subcuenta {i}",
            monto=f["monto"],
            perfil=PerfilRiesgo(f["perfil"]),
            puntaje=f["puntaje"],
            puntaje_max=f["puntaje_max"],
            estado=EstadoPropuesta(f["estado"]) if f["estado"] else None,
            instrumento_principal=f["instrumento_principal"],
            retorno_esperado_anual=f["retorno_esperado_anual"],
        )
        for i, f in enumerate(filas, start=1)
    ]

    asignado = round(sum(s.monto for s in subcuentas), 2)

    techo = fetch_one(
        "select total_capital::float as capital from public.profiles where id = %s",
        (investor_id,),
    )
    capital = techo["capital"] if techo else None

    return ResumenCapital(
        capital_total=capital,
        asignado=asignado,
        # None, no 0: "no declaró su capital" y "lo tiene todo invertido" son cosas
        # distintas, y la pantalla las dibuja distinto.
        sin_asignar=round(capital - asignado, 2) if capital is not None else None,
        subcuentas=subcuentas,
    )


async def fijar_capital(investor_id: str, payload: CapitalUpdate) -> ResumenCapital:
    """Fija el techo de capital del inversionista y devuelve el reparto actualizado.

    No puede quedar por debajo de lo que ya está asignado: eso dejaría un `sin_asignar`
    negativo, es decir, una cartera que promete más plata de la que el cliente declaró.

    Leer y escribir en la misma transacción (con la fila del perfil bloqueada) evita que
    entre la comprobación y el update se cuele una subcuenta nueva que invalide la cuenta.
    """
    with get_connection() as conn:
        conn.execute(
            "select id from public.profiles where id = %s for update", (investor_id,)
        )
        asignado = _asignado(conn, investor_id)

        if float(payload.capital_total) < asignado:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Ya tienes USD {asignado:,.2f} asignados en tus subcuentas: el "
                    "capital total no puede ser menor que eso."
                ),
            )

        conn.execute(
            "update public.profiles set total_capital = %s where id = %s",
            (payload.capital_total, investor_id),
        )

    return await listar_subcuentas(investor_id)


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
    """Las líneas de la propuesta, con emisor, calificación (y su fuente) y los USD.

    Todo sale de la base: el porcentaje de la plantilla, el USD que calculó Postgres y
    la calificación del emisor con la calificadora y la fecha que la sustentan.
    """
    filas = conn.execute(
        """
        select i.code            as instrumento_code,
               i.name            as nombre,
               i.asset_class     as clase_activo,
               i.risk_class      as riesgo,
               pi.percentage     as porcentaje,
               pi.amount         as monto_asignado,
               i.expected_return as retorno_esperado,
               i.term_days       as plazo_dias,
               inst.name          as institucion,
               inst.credit_rating as calificacion,
               inst.rating_source as calificacion_fuente,
               inst.rating_date   as calificacion_fecha
        from public.proposal_items pi
        join public.instruments i         on i.id = pi.instrument_id
        left join public.institutions inst on inst.id = i.institution_id
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


def _datos_explicacion(
    conn: Connection,
    investor: Investor,
    allocations: list[AssetAllocation],
    riesgo: NivelRiesgo,
    monto_total: Decimal | None,
    retorno: float | None,
) -> DatosExplicacion:
    """Reúne TODO lo que el LLM tiene permitido citar. Fuera de esto, no existe.

    Los umbrales y el puntaje máximo se leen de las reglas activas: son números que el
    texto puede mencionar ("12 de 15 puntos"), así que tienen que entrar al conjunto
    permitido del guardarraíl desde la base y no como constantes en el código.
    """
    reglas = conn.execute(
        """
        select rv.version_label,
               pt.min_score,
               pt.max_score,
               (select max(pt2.max_score)
                  from public.profile_thresholds pt2
                 where pt2.rules_version_id = s.rules_version_id) as puntaje_max
        from public.profiling_sessions s
        join public.rules_versions rv     on rv.id = s.rules_version_id
        left join public.profile_thresholds pt
               on pt.rules_version_id = s.rules_version_id
              and pt.risk_profile_id  = s.risk_profile_id
        where s.id = %s
        """,
        (investor.session_id,),
    ).fetchone()

    return DatosExplicacion(
        investor=investor,
        allocations=allocations,
        riesgo=riesgo,
        monto_total=monto_total,
        retorno_anual=retorno,
        rules_version=reglas["version_label"],
        umbral_min=reglas["min_score"],
        umbral_max=reglas["max_score"],
        puntaje_max=reglas["puntaje_max"],
    )


def _guardar_interaccion(
    conn: Connection, session_id: str, proposal_id: str, expl: Explicacion
) -> None:
    """Evidencia del criterio #3: qué modelo escribió, si pasó el guardarraíl y con qué fuentes.

    Se guardan los dos turnos (prompt y respuesta). Si el guardarraíl rechazó al modelo,
    queda registrado en `guardrail_passed` y en los motivos: el rechazo es tan auditable
    como el acierto.
    """
    conn.execute(
        """
        insert into public.llm_interactions
            (session_id, proposal_id, role, content, model, platform)
        values (%s, %s, 'user', %s, %s, 'api')
        """,
        (session_id, proposal_id, expl.prompt, expl.modelo),
    )
    conn.execute(
        """
        insert into public.llm_interactions
            (session_id, proposal_id, role, content, model,
             guardrail_passed, retry_count, metadata, platform)
        values (%s, %s, 'assistant', %s, %s, %s, %s, %s, 'api')
        """,
        (
            session_id,
            proposal_id,
            expl.texto,
            expl.modelo,
            expl.guardrail_passed,
            expl.retry_count,
            Jsonb({"sources": expl.sources, "guardrail_motivos": expl.motivos}),
        ),
    )


async def get_portfolio_proposal(
    investor_id: str, session_id: str | None = None
) -> PortfolioProposal:
    """Devuelve la propuesta del inversionista, generándola la primera vez.

    La propuesta se guarda (proposals + proposal_items): es un snapshot inmutable
    que el asesor revisará en la HU3, así que no se regenera en cada GET.

    `session_id` elige la subcuenta. Sin él sigue siendo la sesión más reciente, que es
    lo que la app de una sola cartera siempre pidió.
    """
    investor = await get_investor(investor_id, session_id)

    with get_connection() as conn:
        existente = conn.execute(
            """
            select id, expected_risk, explanation, status, total_amount
            from public.proposals where session_id = %s
            """,
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
                puntaje_max=investor.puntaje_max,
                riesgo_esperado=NivelRiesgo(existente["expected_risk"]),
                estado=EstadoPropuesta(existente["status"]),
                monto_total=existente["total_amount"],
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

        # El monto se copia de la sesión a la propuesta: snapshot inmutable. Si el
        # cliente se vuelve a perfilar con otro monto, esta propuesta no cambia.
        proposal = conn.execute(
            """
            insert into public.proposals (session_id, template_id, expected_risk, total_amount)
            select %s, %s, %s, s.amount
            from public.profiling_sessions s
            where s.id = %s
            returning id, status, total_amount
            """,
            (
                investor.session_id,
                plantilla["id"],
                plantilla["expected_risk"],
                investor.session_id,
            ),
        ).fetchone()

        # Los porcentajes se copian tal cual de la plantilla: el LLM no los toca.
        # Los USD los calcula Postgres — es el número que después tiene que estar en el
        # set permitido del guardarraíl, así que no puede nacer en Python.
        conn.execute(
            """
            insert into public.proposal_items (proposal_id, instrument_id, percentage, amount)
            select p.id, ati.instrument_id, ati.percentage,
                   round(p.total_amount * ati.percentage / 100.0, 2)
            from public.allocation_template_items ati
            cross join public.proposals p
            where ati.template_id = %s and p.id = %s
            """,
            (plantilla["id"], proposal["id"]),
        )

        allocations = _allocations_de(conn, proposal["id"])
        riesgo = NivelRiesgo(plantilla["expected_risk"])
        retorno = _retorno_ponderado(allocations)

        explicacion = await redactar_explicacion(
            _datos_explicacion(conn, investor, allocations, riesgo, proposal["total_amount"], retorno)
        )
        _guardar_interaccion(conn, investor.session_id, proposal["id"], explicacion)

        conn.execute(
            "update public.proposals set explanation = %s where id = %s",
            (explicacion.texto, proposal["id"]),
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
            puntaje_max=investor.puntaje_max,
            riesgo_esperado=riesgo,
            estado=EstadoPropuesta(proposal["status"]),
            monto_total=proposal["total_amount"],
            allocations=allocations,
            retorno_esperado_anual=retorno,
            explicacion=explicacion.texto,
        )
