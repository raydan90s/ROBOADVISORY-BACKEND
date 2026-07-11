"""Lógica de negocio del asesor (HU3) y de la auditoría.

Las vistas (`v_advisor_review_queue`, `v_investor_proposal_summary`, `v_audit_timeline`)
hacen el trabajo pesado: acá casi todo es plomería. Lo único que no es plomería es
`revisar_propuesta`, y su trabajo real es **desconfiar del cliente**:

- una propuesta solo se revisa una vez (`for update` + chequeo de estado),
- los instrumentos de una asignación editada se verifican contra el catálogo,
- los montos en USD los recalcula Postgres, nunca Python ni el LLM.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from psycopg import Connection
from psycopg.types.json import Jsonb

from src.config.database import fetch_all, get_connection
from src.models.advisor import (
    ColaItem,
    Decision,
    EventoAuditoria,
    LineaPropuesta,
    PropuestaDetalle,
    RevisionPrevia,
    RevisionRequest,
    RevisionResultado,
)
from src.models.auth import CurrentUser
from src.models.investor import EstadoPropuesta


def _uuid_valido(valor: str, campo: str) -> str:
    """Un id malformado es un 422 del cliente, no un 500 nuestro."""
    try:
        return str(UUID(valor))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{campo}' no es un identificador válido: {valor}",
        ) from exc


# ===========================================================================
# Cola de revisión
# ===========================================================================


async def listar_cola() -> list[ColaItem]:
    """Todo lo que espera decisión, lo más viejo primero (es una cola, no un tablero)."""
    filas = fetch_all(
        """
        select proposal_id::text        as proposal_id,
               session_id::text         as session_id,
               investor_id::text        as investor_id,
               investor_name            as investor_nombre,
               cedula_ruc,
               total_score              as puntaje,
               risk_profile_name        as perfil_riesgo,
               expected_risk            as riesgo_esperado,
               status                   as estado,
               total_amount             as monto_total,
               explanation              as explicacion,
               proposal_created_at      as creada_en
        from public.v_advisor_review_queue
        order by proposal_created_at
        """
    )
    return [ColaItem(**f) for f in filas]


# ===========================================================================
# Detalle de una propuesta
# ===========================================================================


def _lineas_de(conn: Connection, proposal_id: str) -> list[LineaPropuesta]:
    """Las líneas de la propuesta con emisor, calificación y su fuente.

    `min_amount` no está en la vista y se une por `code` (que es único en `instruments`):
    es lo que habilita la bandera de "monto bajo el mínimo de acceso".
    """
    filas = conn.execute(
        """
        select v.instrument_code             as instrumento_code,
               v.instrument_name             as nombre,
               v.product_type                as tipo_producto,
               v.risk_class                  as riesgo,
               v.percentage                  as porcentaje,
               v.amount                      as monto_asignado,
               v.expected_return             as retorno_esperado,
               v.term_days                   as plazo_dias,
               v.institution_name            as institucion,
               v.institution_rating          as calificacion,
               v.institution_rating_source   as calificacion_fuente,
               v.institution_rating_date     as calificacion_fecha,
               i.min_amount                  as monto_minimo
        from public.v_investor_proposal_summary v
        join public.instruments i on i.code = v.instrument_code
        where v.proposal_id = %s
        order by v.percentage desc
        """,
        (proposal_id,),
    ).fetchall()
    return [LineaPropuesta(**f) for f in filas]


def _banderas(cabecera: dict[str, Any], lineas: list[LineaPropuesta]) -> list[str]:
    """Puntos de atención para el asesor. Comparaciones, cero IA.

    Que sean deterministas es el punto: son las únicas afirmaciones de la pantalla
    del asesor que no necesitan que nadie confíe en un modelo.
    """
    avisos: list[str] = []

    for linea in lineas:
        if (
            linea.monto_asignado is not None
            and linea.monto_minimo is not None
            and linea.monto_asignado < linea.monto_minimo
        ):
            avisos.append(
                f"El monto asignado a {linea.nombre} (USD {linea.monto_asignado:,.2f}) "
                f"queda bajo su mínimo de acceso (USD {linea.monto_minimo:,.2f})."
            )

    puntaje = cabecera["puntaje"]
    minimo, maximo = cabecera["umbral_min"], cabecera["umbral_max"]
    if puntaje is not None and minimo is not None and maximo is not None:
        if puntaje == minimo or puntaje == maximo:
            avisos.append(
                f"El puntaje ({puntaje}) está en el borde del rango del perfil "
                f"{cabecera['perfil_riesgo']} ({minimo}–{maximo}): un punto más o menos "
                "lo habría cambiado de perfil."
            )

    if cabecera["monto_total"] is None:
        avisos.append(
            "La sesión de perfilamiento no registró un monto: la propuesta solo tiene "
            "porcentajes."
        )

    return avisos


def _cabecera(conn: Connection, proposal_id: str) -> dict[str, Any]:
    fila = conn.execute(
        """
        select p.id::text          as proposal_id,
               s.id::text          as session_id,
               inv.id::text        as investor_id,
               inv.full_name       as investor_nombre,
               inv.email           as investor_email,
               inv.cedula_ruc,
               s.total_score       as puntaje,
               rp.name             as perfil_riesgo,
               p.expected_risk     as riesgo_esperado,
               p.status            as estado,
               p.total_amount      as monto_total,
               p.explanation       as explicacion,
               p.created_at        as creada_en,
               pt.min_score        as umbral_min,
               pt.max_score        as umbral_max
        from public.proposals p
        join public.profiling_sessions s  on s.id = p.session_id
        join public.profiles inv          on inv.id = s.investor_id
        left join public.risk_profiles rp on rp.id = s.risk_profile_id
        left join public.profile_thresholds pt
               on pt.rules_version_id = s.rules_version_id
              and pt.risk_profile_id  = s.risk_profile_id
        where p.id = %s
        """,
        (proposal_id,),
    ).fetchone()

    if not fila:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe la propuesta {proposal_id}.",
        )
    return fila


def _revisiones_de(conn: Connection, proposal_id: str) -> list[RevisionPrevia]:
    filas = conn.execute(
        """
        select ar.id::text          as review_id,
               ar.decision,
               ar.comments,
               ar.advisor_id::text  as advisor_id,
               adv.full_name        as advisor_nombre,
               rv.version_label     as rules_version,
               ar.decided_at
        from public.advisor_reviews ar
        left join public.profiles adv       on adv.id = ar.advisor_id
        left join public.rules_versions rv   on rv.id = ar.rules_version_id
        where ar.proposal_id = %s
        order by ar.decided_at desc
        """,
        (proposal_id,),
    ).fetchall()
    return [RevisionPrevia(**f) for f in filas]


async def obtener_detalle(proposal_id: str) -> PropuestaDetalle:
    """La pantalla de revisión completa: cabecera + líneas + banderas + historial."""
    proposal_id = _uuid_valido(proposal_id, "proposal_id")

    with get_connection() as conn:
        cabecera = _cabecera(conn, proposal_id)
        lineas = _lineas_de(conn, proposal_id)

        return PropuestaDetalle(
            **{k: v for k, v in cabecera.items() if k not in ("umbral_min", "umbral_max")},
            allocations=lineas,
            banderas=_banderas(cabecera, lineas),
            revisiones=_revisiones_de(conn, proposal_id),
        )


# ===========================================================================
# Decisión del asesor (HU3)
# ===========================================================================


def _instrumentos_del_catalogo(conn: Connection, codigos: list[str]) -> dict[str, str]:
    """Traduce códigos a ids, exigiendo que TODOS existan.

    El catálogo es cerrado: un asesor no puede meter a mano un producto que el banco
    no comercializa, igual que no puede hacerlo el LLM.
    """
    filas = conn.execute(
        "select id, code from public.instruments where code = any(%s)",
        (codigos,),
    ).fetchall()

    por_codigo = {f["code"]: f["id"] for f in filas}
    desconocidos = [c for c in codigos if c not in por_codigo]
    if desconocidos:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Estos códigos no están en el catálogo de instrumentos: "
                + ", ".join(desconocidos)
            ),
        )
    return por_codigo


async def revisar_propuesta(
    proposal_id: str, payload: RevisionRequest, asesor: CurrentUser
) -> RevisionResultado:
    """Aprueba, edita o rechaza. Todo o nada, en una sola transacción."""
    proposal_id = _uuid_valido(proposal_id, "proposal_id")

    with get_connection() as conn:
        # `for update` serializa dos asesores decidiendo sobre la misma propuesta:
        # el segundo espera, lee el estado ya cambiado y recibe el 409.
        propuesta = conn.execute(
            """
            select p.id, p.status, s.rules_version_id, rv.version_label
            from public.proposals p
            join public.profiling_sessions s on s.id = p.session_id
            join public.rules_versions rv    on rv.id = s.rules_version_id
            where p.id = %s
            for update of p
            """,
            (proposal_id,),
        ).fetchone()

        if not propuesta:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No existe la propuesta {proposal_id}.",
            )

        if propuesta["status"] != EstadoPropuesta.PENDIENTE_REVISION.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"La propuesta ya fue revisada (estado: {propuesta['status']}). "
                    "Una decisión no se sobrescribe: queda en el registro de auditoría."
                ),
            )

        snapshot: list[dict[str, Any]] | None = None

        if payload.decision is Decision.EDITADA:
            assert payload.edited_allocation is not None  # lo garantiza RevisionRequest
            lineas = payload.edited_allocation
            por_codigo = _instrumentos_del_catalogo(
                conn, [linea.instrumento_code for linea in lineas]
            )

            conn.execute(
                "delete from public.proposal_items where proposal_id = %s", (proposal_id,)
            )
            for linea in lineas:
                # El USD lo calcula Postgres a partir del total guardado: es un número
                # que después entra al set permitido del guardarraíl (Fase 3).
                conn.execute(
                    """
                    insert into public.proposal_items
                        (proposal_id, instrument_id, percentage, amount)
                    select %s, %s, %s, round(p.total_amount * %s / 100.0, 2)
                    from public.proposals p
                    where p.id = %s
                    """,
                    (
                        proposal_id,
                        por_codigo[linea.instrumento_code],
                        linea.porcentaje,
                        linea.porcentaje,
                        proposal_id,
                    ),
                )

            snapshot = [
                {
                    "instrumento_code": linea.instrumento_code,
                    "porcentaje": float(linea.porcentaje),
                }
                for linea in lineas
            ]

        # HU3: fecha (decided_at) + versión de reglas (rules_version_id) + responsable
        # (advisor_id). Las tres en la misma fila, y la fila es inmutable.
        revision = conn.execute(
            """
            insert into public.advisor_reviews
                (proposal_id, advisor_id, decision, comments, rules_version_id, edited_allocation)
            values (%s, %s, %s, %s, %s, %s)
            returning id::text as review_id, decided_at
            """,
            (
                proposal_id,
                asesor.id,
                payload.decision.value,
                payload.comments,
                propuesta["rules_version_id"],
                Jsonb(snapshot) if snapshot is not None else None,
            ),
        ).fetchone()

        estado = conn.execute(
            """
            update public.proposals set status = %s where id = %s
            returning status
            """,
            (payload.decision.value, proposal_id),
        ).fetchone()

        conn.execute(
            """
            insert into public.audit_log
                (entity_type, entity_id, actor_id, action, platform, metadata)
            values ('proposal', %s, %s, %s, 'web', %s)
            """,
            (
                proposal_id,
                asesor.id,
                payload.decision.value,
                Jsonb(
                    {
                        "review_id": revision["review_id"],
                        "rules_version": propuesta["version_label"],
                        "comments": payload.comments,
                        "edited_allocation": snapshot,
                    }
                ),
            ),
        )

        return RevisionResultado(
            review_id=revision["review_id"],
            proposal_id=proposal_id,
            decision=payload.decision,
            estado=EstadoPropuesta(estado["status"]),
            advisor_id=asesor.id,
            advisor_nombre=asesor.full_name,
            rules_version=propuesta["version_label"],
            decided_at=revision["decided_at"],
            comments=payload.comments,
            allocations=_lineas_de(conn, proposal_id),
        )


# ===========================================================================
# Auditoría
# ===========================================================================


async def listar_auditoria(limite: int = 100) -> list[EventoAuditoria]:
    """El timeline de `v_audit_timeline`, ya ordenado por la vista (lo más nuevo primero)."""
    filas = fetch_all(
        """
        select id::text        as id,
               created_at,
               entity_type,
               entity_id::text as entity_id,
               action,
               platform,
               metadata,
               actor_name      as actor_nombre,
               actor_role      as actor_rol
        from public.v_audit_timeline
        limit %s
        """,
        (limite,),
    )
    return [EventoAuditoria(**f) for f in filas]
