"""Esquemas Pydantic del inversionista y de la propuesta de portafolio."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PerfilRiesgo(str, Enum):
    CONSERVADOR = "conservador"
    MODERADO = "moderado"
    AGRESIVO = "agresivo"


class EstadoPropuesta(str, Enum):
    PENDIENTE = "pendiente"      # perfil guardado, IA aún no corre
    GENERANDO = "generando"      # el agente está trabajando
    LISTA = "lista"              # propuesta disponible
    ACEPTADA = "aceptada"        # el usuario la aprobó
    RECHAZADA = "rechazada"


class InvestorProfileCreate(BaseModel):
    """Body del POST /api/investor/profile — lo que manda el frontend (Expo)."""

    nombre: str = Field(..., min_length=2, max_length=120)
    email: str | None = None
    edad: int | None = Field(default=None, ge=18, le=100)
    horizonte_anios: int | None = Field(default=None, ge=1, le=40)
    monto_inicial: float | None = Field(default=None, ge=0)

    # Respuestas crudas del test de riesgo: {"pregunta_1": 3, "pregunta_2": 1, ...}
    # El puntaje NO viene del cliente: se calcula en el backend (ver
    # calcular_puntaje_riesgo en investor_controller.py) para que nadie lo falsee.
    respuestas_riesgo: dict[str, int] = Field(default_factory=dict)


class Investor(BaseModel):
    """Representación completa de la fila en la tabla `investors`."""

    id: str
    nombre: str
    email: str | None = None
    edad: int | None = None
    horizonte_anios: int | None = None
    monto_inicial: float | None = None

    respuestas_riesgo: dict[str, int] = Field(default_factory=dict)
    puntaje_riesgo: int = Field(default=0, ge=0, le=100)
    perfil_riesgo: PerfilRiesgo = PerfilRiesgo.MODERADO
    estado_propuesta: EstadoPropuesta = EstadoPropuesta.PENDIENTE

    created_at: datetime | None = None


class AssetAllocation(BaseModel):
    """Una línea del portafolio propuesto."""

    ticker: str                      # "SPY", "BONO-SOBERANO-2030", "BTC"...
    nombre: str
    clase_activo: str                # "renta_variable" | "renta_fija" | "cripto" | "cash"
    porcentaje: float = Field(..., ge=0, le=100)
    justificacion: str | None = None


class PortfolioProposal(BaseModel):
    """Respuesta del GET /api/investor/{id}/portfolio."""

    investor_id: str
    perfil_riesgo: PerfilRiesgo
    puntaje_riesgo: int
    estado_propuesta: EstadoPropuesta

    allocations: list[AssetAllocation] = Field(default_factory=list)
    retorno_esperado_anual: float | None = None
    volatilidad_esperada: float | None = None

    # Texto en lenguaje natural que genera el agente para mostrar en la app.
    resumen_ia: str | None = None

    # Espacio libre para lo que el agente quiera devolver (trazas, fuentes, etc.)
    metadata: dict[str, Any] = Field(default_factory=dict)
