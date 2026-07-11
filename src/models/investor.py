"""Esquemas Pydantic del inversionista y de la propuesta de portafolio.

Los enums espejan los tipos ENUM de Postgres definidos en schema.sql: si cambias
uno allá, cámbialo aquí. Los porcentajes y puntajes NUNCA los inventa el LLM;
salen de scoring_rules / profile_thresholds / allocation_template_items.
"""

from datetime import date, datetime
from decimal import Decimal
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
    """Body del POST /api/investor/profile.

    El endpoint es autenticado: el perfilamiento se le adjunta al **usuario del token**.
    Por eso acá no viajan ni el nombre ni el correo — dejar que el cliente los mandara
    permitiría perfilar a nombre de otra persona.
    """

    # Sin monto la propuesta son porcentajes flotando en el aire. El ejemplo del reto
    # muestra "60% (USD 12.000)", y esos USD los calcula Postgres, no el LLM.
    monto: Decimal = Field(..., gt=0, max_digits=14, decimal_places=2)

    # La cédula no se pide en el registro; si el cliente la aporta al perfilarse, se
    # completa en su perfil (nunca se sobrescribe una ya existente).
    cedula_ruc: str | None = None

    # Si viene, esta sesión ES una subcuenta: se valida contra el capital sin asignar
    # (trigger `fn_valida_capital_subcuenta`, migración 002). Sin nombre es un
    # perfilamiento normal, como antes de las subcuentas.
    subaccount_name: str | None = Field(None, min_length=1, max_length=120)

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

    # El monto que declaró en el cuestionario. None solo en sesiones viejas.
    monto: float | None = None
    # Nombre de la subcuenta. None si esta sesión no es una subcuenta.
    subaccount_name: str | None = None

    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Propuesta de portafolio
# ---------------------------------------------------------------------------


class AssetAllocation(BaseModel):
    """Una línea del portafolio: producto + emisor + calificación + USD asignados.

    Los USD (`monto_asignado`) los calcula Postgres a partir del porcentaje y del monto
    total. La calificación viaja siempre con su fuente y su fecha: mostrarla sin ellas
    sería presentar como vigente un dato que es referencial.
    """

    instrumento_code: str
    nombre: str
    clase_activo: str
    riesgo: NivelRiesgo
    porcentaje: float = Field(..., gt=0, le=100)
    retorno_esperado: float | None = None

    monto_asignado: float | None = None
    plazo_dias: int | None = None

    institucion: str | None = None
    calificacion: str | None = None
    calificacion_fuente: str | None = None
    calificacion_fecha: date | None = None


class PortfolioProposal(BaseModel):
    """Respuesta del GET /api/investor/{id}/portfolio."""

    proposal_id: str
    investor_id: str
    session_id: str

    perfil_riesgo: PerfilRiesgo
    puntaje: int
    riesgo_esperado: NivelRiesgo
    estado: EstadoPropuesta

    monto_total: float | None = None

    allocations: list[AssetAllocation] = Field(default_factory=list)
    # Promedio ponderado de instruments.expected_return. Ficticio, solo demo.
    retorno_esperado_anual: float | None = None

    # Único campo que redacta el LLM.
    explicacion: str | None = None


# ---------------------------------------------------------------------------
# "¿Cómo se calculó?" (HU1, criterio 3)
# ---------------------------------------------------------------------------


class BreakdownRespuesta(BaseModel):
    """Una fila de la tabla respuesta → puntos."""

    question_code: str
    question_text: str
    option_code: str
    option_label: str
    puntos: int


# ---------------------------------------------------------------------------
# Subcuentas (capital total dividido en sub-inversiones con perfil propio)
# ---------------------------------------------------------------------------


class CapitalAsignar(BaseModel):
    """Body del POST /api/investor/capital: declara el capital total disponible."""

    monto: Decimal = Field(..., gt=0, max_digits=14, decimal_places=2)


class CapitalResponse(BaseModel):
    """Respuesta del POST /api/investor/capital."""

    investor_id: str
    capital_total: float
    capital_asignado: float
    capital_disponible: float


class Subcuenta(BaseModel):
    """Una subcuenta: una sesión de perfilamiento con nombre propio y su propio monto.

    Cada subcuenta puede tener su propio perfil de riesgo (responde su propio
    cuestionario) y su propia propuesta de portafolio, seleccionable vía
    `?session_id=` en GET /api/investor/{id}/portfolio.
    """

    session_id: str
    investor_id: str
    subaccount_name: str
    monto: float | None = None
    perfil_riesgo: PerfilRiesgo | None = None
    puntaje: int | None = None
    estado_propuesta: EstadoPropuesta | None = None
    created_at: datetime | None = None


class ProfilingBreakdown(BaseModel):
    """El desglose completo del puntaje: es la ComoSeCalculoPage, servida por la BD.

    Trae la **versión de reglas** y los **umbrales** a la vista, y la regla de
    elegibilidad por calificación de la institución. Nada de esto lo escribe el front:
    si mañana cambian los puntos de una opción, la pantalla cambia sola.
    """

    session_id: str
    investor_id: str

    puntaje: int
    monto: float | None = None
    rules_version: str

    perfil_code: PerfilRiesgo | None = None
    perfil_nombre: str | None = None
    umbral_min: int | None = None
    umbral_max: int | None = None

    # "Tu perfil admite instituciones hasta AA" — la regla, en las palabras de la BD.
    regla_institucion: str | None = None
    max_rating_tier: int | None = None

    respuestas: list[BreakdownRespuesta] = Field(default_factory=list)
