"""Lógica del agente conversacional. Solo orquesta: la máquina de estados vive en
`services/agent_graph.py` y los guardarraíles en `services/guardrails.py`.

Un turno de chat hace tres cosas:
1. Arma el contexto REAL del inversionista desde la base (perfil, propuesta, catálogo).
   Ese contexto es lo único que el agente tiene permitido citar.
2. Corre el grafo (router → qa → guardrail → refuse/fallback).
3. Guarda los dos turnos (usuario y asistente) en `llm_interactions` con `thread_id`
   = la sesión. Esa tabla es la evidencia del criterio #3 y, a la vez, la memoria de
   la conversación (los turnos previos se releen para dar continuidad).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status
from psycopg import Connection
from psycopg.types.json import Jsonb

from src.config.database import get_connection
from src.controllers import catalog_controller
from src.controllers.investor_controller import (
    _allocations_de,
    _datos_explicacion,
    _retorno_ponderado,
)
from src.dependencies.auth import exige_dueno_o_asesor
from src.models.agent import (
    AgentChatRequest,
    AgentChatResponse,
    ProviderInfo,
    SimuladorRequest,
    SimuladorResponse,
    SourceChip,
)
from src.models.auth import CurrentUser
from src.models.investor import Investor, NivelRiesgo, PerfilRiesgo, RespuestaDetalle
from src.services import simulator_ai
from src.services.ai_agent import DatosExplicacion
from src.services.agent_graph import (
    ContextoAgente,
    ItemCatalogo,
    Subcuenta,
    responder,
)
from src.services.llm_provider import listar_proveedores

log = logging.getLogger(__name__)

# Cuántos turnos previos se releen para dar continuidad. Corto a propósito: el
# contexto pesado (perfil, propuesta) ya viaja en el prompt cada turno.
_MAX_HISTORIAL_FILAS = 12


# ===========================================================================
# Resolver la sesión sobre la que se conversa
# ===========================================================================


def _resolver_session_id(conn: Connection, session_id: str | None, usuario: CurrentUser) -> str:
    """Sin `session_id`, usa la última sesión completada del usuario del token.

    El asesor SIEMPRE debe indicar la sesión: no tiene "su propia" cartera de la cual
    hablar por defecto.
    """
    if session_id:
        return session_id

    fila = conn.execute(
        """
        select id::text as session_id
        from public.profiling_sessions
        where investor_id = %s and completed_at is not null
        order by created_at desc
        limit 1
        """,
        (usuario.id,),
    ).fetchone()
    if not fila:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No tienes un perfilamiento completo. Indica una sesión (session_id).",
        )
    return fila["session_id"]


def _investor_de_sesion(conn: Connection, session_id: str) -> Investor:
    """El inversionista tal como quedó en ESA sesión (no la más reciente).

    Importa para subcuentas: un mismo cliente puede tener varias sesiones con perfiles
    y montos distintos, y el chat es sobre la subcuenta que el cliente abrió.
    """
    cab = conn.execute(
        """
        select p.id as investor_id, p.full_name, p.email, p.cedula_ruc, p.created_at,
               s.id as session_id, s.total_score, s.amount, rp.code as perfil_code
        from public.profiling_sessions s
        join public.profiles p         on p.id = s.investor_id
        left join public.risk_profiles rp on rp.id = s.risk_profile_id
        where s.id = %s and s.completed_at is not null
        """,
        (session_id,),
    ).fetchone()

    if not cab or cab["perfil_code"] is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe una sesión de perfilamiento completa con id {session_id}.",
        )

    respuestas = conn.execute(
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
        (session_id,),
    ).fetchall()

    return Investor(
        investor_id=str(cab["investor_id"]),
        session_id=str(cab["session_id"]),
        nombre=cab["full_name"],
        email=cab["email"],
        cedula_ruc=cab["cedula_ruc"],
        puntaje=cab["total_score"],
        perfil_riesgo=PerfilRiesgo(cab["perfil_code"]),
        respuestas=[RespuestaDetalle(**r) for r in respuestas],
        monto=cab["amount"],
        created_at=cab["created_at"],
    )


def _datos_de_sesion(conn: Connection, session_id: str) -> tuple[DatosExplicacion, str]:
    """Reúne el contexto del inversionista + su propuesta. Devuelve (datos, proposal_id).

    Reutiliza los helpers de `investor_controller`: el agente cita EXACTAMENTE los
    mismos números y fuentes que la propuesta, porque salen de las mismas filas.
    """
    investor = _investor_de_sesion(conn, session_id)

    propuesta = conn.execute(
        """
        select id, expected_risk, total_amount
        from public.proposals where session_id = %s
        """,
        (session_id,),
    ).fetchone()
    if not propuesta:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Esta sesión aún no tiene una propuesta. Genérala antes de conversar.",
        )

    allocations = _allocations_de(conn, propuesta["id"])
    riesgo = NivelRiesgo(propuesta["expected_risk"])
    retorno = _retorno_ponderado(allocations)
    datos = _datos_explicacion(
        conn, investor, allocations, riesgo, propuesta["total_amount"], retorno
    )
    return datos, str(propuesta["id"])


def _elegibilidad(conn: Connection, session_id: str) -> tuple[int | None, str | None]:
    """La regla de elegibilidad por calificación del perfil de esta sesión (versionada)."""
    fila = conn.execute(
        """
        select pir.max_rating_tier, pir.rationale
        from public.profiling_sessions s
        join public.profile_institution_rules pir
          on pir.rules_version_id = s.rules_version_id
         and pir.risk_profile_id  = s.risk_profile_id
        where s.id = %s
        """,
        (session_id,),
    ).fetchone()
    if not fila:
        return None, None
    return fila["max_rating_tier"], fila["rationale"]


def _catalogo(conn: Connection, max_rating_tier: int | None) -> list[ItemCatalogo]:
    """Todo el catálogo aprobado, marcando qué puede tomar el perfil (rating_tier <= max).

    Es lo que deja al agente EXPLICAR por qué un producto no es elegible o el trade-off
    tasa/calificación — con productos reales, no inventados.
    """
    filas = conn.execute(
        """
        select i.name         as nombre,
               i.asset_class   as clase_activo,
               i.expected_return as retorno,
               i.term_days     as plazo_dias,
               i.min_amount,
               inst.name          as institucion,
               inst.credit_rating as calificacion,
               inst.rating_tier
        from public.instruments i
        join public.institutions inst on inst.id = i.institution_id
        where i.is_active
        order by inst.rating_tier, i.expected_return desc nulls last
        """
    ).fetchall()
    return [
        ItemCatalogo(
            nombre=f["nombre"],
            institucion=f["institucion"],
            calificacion=f["calificacion"],
            rating_tier=f["rating_tier"],
            clase_activo=f["clase_activo"],
            retorno=float(f["retorno"]) if f["retorno"] is not None else None,
            plazo_dias=f["plazo_dias"],
            min_amount=float(f["min_amount"]) if f["min_amount"] is not None else None,
            elegible=max_rating_tier is not None and f["rating_tier"] <= max_rating_tier,
        )
        for f in filas
    ]


def _subcuentas(conn: Connection, investor_id: str, session_id: str) -> list[Subcuenta]:
    """Las sesiones (subcuentas) completadas del inversionista, para poder compararlas."""
    filas = conn.execute(
        """
        select s.id::text as sid, s.subaccount_name, s.amount, rp.code as perfil,
               (select i.name
                  from public.proposal_items pi
                  join public.instruments i on i.id = pi.instrument_id
                  join public.proposals pr  on pr.id = pi.proposal_id
                 where pr.session_id = s.id
                 order by pi.percentage desc
                 limit 1) as principal
        from public.profiling_sessions s
        left join public.risk_profiles rp on rp.id = s.risk_profile_id
        where s.investor_id = %s and s.completed_at is not null and rp.code is not null
        order by s.created_at desc
        """,
        (investor_id,),
    ).fetchall()
    return [
        Subcuenta(
            nombre=f["subaccount_name"],
            monto=float(f["amount"]) if f["amount"] is not None else None,
            perfil=f["perfil"],
            instrumento_principal=f["principal"],
            es_actual=f["sid"] == session_id,
        )
        for f in filas
    ]


def _calificaciones_validas(conn: Connection) -> list[str]:
    """Todas las calificaciones que existen en la base — las que el agente puede nombrar."""
    filas = conn.execute(
        "select distinct credit_rating from public.institutions"
    ).fetchall()
    return [f["credit_rating"] for f in filas]


def _capital(
    conn: Connection, investor_id: str, subcuentas: list[Subcuenta]
) -> tuple[float | None, float | None, float | None]:
    """Techo de capital del inversionista, lo repartido y lo libre (misma lógica que
    `listar_subcuentas`). None si no declaró un capital total."""
    fila = conn.execute(
        "select total_capital::float as capital from public.profiles where id = %s",
        (investor_id,),
    ).fetchone()
    capital = fila["capital"] if fila else None
    asignado = round(sum(s.monto for s in subcuentas if s.monto is not None), 2)
    sin_asignar = round(capital - asignado, 2) if capital is not None else None
    return capital, asignado, sin_asignar


def _contexto_agente(conn: Connection, session_id: str) -> tuple[ContextoAgente, str]:
    """Reúne TODO lo que el agente conoce del inversionista para analizar, no solo describir."""
    datos, proposal_id = _datos_de_sesion(conn, session_id)
    max_tier, regla = _elegibilidad(conn, session_id)
    subcuentas = _subcuentas(conn, datos.investor.investor_id, session_id)
    capital_total, asignado, sin_asignar = _capital(conn, datos.investor.investor_id, subcuentas)
    contexto = ContextoAgente(
        datos=datos,
        max_rating_tier=max_tier,
        regla_elegibilidad=regla,
        catalogo=_catalogo(conn, max_tier),
        subcuentas=subcuentas,
        calificaciones_validas=_calificaciones_validas(conn),
        capital_total=capital_total,
        asignado=asignado,
        sin_asignar=sin_asignar,
    )
    return contexto, proposal_id


# ===========================================================================
# Historial (continuidad de la conversación)
# ===========================================================================


def _cargar_historial(conn: Connection, session_id: str) -> list[tuple[str, str]]:
    """Los últimos turnos del chat de esa sesión, en orden cronológico.

    Se filtra por `thread_id` para leer SOLO el chat: las interacciones de la
    generación de la propuesta viven en la misma tabla pero sin `thread_id`, y su
    contenido es el prompt completo — no debe entrar como historial.
    """
    filas = conn.execute(
        """
        select role, content
        from public.llm_interactions
        where thread_id = %s and role in ('user', 'assistant')
        order by created_at desc
        limit %s
        """,
        (session_id, _MAX_HISTORIAL_FILAS),
    ).fetchall()

    # LangChain espera 'human'/'ai'; la tabla guarda 'user'/'assistant'.
    mapa = {"user": "human", "assistant": "ai"}
    return [(mapa[f["role"]], f["content"]) for f in reversed(filas)]


def _guardar_turno(
    conn: Connection,
    session_id: str,
    proposal_id: str,
    mensaje: str,
    estado: dict[str, Any],
) -> None:
    """Persiste el turno del usuario y el del asistente (evidencia + memoria)."""
    conn.execute(
        """
        insert into public.llm_interactions
            (session_id, proposal_id, thread_id, role, content, platform)
        values (%s, %s, %s, 'user', %s, 'api')
        """,
        (session_id, proposal_id, session_id, mensaje),
    )
    conn.execute(
        """
        insert into public.llm_interactions
            (session_id, proposal_id, thread_id, role, content, model,
             guardrail_passed, retry_count, metadata, platform)
        values (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s, 'api')
        """,
        (
            session_id,
            proposal_id,
            session_id,
            estado["texto"],
            estado["modelo"],
            estado["guardrail_passed"],
            estado.get("retry_count", 0),
            Jsonb(
                {
                    "sources": estado.get("sources", []),
                    "guardrail_motivos": estado.get("motivos", []),
                    "en_alcance": estado.get("en_alcance", True),
                }
            ),
        ),
    )


# ===========================================================================
# Entrada pública
# ===========================================================================


def proveedores() -> list[ProviderInfo]:
    """Catálogo de proveedores para el selector del front (sin exponer keys)."""
    return [ProviderInfo(**p) for p in listar_proveedores()]


async def chat(payload: AgentChatRequest, usuario: CurrentUser) -> AgentChatResponse:
    """Un turno de conversación, de punta a punta."""
    # 1. Resolver sesión + autorizar + armar contexto e historial (todo en una conexión).
    with get_connection() as conn:
        session_id = _resolver_session_id(conn, payload.session_id, usuario)
        contexto, proposal_id = _contexto_agente(conn, session_id)
        # El cliente solo conversa sobre lo suyo; el asesor, sobre lo de cualquiera.
        exige_dueno_o_asesor(contexto.datos.investor.investor_id, usuario)
        historial = _cargar_historial(conn, session_id)

    # 2. Correr el grafo (fuera de la transacción: la llamada al LLM puede tardar).
    estado = await responder(contexto, payload.mensaje, historial, provider=payload.provider)

    # Prueba en el terminal de qué proveedor/modelo contestó este turno.
    log.warning(
        "[agent] provider_pedido=%s -> modelo=%s | guardrail=%s | en_alcance=%s",
        payload.provider or "(default .env)",
        estado["modelo"],
        estado["guardrail_passed"],
        estado.get("en_alcance"),
    )

    # 3. Guardar los dos turnos.
    with get_connection() as conn:
        _guardar_turno(conn, session_id, proposal_id, payload.mensaje, estado)

    return AgentChatResponse(
        texto=estado["texto"],
        sources=[SourceChip(**c) for c in estado.get("sources", [])],
        guardrail_passed=estado["guardrail_passed"],
        modelo=estado["modelo"],
        en_alcance=estado.get("en_alcance", True),
    )


# ===========================================================================
# La recomendación del simulador (el motor elige, la IA explica)
# ===========================================================================


def _ultima_sesion(conn: Connection, investor_id: str) -> str | None:
    """La última sesión completada, solo para poder ARCHIVAR el turno. Puede no haber."""
    fila = conn.execute(
        """
        select id::text as sid
        from public.profiling_sessions
        where investor_id = %s and completed_at is not null
        order by created_at desc
        limit 1
        """,
        (investor_id,),
    ).fetchone()
    return fila["sid"] if fila else None


def _archivar_simulacion(
    conn: Connection,
    session_id: str | None,
    investor_id: str,
    pregunta: str,
    rec: simulator_ai.Recomendacion,
) -> None:
    """Deja la recomendación en `llm_interactions` (misma evidencia que el chat).

    El `thread_id` NO es la sesión sino `sim:<investor_id>`: así el simulador queda
    auditable sin colarse en el historial del chat, que se relee filtrando por
    `thread_id = session_id`.
    """
    conn.execute(
        """
        insert into public.llm_interactions
            (session_id, thread_id, role, content, platform)
        values (%s, %s, 'user', %s, 'api')
        """,
        (session_id, f"sim:{investor_id}", pregunta),
    )
    conn.execute(
        """
        insert into public.llm_interactions
            (session_id, thread_id, role, content, model,
             guardrail_passed, retry_count, metadata, platform)
        values (%s, %s, 'assistant', %s, %s, %s, %s, %s, 'api')
        """,
        (
            session_id,
            f"sim:{investor_id}",
            rec.texto,
            rec.modelo,
            rec.guardrail_passed,
            rec.retry_count,
            Jsonb({"sources": rec.sources, "guardrail_motivos": rec.motivos, "origen": "simulador"}),
        ),
    )


async def recomendar_simulacion(
    payload: SimuladorRequest, usuario: CurrentUser
) -> SimuladorResponse:
    """Recomendación de IA sobre la simulación que el usuario tiene en pantalla.

    Las opciones se piden con el MISMO `listar_tasas` que sirve al simulador (y con
    `todos_los_plazos`, porque el usuario puede haber cambiado de banco o de fondo): la
    IA cita exactamente las cifras que él está viendo, no otras calculadas aparte.
    """
    catalogo = await catalog_controller.listar_tasas(
        usuario.id, payload.monto, payload.plazo_dias, todos_los_plazos=True
    )

    por_code = {t.code: t for t in catalogo.tasas}
    seleccionado = por_code.get(payload.seleccion_code) if payload.seleccion_code else None
    if payload.seleccion_code and seleccionado is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"El producto {payload.seleccion_code} no está en el catálogo activo.",
        )

    sim = simulator_ai.Simulacion(
        monto=payload.monto,
        plazo_dias=payload.plazo_dias,
        perfil=catalogo.perfil,
        tasas=catalogo.tasas,
        # La misma fila que el front tiene destacada: `listar_tasas` ya la marcó.
        recomendado=next((t for t in catalogo.tasas if t.recomendado), None),
        seleccionado=seleccionado,
    )

    rec = await simulator_ai.recomendar(sim, provider=payload.provider)

    log.warning(
        "[simulador] monto=%s plazo=%s seleccion=%s -> modelo=%s | guardrail=%s",
        payload.monto,
        payload.plazo_dias,
        payload.seleccion_code,
        rec.modelo,
        rec.guardrail_passed,
    )

    pregunta = (
        f"[simulador] monto={payload.monto} plazo_dias={payload.plazo_dias} "
        f"seleccion={payload.seleccion_code or '-'}"
    )
    with get_connection() as conn:
        _archivar_simulacion(conn, _ultima_sesion(conn, usuario.id), usuario.id, pregunta, rec)

    return SimuladorResponse(
        recomendado_code=sim.recomendado.code if sim.recomendado else None,
        texto=rec.texto,
        sources=[SourceChip(**c) for c in rec.sources],
        guardrail_passed=rec.guardrail_passed,
        modelo=rec.modelo,
    )
