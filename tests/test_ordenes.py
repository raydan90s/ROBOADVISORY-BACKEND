"""La orden de inversión: el paso de la propuesta firmada a N instrucciones bancarias.

Qué se prueba acá y por qué contra la base real: las cuatro reglas que sostienen el
producto no viven en Python, viven en Postgres (dos triggers y dos UNIQUE). Probarlas
contra un mock validaría el mock. Cada test de este archivo empuja contra la base de
verdad, igual que `test_scoring` y `test_subcuentas`.

Las reglas, en orden de importancia:

1. Una propuesta no se invierte hasta que un asesor la firme.  ← el corazón del pitch
2. La comisión es la misma para todos los bancos con convenio. ← la respuesta al sesgo
3. No se cursa plata a un banco sin convenio.
4. Una propuesta se invierte una sola vez.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# pyrefly: ignore [missing-import]
from fastapi.testclient import TestClient

from src.config.database import fetch_all, fetch_one, get_connection
from src.main import app
from src.services import bank_gateway
from tests.ayudas_auth import cabeceras_de, registrar_verificado

CLIENTE = TestClient(app)

# El cuestionario que produce un perfil moderado, con montos que dan una comisión redonda:
# USD 10.000 a 50 bps son exactamente USD 50,00.
RESPUESTAS = {
    "objetivo": "balancear",
    "horizonte": "medio",
    "liquidez": "tal_vez",
    "tolerancia": "esperar",
    "preferencia": "seguridad_rentable",
}
MONTO = 10_000


def _borrar_cuenta(profile_id: str) -> None:
    """Borra un inversionista de test y TODO lo que dejó, de adentro hacia afuera.

    Sin esto, cada corrida deja un inversionista con su propuesta y su orden en la base
    real — y esas órdenes salen en el feed del asesor, que es una pantalla de la demo. Es
    la misma regla que ya aplica `test_roles.cuenta_desechable`: un test no puede ensuciar
    lo que la demo va a mostrar. Acá pesa más todavía, porque una orden es plata en
    pantalla.

    El orden importa: ninguna de estas tablas tiene ON DELETE CASCADE hacia `profiles`.
    """
    with get_connection() as conn:
        conn.execute(
            """
            delete from public.investment_order_items where order_id in
                (select id from public.investment_orders where investor_id = %s)
            """,
            (profile_id,),
        )
        conn.execute(
            "delete from public.investment_orders where investor_id = %s", (profile_id,)
        )

        sesiones = "(select id from public.profiling_sessions where investor_id = %s)"
        propuestas = f"""
            (select p.id from public.proposals p
             where p.session_id in {sesiones})
        """
        conn.execute(
            f"delete from public.advisor_reviews where proposal_id in {propuestas}",
            (profile_id,),
        )
        conn.execute(
            f"delete from public.llm_interactions where session_id in {sesiones}",
            (profile_id,),
        )
        conn.execute(
            f"delete from public.proposal_items where proposal_id in {propuestas}",
            (profile_id,),
        )
        conn.execute(
            f"delete from public.proposals where session_id in {sesiones}", (profile_id,)
        )
        conn.execute(
            f"delete from public.profiling_answers where session_id in {sesiones}",
            (profile_id,),
        )
        conn.execute(
            "delete from public.profiling_sessions where investor_id = %s", (profile_id,)
        )
        # audit_log.actor_id no tiene ON DELETE CASCADE: hay que soltar la referencia
        # antes de borrar el perfil.
        conn.execute("delete from public.audit_log where actor_id = %s", (profile_id,))
        conn.execute("delete from public.auth_codes where profile_id = %s", (profile_id,))
        conn.execute("delete from public.auth_sessions where profile_id = %s", (profile_id,))
        conn.execute("delete from public.profiles where id = %s", (profile_id,))


@pytest.fixture
def inversionista() -> Iterator[dict[str, str]]:
    """Un inversionista nuevo con capital declarado y una propuesta ya generada.

    Se borra al terminar: ver `_borrar_cuenta`.
    """
    registro = registrar_verificado(CLIENTE, "ordenes", "ZZ Ordenes")
    cab = cabeceras_de(registro)

    r = CLIENTE.post("/api/investor/capital", headers=cab, json={"capital_total": MONTO})
    assert r.status_code == 200, r.text

    r = CLIENTE.post(
        "/api/investor/profile",
        headers=cab,
        json={"monto": MONTO, "nombre_subcuenta": "ZZ Orden", "respuestas": RESPUESTAS},
    )
    assert r.status_code == 201, r.text
    investor_id = r.json()["investor_id"]

    # El GET es lo que materializa la propuesta: sin esto no hay proposal_id que invertir.
    r = CLIENTE.get(f"/api/investor/{investor_id}/portfolio", headers=cab)
    assert r.status_code == 200, r.text

    yield {
        "cabeceras": cab,
        "investor_id": investor_id,
        "proposal_id": r.json()["proposal_id"],
    }

    _borrar_cuenta(investor_id)


@pytest.fixture
def otra_cuenta() -> Iterator[object]:
    """Fábrica de cuentas ajenas (el intruso, el mirón), que se borran al terminar.

    Devuelve una función para que cada test pida las que necesite: son cuentas de usar y
    tirar, no un fixture con un dueño fijo.
    """
    creadas: list[str] = []

    def _nueva(prefijo: str, nombre: str) -> dict[str, str]:
        registro = registrar_verificado(CLIENTE, prefijo, nombre)
        creadas.append(registro["user_id"])
        return cabeceras_de(registro)

    yield _nueva

    for profile_id in creadas:
        _borrar_cuenta(profile_id)


def _firmar(proposal_id: str) -> None:
    """Aprueba la propuesta como lo haría un asesor.

    Se escribe directo en la base en vez de pasar por `/api/advisor/*` porque lo que este
    archivo prueba es la ORDEN, no la revisión (eso ya lo cubre la suite del asesor). Lo
    que hace falta es el hecho —la propuesta está firmada—, no el camino.
    """
    with get_connection() as conn:
        asesor = conn.execute(
            "select id from public.profiles where role = 'advisor' limit 1"
        ).fetchone()
        datos = conn.execute(
            """
            select s.rules_version_id
            from public.proposals p
            join public.profiling_sessions s on s.id = p.session_id
            where p.id = %s
            """,
            (proposal_id,),
        ).fetchone()
        conn.execute(
            """
            insert into public.advisor_reviews
                (proposal_id, advisor_id, decision, comments, rules_version_id)
            values (%s, %s, 'approved', 'ZZ test', %s)
            """,
            (proposal_id, asesor["id"], datos["rules_version_id"]),
        )
        conn.execute(
            "update public.proposals set status = 'approved' where id = %s", (proposal_id,)
        )


# ===========================================================================
# 1. Una propuesta no se invierte hasta que un asesor la firme
# ===========================================================================


def test_una_propuesta_en_revision_no_se_puede_invertir(inversionista: dict[str, str]) -> None:
    """El corazón del producto: la IA propone, pero nadie mueve plata sin una firma humana."""
    r = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    )
    assert r.status_code == 409, r.text
    assert "asesor" in r.json()["detail"].lower()


def test_el_trigger_bloquea_aunque_alguien_se_salte_el_controller(
    inversionista: dict[str, str],
) -> None:
    """La regla vale aunque el 409 del controller no exista.

    Este test es el que hace que la afirmación del pitch sea verdad y no una costumbre: si
    mañana alguien agrega un endpoint nuevo, un script de migración o un job que inserte en
    `investment_orders`, la base sigue diciendo que no. Por eso se salta la API a propósito.
    """
    fila = fetch_one(
        """
        select s.investor_id, s.rules_version_id
        from public.proposals p
        join public.profiling_sessions s on s.id = p.session_id
        where p.id = %s
        """,
        (inversionista["proposal_id"],),
    )

    with pytest.raises(Exception, match="firmó"):
        with get_connection() as conn:
            conn.execute(
                """
                insert into public.investment_orders
                    (proposal_id, investor_id, rules_version_id, comision_bps, total_amount)
                values (%s, %s, %s, 50, %s)
                """,
                (
                    inversionista["proposal_id"],
                    fila["investor_id"],
                    fila["rules_version_id"],
                    MONTO,
                ),
            )


def test_una_propuesta_firmada_se_cursa_y_nace_enviada(inversionista: dict[str, str]) -> None:
    """El camino feliz. Nace 'sent': el cliente decidió, el banco todavía no acusó."""
    _firmar(inversionista["proposal_id"])

    r = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    )
    assert r.status_code == 201, r.text
    orden = r.json()

    assert orden["estado"] == "sent"
    assert orden["monto_total"] == MONTO
    assert orden["lineas"], "una orden sin líneas no es una orden"

    # Mientras está 'sent' ninguna línea tiene referencia: eso es exactamente lo que
    # distingue "mandada" de "acusada".
    assert all(linea["bank_reference"] is None for linea in orden["lineas"])
    assert all(linea["estado"] == "sent" for linea in orden["lineas"])

    # Y la app tiene cómo decir que esto no movió plata de verdad.
    assert orden["is_simulated"] is True


# ===========================================================================
# 2. La comisión
# ===========================================================================


def _bps_vigentes() -> int:
    """La tasa publicada hoy.

    Se lee de la base en vez de fijarla acá porque es una decisión de negocio: ya pasó de
    50 a 150 y de ahí a 450 bps, y va a volver a moverse. Un test que la clave a mano se
    cae en cada cambio de precio sin que nada esté roto — y lo que hay que probar no es
    cuánto se cobra, sino que la aritmética la haga Postgres y cuadre.
    """
    fila = fetch_one(
        """
        select cp.comision_bps
        from public.commission_policies cp
        join public.rules_versions rv on rv.id = cp.rules_version_id
        where rv.is_active
        """
    )
    assert fila, "no hay política de comisión publicada para las reglas activas"
    return fila["comision_bps"]


def test_la_comision_la_calcula_postgres_y_cuadra_con_las_lineas(
    inversionista: dict[str, str],
) -> None:
    """La comisión sale de `monto * bps`, y la suma de las líneas da el total.

    Que cuadre no es casualidad aritmética: `comision_total` y `comision` son columnas
    GENERATED. Python no las escribe, así que no las puede equivocar.
    """
    _firmar(inversionista["proposal_id"])
    r = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    )
    orden = r.json()

    bps = _bps_vigentes()
    assert orden["comision_bps"] == bps
    assert orden["comision_total"] == pytest.approx(MONTO * bps / 10_000)

    suma_lineas = sum(linea["comision"] for linea in orden["lineas"])
    assert suma_lineas == pytest.approx(orden["comision_total"], abs=0.01)

    suma_montos = sum(linea["monto"] for linea in orden["lineas"])
    assert suma_montos == pytest.approx(orden["monto_total"], abs=0.01)


def test_la_comision_la_paga_el_inversionista_y_sale_de_su_inversion(
    inversionista: dict[str, str],
) -> None:
    """Lo que se coloca en bancos es el total MENOS la comisión.

    Este test es la regla de negocio entera: el cliente pone `monto_total`, paga
    `comision_total` y se invierte `monto_invertido`. Mientras la comisión la pagaba la
    institución, `monto_invertido` no existía porque era igual al total — que este assert
    exista y falle si alguien los vuelve a igualar es el punto.
    """
    _firmar(inversionista["proposal_id"])
    r = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    )
    orden = r.json()

    assert orden["comision_total"] > 0, "si la comisión es 0, este test no prueba nada"
    assert orden["monto_invertido"] == pytest.approx(
        orden["monto_total"] - orden["comision_total"]
    )
    assert orden["monto_invertido"] < orden["monto_total"]

    # Y la misma resta, banco por banco: la comisión se prorratea, no sale toda de la
    # primera línea. Si saliera de una sola, esa institución recibiría menos de su
    # porcentaje y el donut de la propuesta dejaría de describir la cartera real.
    for linea in orden["lineas"]:
        assert linea["monto_invertido"] == pytest.approx(
            linea["monto"] - linea["comision"]
        )

    # El neto de la orden manda sobre la suma de los netos por línea: son N redondeos a 2
    # decimales contra uno solo. La tolerancia de un centavo es la misma que ya se acepta
    # arriba para la comisión, y por la misma razón.
    suma_netos = sum(linea["monto_invertido"] for linea in orden["lineas"])
    assert suma_netos == pytest.approx(orden["monto_invertido"], abs=0.01)


def test_la_comision_es_la_misma_para_todos_los_bancos() -> None:
    """La respuesta a «¿me recomiendas al que más te paga?», como test.

    No hay una comisión por banco que comparar porque `commission_policies` no tiene
    columna de institución: la comisión no puede depender del emisor ni queriendo. Lo que
    se verifica acá es que esa propiedad del esquema siga siendo cierta.
    """
    columnas = fetch_all(
        """
        select column_name
        from information_schema.columns
        where table_schema = 'public' and table_name = 'commission_policies'
        """
    )
    nombres = {c["column_name"] for c in columnas}
    assert "institution_id" not in nombres, (
        "commission_policies ganó una columna de institución: la comisión ahora PUEDE "
        "depender del banco, y con eso se cae el argumento anti-sesgo del producto."
    )

    politicas = fetch_all(
        """
        select cp.comision_bps
        from public.commission_policies cp
        join public.rules_versions rv on rv.id = cp.rules_version_id
        where rv.is_active
        """
    )
    assert len(politicas) == 1, "hay más de una comisión vigente: ya no es 'la misma para todos'"


def test_no_se_pueden_publicar_dos_comisiones_en_la_misma_version_de_reglas() -> None:
    """Y que no se pueda es una restricción, no una convención."""
    rv = fetch_one("select id from public.rules_versions where is_active limit 1")

    with pytest.raises(Exception, match="commission_policies_una_por_version"):
        with get_connection() as conn:
            conn.execute(
                """
                insert into public.commission_policies (rules_version_id, comision_bps, rationale)
                values (%s, 300, 'ZZ tasa preferente para un banco amigo')
                """,
                (rv["id"],),
            )


# ===========================================================================
# 3. El convenio
# ===========================================================================


def test_no_se_cursa_una_orden_a_un_banco_sin_convenio(inversionista: dict[str, str]) -> None:
    """El catálogo informa; el convenio habilita. Son dos listas distintas.

    Es además la respuesta a «¿por qué no me aparece Interactive Brokers?»: no porque lo
    escondamos, sino porque no hay convenio — y sin convenio la base no deja cursar.
    """
    _firmar(inversionista["proposal_id"])
    CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    )

    sin_convenio = fetch_one(
        "select id, name from public.institutions where convenio_activo = false limit 1"
    )
    orden = fetch_one(
        "select id from public.investment_orders where proposal_id = %s",
        (inversionista["proposal_id"],),
    )
    instrumento = fetch_one("select id from public.instruments limit 1")

    with pytest.raises(Exception, match="convenio"):
        with get_connection() as conn:
            conn.execute(
                """
                insert into public.investment_order_items
                    (order_id, instrument_id, institution_id, amount, percentage, comision_bps)
                values (%s, %s, %s, 100, 10, 50)
                """,
                (orden["id"], instrumento["id"], sin_convenio["id"]),
            )


def test_el_catalogo_de_convenios_separa_lo_que_informa_de_lo_que_habilita(
    inversionista: dict[str, str],
) -> None:
    r = CLIENTE.get("/api/catalog/convenios", headers=inversionista["cabeceras"])
    assert r.status_code == 200, r.text
    cat = r.json()

    assert cat["politica"]["comision_bps"] == _bps_vigentes()
    # 150 bps → 1,5%. La división la hace el servidor: si el front la hiciera, sería el
    # segundo lugar donde vive la misma cuenta.
    assert cat["politica"]["comision_porcentaje"] == pytest.approx(
        cat["politica"]["comision_bps"] / 100
    )
    assert cat["politica"]["misma_para_todas"] is True
    assert cat["politica"]["rationale"], "una comisión sin porqué no se puede defender"

    con = [c for c in cat["convenios"] if c["convenio_activo"]]
    sin = [c for c in cat["convenios"] if not c["convenio_activo"]]
    assert con and sin, (
        "si todas las instituciones tienen convenio, la distinción entre catálogo y "
        "convenio no se puede demostrar en pantalla"
    )
    assert all(c["convenio_desde"] for c in con), "un convenio activo sin fecha no es un convenio"


# ===========================================================================
# 4. Una sola vez, y de quién es
# ===========================================================================


def test_una_propuesta_se_invierte_una_sola_vez(inversionista: dict[str, str]) -> None:
    """Dos taps seguidos en «Invertir ahora» no cursan la cartera dos veces."""
    _firmar(inversionista["proposal_id"])
    ruta = f"/api/investor/proposals/{inversionista['proposal_id']}/invest"

    assert CLIENTE.post(ruta, headers=inversionista["cabeceras"]).status_code == 201
    segunda = CLIENTE.post(ruta, headers=inversionista["cabeceras"])
    assert segunda.status_code == 409
    assert "comprobante" in segunda.json()["detail"].lower()


def test_nadie_invierte_la_propuesta_de_otro(
    inversionista: dict[str, str], otra_cuenta: object
) -> None:
    _firmar(inversionista["proposal_id"])
    intruso = otra_cuenta("ordenes-intruso", "ZZ Intruso")  # type: ignore[operator]

    r = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest", headers=intruso
    )
    assert r.status_code == 403, r.text


def test_nadie_ve_el_comprobante_de_otro(
    inversionista: dict[str, str], otra_cuenta: object
) -> None:
    _firmar(inversionista["proposal_id"])
    orden = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    ).json()

    miron = otra_cuenta("ordenes-miron", "ZZ Miron")  # type: ignore[operator]
    r = CLIENTE.get(f"/api/investor/orders/{orden['order_id']}", headers=miron)
    assert r.status_code == 403, r.text


def test_la_comision_es_del_asesor_que_firmo(inversionista: dict[str, str]) -> None:
    """Quien respondió con su nombre por la propuesta es de quien es la prima."""
    _firmar(inversionista["proposal_id"])
    orden = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    ).json()

    assert orden["advisor_id"], "una orden sin asesor no tiene a quién atribuirle la comisión"
    assert orden["advisor_nombre"]

    firmante = fetch_one(
        """
        select ar.advisor_id::text as advisor_id
        from public.advisor_reviews ar
        where ar.proposal_id = %s and ar.decision in ('approved', 'edited')
        order by ar.decided_at desc limit 1
        """,
        (inversionista["proposal_id"],),
    )
    assert orden["advisor_id"] == firmante["advisor_id"]


# ===========================================================================
# 5. La confirmación (el acuse del banco)
# ===========================================================================


def test_confirmar_le_pone_referencia_a_cada_linea(inversionista: dict[str, str]) -> None:
    """Una cartera en N bancos son N instrucciones con N referencias distintas.

    Es la diversificación dejando de ser un gráfico: no es "tu plata está repartida", es
    "estas son las tres órdenes, cada una con su acuse".
    """
    _firmar(inversionista["proposal_id"])
    orden = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    ).json()

    r = CLIENTE.post(
        f"/api/investor/orders/{orden['order_id']}/confirm",
        headers=inversionista["cabeceras"],
    )
    assert r.status_code == 200, r.text
    confirmada = r.json()

    assert confirmada["estado"] == "confirmed"
    assert confirmada["confirmada_en"] is not None
    assert all(linea["bank_reference"] for linea in confirmada["lineas"])
    assert all(linea["estado"] == "confirmed" for linea in confirmada["lineas"])

    referencias = [linea["bank_reference"] for linea in confirmada["lineas"]]
    assert len(set(referencias)) == len(referencias), "dos líneas con la misma referencia"


def test_confirmar_dos_veces_devuelve_lo_mismo(inversionista: dict[str, str]) -> None:
    """Idempotente: un reintento por red inestable no es un error del usuario."""
    _firmar(inversionista["proposal_id"])
    orden = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    ).json()
    ruta = f"/api/investor/orders/{orden['order_id']}/confirm"

    primera = CLIENTE.post(ruta, headers=inversionista["cabeceras"])
    segunda = CLIENTE.post(ruta, headers=inversionista["cabeceras"])

    assert primera.status_code == segunda.status_code == 200
    assert [linea["bank_reference"] for linea in primera.json()["lineas"]] == [
        linea["bank_reference"] for linea in segunda.json()["lineas"]
    ]


def test_la_orden_de_una_propuesta_sin_invertir_es_null(inversionista: dict[str, str]) -> None:
    """«Todavía no» es una respuesta, no un 404: es lo que decide si se pinta el botón."""
    r = CLIENTE.get(
        f"/api/investor/proposals/{inversionista['proposal_id']}/order",
        headers=inversionista["cabeceras"],
    )
    assert r.status_code == 200
    assert r.json() is None


# ===========================================================================
# 6. El gateway simulado
# ===========================================================================


def test_la_referencia_del_banco_es_determinista() -> None:
    """Misma línea, misma referencia: la demo no cambia de números entre ensayo y función.

    Misma disciplina que el respaldo de `market_data`: se siembra con el id, no con el
    reloj ni con uuid4.
    """
    a = bank_gateway.referencia("11111111-2222-3333-4444-555555555555", "Banco Pichincha")
    b = bank_gateway.referencia("11111111-2222-3333-4444-555555555555", "Banco Pichincha")
    otra = bank_gateway.referencia("99999999-8888-7777-6666-555555555555", "Banco Pichincha")

    assert a == b
    assert a != otra
    assert a.startswith("BRK-")


def test_la_referencia_no_usa_caracteres_ambiguos() -> None:
    """Sin 0/O ni 1/I: alguien va a leerla en voz alta durante la demo."""
    ref = bank_gateway.referencia("abc-def", "Banco Loja")
    cuerpo = ref.replace("BRK-", "").replace("-", "")
    assert not (set(cuerpo) & set("01OI")), f"referencia ambigua: {ref}"


# ===========================================================================
# 7. El asesor
# ===========================================================================


def test_el_asesor_ve_la_orden_en_su_feed(inversionista: dict[str, str]) -> None:
    """El aviso que dispara la llamada: quién, cuánto y en cuántos bancos."""
    _firmar(inversionista["proposal_id"])
    orden = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    ).json()

    asesor = cabeceras_de_asesor()
    r = CLIENTE.get("/api/advisor/orders", headers=asesor)
    assert r.status_code == 200, r.text

    mia = [f for f in r.json() if f["order_id"] == orden["order_id"]]
    assert mia, "la orden recién cursada no apareció en el feed del asesor"
    item = mia[0]

    assert item["investor_nombre"] == "ZZ Ordenes"
    assert item["monto_total"] == MONTO
    assert item["instituciones"] >= 1
    assert item["instituciones_nombres"]
    assert item["lineas"] == len(orden["lineas"])


def test_un_inversionista_no_ve_el_feed_del_asesor(inversionista: dict[str, str]) -> None:
    r = CLIENTE.get("/api/advisor/orders", headers=inversionista["cabeceras"])
    assert r.status_code == 403, r.text


def test_la_comision_ganada_solo_cuenta_ordenes_confirmadas(
    inversionista: dict[str, str],
) -> None:
    """Una orden que el banco no acusó no facturó nada.

    Contarla sería exactamente el tipo de cifra optimista que este proyecto no se permite
    en ninguna otra pantalla.
    """
    _firmar(inversionista["proposal_id"])
    orden = CLIENTE.post(
        f"/api/investor/proposals/{inversionista['proposal_id']}/invest",
        headers=inversionista["cabeceras"],
    ).json()

    asesor_id = orden["advisor_id"]
    antes = _comision_de(asesor_id)

    # Sigue en 'sent': todavía no facturó.
    assert _comision_de(asesor_id) == antes

    CLIENTE.post(
        f"/api/investor/orders/{orden['order_id']}/confirm",
        headers=inversionista["cabeceras"],
    )
    despues = _comision_de(asesor_id)
    assert despues == pytest.approx(antes + orden["comision_total"], abs=0.01)


def _comision_de(advisor_id: str) -> float:
    fila = fetch_one(
        """
        select coalesce(comision_ganada, 0) as ganada
        from public.v_advisor_commissions
        where advisor_id = %s
        """,
        (advisor_id,),
    )
    return float(fila["ganada"]) if fila else 0.0


def cabeceras_de_asesor() -> dict[str, str]:
    """El asesor de la demo. No se registra: `/api/auth/register` nunca crea asesores."""
    r = CLIENTE.post(
        "/api/auth/login", json={"email": "asesor@demo.ec", "password": "demo1234"}
    )
    assert r.status_code == 200, f"No se pudo entrar como asesor de demo: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
