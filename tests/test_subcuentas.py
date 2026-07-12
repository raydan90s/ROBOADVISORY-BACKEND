"""⭐ El motor de subcuentas: un capital total repartido en varias subcuentas.

`test_el_capital_se_reparte_en_subcuentas_sin_pasarse` es el caso del reto: USD 40.000
repartidos en 20k/10k/10k caben exactos, y una cuarta subcuenta de USD 1 ya no cabe.

La regla vive en el trigger `fn_valida_capital_subcuenta` (migración 002), no en
Python: este test ejercita la app real por HTTP porque lo que se quiere probar es que
el guardarraíl de la base efectivamente bloquea al cliente, no una función suelta.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.ayudas_auth import cabeceras_de, registrar_verificado

from src.config.database import get_connection
from src.main import app

CLIENTE = TestClient(app)

RESPUESTAS = {
    "objetivo": "crecer",
    "horizonte": "medio",
    "liquidez": "no",
    "tolerancia": "esperar",
    "preferencia": "seguridad_rentable",
}


@pytest.fixture
def cuenta_desechable() -> Iterator[list[str]]:
    """Cuentas que el test crea y que el test borra (ver test_roles.py)."""
    creadas: list[str] = []
    yield creadas

    with get_connection() as conn:
        for profile_id in creadas:
            conn.execute("delete from public.audit_log where actor_id = %s", (profile_id,))
            conn.execute("delete from public.profiles where id = %s", (profile_id,))


@pytest.fixture
def cabeceras(cuenta_desechable: list[str]) -> dict[str, str]:
    """Un inversionista nuevo, logueado, listo para declarar capital.

    Registrar ya no basta para tener token: hay que verificar el correo. `registrar_
    verificado` lee el código de `auth_codes` — lo que el usuario haría en su bandeja.
    """
    registro = registrar_verificado(CLIENTE, "subcuentas", "ZZ Subcuentas")
    cuenta_desechable.append(registro["user_id"])
    return cabeceras_de(registro)


def _fijar_capital(monto: int, cabeceras: dict[str, str]):
    return CLIENTE.post(
        "/api/investor/capital", headers=cabeceras, json={"capital_total": monto}
    )


def _crear_subcuenta(nombre: str, monto: int, cabeceras: dict[str, str]):
    return CLIENTE.post(
        "/api/investor/profile",
        headers=cabeceras,
        json={"monto": monto, "nombre_subcuenta": nombre, "respuestas": RESPUESTAS},
    )


def test_el_capital_se_reparte_en_subcuentas_sin_pasarse(cabeceras: dict[str, str]) -> None:
    """⭐ USD 40.000 -> 20k/10k/10k caben; una cuarta subcuenta de USD 1 ya no."""
    capital = _fijar_capital(40000, cabeceras)
    assert capital.status_code == 200, capital.text
    # Recién declarado: no hay nada repartido todavía.
    assert capital.json()["sin_asignar"] == 40000.0

    a = _crear_subcuenta("Jubilación", 20000, cabeceras)
    assert a.status_code == 201, a.text

    b = _crear_subcuenta("Viaje", 10000, cabeceras)
    assert b.status_code == 201, b.text

    c = _crear_subcuenta("Emergencia", 10000, cabeceras)
    assert c.status_code == 201, c.text

    # El capital está exactamente agotado: una cuarta subcuenta, aunque sea de USD 1,
    # no cabe.
    d = _crear_subcuenta("Extra", 1, cabeceras)
    assert d.status_code == 422, d.text
    assert "capital" in d.json()["detail"].lower()


def test_las_subcuentas_creadas_aparecen_en_el_listado(cabeceras: dict[str, str]) -> None:
    _fijar_capital(20000, cabeceras)
    _crear_subcuenta("Casa", 15000, cabeceras)

    yo = CLIENTE.get("/api/auth/me", headers=cabeceras).json()
    respuesta = CLIENTE.get(f"/api/investor/{yo['id']}/subaccounts", headers=cabeceras)
    assert respuesta.status_code == 200, respuesta.text

    resumen = respuesta.json()
    assert [s["nombre"] for s in resumen["subcuentas"]] == ["Casa"]
    assert resumen["subcuentas"][0]["monto"] == 15000.0

    # Las tres cifras del encabezado las calcula el servidor: el front no resta nada.
    assert resumen["capital_total"] == 20000.0
    assert resumen["asignado"] == 15000.0
    assert resumen["sin_asignar"] == 5000.0


def test_no_se_puede_declarar_menos_capital_del_ya_repartido(cabeceras: dict[str, str]) -> None:
    """Bajar el capital total por debajo de lo ya asignado dejaría un sin_asignar negativo."""
    _fijar_capital(10000, cabeceras)
    _crear_subcuenta("Ahorro", 10000, cabeceras)

    r = _fijar_capital(5000, cabeceras)
    assert r.status_code == 422, r.text
