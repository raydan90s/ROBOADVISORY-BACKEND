"""Esquemas Pydantic del asesor (HU3) y de la auditoría.

La regla del track: cada decisión queda registrada con **fecha**, **versión de reglas**
y **responsable**. Eso vive en `advisor_reviews` y se refleja en `RevisionResultado`.

`RevisionRequest` valida en el servidor lo que el cliente no puede decidir:
- rechazar exige comentario,
- editar exige una asignación que sume exactamente 100%, sin instrumentos repetidos.
Los códigos de instrumento se verifican contra el catálogo en el controller (la lista
de códigos válidos está en la base, no aquí).
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.models.investor import EstadoPropuesta, NivelRiesgo


class Decision(str, Enum):
    """enum review_decision"""

    APROBADA = "approved"
    EDITADA = "edited"
    RECHAZADA = "rejected"


# ---------------------------------------------------------------------------
# Cola de revisión — GET /api/advisor/queue
# ---------------------------------------------------------------------------


class ColaItem(BaseModel):
    """Una tarjeta de `v_advisor_review_queue`: solo propuestas en `pending_review`."""

    proposal_id: str
    session_id: str
    investor_id: str

    investor_nombre: str
    cedula_ruc: str | None = None
    # De qué subcuenta salió esta solicitud. None si la sesión no es una subcuenta.
    subaccount_name: str | None = None

    puntaje: int | None = None
    perfil_riesgo: str | None = None
    riesgo_esperado: NivelRiesgo
    estado: EstadoPropuesta

    monto_total: float | None = None
    explicacion: str | None = None
    creada_en: datetime


# ---------------------------------------------------------------------------
# Detalle de la propuesta — GET /api/advisor/proposals/{id}
# ---------------------------------------------------------------------------


class LineaPropuesta(BaseModel):
    """Una línea de `v_investor_proposal_summary`: producto + emisor + calificación.

    La calificación viaja siempre con su fuente y su fecha: mostrarla sin ellas sería
    presentar como vigente un dato que es referencial.
    """

    instrumento_code: str
    nombre: str
    tipo_producto: str | None = None
    riesgo: NivelRiesgo
    porcentaje: float
    monto_asignado: float | None = None
    retorno_esperado: float | None = None
    plazo_dias: int | None = None

    institucion: str
    calificacion: str
    calificacion_fuente: str | None = None
    calificacion_fecha: date | None = None

    monto_minimo: float | None = None


class RevisionPrevia(BaseModel):
    """Una decisión ya tomada sobre esta propuesta (fecha · versión · responsable)."""

    review_id: str
    decision: Decision
    comments: str | None = None
    advisor_id: str
    advisor_nombre: str | None = None
    rules_version: str | None = None
    decided_at: datetime


class RefutacionPrevia(BaseModel):
    """Una refutación del inversionista: por qué devolvió a la cola lo ya firmado.

    Sale del `audit_log` (action = 'investor_refuted'), no de `advisor_reviews`: refutar
    no es una decisión del asesor, es el cliente contestándola. El asesor la necesita a
    la vista para que su segunda decisión responda al reclamo y no lo repita.
    """

    comments: str | None = None
    # Qué decisión estaba contestando ('approved' o 'edited').
    estado_refutado: str | None = None
    investor_nombre: str | None = None
    refutada_en: datetime


class PropuestaDetalle(BaseModel):
    """Lo que ve el asesor antes de decidir. Todo determinista salvo `explicacion`."""

    proposal_id: str
    session_id: str
    investor_id: str

    investor_nombre: str
    investor_email: str | None = None
    cedula_ruc: str | None = None
    # De qué subcuenta salió esta solicitud. None si la sesión no es una subcuenta.
    subaccount_name: str | None = None

    puntaje: int | None = None
    perfil_riesgo: str | None = None
    riesgo_esperado: NivelRiesgo
    estado: EstadoPropuesta

    monto_total: float | None = None
    explicacion: str | None = None
    creada_en: datetime

    allocations: list[LineaPropuesta] = Field(default_factory=list)

    # Comparaciones puras contra la base, sin IA: "el monto asignado a X queda bajo el
    # mínimo de acceso", "el puntaje está en el borde del umbral". Son el resumen que
    # la HU3 le debe al asesor.
    banderas: list[str] = Field(default_factory=list)

    revisiones: list[RevisionPrevia] = Field(default_factory=list)
    # Las veces que el inversionista devolvió una decisión firmada. Intercaladas con
    # `revisiones` por fecha, cuentan la conversación completa sobre esta cartera.
    refutaciones: list[RefutacionPrevia] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Decisión — POST /api/advisor/proposals/{id}/review
# ---------------------------------------------------------------------------


class LineaEditada(BaseModel):
    """Una línea de la asignación corregida a mano por el asesor."""

    instrumento_code: str = Field(..., min_length=1, max_length=60)
    # numeric(5,2) en la base: más de 2 decimales se perdería en silencio.
    porcentaje: Decimal = Field(..., gt=0, le=100, max_digits=5, decimal_places=2)


class RevisionRequest(BaseModel):
    """Body del POST. La coherencia se valida acá; el catálogo, contra la base."""

    decision: Decision
    comments: str | None = Field(None, max_length=2000)
    edited_allocation: list[LineaEditada] | None = None

    @model_validator(mode="after")
    def _coherente(self) -> "RevisionRequest":
        if self.decision is Decision.RECHAZADA and not (self.comments or "").strip():
            raise ValueError("Al rechazar una propuesta, 'comments' es obligatorio.")

        if self.decision is Decision.EDITADA:
            if not self.edited_allocation:
                raise ValueError(
                    "Al editar una propuesta, 'edited_allocation' es obligatorio."
                )

            codigos = [linea.instrumento_code for linea in self.edited_allocation]
            if len(set(codigos)) != len(codigos):
                raise ValueError("'edited_allocation' repite un instrumento.")

            total = sum((linea.porcentaje for linea in self.edited_allocation), Decimal(0))
            if total != Decimal(100):
                raise ValueError(
                    f"Los porcentajes deben sumar exactamente 100; suman {total}."
                )

        elif self.edited_allocation:
            raise ValueError(
                "'edited_allocation' solo aplica cuando decision = 'edited'."
            )

        return self


class RevisionResultado(BaseModel):
    """La evidencia de la HU3: qué se decidió, cuándo, bajo qué reglas y quién."""

    review_id: str
    proposal_id: str
    decision: Decision
    estado: EstadoPropuesta

    advisor_id: str
    advisor_nombre: str
    rules_version: str
    decided_at: datetime

    comments: str | None = None
    allocations: list[LineaPropuesta] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Auditoría — GET /api/audit
# ---------------------------------------------------------------------------


class EventoAuditoria(BaseModel):
    """Una fila de `v_audit_timeline`. Alimenta la AuditoriaPage."""

    id: str
    created_at: datetime
    entity_type: str
    entity_id: str
    action: str
    platform: str
    metadata: dict[str, Any] | None = None
    actor_nombre: str | None = None
    actor_rol: str | None = None
