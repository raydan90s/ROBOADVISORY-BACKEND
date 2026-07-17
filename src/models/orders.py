"""Esquemas Pydantic de la orden de inversión y del modelo de negocio.

Los enums espejan los tipos ENUM de Postgres definidos en
`migrations/005_convenios_ordenes.sql`: si cambias uno allá, cámbialo aquí.

Igual que con la propuesta, **ningún número de acá nace en Python**: `comision`,
`comision_total` y `monto_invertido` son columnas GENERATED de Postgres, y los USD por
línea salen de `proposal_items`. Estos modelos solo los transportan.

La comisión la paga el INVERSIONISTA (4,5% del total de la subcuenta) y se descuenta de su
inversión: por eso `monto_total` y `monto_invertido` son distintos. Ver migrations/006.
"""

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class EstadoOrden(str, Enum):
    """enum order_status"""

    ENVIADA = "sent"
    CONFIRMADA = "confirmed"
    FALLIDA = "failed"


class TipoInstitucion(str, Enum):
    """institutions.institution_type"""

    BANCO = "banco"
    COOPERATIVA = "cooperativa"
    BROKER_INTERNACIONAL = "broker_internacional"


# ---------------------------------------------------------------------------
# La orden
# ---------------------------------------------------------------------------


class LineaOrden(BaseModel):
    """Una instrucción hacia UN banco.

    Una cartera diversificada en tres instituciones son tres de estas: es la
    diversificación dejando de ser un gráfico y volviéndose tres órdenes con tres
    referencias distintas.
    """

    item_id: str
    instrumento_code: str
    instrumento_nombre: str

    institucion: str | None = None
    calificacion: str | None = None
    tipo_institucion: TipoInstitucion | None = None

    # `monto` es la porción BRUTA de esta línea (total de la orden × %); `monto_invertido`
    # es lo que llega al banco una vez descontada su parte de comisión. Son dos cifras y
    # no una desde que la comisión la paga el cliente: antes coincidían.
    monto: float
    porcentaje: float

    # La parte de la comisión que le toca a esta línea. Columna GENERATED.
    comision: float
    monto_invertido: float

    # La devuelve el banco al confirmar. Nula mientras la línea está `sent`: es
    # justamente lo que distingue "mandada" de "acusada".
    bank_reference: str | None = None
    estado: EstadoOrden
    confirmada_en: datetime | None = None


class Orden(BaseModel):
    """Respuesta de POST /invest y de GET /orders/{id}: el comprobante completo."""

    order_id: str
    proposal_id: str
    investor_id: str
    investor_nombre: str | None = None

    # Quién firmó la propuesta de la que nació esta orden. Es la persona que respondió
    # con su nombre por esto, y por eso mismo es quien cobra la comisión.
    advisor_id: str | None = None
    advisor_nombre: str | None = None

    estado: EstadoOrden

    # Que este campo exista, viaje al cliente y se pinte en pantalla es el punto: la
    # integración con la banca es simulada y la app lo dice, no lo insinúa.
    is_simulated: bool

    # Los tres números del comprobante, y el orden importa: el cliente pone `monto_total`,
    # paga `comision_total` y se invierten `monto_invertido`. Que sean tres campos y no dos
    # es la consecuencia de que la comisión la pague él y salga de su inversión.
    monto_total: float
    comision_bps: int
    comision_total: float
    monto_invertido: float
    # Por qué se cobra eso y por qué es la misma en todos los bancos. Sale de
    # `commission_policies.rationale`, no de un texto del front ni del LLM.
    comision_rationale: str | None = None

    rules_version: str | None = None
    creada_en: datetime
    confirmada_en: datetime | None = None

    lineas: list[LineaOrden] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# El convenio — GET /api/catalog/convenios
# ---------------------------------------------------------------------------


class Convenio(BaseModel):
    """Una institución del catálogo y si puede o no recibir una orden.

    Existe para contestar en pantalla —y con datos, no con un discurso— la pregunta
    «¿por qué me sale este banco y no aquel otro?»: el catálogo informa, el convenio
    habilita, y son dos listas distintas.
    """

    code: str
    nombre: str
    tipo: TipoInstitucion
    calificacion: str
    calificacion_fuente: str | None = None
    calificacion_fecha: date | None = None

    convenio_activo: bool
    convenio_desde: date | None = None

    # Cuántos productos suyos están en el catálogo. Con 0, la institución existe pero no
    # aparece en ninguna propuesta.
    productos: int = 0


class PoliticaComision(BaseModel):
    """La tasa única que paga el inversionista, con su porqué.

    `misma_para_todas` no es un dato de la fila: es una propiedad del esquema
    (`commission_policies` no tiene columna de institución y tiene UNIQUE por versión de
    reglas). Viaja servida para que la pantalla pueda afirmarlo sin que el front lo
    escriba a mano.
    """

    comision_bps: int
    comision_porcentaje: float
    rationale: str
    rules_version: str
    misma_para_todas: bool = True


class CatalogoConvenios(BaseModel):
    """Respuesta del GET /api/catalog/convenios: con quién trabajamos y cuánto cobramos."""

    politica: PoliticaComision | None = None
    convenios: list[Convenio] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# El asesor — GET /api/advisor/orders
# ---------------------------------------------------------------------------


class OrdenFeedItem(BaseModel):
    """Una fila de `v_advisor_order_feed`: el aviso de que alguien acaba de invertir.

    Es la pantalla que le faltaba al flujo: hasta ahora el asesor firmaba y no se
    enteraba de qué pasaba después. Acá ve el hecho —quién, cuánto, en cuántos bancos— y
    con eso llama.
    """

    order_id: str
    proposal_id: str
    investor_id: str
    investor_nombre: str
    investor_email: str | None = None
    cedula_ruc: str | None = None
    subaccount_name: str | None = None
    perfil_riesgo: str | None = None

    estado: EstadoOrden
    is_simulated: bool

    monto_total: float
    comision_total: float
    monto_invertido: float

    lineas: int
    instituciones: int
    instituciones_nombres: str | None = None

    creada_en: datetime
    confirmada_en: datetime | None = None


class ResumenComisiones(BaseModel):
    """Lo que un asesor ha intermediado y lo que eso factura.

    `comision_ganada` solo suma órdenes confirmadas: una orden que el banco no acusó no
    facturó nada, y contarla sería exactamente el tipo de cifra optimista que este
    proyecto no se permite.
    """

    advisor_id: str | None = None
    advisor_nombre: str | None = None
    ordenes: int
    ordenes_confirmadas: int
    monto_intermediado: float
    comision_ganada: float
