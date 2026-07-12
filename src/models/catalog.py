"""Modelos del catálogo de tasas (comparador y simulador del inversionista)."""

from datetime import date

# pyrefly: ignore [missing-import]
from pydantic import BaseModel


class TasaInstrumento(BaseModel):
    """Una fila del comparador: producto + institución + calificación citada + tasa.

    La calificación viaja siempre con su fuente y su fecha: el front las pinta con
    el componente `Calificacion`, que no muestra un rating sin su cita (criterio #3).
    """

    code: str
    producto: str
    product_type: str | None
    institucion: str
    calificacion: str
    rating_tier: int
    fuente_calificacion: str | None
    fecha_calificacion: date | None
    tasa_anual: float
    plazo_dias: int | None
    monto_minimo: float | None

    # None si quien consulta aún no tiene perfil (o es asesor): no hay regla que aplicar.
    elegible: bool | None
    # El `rationale` versionado de profile_institution_rules: la regla, no una excusa.
    motivo_no_elegible: str | None

    # Solo cuando el request trae ?monto=. Los calcula Postgres, no el front.
    cumple_monto_minimo: bool | None
    interes_estimado: float | None
    monto_final: float | None

    # La opción que el motor recomienda para este monto (ver `elegir_recomendado`). El
    # front la destaca y la IA la explica: los tres miran la MISMA fila, y por eso no
    # puede pasar que la tarjeta destaque una opción y el asistente recomiende otra.
    recomendado: bool = False


class CatalogoTasas(BaseModel):
    perfil: str | None
    monto: float | None
    plazo_dias: int | None
    tasas: list[TasaInstrumento]
