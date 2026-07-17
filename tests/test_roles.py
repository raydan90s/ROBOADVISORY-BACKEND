"""Un investor llamando /api/advisor/* recibe 403.

Es el test que justifica `require_role`: sin él, cualquier cliente autenticado podría leer
la cola del asesor y aprobar su propia propuesta. Se ejercita la app real (no un mock del
guardia) porque lo que se quiere probar es el cableado, no la función suelta.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.ayudas_auth import (
    cabeceras_de,
    codigo_pendiente,
    correo_desechable,
    registrar_verificado,
)

from src.config.database import fetch_one, get_connection
from src.main import app

CLIENTE = TestClient(app)


@pytest.fixture
def cuenta_desechable() -> Iterator[list[str]]:
    """Cuentas que el test crea y que el test borra.

    Sin esto, cada corrida de la suite deja un inversionista y una propuesta en la base
    real — y esas filas salen en la AuditoriaPage de la demo. Un test no puede ensuciar
    la pantalla que la demo va a mostrar.
    """
    creadas: list[str] = []
    yield creadas

    with get_connection() as conn:
        for profile_id in creadas:
            # audit_log.actor_id no tiene ON DELETE CASCADE: hay que soltar la referencia
            # antes de borrar el perfil. Solo se tocan filas de ESTE profile.
            conn.execute("delete from public.audit_log where actor_id = %s", (profile_id,))
            conn.execute("delete from public.profiles where id = %s", (profile_id,))

RUTAS_DEL_ASESOR = [
    ("GET", "/api/advisor/queue"),
    ("GET", "/api/advisor/proposals/00000000-0000-0000-0000-000000000000"),
    ("POST", "/api/advisor/proposals/00000000-0000-0000-0000-000000000000/review"),
    ("GET", "/api/audit"),
]


def _token(email: str, password: str = "demo1234") -> str:
    r = CLIENTE.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"No se pudo loguear a {email}: {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def token_investor() -> str:
    return _token("juan@demo.ec")


@pytest.fixture(scope="module")
def token_advisor() -> str:
    return _token("asesor@demo.ec")


@pytest.mark.parametrize(("metodo", "ruta"), RUTAS_DEL_ASESOR)
def test_investor_en_rutas_del_asesor_recibe_403(
    metodo: str, ruta: str, token_investor: str
) -> None:
    r = CLIENTE.request(metodo, ruta, headers={"Authorization": f"Bearer {token_investor}"}, json={})
    assert r.status_code == 403, f"{metodo} {ruta} debió dar 403, dio {r.status_code}"


@pytest.mark.parametrize(("metodo", "ruta"), RUTAS_DEL_ASESOR)
def test_sin_token_recibe_401(metodo: str, ruta: str) -> None:
    r = CLIENTE.request(metodo, ruta, json={})
    assert r.status_code == 401, f"{metodo} {ruta} debió dar 401, dio {r.status_code}"


def test_el_asesor_si_entra(token_advisor: str) -> None:
    """El contrapeso: si el guardia bloqueara a todos, los tests de 403 pasarían igual."""
    r = CLIENTE.get("/api/advisor/queue", headers={"Authorization": f"Bearer {token_advisor}"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_el_rol_no_es_negociable_desde_el_cliente(cuenta_desechable: list[str]) -> None:
    """El self-signup crea investors. Pedir 'advisor' en el body no cambia nada.

    El `role` ya no se puede leer en la respuesta del registro (que no trae token, porque
    el correo todavía no está verificado): se lee en el token que sale de /verify-email,
    que es donde de verdad importa — es el que firma el backend.
    """
    email = correo_desechable("rol")
    r = CLIENTE.post(
        "/api/auth/register",
        json={"nombre": "ZZ Rol", "email": email, "password": "demo1234", "role": "advisor"},
    )
    assert r.status_code == 201, r.text
    assert "access_token" not in r.json(), "El registro no puede dejar logueado a nadie"

    r = CLIENTE.post(
        "/api/auth/verify-email",
        json={"email": email, "codigo": codigo_pendiente(email)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "investor"
    cuenta_desechable.append(r.json()["user_id"])

    token = r.json()["access_token"]
    r = CLIENTE.get("/api/advisor/queue", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_sin_verificar_el_correo_no_se_entra(cuenta_desechable: list[str]) -> None:
    """⭐ El gate: la contraseña correcta no alcanza si el correo nunca se probó.

    Sin este test, `email_verified_at` sería una columna decorativa: el registro mandaría
    un código que nadie estaría obligado a leer.
    """
    email = correo_desechable("noverif")
    r = CLIENTE.post(
        "/api/auth/register",
        json={"nombre": "ZZ Sin Verificar", "email": email, "password": "demo1234"},
    )
    assert r.status_code == 201, r.text

    # Credenciales correctas, correo sin verificar → 403, no token.
    r = CLIENTE.post("/api/auth/login", json={"email": email, "password": "demo1234"})
    assert r.status_code == 403, r.text
    assert "access_token" not in r.json()

    # Y con el código, sí entra.
    r = CLIENTE.post(
        "/api/auth/verify-email",
        json={"email": email, "codigo": codigo_pendiente(email)},
    )
    assert r.status_code == 200, r.text
    cuenta_desechable.append(r.json()["user_id"])

    r = CLIENTE.post("/api/auth/login", json={"email": email, "password": "demo1234"})
    assert r.status_code == 200, r.text


def test_un_codigo_equivocado_no_verifica_nada(cuenta_desechable: list[str]) -> None:
    """El código es un secreto, no un formulario: adivinarlo no abre la cuenta."""
    email = correo_desechable("malcodigo")
    CLIENTE.post(
        "/api/auth/register",
        json={"nombre": "ZZ Mal Código", "email": email, "password": "demo1234"},
    )

    real = codigo_pendiente(email)
    falso = f"{(int(real) + 1) % 1_000_000:06d}"

    r = CLIENTE.post("/api/auth/verify-email", json={"email": email, "codigo": falso})
    assert r.status_code == 400, r.text

    # El intento fallido quedó CONTADO (si el rollback se lo comiera, MAX_INTENTOS no
    # frenaría a un script que prueba el millón de códigos).
    fila = fetch_one(
        """
        select c.attempts
          from public.auth_codes c join public.profiles p on p.id = c.profile_id
         where p.email = %s and c.used_at is null
        """,
        (email,),
    )
    assert fila and fila["attempts"] == 1

    # Y el bueno sigue sirviendo.
    r = CLIENTE.post("/api/auth/verify-email", json={"email": email, "codigo": real})
    assert r.status_code == 200, r.text
    cuenta_desechable.append(r.json()["user_id"])


RUTAS_DEL_CLIENTE = ["/breakdown", "/portfolio", ""]


@pytest.mark.parametrize("sufijo", RUTAS_DEL_CLIENTE)
def test_un_investor_no_lee_los_datos_de_otro(sufijo: str, token_investor: str) -> None:
    """La cartera y el perfilamiento de otro cliente son datos ajenos, aunque se sepa su id."""
    ajeno = "00000000-0000-0000-0000-000000000000"
    r = CLIENTE.get(
        f"/api/investor/{ajeno}{sufijo}",
        headers={"Authorization": f"Bearer {token_investor}"},
    )
    assert r.status_code == 403, f"{sufijo or '/{id}'} debió dar 403, dio {r.status_code}"


@pytest.mark.parametrize("sufijo", RUTAS_DEL_CLIENTE)
def test_los_datos_del_cliente_no_son_publicos(sufijo: str) -> None:
    """Sin token no se lee la cartera de nadie. Era el agujero: bastaba conocer un id."""
    alguien = "00000000-0000-0000-0000-000000000000"
    assert CLIENTE.get(f"/api/investor/{alguien}{sufijo}").status_code == 401


def test_el_cliente_si_lee_lo_suyo(token_investor: str) -> None:
    """El contrapeso: si el guardia bloqueara también al dueño, la app no serviría."""
    yo = CLIENTE.get("/api/auth/me", headers={"Authorization": f"Bearer {token_investor}"}).json()
    r = CLIENTE.get(
        f"/api/investor/{yo['id']}/portfolio",
        headers={"Authorization": f"Bearer {token_investor}"},
    )
    assert r.status_code == 200
    assert r.json()["investor_id"] == yo["id"]


def test_el_asesor_lee_la_cartera_de_un_cliente(token_investor: str, token_advisor: str) -> None:
    """Revisar carteras ajenas es el trabajo del asesor (HU3)."""
    yo = CLIENTE.get("/api/auth/me", headers={"Authorization": f"Bearer {token_investor}"}).json()
    r = CLIENTE.get(
        f"/api/investor/{yo['id']}/portfolio",
        headers={"Authorization": f"Bearer {token_advisor}"},
    )
    assert r.status_code == 200


def test_el_perfilamiento_se_adjunta_al_usuario_del_token(
    cuenta_desechable: list[str],
) -> None:
    """⭐ El id del cuestionario ES el del login.

    Antes se creaba un `profiles` nuevo en cada perfilamiento: el cliente terminaba con
    dos identidades y su propia propuesta le quedaba inaccesible. Este test lo impide.
    """
    registro = registrar_verificado(CLIENTE, "dueno", "ZZ Dueño")
    cuenta_desechable.append(registro["user_id"])
    cabeceras = cabeceras_de(registro)

    perfil = CLIENTE.post(
        "/api/investor/profile",
        headers=cabeceras,
        json={
            "monto": 20000,
            "respuestas": {
                "objetivo": "crecer",
                "horizonte": "medio",
                "liquidez": "no",
                "tolerancia": "esperar",
                "preferencia": "seguridad_rentable",
            },
        },
    )
    assert perfil.status_code == 201, perfil.text
    assert perfil.json()["investor_id"] == registro["user_id"]

    # …y por lo tanto puede leer su propia propuesta sin recibir un 403.
    cartera = CLIENTE.get(f"/api/investor/{registro['user_id']}/portfolio", headers=cabeceras)
    assert cartera.status_code == 200
    assert cartera.json()["monto_total"] == 20000.0


def test_un_asesor_no_se_perfila_a_si_mismo(token_advisor: str) -> None:
    """El cuestionario es del inversionista. Un asesor llamándolo recibe 403."""
    r = CLIENTE.post(
        "/api/investor/profile",
        headers={"Authorization": f"Bearer {token_advisor}"},
        json={"monto": 1000, "respuestas": {"objetivo": "crecer"}},
    )
    assert r.status_code == 403
