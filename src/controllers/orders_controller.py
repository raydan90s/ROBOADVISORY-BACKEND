"""Lógica de negocio de la orden de inversión: el paso que le faltaba al producto.

Hasta la fase 5 la app terminaba en la firma del asesor. Acá esa firma se convierte en N
instrucciones hacia N bancos, y en la prima que sostiene el negocio.

Igual que `advisor_controller.revisar_propuesta`, el trabajo real de este módulo es
**desconfiar del cliente**:

- solo se cursa una propuesta propia,
- solo si un asesor la firmó (lo verifica además un trigger: ver `fn_valida_orden_firmada`),
- solo hacia instituciones con convenio (otro trigger: `fn_valida_convenio_item`),
- una sola vez (UNIQUE en `investment_orders.proposal_id`),
- y la comisión no la calcula Python: es una columna GENERATED de Postgres.

Los chequeos de acá no reemplazan a los triggers, los anteceden: la base es la que
garantiza la regla, y este módulo la traduce a un 403/409 con un mensaje que se puede
leer. Si alguien borra estas líneas, el sistema sigue siendo correcto — solo se vuelve
grosero.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from psycopg import Connection
from psycopg.types.json import Jsonb

from src.config.database import fetch_all, get_connection
from src.models.auth import CurrentUser, Rol
from src.models.investor import EstadoPropuesta
from src.models.orders import (
    CatalogoConvenios,
    Convenio,
    EstadoOrden,
    LineaOrden,
    Orden,
    OrdenFeedItem,
    PoliticaComision,
    ResumenComisiones,
)
from src.services import bank_gateway

# Los estados desde los que una propuesta se puede cursar. 'edited' cuenta: el asesor no
# solo la firmó, además la corrigió con su nombre — está MÁS revisada, no menos.
_FIRMADAS = (EstadoPropuesta.APROBADA.value, EstadoPropuesta.EDITADA.value)


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
# Lectura del comprobante
# ===========================================================================


def _orden_completa(conn: Connection, order_id: str) -> Orden:
    """El comprobante: la cabecera de `v_investment_order_summary` + sus líneas.

    La vista devuelve una fila por línea, así que la cabecera se toma de la primera: son
    todas la misma orden.
    """
    filas = conn.execute(
        """
        select v.*, cp.rationale as comision_rationale
        from public.v_investment_order_summary v
        join public.investment_orders o on o.id = v.order_id
        left join public.commission_policies cp
               on cp.rules_version_id = o.rules_version_id
        where v.order_id = %s
        order by v.amount desc
        """,
        (order_id,),
    ).fetchall()

    if not filas:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe la orden {order_id}.",
        )

    c = filas[0]
    return Orden(
        order_id=str(c["order_id"]),
        proposal_id=str(c["proposal_id"]),
        investor_id=str(c["investor_id"]),
        investor_nombre=c["investor_name"],
        advisor_id=str(c["advisor_id"]) if c["advisor_id"] else None,
        advisor_nombre=c["advisor_name"],
        estado=EstadoOrden(c["order_status"]),
        is_simulated=c["is_simulated"],
        monto_total=float(c["total_amount"]),
        comision_bps=c["comision_bps"],
        comision_total=float(c["comision_total"]),
        monto_invertido=float(c["monto_invertido"]),
        comision_rationale=c["comision_rationale"],
        rules_version=c["rules_version"],
        creada_en=c["created_at"],
        confirmada_en=c["confirmed_at"],
        lineas=[
            LineaOrden(
                item_id=str(f["item_id"]),
                instrumento_code=f["instrument_code"],
                instrumento_nombre=f["instrument_name"],
                institucion=f["institution_name"],
                calificacion=f["institution_rating"],
                tipo_institucion=f["institution_type"],
                monto=float(f["amount"]),
                porcentaje=float(f["percentage"]),
                comision=float(f["item_comision"]),
                monto_invertido=float(f["item_monto_invertido"]),
                bank_reference=f["bank_reference"],
                estado=EstadoOrden(f["item_status"]),
                confirmada_en=f["item_confirmed_at"],
            )
            for f in filas
        ],
    )


def _exige_dueno_de_la_orden(orden: dict[str, Any], usuario: CurrentUser) -> None:
    """El dueño de la plata, o un asesor. El id del token manda, no el de la URL.

    Mismo criterio que `exige_dueno_o_asesor`, pero sobre el dueño que dice la ORDEN y no
    un id que venga en la ruta: acá el `investor_id` no lo escribe el cliente, sale de la
    fila. Por eso no se reutiliza aquella: no hay nada de qué desconfiar en la URL.
    """
    if usuario.role is Rol.ADVISOR:
        return
    if str(orden["investor_id"]) != usuario.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo puedes ver tus propias órdenes.",
        )


async def obtener_orden(order_id: str, usuario: CurrentUser) -> Orden:
    order_id = _uuid_valido(order_id, "order_id")
    with get_connection() as conn:
        fila = conn.execute(
            "select investor_id from public.investment_orders where id = %s", (order_id,)
        ).fetchone()
        if not fila:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No existe la orden {order_id}.",
            )
        _exige_dueno_de_la_orden(fila, usuario)
        return _orden_completa(conn, order_id)


async def orden_de_propuesta(proposal_id: str, usuario: CurrentUser) -> Orden | None:
    """La orden de una propuesta, si ya se cursó.

    Devuelve `None` en vez de 404 a propósito: la pregunta que hace la pantalla de la
    propuesta es «¿esto ya se invirtió?», y "todavía no" es una respuesta legítima, no un
    error. Es lo que decide si se pinta el botón o el comprobante.
    """
    proposal_id = _uuid_valido(proposal_id, "proposal_id")
    with get_connection() as conn:
        fila = conn.execute(
            "select id, investor_id from public.investment_orders where proposal_id = %s",
            (proposal_id,),
        ).fetchone()
        if not fila:
            return None
        _exige_dueno_de_la_orden(fila, usuario)
        return _orden_completa(conn, str(fila["id"]))


# ===========================================================================
# Cursar la orden (paso 1: 'sent')
# ===========================================================================


async def cursar_orden(
    proposal_id: str, usuario: CurrentUser, platform: str = "mobile"
) -> Orden:
    """«Invertir ahora»: convierte una propuesta firmada en N instrucciones bancarias.

    Nace en 'sent' y no en 'confirmed' porque son dos hechos distintos y el sistema no
    debe confundirlos: uno es "el cliente decidió", el otro es "el banco acusó". Entre los
    dos está el momento en que al asesor le entra el aviso y llama — que es exactamente el
    momento que el producto no tenía.
    """
    proposal_id = _uuid_valido(proposal_id, "proposal_id")

    with get_connection() as conn:
        # Mismo candado que toma `revisar_propuesta` (`for update of p`): si un asesor está
        # rechazando esta propuesta en este instante, uno de los dos espera al otro y el
        # segundo lee el estado ya cambiado. Sin esto se podría cursar una orden sobre una
        # propuesta que acaba de ser rechazada.
        propuesta = conn.execute(
            """
            select p.id, p.status, p.total_amount,
                   s.investor_id, s.rules_version_id, rv.version_label
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

        if str(propuesta["investor_id"]) != usuario.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo puedes invertir tu propia propuesta.",
            )

        # El corazón del producto, y por eso está dicho dos veces: acá para que el mensaje
        # se entienda, y en `fn_valida_orden_firmada` para que sea verdad.
        if propuesta["status"] not in _FIRMADAS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Esta propuesta está en estado '{propuesta['status']}'. Una propuesta "
                    "no se puede invertir hasta que un asesor la revise y la firme."
                ),
            )

        if propuesta["total_amount"] is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Esta propuesta no tiene monto: solo tiene porcentajes, y no hay nada "
                    "que cursar."
                ),
            )

        ya_existe = conn.execute(
            "select id from public.investment_orders where proposal_id = %s",
            (proposal_id,),
        ).fetchone()
        if ya_existe:
            # 409 y no un error feo del UNIQUE: dos taps seguidos en "Invertir ahora" son
            # lo más normal del mundo y el cliente merece ver su comprobante, no un 500.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Esta propuesta ya fue invertida. Revisa tu comprobante.",
            )

        politica = conn.execute(
            "select comision_bps from public.commission_policies where rules_version_id = %s",
            (propuesta["rules_version_id"],),
        ).fetchone()
        if not politica:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "No hay una política de comisión publicada para la versión de reglas "
                    f"{propuesta['version_label']}. No se puede cursar una orden sin saber "
                    "cuánto cuesta."
                ),
            )

        # Quién firmó: la revisión más reciente que aprobó o editó. Es quien respondió con
        # su nombre por esta propuesta, y por eso es de quien es la comisión.
        revision = conn.execute(
            """
            select id, advisor_id
            from public.advisor_reviews
            where proposal_id = %s and decision in ('approved', 'edited')
            order by decided_at desc
            limit 1
            """,
            (proposal_id,),
        ).fetchone()

        lineas = conn.execute(
            """
            select pi.instrument_id, pi.percentage, pi.amount, i.institution_id
            from public.proposal_items pi
            join public.instruments i on i.id = pi.instrument_id
            where pi.proposal_id = %s
            order by pi.percentage desc
            """,
            (proposal_id,),
        ).fetchall()

        if not lineas:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Esta propuesta no tiene líneas que cursar.",
            )
        if any(linea["amount"] is None for linea in lineas):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Alguna línea de esta propuesta no tiene monto en USD. Vuelve a abrir "
                    "tu propuesta para que se recalcule."
                ),
            )

        orden = conn.execute(
            """
            insert into public.investment_orders
                (proposal_id, investor_id, advisor_id, review_id,
                 rules_version_id, comision_bps, total_amount)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id::text as id
            """,
            (
                proposal_id,
                usuario.id,
                revision["advisor_id"] if revision else None,
                revision["id"] if revision else None,
                propuesta["rules_version_id"],
                politica["comision_bps"],
                propuesta["total_amount"],
            ),
        ).fetchone()

        for linea in lineas:
            # `comision_bps` se copia a cada línea para que la comisión por línea también
            # sea GENERATED: ninguna cifra de plata en esta tabla la escribe Python.
            conn.execute(
                """
                insert into public.investment_order_items
                    (order_id, instrument_id, institution_id, amount, percentage, comision_bps)
                values (%s, %s, %s, %s, %s, %s)
                """,
                (
                    orden["id"],
                    linea["instrument_id"],
                    linea["institution_id"],
                    linea["amount"],
                    linea["percentage"],
                    politica["comision_bps"],
                ),
            )

        conn.execute(
            """
            insert into public.audit_log
                (entity_type, entity_id, actor_id, action, platform, metadata)
            values ('order', %s, %s, 'sent', %s, %s)
            """,
            (
                orden["id"],
                usuario.id,
                platform,
                Jsonb(
                    {
                        "proposal_id": proposal_id,
                        "rules_version": propuesta["version_label"],
                        "comision_bps": politica["comision_bps"],
                        "lineas": len(lineas),
                        "review_id": str(revision["id"]) if revision else None,
                        "is_simulated": True,
                    }
                ),
            ),
        )

        return _orden_completa(conn, orden["id"])


# ===========================================================================
# Confirmar la orden (paso 2: 'confirmed')
# ===========================================================================


async def confirmar_orden(
    order_id: str, usuario: CurrentUser, platform: str = "mobile"
) -> Orden:
    """El acuse del banco: cada línea recibe su referencia y la orden queda confirmada.

    Quién dispara esto: hoy, la app, cuando termina de mostrar la conexión con cada banco.
    En una integración real sería el banco quien llame (un webhook firmado, como el de
    Twilio en `whatsapp_routes`) y este endpoint dejaría de estar detrás del token del
    cliente. Que hoy lo llame la app es una decisión de la simulación, no del diseño: el
    estado 'sent' existe y significa lo mismo en ambos mundos.

    Es idempotente: confirmar una orden ya confirmada devuelve el comprobante en vez de
    fallar. Un reintento por red inestable no es un error del usuario.
    """
    order_id = _uuid_valido(order_id, "order_id")

    with get_connection() as conn:
        orden = conn.execute(
            """
            select id, investor_id, status
            from public.investment_orders
            where id = %s
            for update
            """,
            (order_id,),
        ).fetchone()

        if not orden:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No existe la orden {order_id}.",
            )

        _exige_dueno_de_la_orden(orden, usuario)

        if orden["status"] == EstadoOrden.CONFIRMADA.value:
            return _orden_completa(conn, order_id)

        if orden["status"] == EstadoOrden.FALLIDA.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Esta orden falló en el banco. No se puede confirmar.",
            )

        lineas = conn.execute(
            """
            select it.id::text as id, inst.name as institucion
            from public.investment_order_items it
            left join public.institutions inst on inst.id = it.institution_id
            where it.order_id = %s
            """,
            (order_id,),
        ).fetchall()

        acuses = bank_gateway.cursar([(f["id"], f["institucion"]) for f in lineas])

        for acuse in acuses:
            conn.execute(
                """
                update public.investment_order_items
                set bank_reference = %s, status = 'confirmed', confirmed_at = now()
                where id = %s
                """,
                (acuse.bank_reference, acuse.item_id),
            )

        conn.execute(
            """
            update public.investment_orders
            set status = 'confirmed', confirmed_at = now()
            where id = %s
            """,
            (order_id,),
        )

        conn.execute(
            """
            insert into public.audit_log
                (entity_type, entity_id, actor_id, action, platform, metadata)
            values ('order', %s, %s, 'confirmed', %s, %s)
            """,
            (
                order_id,
                usuario.id,
                platform,
                Jsonb(
                    {
                        "referencias": {a.item_id: a.bank_reference for a in acuses},
                        "is_simulated": True,
                    }
                ),
            ),
        )

        return _orden_completa(conn, order_id)


# ===========================================================================
# El convenio y la comisión, a la vista
# ===========================================================================


async def catalogo_convenios() -> CatalogoConvenios:
    """Con quién tenemos convenio y cuánto cobramos. Lectura pura.

    Es la pantalla que contesta «¿me recomiendas al que más te paga?». La respuesta no es
    un párrafo tranquilizador: es la lista de instituciones y UNA tasa que no depende de
    ninguna de ellas.
    """
    politica_fila = fetch_all(
        """
        select cp.comision_bps, cp.rationale, rv.version_label
        from public.commission_policies cp
        join public.rules_versions rv on rv.id = cp.rules_version_id
        where rv.is_active
        limit 1
        """
    )

    convenios = fetch_all(
        """
        select inst.code,
               inst.name              as nombre,
               inst.institution_type  as tipo,
               inst.credit_rating     as calificacion,
               inst.rating_source     as calificacion_fuente,
               inst.rating_date       as calificacion_fecha,
               inst.convenio_activo,
               inst.convenio_desde,
               count(i.id)            as productos
        from public.institutions inst
        left join public.instruments i on i.institution_id = inst.id and i.is_active
        where inst.is_active
        group by inst.id, inst.code, inst.name, inst.institution_type,
                 inst.credit_rating, inst.rating_source, inst.rating_date,
                 inst.convenio_activo, inst.convenio_desde
        order by inst.convenio_activo desc, inst.rating_tier, inst.name
        """
    )

    politica = None
    if politica_fila:
        p = politica_fila[0]
        politica = PoliticaComision(
            comision_bps=p["comision_bps"],
            # La división la hace el servidor y no el front por la misma razón de siempre:
            # 50 bps son 0,5% en un solo lugar, no en cada pantalla que lo necesite.
            comision_porcentaje=p["comision_bps"] / 100,
            rationale=p["rationale"],
            rules_version=p["version_label"],
        )

    return CatalogoConvenios(
        politica=politica,
        convenios=[Convenio(**f) for f in convenios],
    )


# ===========================================================================
# El asesor
# ===========================================================================


async def listar_feed_ordenes(limite: int = 50) -> list[OrdenFeedItem]:
    """El aviso: quién acaba de invertir, cuánto y en cuántos bancos.

    Lo más nuevo primero — al revés que la cola de revisión, que es una cola y se atiende
    por antigüedad. Esto no es una cola: es lo que está pasando ahora.
    """
    filas = fetch_all(
        """
        select order_id::text       as order_id,
               proposal_id::text    as proposal_id,
               investor_id::text    as investor_id,
               investor_name        as investor_nombre,
               investor_email,
               cedula_ruc,
               subaccount_name,
               risk_profile_name    as perfil_riesgo,
               status               as estado,
               is_simulated,
               total_amount         as monto_total,
               comision_total,
               monto_invertido,
               lineas,
               instituciones,
               instituciones_nombres,
               created_at           as creada_en,
               confirmed_at         as confirmada_en
        from public.v_advisor_order_feed
        order by created_at desc
        limit %s
        """,
        (limite,),
    )
    return [OrdenFeedItem(**f) for f in filas]


async def resumen_comisiones() -> list[ResumenComisiones]:
    """Lo que Brokeate intermedió y lo que factura, por asesor."""
    filas = fetch_all(
        """
        select advisor_id::text as advisor_id,
               advisor_name     as advisor_nombre,
               ordenes,
               ordenes_confirmadas,
               coalesce(monto_intermediado, 0) as monto_intermediado,
               coalesce(comision_ganada, 0)    as comision_ganada
        from public.v_advisor_commissions
        order by coalesce(comision_ganada, 0) desc
        """
    )
    return [ResumenComisiones(**f) for f in filas]
