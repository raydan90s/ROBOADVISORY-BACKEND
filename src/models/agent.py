"""Esquemas del agente conversacional (HU: asistente que explica perfil y propuesta).

El contrato es deliberadamente chico: el cliente manda un mensaje y, opcionalmente,
la sesión (subcuenta) sobre la que pregunta. La respuesta trae el texto, las fuentes
citables (source chips) y si pasó el guardarraíl — la evidencia del criterio #3.
"""

from pydantic import BaseModel, Field


class AgentChatRequest(BaseModel):
    """Body del POST /api/agent/chat."""

    # Sin sesión, el agente usa la última sesión completada del usuario del token.
    # Con sesión, responde sobre esa subcuenta concreta (el botón "conversar sobre
    # esta subcuenta" la manda). La autorización se valida contra el dueño.
    session_id: str | None = None

    mensaje: str = Field(..., min_length=1, max_length=1000)

    # Proveedor de IA elegido en el front para este turno ("google" | "openai" |
    # "anthropic"). None → el default del .env. Si no tiene key, cae a la plantilla.
    provider: str | None = None


class ProviderInfo(BaseModel):
    """Un proveedor del catálogo, para pintar el selector del front (sin keys)."""

    id: str
    modelo: str
    disponible: bool
    es_default: bool


class SourceChip(BaseModel):
    """De dónde salió un dato citado. Se pinta como chip tocable en el front."""

    table: str
    record_id: str
    label: str


class AgentChatResponse(BaseModel):
    """Lo que ve el front por cada turno del asistente."""

    texto: str
    sources: list[SourceChip] = Field(default_factory=list)

    # Evidencia anti-alucinación: si el texto que se muestra pasó el validador.
    # Siempre True para lo que llega al cliente (un texto que no pasa se descarta),
    # pero viaja explícito para poder mostrarlo/auditarlo.
    guardrail_passed: bool

    # Qué escribió la respuesta: el modelo de Gemini, la plantilla determinista, o
    # el rechazo por fuera de alcance. Útil para la demo y la auditoría.
    modelo: str
    en_alcance: bool = True


class SimuladorRequest(BaseModel):
    """Body del POST /api/agent/simulador: la simulación que el usuario está viendo."""

    monto: float = Field(..., gt=0)

    # El horizonte de la simulación. Filtra nada: estima los productos sin plazo fijo
    # (los fondos). Los depósitos se estiman siempre con SU propio plazo.
    plazo_dias: int | None = Field(None, gt=0)

    # El producto que el usuario eligió al cambiar de banco o de fondo. None = está
    # mirando la que el motor recomienda.
    seleccion_code: str | None = None

    provider: str | None = None


class SimuladorResponse(BaseModel):
    """La recomendación del simulador. El motor eligió; la IA solo lo explica."""

    # El `instruments.code` que eligió el MOTOR (no el modelo). El front lo destaca.
    recomendado_code: str | None

    texto: str
    sources: list[SourceChip] = Field(default_factory=list)
    guardrail_passed: bool
    modelo: str
