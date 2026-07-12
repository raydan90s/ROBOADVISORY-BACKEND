"""El feed de noticias degrada con gracia: sin key (o con GNews caído) sirve el
respaldo marcado como tal — la pantalla nunca se queda en blanco ni finge tiempo real.
"""

import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings
from src.main import app
from src.services.feed_service import TEMAS

CLIENTE = TestClient(app)


@pytest.fixture(scope="module")
def cabeceras() -> dict[str, str]:
    r = CLIENTE.post(
        "/api/auth/login", json={"email": "juan@demo.ec", "password": "demo1234"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_sin_token_recibe_401() -> None:
    assert CLIENTE.get("/api/feed").status_code == 401


@pytest.mark.parametrize("tema", list(TEMAS))
def test_cada_tema_responde_noticias_citadas(tema: str, cabeceras: dict[str, str]) -> None:
    """Con o sin key: siempre hay noticias, y cada una trae titular, fuente y link."""
    r = CLIENTE.get(f"/api/feed?tema={tema}", headers=cabeceras)
    assert r.status_code == 200, r.text

    feed = r.json()
    assert feed["tema"] == tema
    assert feed["fuente_datos"] in ("gnews", "respaldo")
    assert len(feed["noticias"]) > 0
    for noticia in feed["noticias"]:
        assert noticia["titulo"]
        assert noticia["fuente"]
        assert noticia["url"].startswith("http")


def test_tema_desconocido_es_422(cabeceras: dict[str, str]) -> None:
    r = CLIENTE.get("/api/feed?tema=deportes", headers=cabeceras)
    assert r.status_code == 422
    assert "Tema desconocido" in r.json()["detail"]


def test_sin_key_el_respaldo_se_declara_como_tal(cabeceras: dict[str, str]) -> None:
    """Si no hay GNEWS_API_KEY, `fuente_datos` debe decir "respaldo": el front avisa
    "modo sin conexión" en vez de fingir que los titulares son de hoy."""
    if settings.GNEWS_API_KEY:
        pytest.skip("Hay key de GNews configurada: este test es para el modo sin key.")
    r = CLIENTE.get("/api/feed?tema=mercados", headers=cabeceras)
    assert r.json()["fuente_datos"] == "respaldo"
