"""Orquesta el wrapper cacheado de Alpha Vantage (`services/market_data.py`)."""

from src.models.market import MarketQuoteOut, MarketQuotesResponse
from src.services import market_data


async def obtener_cotizaciones(symbols: list[str]) -> MarketQuotesResponse:
    cotizaciones = await market_data.obtener_cotizaciones(symbols)
    return MarketQuotesResponse(
        quotes=[
            MarketQuoteOut(
                symbol=c.symbol,
                price=c.price,
                change_percent=c.change_percent,
                source=c.source,
                as_of=c.as_of,
            )
            for c in cotizaciones
        ]
    )
