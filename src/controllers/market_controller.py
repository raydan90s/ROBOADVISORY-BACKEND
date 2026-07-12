"""Orquesta el wrapper cacheado de Alpha Vantage (`services/market_data.py`)."""

from src.models.market import (
    HistoricalPointOut,
    MarketHistoryResponse,
    MarketQuoteOut,
    MarketQuotesResponse,
)
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


async def obtener_historico(symbol: str, dias: int) -> MarketHistoryResponse:
    serie = await market_data.obtener_historico(symbol, dias)
    return MarketHistoryResponse(
        symbol=serie.symbol,
        source=serie.source,
        points=[HistoricalPointOut(date=p.date, close=p.close) for p in serie.points],
    )
