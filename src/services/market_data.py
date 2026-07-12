"""Wrapper cacheado de Alpha Vantage: cotizaciones de mercados externos (NO el catálogo
del banco — eso sigue viviendo en `instruments`/`institutions` y en `catalog_controller.py`).

REGLA CRÍTICA: la cuota gratuita de Alpha Vantage es de 25 requests/día. Una caché en
memoria con TTL de 1 hora es lo que hace que el ticker del front (que consulta cada
pocos segundos) y el agente conversacional (Rutas B/C) no la agoten en los primeros
minutos de la demo. Si la API responde "Note"/"Information" (rate limit) o falla, se
cae a una cotización simulada — el ticker y el chat nunca se quedan en blanco.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from cachetools import TTLCache

from src.config.settings import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://www.alphavantage.co/query"

# 1 hora: suficiente para que el ticker (refrescando cada 30-60s en el front) y el
# agente reutilicen la misma llamada en vez de gastar cuota en cada request.
_cache: TTLCache[str, "MarketQuote"] = TTLCache(maxsize=64, ttl=3600)


@dataclass(frozen=True)
class MarketQuote:
    """Una cotización, venga de Alpha Vantage o del mock de respaldo."""

    symbol: str
    price: float
    change_percent: float
    source: str  # "alpha_vantage" | "mock"
    as_of: str  # ISO 8601


# Cotizaciones de respaldo: solo se usan si Alpha Vantage falla o agotó la cuota (la
# demo nunca debe mostrar un ticker vacío). Son valores de referencia, no en vivo.
_MOCK: dict[str, tuple[float, float]] = {
    "BTCUSD": (67250.32, 1.85),
    "XAUUSD": (2385.10, -0.32),
    "JPN225": (39250.75, 0.64),
    "SPY": (552.18, 0.41),
    "EURUSD": (1.0875, -0.12),
}

# Cómo pedirle cada símbolo a Alpha Vantage. CURRENCY_EXCHANGE_RATE cubre forex Y
# cripto-a-fiat (BTC/USD) Y metales (XAU/USD se trata como "moneda física" en su API).
# GLOBAL_QUOTE es para acciones/ETFs. JPN225 (Nikkei) no tiene un símbolo limpio en el
# free tier de Alpha Vantage: se deja en GLOBAL_QUOTE a propósito, y si no responde,
# cae al mock — es una limitación conocida de la cuota gratuita, no un bug.
_SYMBOL_CONFIG: dict[str, dict[str, str]] = {
    "BTCUSD": {"function": "CURRENCY_EXCHANGE_RATE", "from_currency": "BTC", "to_currency": "USD"},
    "XAUUSD": {"function": "CURRENCY_EXCHANGE_RATE", "from_currency": "XAU", "to_currency": "USD"},
    "EURUSD": {"function": "CURRENCY_EXCHANGE_RATE", "from_currency": "EUR", "to_currency": "USD"},
    "SPY": {"function": "GLOBAL_QUOTE", "symbol": "SPY"},
    "JPN225": {"function": "GLOBAL_QUOTE", "symbol": "JPN225"},
}

SIMBOLOS_DEFAULT = list(_SYMBOL_CONFIG)  # el set que pide el ticker del front


def _mock_de(symbol: str) -> MarketQuote:
    precio, cambio = _MOCK.get(symbol, (0.0, 0.0))
    return MarketQuote(
        symbol=symbol,
        price=precio,
        change_percent=cambio,
        source="mock",
        as_of=datetime.now(timezone.utc).isoformat(),
    )


def _es_error_de_cuota(payload: dict) -> bool:
    """Alpha Vantage no usa códigos HTTP de error para el rate limit: devuelve 200 con
    una clave "Note" (límite por minuto) o "Information" (límite diario) en el JSON."""
    return "Note" in payload or "Information" in payload or "Error Message" in payload


async def _pedir_alpha_vantage(client: httpx.AsyncClient, symbol: str) -> MarketQuote | None:
    cfg = _SYMBOL_CONFIG.get(symbol, {"function": "GLOBAL_QUOTE", "symbol": symbol})
    params = {**cfg, "apikey": settings.ALPHA_VANTAGE_API_KEY}

    try:
        resp = await client.get(_BASE_URL, params=params, timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning("Alpha Vantage falló para %s: %s", symbol, exc)
        return None

    if _es_error_de_cuota(payload):
        log.warning("Alpha Vantage: cuota agotada o símbolo inválido para %s: %s", symbol, payload)
        return None

    try:
        if cfg["function"] == "CURRENCY_EXCHANGE_RATE":
            datos = payload["Realtime Currency Exchange Rate"]
            precio = float(datos["5. Exchange Rate"])
            # El endpoint de forex no trae variación %: Alpha Vantage no la calcula acá.
            return MarketQuote(
                symbol=symbol,
                price=round(precio, 4),
                change_percent=0.0,
                source="alpha_vantage",
                as_of=datos.get("6. Last Refreshed", datetime.now(timezone.utc).isoformat()),
            )

        datos = payload["Global Quote"]
        precio = float(datos["05. price"])
        cambio_pct = float(datos["10. change percent"].rstrip("%"))
        return MarketQuote(
            symbol=symbol,
            price=round(precio, 2),
            change_percent=round(cambio_pct, 2),
            source="alpha_vantage",
            as_of=datos.get("07. latest trading day", datetime.now(timezone.utc).isoformat()),
        )
    except (KeyError, ValueError) as exc:
        log.warning("Alpha Vantage: respuesta inesperada para %s: %s (%s)", symbol, payload, exc)
        return None


async def obtener_cotizacion(symbol: str) -> MarketQuote:
    """Una cotización, cacheada 1 hora. Nunca lanza: si todo falla, devuelve el mock."""
    symbol = symbol.strip().upper()

    if symbol in _cache:
        return _cache[symbol]

    cotizacion: MarketQuote | None = None
    if settings.ALPHA_VANTAGE_API_KEY:
        async with httpx.AsyncClient() as client:
            cotizacion = await _pedir_alpha_vantage(client, symbol)

    if cotizacion is None:
        cotizacion = _mock_de(symbol)

    _cache[symbol] = cotizacion
    return cotizacion


async def obtener_cotizaciones(symbols: list[str]) -> list[MarketQuote]:
    """Varias cotizaciones. Secuencial y con una pequeña pausa entre llamadas EN FRÍO
    (Alpha Vantage free tier: 1 req/segundo): con la caché tibia, esto no se nota —
    solo paga el costo la primera vez que se piden todos los símbolos."""
    resultado: list[MarketQuote] = []
    for i, symbol in enumerate(symbols):
        en_cache = symbol.strip().upper() in _cache
        if i > 0 and not en_cache:
            await asyncio.sleep(1.1)
        resultado.append(await obtener_cotizacion(symbol))
    return resultado
