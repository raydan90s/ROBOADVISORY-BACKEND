"""Toda plantilla suma exactamente 100%.

Un assert. Si una plantilla sumara 95, el cliente recibiría una propuesta que deja el 5%
de su dinero sin asignar y nadie se enteraría hasta producción.
"""

from src.config.database import fetch_all


def test_toda_plantilla_suma_100() -> None:
    filas = fetch_all("select * from public.v_template_integrity")

    assert filas, "No hay plantillas: ¿corriste seed.sql?"
    invalidas = [f"{f['name']} suma {f['total_percentage']}%" for f in filas if not f["is_valid"]]
    assert not invalidas, f"Plantillas que no suman 100%: {invalidas}"
