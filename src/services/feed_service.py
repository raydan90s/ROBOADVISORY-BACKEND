"""Feed de noticias financieras: wrapper cacheado de gnews.io (misma receta que
`market_data.py`: httpx + TTLCache + respaldo si la fuente falla).

REGLA CRÍTICA: la cuota gratuita de GNews es de 100 requests/día. La caché de 1 hora
por tema es lo que permite que todos los usuarios de la demo compartan las mismas
4 llamadas por hora. Si la API falla o agota la cuota, se sirven las noticias de
respaldo — el feed nunca se queda en blanco.

Cada noticia viaja con su fuente, su fecha y su link: el mismo principio de
antialucinación del resto del proyecto, aplicado al feed. La app no redacta noticias;
las cita.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from cachetools import TTLCache

from src.config.settings import settings
from src.models.feed import FeedResponse, NoticiaFeed

log = logging.getLogger(__name__)

_BASE_URL = "https://gnews.io/api/v4"

# 1 hora por tema: 4 temas ≈ 96 requests/día como techo, dentro de la cuota de 100.
_cache: TTLCache[str, FeedResponse] = TTLCache(maxsize=16, ttl=3600)
_lock = asyncio.Lock()

# Cada tema es una consulta distinta a GNews. Todas van por `search` con `in=title,
# description`: el `top-headlines` de negocios mete ruido (probado en vivo: coló una
# reseña de cascos de moto), y buscar solo en el cuerpo trae países vecinos.
TEMAS: dict[str, dict[str, str]] = {
    "mercados": {
        "endpoint": "search",
        "q": '"mercados financieros" OR "bolsa de valores" OR inflación OR "tasas de interés" OR dólar',
    },
    "cripto": {"endpoint": "search", "q": "bitcoin OR ethereum OR criptomonedas"},
    "materias": {
        "endpoint": "search",
        "q": 'petróleo OR "precio del oro" OR "materias primas"',
    },
    # Frases exactas: "Ecuador AND economía" en GNews matchea notas de Perú o de fútbol
    # que apenas mencionan al país (probado en vivo). Mejor pocas noticias y nuestras.
    # `in: title`: exigir "Ecuador" en el TITULAR es lo que filtra las notas de Perú y
    # de fútbol que solo lo mencionan de pasada (las frases exactas dieron 0 resultados
    # y el AND sobre el cuerpo trajo vecinos — ambos probados en vivo).
    "ecuador": {
        "endpoint": "search",
        "q": "Ecuador AND (economía OR económico OR inversión OR financiero OR dólar OR tasas)",
        "in": "title",
    },
}

TEMA_DEFAULT = "mercados"

# Respaldo: titulares reales de referencia (11-jul-2026), NO en vivo. Solo aparecen si
# GNews falla o no hay key, y van marcados con fuente_datos="respaldo" para que el
# front pueda decir "modo sin conexión" en vez de fingir que es tiempo real.
_RESPALDO: dict[str, list[dict[str, str]]] = {
    "mercados": [
        {
            "titulo": "El dólar en Colombia cerró en su menor nivel en más de siete años",
            "fuente": "El Tiempo",
            "url": "https://www.eltiempo.com/economia",
        },
        {
            "titulo": "El precio del combustible para aviones se desplomó: la explicación del CEO de Delta",
            "fuente": "CNN en Español",
            "url": "https://cnnespanol.cnn.com",
        },
    ],
    "cripto": [
        {
            "titulo": "Bitcoin se aferra a los USD 64.000 mientras el volumen se desploma 40%",
            "fuente": "DiarioBitcoin",
            "url": "https://www.diariobitcoin.com",
        },
        {
            "titulo": "Bitcoin y ether apenas cambian ante la incertidumbre geopolítica",
            "fuente": "CoinDesk",
            "url": "https://www.coindesk.com/es",
        },
    ],
    "materias": [
        {
            "titulo": "El petróleo sube 5% y los mercados bajan ante la incertidumbre global",
            "fuente": "Hartford Courant",
            "url": "https://www.courant.com",
        },
        {
            "titulo": "El oro se mantiene como refugio ante la volatilidad de los mercados",
            "fuente": "Infobae",
            "url": "https://www.infobae.com/economia",
        },
    ],
    "ecuador": [
        {
            "titulo": "Los depósitos a plazo fijo en Ecuador pagan entre 4,5% y 9,7% según el plazo",
            "fuente": "Primicias",
            "url": "https://www.primicias.ec",
        },
        {
            "titulo": "La banca ecuatoriana reporta crecimiento en captaciones a plazo",
            "fuente": "El Universo",
            "url": "https://www.eluniverso.com/noticias/economia",
        },
    ],
}


def _respaldo_de(tema: str) -> FeedResponse:
    ahora = datetime.now(timezone.utc).isoformat()
    return FeedResponse(
        tema=tema,
        fuente_datos="respaldo",
        actualizado_en=ahora,
        noticias=[
            NoticiaFeed(
                titulo=n["titulo"],
                descripcion=None,
                url=n["url"],
                imagen=None,  # el front pinta el visual del tema
                fuente=n["fuente"],
                fecha=None,   # no es de hoy y no se finge que lo sea
                tema=tema,
            )
            for n in _RESPALDO[tema]
        ],
    )


async def _pedir_gnews(tema: str) -> FeedResponse | None:
    cfg = TEMAS[tema]
    params: dict[str, str] = {
        "lang": "es",
        "max": "10",
        # Solo título y descripción (o lo que pida el tema): buscar también en el
        # cuerpo trae artículos que apenas rozan el tema.
        "in": cfg.get("in", "title,description"),
        "apikey": settings.GNEWS_API_KEY,
    }
    if cfg["endpoint"] == "top-headlines":
        params["category"] = cfg["category"]
        params.pop("in")  # top-headlines no acepta `in`
    else:
        params["q"] = cfg["q"]

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_BASE_URL}/{cfg['endpoint']}", params=params, timeout=10.0
            )
            if resp.status_code == 429:
                # El free tier limita a ~1 request/segundo. Abrir la pantalla dispara
                # varios temas casi a la vez: esperar un tick y reintentar UNA vez.
                await asyncio.sleep(1.5)
                resp = await client.get(
                    f"{_BASE_URL}/{cfg['endpoint']}", params=params, timeout=10.0
                )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # red caída, cuota agotada (403/429), key inválida…
        log.warning("GNews falló para el tema '%s': %s", tema, exc)
        return None

    articulos = payload.get("articles") or []
    if not articulos:
        return None

    return FeedResponse(
        tema=tema,
        fuente_datos="gnews",
        actualizado_en=datetime.now(timezone.utc).isoformat(),
        noticias=[
            NoticiaFeed(
                titulo=a.get("title") or "(sin título)",
                descripcion=a.get("description"),
                url=a.get("url") or "",
                imagen=a.get("image"),
                fuente=(a.get("source") or {}).get("name") or "Fuente no declarada",
                fecha=a.get("publishedAt"),
                tema=tema,
            )
            for a in articulos
            if a.get("url")
        ],
    )


async def obtener_feed(tema: str) -> FeedResponse:
    """Noticias del tema, de la caché si están tibias. Nunca lanza: degrada al respaldo."""
    if tema in _cache:
        return _cache[tema]

    # El lock evita que dos requests simultáneos del mismo tema gasten dos llamadas
    # de la cuota diaria (la demo abre esta pantalla en varios dispositivos a la vez).
    async with _lock:
        if tema in _cache:
            return _cache[tema]

        if not settings.GNEWS_API_KEY:
            log.warning("Sin GNEWS_API_KEY: el feed sirve las noticias de respaldo.")
            return _respaldo_de(tema)

        feed = await _pedir_gnews(tema)
        if feed is None:
            return _respaldo_de(tema)  # no se cachea: a la próxima se reintenta

        _cache[tema] = feed
        return feed
