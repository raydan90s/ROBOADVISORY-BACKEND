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

    # Señal explícita del botón "Recomendación de Mercados (IA)" del simulador: fuerza
    # la Ruta C (100% Alpha Vantage, cero contexto del banco) para estos símbolos, sin
    # depender de que el texto del mensaje contenga las palabras que el router
    # reconoce. None → el router clasifica el mensaje como siempre (rutas A/B/C/rechazo).
    symbols: list[str] | None = Field(None, max_length=5)


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

    # La ruta que tomó el router: "bancario" (solo datos del banco) | "mixto" (banco +
    # Alpha Vantage) | "externo" (100% Alpha Vantage) | "rechazo" (fuera de alcance).
    # El front la usa para diferenciar visualmente la burbuja (borde/ícono de aviso en
    # "mixto"/"externo": son instrumentos simulados, fuera del catálogo del banco).
    ruta: str = "bancario"


class SimuladorRequest(BaseModel):
    """Body del POST /api/agent/simulador: la simulación que el usuario está viendo."""

    monto: float = Field(..., gt=0)

    # Qué significa `plazo_dias` lo decide `todos_los_plazos` (abajo): en el simulador es
    # el horizonte con el que se estiman los productos sin plazo fijo (los fondos), y los
    # depósitos rinden siempre a SU propio plazo. En el comparador es un filtro.
    plazo_dias: int | None = Field(None, gt=0)

    # El producto que el usuario eligió al cambiar de banco o de fondo. None = está
    # mirando la que el motor recomienda.
    seleccion_code: str | None = None

    # La IA solo tiene derecho a hablar de las filas que el usuario TIENE EN PANTALLA.
    # El simulador las muestra todas (por eso el default), pero el comparador filtra por
    # plazo y manda `false`: si no, el texto recomendaría un depósito a 720 días mientras
    # la lista visible solo tiene los de 360. Citar la base sin mentir no basta: hay que
    # citar lo que el usuario está viendo.
    todos_los_plazos: bool = True

    provider: str | None = None


class SimuladorResponse(BaseModel):
    """La recomendación del simulador. El motor eligió; la IA solo lo explica."""

    # El `instruments.code` que eligió el MOTOR (no el modelo). El front lo destaca.
    recomendado_code: str | None

    texto: str
    sources: list[SourceChip] = Field(default_factory=list)
    guardrail_passed: bool
    modelo: str
