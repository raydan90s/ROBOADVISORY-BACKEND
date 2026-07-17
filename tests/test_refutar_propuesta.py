"""El inversionista refuta la decisión firmada — la plata es suya, la última palabra también.

POST /api/investor/proposals/{id}/refute devuelve una propuesta 'approved'/'edited' a
`pending_review`: reaparece en la cola del asesor y el cliente recupera la edición de su
mezcla. La decisión del asesor no se borra (advisor_reviews es inmutable): la refutación
queda al lado, en audit_log, y el asesor la ve en el detalle antes de decidir de nuevo.

Los candados que este archivo ejercita por HTTP contra la app real:

1. refutar exige motivo (mismo trato que el rechazo del asesor),
2. solo hay refutación donde hay firma: ni 'pending_review' ni 'rejected' se refutan,
3. una propuesta ya invertida no se refuta: la orden cursada no se deshace,
4. solo el dueño refuta, y la firma vieja deja de mostrarse (nadie responde ya por ella).
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.ayudas_auth import cabeceras_de, registrar_verificado

from src.config.database import get_connection
from src.main import app

CLIENTE = TestClient(app)

# Perfil moderado con montos redondos: el mismo del test de órdenes, porque acá también
# hay un test que invierte (y la plantilla del moderado va a bancos con convenio).
RESPUESTAS = {
    "objetivo": "balancear",
    "horizonte": "medio",
    "liquidez": "tal_vez",
    "tolerancia": "esperar",
    "preferencia": "seguridad_rentable",
}
MONTO = 10_000

MOTIVO = "Demasiado plazo fijo: prefiero más liquidez en esta subcuenta."


def _borrar_cuenta(profile_id: str) -> None:
    """Borra un inversionista de test y TODO lo que dejó (ver test_ordenes._borrar_cuenta).

    Además del patrón de siempre, acá el audit_log tiene filas cuyo actor es el ASESOR
    demo (la aprobación) colgando de la propuesta del test: se borran por entity_id.
    """
    with get_connection() as conn:
        sesiones = "(select id from public.profiling_sessions where investor_id = %s)"
        propuestas = f"""
            (select p.id from public.proposals p
             where p.session_id in {sesiones})
        """
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
        conn.execute(
            f"delete from public.audit_log where actor_id = %s or entity_id in {propuestas}",
            (profile_id, profile_id),
        )
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
        conn.execute("delete from public.auth_codes where profile_id = %s", (profile_id,))
        conn.execute("delete from public.auth_sessions where profile_id = %s", (profile_id,))
        conn.execute("delete from public.profiles where id = %s", (profile_id,))


@pytest.fixture
def cuenta_desechable() -> Iterator[list[str]]:
    creadas: list[str] = []
    yield creadas
    for profile_id in creadas:
        _borrar_cuenta(profile_id)


def _registrar(cuenta_desechable: list[str]) -> tuple[str, dict[str, str]]:
    registro = registrar_verificado(CLIENTE, "refutar", "ZZ Refutar")
    cuenta_desechable.append(registro["user_id"])
    return registro["user_id"], cabeceras_de(registro)


def _asesor() -> dict[str, str]:
    login = CLIENTE.post(
        "/api/auth/login", json={"email": "asesor@demo.ec", "password": "demo1234"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
def propuesta(cuenta_desechable: list[str]) -> tuple[str, dict[str, str], dict]:
    """Un moderado nuevo con capital declarado y su propuesta recién generada."""
    user_id, cabeceras = _registrar(cuenta_desechable)

    r = CLIENTE.post(
        "/api/investor/capital", headers=cabeceras, json={"capital_total": MONTO}
    )
    assert r.status_code == 200, r.text

    r = CLIENTE.post(
        "/api/investor/profile",
        headers=cabeceras,
        json={"monto": MONTO, "nombre_subcuenta": "ZZ Refutable", "respuestas": RESPUESTAS},
    )
    assert r.status_code == 201, r.text

    # El GET genera la propuesta (plantilla + explicación) la primera vez.
    p = CLIENTE.get(f"/api/investor/{user_id}/portfolio", headers=cabeceras)
    assert p.status_code == 200, p.text
    return user_id, cabeceras, p.json()


def _aprobar(proposal_id: str) -> dict[str, str]:
    """El asesor demo firma la propuesta. Devuelve sus cabeceras por si el test las quiere."""
    asesor = _asesor()
    r = CLIENTE.post(
        f"/api/advisor/proposals/{proposal_id}/review",
        headers=asesor,
        json={"decision": "approved"},
    )
    assert r.status_code == 200, r.text
    return asesor


def _refutar(proposal_id: str, cabeceras: dict[str, str], comments: str = MOTIVO):
    return CLIENTE.post(
        f"/api/investor/proposals/{proposal_id}/refute",
        headers=cabeceras,
        json={"comments": comments},
    )


def test_refutar_devuelve_la_propuesta_a_la_cola(propuesta) -> None:
    """⭐ Aprobada → refutada → pending_review, sin firma a la vista y editable otra vez."""
    user_id, cabeceras, original = propuesta
    _aprobar(original["proposal_id"])

    r = _refutar(original["proposal_id"], cabeceras)
    assert r.status_code == 200, r.text
    resultado = r.json()
    assert resultado["estado"] == "pending_review"
    assert resultado["estado_anterior"] == "approved"
    assert resultado["comments"] == MOTIVO

    # La firma vieja deja de mostrarse: nadie responde ya por esta cartera.
    p = CLIENTE.get(f"/api/investor/{user_id}/portfolio", headers=cabeceras)
    assert p.status_code == 200, p.text
    assert p.json()["estado"] == "pending_review"
    assert p.json()["advisor_nombre"] is None
    assert p.json()["firmada_en"] is None

    # Y el cliente recupera la edición de su mezcla (solo existe en pending_review).
    e = CLIENTE.put(
        f"/api/investor/proposals/{original['proposal_id']}/allocation",
        headers=cabeceras,
        json={
            "allocations": [
                {"instrumento_code": "DPF_PICHINCHA_360", "porcentaje": 60},
                {"instrumento_code": "DPF_PICHINCHA_180", "porcentaje": 40},
            ]
        },
    )
    assert e.status_code == 200, e.text


def test_el_asesor_ve_la_refutacion_y_puede_volver_a_decidir(propuesta) -> None:
    """La propuesta reaparece en la cola con el motivo a la vista, y la conversación cierra."""
    _, cabeceras, original = propuesta
    asesor = _aprobar(original["proposal_id"])

    assert _refutar(original["proposal_id"], cabeceras).status_code == 200

    cola = CLIENTE.get("/api/advisor/queue", headers=asesor)
    assert cola.status_code == 200, cola.text
    assert original["proposal_id"] in [item["proposal_id"] for item in cola.json()]

    detalle = CLIENTE.get(
        f"/api/advisor/proposals/{original['proposal_id']}", headers=asesor
    )
    assert detalle.status_code == 200, detalle.text
    refutaciones = detalle.json()["refutaciones"]
    assert len(refutaciones) == 1
    assert refutaciones[0]["comments"] == MOTIVO
    assert refutaciones[0]["estado_refutado"] == "approved"
    # La decisión refutada no se borró: sigue en el historial.
    assert len(detalle.json()["revisiones"]) == 1

    # El asesor puede decidir de nuevo: la propuesta volvió a ser suya.
    r = CLIENTE.post(
        f"/api/advisor/proposals/{original['proposal_id']}/review",
        headers=asesor,
        json={"decision": "approved"},
    )
    assert r.status_code == 200, r.text


def test_refutar_exige_motivo(propuesta) -> None:
    _, cabeceras, original = propuesta
    _aprobar(original["proposal_id"])

    r = _refutar(original["proposal_id"], cabeceras, comments="   ")
    assert r.status_code == 422, r.text


def test_no_se_refuta_sin_decision(propuesta) -> None:
    """En pending_review no hay firma que refutar: para eso está editar la mezcla."""
    _, cabeceras, original = propuesta

    r = _refutar(original["proposal_id"], cabeceras)
    assert r.status_code == 409, r.text


def test_no_se_refuta_un_rechazo(propuesta) -> None:
    """Un rechazo no es una firma: el asesor ya le dio la razón al cliente."""
    _, cabeceras, original = propuesta
    asesor = _asesor()
    r = CLIENTE.post(
        f"/api/advisor/proposals/{original['proposal_id']}/review",
        headers=asesor,
        json={"decision": "rejected", "comments": "ZZ test: perfil por confirmar"},
    )
    assert r.status_code == 200, r.text

    r = _refutar(original["proposal_id"], cabeceras)
    assert r.status_code == 409, r.text


def test_nadie_refuta_la_propuesta_de_otro(propuesta, cuenta_desechable) -> None:
    _, _, original = propuesta
    _aprobar(original["proposal_id"])
    _, cabeceras_intruso = _registrar(cuenta_desechable)

    r = _refutar(original["proposal_id"], cabeceras_intruso)
    assert r.status_code == 403, r.text


def test_una_propuesta_invertida_no_se_refuta(propuesta) -> None:
    """La orden cursada no se deshace refutando: la plata ya salió."""
    _, cabeceras, original = propuesta
    _aprobar(original["proposal_id"])

    orden = CLIENTE.post(
        f"/api/investor/proposals/{original['proposal_id']}/invest",
        headers=cabeceras,
    )
    assert orden.status_code == 201, orden.text

    r = _refutar(original["proposal_id"], cabeceras)
    assert r.status_code == 409, r.text
    assert "invirtió" in r.json()["detail"] or "invirti" in r.json()["detail"]
