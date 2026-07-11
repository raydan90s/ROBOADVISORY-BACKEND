"""Esquemas Pydantic del inversionista y de la propuesta de portafolio.

Los enums espejan los tipos ENUM de Postgres definidos en schema.sql: si cambias
uno allá, cámbialo aquí. Los porcentajes y puntajes NUNCA los inventa el LLM;
salen de scoring_rules / profile_thresholds / allocation_template_items.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class PerfilRiesgo(str, Enum):
    """risk_profiles.code"""

    CONSERVADOR = "conservador"
    MODERADO = "moderado"
    AGRESIVO = "agresivo"


class NivelRiesgo(str, Enum):
    """enum risk_level"""

    BAJO = "bajo"
    MEDIO = "medio"
    ALTO = "alto"


class EstadoPropuesta(str, Enum):
    """enum proposal_status"""

    PENDIENTE_REVISION = "pending_review"
    APROBADA = "approved"
    EDITADA = "edited"
    RECHAZADA = "rejected"


# ---------------------------------------------------------------------------
# Cuestionario (se sirve desde la BD, no está hardcodeado en el front)
# ---------------------------------------------------------------------------


class OpcionPregunta(BaseModel):
    code: str
    label: str


class Pregunta(BaseModel):
    code: str
    text: str
    opciones: list[OpcionPregunta]


# ---------------------------------------------------------------------------
# Perfilamiento
# ---------------------------------------------------------------------------


class InvestorProfileCreate(BaseModel):
    """Body del POST /api/investor/profile."""

    nombre: str = Field(..., min_length=2, max_length=120)
    email: str | None = None
    cedula_ruc: str | None = None

    # {question_code: option_code}, ej. {"objetivo": "crecer", "horizonte": "largo"}
    # Los códigos válidos salen de GET /api/investor/questions.
    # El puntaje no viaja desde el cliente: lo calcula la BD vía scoring_rules.
    respuestas: dict[str, str] = Field(..., min_length=1)


class RespuestaDetalle(BaseModel):
    """Una respuesta con los puntos que aportó — permite explicarle al usuario el porqué."""

    pregunta_code: str
    pregunta_text: str
    opcion_code: str
    opcion_label: str
    puntos: int


class Investor(BaseModel):
    """Resultado del perfilamiento: el profile + su última sesión."""

    investor_id: str
    session_id: str
    nombre: str
    email: str | None = None
    cedula_ruc: str | None = None

    puntaje: int
    perfil_riesgo: PerfilRiesgo
    respuestas: list[RespuestaDetalle] = Field(default_factory=list)

    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Propuesta de portafolio
# ---------------------------------------------------------------------------


class AssetAllocation(BaseModel):
    """Una línea del portafolio, tomada de allocation_template_items + instruments."""

    instrumento_code: str
    nombre: str
    clase_activo: str
    riesgo: NivelRiesgo
    porcentaje: float = Field(..., gt=0, le=100)
    retorno_esperado: float | None = None


class PortfolioProposal(BaseModel):
    """Respuesta del GET /api/investor/{id}/portfolio."""

    proposal_id: str
    investor_id: str
    session_id: str

    perfil_riesgo: PerfilRiesgo
    puntaje: int
    riesgo_esperado: NivelRiesgo
    estado: EstadoPropuesta

    allocations: list[AssetAllocation] = Field(default_factory=list)
    # Promedio ponderado de instruments.expected_return. Ficticio, solo demo.
    retorno_esperado_anual: float | None = None

    # Único campo que redacta el LLM.
    explicacion: str | None = None
