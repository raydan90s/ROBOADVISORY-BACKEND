"""Endpoint de mercados externos: el ticker del dashboard. Solo I/O HTTP.

Autenticado como el resto de la app (nada de datos públicos salvo `/questions`), aunque
acá no hay dueño que validar: cualquier usuario logueado ve el mismo ticker.
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, Query

from src.controllers import market_controller
from src.dependencies.auth import get_current_user
from src.models.auth import CurrentUser
from src.models.market import MarketHistoryResponse, MarketQuotesResponse
from src.services.market_data import SIMBOLOS_DEFAULT

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get(
    "/quotes",
    response_model=MarketQuotesResponse,
    summary="Cotizaciones de mercados externos (Alpha Vantage, cacheadas 1h)",
)
async def get_quotes(
    symbols: str | None = Query(
        None,
        description=f"Símbolos separados por coma. Sin este parámetro: {','.join(SIMBOLOS_DEFAULT)}.",
    ),
    _usuario: CurrentUser = Depends(get_current_user),
) -> MarketQuotesResponse:
    lista = [s.strip().upper() for s in symbols.split(",")] if symbols else SIMBOLOS_DEFAULT
    lista = [s for s in lista if s]
    return await market_controller.obtener_cotizaciones(lista)


@router.get(
    "/history",
    response_model=MarketHistoryResponse,
    summary="Serie diaria de un símbolo para gráficos (Alpha Vantage, cacheada 1h)",
)
async def get_history(
    symbol: str = Query(..., description="Un símbolo del ticker, ej. BTCUSD, SPY, EURUSD."),
    days: int = Query(30, ge=5, le=100, description="Cantidad de días de historial (5-100)."),
    _usuario: CurrentUser = Depends(get_current_user),
) -> MarketHistoryResponse:
    return await market_controller.obtener_historico(symbol.strip().upper(), days)
