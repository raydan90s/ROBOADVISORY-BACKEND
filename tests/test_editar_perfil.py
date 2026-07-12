"""El inversionista corrige sus respuestas — y con eso reabre la revisión.

PUT /api/investor/sessions/{session_id}/profile re-puntúa la sesión contra las reglas
activas, regenera la propuesta con la plantilla del perfil nuevo y la devuelve a
`pending_review`. Este archivo lo ejercita por HTTP contra la app real:

1. cambiar a respuestas de más riesgo sube el puntaje y cambia el perfil,
2. una propuesta YA decidida por el asesor vuelve a la cola (a diferencia de la mezcla,
   editar el perfil sí se permite: el insumo de la decisión estaba mal),
3. la decisión anterior no se borra: queda en `advisor_reviews`,
4. solo el dueño de la sesión puede hacerlo (403 al intruso).
"""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from src.config.database import fetch_all, get_connection
from src.main import app

CLIENTE = TestClient(app)

# Todas de 1 punto: 5/15 → conservador.
RESPUESTAS_CONSERVADOR = {
    "objetivo": "preservar",
    "horizonte": "corto",
    "liquidez": "si_probable",
    "tolerancia": "vender",
    "preferencia": "seguridad",
}

# Todas del máximo (3 puntos): 15/15 → agresivo.
RESPUESTAS_AGRESIVO = {
    "objetivo": "crecer",
    "horizonte": "largo",
    "liquidez": "no",
    "tolerancia": "comprar_mas",
    "preferencia": "maxima_rentabilidad",
}


@pytest.fixture
def cuenta_desechable() -> Iterator[list[str]]:
    """Cuentas que el test crea y borra con toda su descendencia (mismo orden de FKs
    que test_editar_asignacion, más la fila de auditoría de la sesión)."""
    creadas: list[str] = []
    yield creadas

    with get_connection() as conn:
        for profile_id in creadas:
            conn.execute(
                """
                delete from public.audit_log
                where actor_id = %s
                   or entity_id in (select p.id from public.proposals p
                                    join public.profiling_sessions s on s.id = p.session_id
                                    where s.investor_id = %s)
                   or entity_id in (select id from public.profiling_sessions
                                    where investor_id = %s)
                """,
                (profile_id, profile_id, profile_id),
            )
            conn.execute(
                """
                delete from public.advisor_reviews
                where proposal_id in (select p.id from public.proposals p
                                      join public.profiling_sessions s on s.id = p.session_id
                                      where s.investor_id = %s)
                """,
                (profile_id,),
            )
            conn.execute(
                """
                delete from public.llm_interactions
                where session_id in (select id from public.profiling_sessions
                                     where investor_id = %s)
                """,
                (profile_id,),
            )
            conn.execute(
                """
                delete from public.proposal_items
                where proposal_id in (select p.id from public.proposals p
                                      join public.profiling_sessions s on s.id = p.session_id
                                      where s.investor_id = %s)
                """,
                (profile_id,),
            )
            conn.execute(
                """
                delete from public.proposals
                where session_id in (select id from public.profiling_sessions
                                     where investor_id = %s)
                """,
                (profile_id,),
            )
            conn.execute(
                """
                delete from public.profiling_answers
                where session_id in (select id from public.profiling_sessions
                                     where investor_id = %s)
                """,
                (profile_id,),
            )
            conn.execute(
                "delete from public.profiling_sessions where investor_id = %s", (profile_id,)
            )
            conn.execute("delete from public.profiles where id = %s", (profile_id,))


def _registrar(cuenta_desechable: list[str]) -> tuple[str, dict[str, str]]:
    email = f"zz-perfil-{uuid.uuid4().hex[:8]}@test.local"
    registro = CLIENTE.post(
        "/api/auth/register",
        json={"nombre": "ZZ Perfil", "email": email, "password": "demo1234"},
    ).json()
    cuenta_desechable.append(registro["user_id"])
    return registro["user_id"], {"Authorization": f"Bearer {registro['access_token']}"}


@pytest.fixture
def conservador(cuenta_desechable: list[str]) -> tuple[str, dict[str, str], dict]:
    """Un conservador nuevo con su propuesta generada, listo para reperfilarse."""
    user_id, cabeceras = _registrar(cuenta_desechable)

    r = CLIENTE.post(
        "/api/investor/profile",
        headers=cabeceras,
        json={
            "monto": 10000,
            "nombre_subcuenta": "Reperfilable",
            "respuestas": RESPUESTAS_CONSERVADOR,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["perfil_riesgo"] == "conservador", r.text

    p = CLIENTE.get(f"/api/investor/{user_id}/portfolio", headers=cabeceras)
    assert p.status_code == 200, p.text
    return user_id, cabeceras, p.json()


def _editar_perfil(session_id: str, respuestas: dict, cabeceras: dict[str, str]):
    return CLIENTE.put(
        f"/api/investor/sessions/{session_id}/profile",
        headers=cabeceras,
        json={"respuestas": respuestas},
    )


def _asesor() -> dict[str, str]:
    login = CLIENTE.post(
        "/api/auth/login", json={"email": "asesor@demo.ec", "password": "demo1234"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_reperfilarse_recalcula_puntaje_y_perfil(conservador) -> None:
    """⭐ De conservador (5) a agresivo (15): el desglose que devuelve ya trae lo nuevo."""
    _, cabeceras, original = conservador
    session_id = original["session_id"]

    r = _editar_perfil(session_id, RESPUESTAS_AGRESIVO, cabeceras)
    assert r.status_code == 200, r.text
    desglose = r.json()

    assert desglose["puntaje"] == 15
    assert desglose["perfil_code"] == "agresivo"


def test_editar_el_perfil_regenera_la_propuesta_con_la_plantilla_nueva(conservador) -> None:
    """⭐ La propuesta cambia de productos (los del agresivo) y el monto se conserva."""
    user_id, cabeceras, original = conservador
    session_id = original["session_id"]

    assert _editar_perfil(session_id, RESPUESTAS_AGRESIVO, cabeceras).status_code == 200

    p = CLIENTE.get(
        f"/api/investor/{user_id}/portfolio?session_id={session_id}", headers=cabeceras
    )
    assert p.status_code == 200, p.text
    regenerada = p.json()

    assert regenerada["perfil_riesgo"] == "agresivo"
    assert regenerada["riesgo_esperado"] != original["riesgo_esperado"]
    # Mismo proposal_id: se reescribió, no se creó otra (el historial sigue apuntando ahí).
    assert regenerada["proposal_id"] == original["proposal_id"]
    # El monto no lo toca la edición del perfil.
    assert regenerada["monto_total"] == original["monto_total"]
    assert regenerada["estado"] == "pending_review"


def test_una_propuesta_aprobada_vuelve_a_la_cola_al_editar_el_perfil(conservador) -> None:
    """⭐ A diferencia de la mezcla, el perfil SÍ se edita tras la decisión: el insumo
    con el que el asesor decidió estaba mal, así que la propuesta vuelve a pendiente."""
    user_id, cabeceras, original = conservador
    session_id = original["session_id"]
    proposal_id = original["proposal_id"]
    asesor = _asesor()

    aprobada = CLIENTE.post(
        f"/api/advisor/proposals/{proposal_id}/review",
        headers=asesor,
        json={"decision": "approved"},
    )
    assert aprobada.status_code == 200, aprobada.text

    # Editar el perfil pese a la aprobación: reabre la revisión.
    assert _editar_perfil(session_id, RESPUESTAS_AGRESIVO, cabeceras).status_code == 200

    p = CLIENTE.get(
        f"/api/investor/{user_id}/portfolio?session_id={session_id}", headers=cabeceras
    )
    assert p.json()["estado"] == "pending_review", p.text

    # Y reaparece en la cola del asesor.
    cola = CLIENTE.get("/api/advisor/queue", headers=asesor)
    assert cola.status_code == 200, cola.text
    assert proposal_id in {item["proposal_id"] for item in cola.json()}

    # La decisión anterior no se borró: queda como evidencia.
    revisiones = fetch_all(
        "select decision from public.advisor_reviews where proposal_id = %s",
        (proposal_id,),
    )
    assert any(rev["decision"] == "approved" for rev in revisiones)


def test_nadie_edita_el_perfil_de_otro(conservador, cuenta_desechable) -> None:
    _, _, original = conservador
    _, cabeceras_intruso = _registrar(cuenta_desechable)

    r = _editar_perfil(original["session_id"], RESPUESTAS_AGRESIVO, cabeceras_intruso)
    assert r.status_code == 403, r.text
