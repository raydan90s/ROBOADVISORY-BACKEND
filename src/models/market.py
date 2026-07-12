"""Esquemas de mercados externos (ticker del inversionista y Rutas B/C del agente).

Deliberadamente separado de `models/investor.py`: estos instrumentos NO están en el
catálogo del banco (`instruments`/`institutions`) y nunca deben mezclarse con una
propuesta real.
"""

from pydantic import BaseModel


class MarketQuoteOut(BaseModel):
    """Una cotización, con su origen: Alpha Vantage en vivo o el respaldo simulado."""

    symbol: str
    price: float
    change_percent: float
    source: str  # "alpha_vantage" | "mock"
    as_of: str


class MarketQuotesResponse(BaseModel):
    quotes: list[MarketQuoteOut]
