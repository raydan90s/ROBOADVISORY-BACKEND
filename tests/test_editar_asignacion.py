"""El inversionista arma su propia mezcla de fondos — dentro de las reglas.

PUT /api/investor/proposals/{id}/allocation deja agregar, quitar y reponderar
instrumentos, pero con tres candados que este archivo ejercita por HTTP contra la
app real (los candados viven en el servidor, no en la pantalla):

1. la suma debe ser exactamente 100% y los códigos existir en el catálogo,
2. un perfil conservador NO puede meterse un producto de un emisor bajo su regla
   de calificación (la misma regla de `v_institution_eligibility`),
3. una propuesta ya decidida por el asesor no se toca (409), y la edición nunca
   aprueba nada: el estado sigue `pending_review`.
"""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from src.config.database import get_connection
from src.main import app

CLIENTE = TestClient(app)

# Todas las respuestas de 1 punto: 5/15 → perfil conservador (solo emisores AAA/AAA-).
RESPUESTAS_CONSERVADOR = {
    "objetivo": "preservar",
    "horizonte": "corto",
    "liquidez": "si_probable",
    "tolerancia": "vender",
    "preferencia": "seguridad",
}

# Elegibles para el conservador: Pichincha es AAA (tier 1).
MEZCLA_VALIDA = [
    {"instrumento_code": "DPF_PICHINCHA_360", "porcentaje": 60},
    {"instrumento_code": "DPF_PICHINCHA_180", "porcentaje": 40},
]


@pytest.fixture
def cuenta_desechable() -> Iterator[list[str]]:
    """Cuentas que el test crea y borra, con toda su descendencia (ver test_roles.py).

    Acá se borra a mano en orden de FKs porque este test deja además una revisión
    del asesor y filas de auditoría colgando de la propuesta.
    """
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
                """,
                (profile_id, profile_id),
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
    email = f"zz-editar-{uuid.uuid4().hex[:8]}@test.local"
    registro = CLIENTE.post(
        "/api/auth/register",
        json={"nombre": "ZZ Editar", "email": email, "password": "demo1234"},
    ).json()
    cuenta_desechable.append(registro["user_id"])
    return registro["user_id"], {"Authorization": f"Bearer {registro['access_token']}"}


@pytest.fixture
def propuesta(cuenta_desechable: list[str]) -> tuple[dict[str, str], dict]:
    """Un conservador nuevo con su propuesta recién generada, lista para editar."""
    user_id, cabeceras = _registrar(cuenta_desechable)

    r = CLIENTE.post(
        "/api/investor/profile",
        headers=cabeceras,
        json={
            "monto": 10000,
            "nombre_subcuenta": "Editable",
            "respuestas": RESPUESTAS_CONSERVADOR,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["perfil_riesgo"] == "conservador", r.text

    # El GET genera la propuesta (plantilla + explicación) la primera vez.
    p = CLIENTE.get(f"/api/investor/{user_id}/portfolio", headers=cabeceras)
    assert p.status_code == 200, p.text
    return cabeceras, p.json()


def _editar(proposal_id: str, allocations: list[dict], cabeceras: dict[str, str]):
    return CLIENTE.put(
        f"/api/investor/proposals/{proposal_id}/allocation",
        headers=cabeceras,
        json={"allocations": allocations},
    )


def test_agregar_y_quitar_fondos_reescribe_la_propuesta(propuesta) -> None:
    """⭐ La mezcla nueva reemplaza a la plantilla, con los USD calculados en Postgres."""
    cabeceras, original = propuesta

    r = _editar(original["proposal_id"], MEZCLA_VALIDA, cabeceras)
    assert r.status_code == 200, r.text
    editada = r.json()

    por_codigo = {a["instrumento_code"]: a for a in editada["allocations"]}
    assert set(por_codigo) == {"DPF_PICHINCHA_360", "DPF_PICHINCHA_180"}

    # 60% y 40% de 10.000: los montos vienen del servidor, redondeados a 2 decimales.
    assert por_codigo["DPF_PICHINCHA_360"]["monto_asignado"] == 6000.0
    assert por_codigo["DPF_PICHINCHA_180"]["monto_asignado"] == 4000.0

    # Editar no aprueba: el asesor sigue teniendo la última palabra (HU3).
    assert editada["estado"] == "pending_review"


def test_un_conservador_no_puede_meter_un_emisor_bajo_su_regla(propuesta) -> None:
    """⭐ DPF_LOJA_360 tiene la mejor tasa y la peor calificación (AA, tier 4):
    el comparador la muestra en gris y este endpoint la rechaza. Misma regla."""
    cabeceras, original = propuesta

    r = _editar(
        original["proposal_id"],
        [{"instrumento_code": "DPF_LOJA_360", "porcentaje": 100}],
        cabeceras,
    )
    assert r.status_code == 422, r.text
    assert "perfil" in r.json()["detail"].lower()


def test_la_suma_distinta_de_100_se_rechaza(propuesta) -> None:
    cabeceras, original = propuesta

    r = _editar(
        original["proposal_id"],
        [{"instrumento_code": "DPF_PICHINCHA_360", "porcentaje": 90}],
        cabeceras,
    )
    assert r.status_code == 422, r.text


def test_un_codigo_fuera_del_catalogo_se_rechaza(propuesta) -> None:
    cabeceras, original = propuesta

    r = _editar(
        original["proposal_id"],
        [{"instrumento_code": "BITCOIN", "porcentaje": 100}],
        cabeceras,
    )
    assert r.status_code == 422, r.text
    assert "catálogo" in r.json()["detail"]


def test_nadie_edita_la_propuesta_de_otro(propuesta, cuenta_desechable) -> None:
    _, original = propuesta
    _, cabeceras_intruso = _registrar(cuenta_desechable)

    r = _editar(original["proposal_id"], MEZCLA_VALIDA, cabeceras_intruso)
    assert r.status_code == 403, r.text


def test_una_propuesta_ya_decidida_no_se_edita(propuesta) -> None:
    """El asesor aprueba → el cliente ya no puede tocarla (una decisión no se pisa)."""
    cabeceras, original = propuesta

    login = CLIENTE.post(
        "/api/auth/login", json={"email": "asesor@demo.ec", "password": "demo1234"}
    )
    assert login.status_code == 200, login.text
    asesor = {"Authorization": f"Bearer {login.json()['access_token']}"}

    aprobada = CLIENTE.post(
        f"/api/advisor/proposals/{original['proposal_id']}/review",
        headers=asesor,
        json={"decision": "approved"},
    )
    assert aprobada.status_code == 200, aprobada.text

    r = _editar(original["proposal_id"], MEZCLA_VALIDA, cabeceras)
    assert r.status_code == 409, r.text
