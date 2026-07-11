"""El agente: caída elegante y explicación fiel a los datos.

Lo que se prueba acá no es que Gemini escriba bonito (eso no es testeable), sino las tres
garantías que sí lo son:

1. Si Gemini alucina dos veces, el usuario recibe la explicación determinista — no el texto
   inventado. **La demo no se rompe por culpa del LLM.**
2. Si Gemini se cae, pasa lo mismo.
3. Si Gemini responde bien, ese texto se usa y queda marcado como `guardrail_passed`.

El modelo se mockea: llamar a la API real en un test lo haría lento, caro y no determinista.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from src.models.investor import (
    AssetAllocation,
    Investor,
    NivelRiesgo,
    PerfilRiesgo,
    RespuestaDetalle,
)
from src.services import ai_agent
from src.services.ai_agent import (
    DatosExplicacion,
    Explicacion,
    PLANTILLA,
    contexto_permitido,
    explicacion_determinista,
    redactar_explicacion,
)
from src.services.guardrails import validar

DATOS = DatosExplicacion(
    investor=Investor(
        investor_id="i1",
        session_id="s1",
        nombre="Juan Pérez",
        puntaje=12,
        perfil_riesgo=PerfilRiesgo.MODERADO,
        monto=20000.0,
        respuestas=[
            RespuestaDetalle(
                pregunta_code="objetivo",
                pregunta_text="¿Cuál es tu objetivo?",
                opcion_code="crecer",
                opcion_label="Hacer crecer mi capital",
                puntos=3,
            )
        ],
    ),
    allocations=[
        AssetAllocation(
            instrumento_code="DPF_PICHINCHA_360",
            nombre="Depósito a Plazo Fijo 360 días",
            clase_activo="renta_fija",
            riesgo=NivelRiesgo.BAJO,
            porcentaje=60,
            monto_asignado=12000,
            retorno_esperado=7.2,
            plazo_dias=360,
            institucion="Banco Pichincha",
            calificacion="AAA",
        ),
        AssetAllocation(
            instrumento_code="FONDO_BALANCEADO",
            nombre="Fondo Balanceado",
            clase_activo="mixto",
            riesgo=NivelRiesgo.MEDIO,
            porcentaje=40,
            monto_asignado=8000,
            retorno_esperado=8.3,
            institucion="Banco Guayaquil",
            calificacion="AAA",
        ),
    ],
    riesgo=NivelRiesgo.MEDIO,
    monto_total=Decimal(20000),
    retorno_anual=7.64,
    rules_version="v1",
    umbral_min=9,
    umbral_max=12,
    puntaje_max=15,
)

TEXTO_ALUCINADO = (
    "Hola Juan: te proponemos 65% en el Fondo Tecnológico Global de Banco Fantasma (AA+), "
    "con una rentabilidad garantizada de USD 99.000."
)
TEXTO_FIEL = (
    "Hola Juan: con 12 de 15 puntos tu perfil es moderado. Sobre USD 20.000 te proponemos "
    "60% (USD 12.000) en Depósito a Plazo Fijo 360 días de Banco Pichincha (AAA) y 40% "
    "(USD 8.000) en Fondo Balanceado de Banco Guayaquil (AAA)."
)


def test_la_explicacion_determinista_pasa_su_propio_guardarrail() -> None:
    """El fallback tiene que ser válido: es lo que se muestra cuando todo lo demás falla."""
    texto = explicacion_determinista(DATOS)
    veredicto = validar(texto, contexto_permitido(DATOS))
    assert veredicto.ok, veredicto.motivos
    assert "USD 12.000" in texto and "USD 8.000" in texto


def test_el_contexto_permitido_sale_de_los_datos() -> None:
    ctx = contexto_permitido(DATOS)
    assert {Decimal(60), Decimal(40), Decimal(12000), Decimal(8000), Decimal(20000)} <= ctx.numeros
    assert ctx.instituciones == {"Banco Pichincha", "Banco Guayaquil"}
    assert Decimal(99000) not in ctx.numeros  # el número alucinado, justamente


@pytest.mark.asyncio
async def test_si_gemini_alucina_dos_veces_se_usa_la_plantilla() -> None:
    """⭐ La garantía central: nunca se le muestra al usuario un número que la IA inventó."""
    with patch.object(ai_agent.settings, "GEMINI_API_KEY", "fake-key"), patch.object(
        ai_agent, "_generar_con_gemini", return_value=TEXTO_ALUCINADO
    ) as llm:
        expl: Explicacion = await redactar_explicacion(DATOS)

    assert llm.call_count == 2, "Debió reintentar exactamente una vez."
    assert expl.modelo == PLANTILLA
    assert expl.retry_count == 1
    assert "Banco Fantasma" not in expl.texto and "99.000" not in expl.texto
    assert expl.motivos, "El rechazo debe quedar registrado para auditoría."
    assert validar(expl.texto, contexto_permitido(DATOS)).ok


@pytest.mark.asyncio
async def test_si_gemini_se_cae_la_app_sigue_funcionando() -> None:
    """La demo no se rompe porque la API de Gemini esté caída o sin cuota."""
    with patch.object(ai_agent.settings, "GEMINI_API_KEY", "fake-key"), patch.object(
        ai_agent, "_generar_con_gemini", side_effect=RuntimeError("429 quota exceeded")
    ):
        expl = await redactar_explicacion(DATOS)

    assert expl.modelo == PLANTILLA
    assert expl.guardrail_passed
    assert "USD 12.000" in expl.texto


@pytest.mark.asyncio
async def test_si_gemini_responde_bien_se_usa_su_texto() -> None:
    """El contrapeso: si la plantilla ganara siempre, los tests de arriba no probarían nada."""
    with patch.object(ai_agent.settings, "GEMINI_API_KEY", "fake-key"), patch.object(
        ai_agent, "_generar_con_gemini", return_value=TEXTO_FIEL
    ) as llm:
        expl = await redactar_explicacion(DATOS)

    assert llm.call_count == 1
    assert expl.modelo != PLANTILLA
    assert expl.guardrail_passed and expl.retry_count == 0
    assert "Fondo Balanceado" in expl.texto
    # El disclaimer se anexa aunque el modelo se olvide de escribirlo.
    assert "no constituye una orden" in expl.texto


@pytest.mark.asyncio
async def test_el_reintento_le_dice_al_modelo_que_hizo_mal() -> None:
    """Un reintento a ciegas repetiría el error: el segundo prompt lleva los motivos del rechazo."""
    respuestas = iter([TEXTO_ALUCINADO, TEXTO_FIEL])

    async def _fake(prompt: str, correccion: str = "") -> str:
        texto = next(respuestas)
        if texto is TEXTO_FIEL:  # el segundo intento
            assert "RECHAZADA" in correccion
            assert any("Banco Fantasma" in c for c in [correccion])
        return texto

    with patch.object(ai_agent.settings, "GEMINI_API_KEY", "fake-key"), patch.object(
        ai_agent, "_generar_con_gemini", side_effect=_fake
    ):
        expl = await redactar_explicacion(DATOS)

    assert expl.guardrail_passed and expl.retry_count == 1
    assert expl.modelo != PLANTILLA  # el segundo intento sí sirvió


@pytest.mark.asyncio
async def test_sin_api_key_no_se_llama_a_gemini() -> None:
    with patch.object(ai_agent.settings, "GEMINI_API_KEY", ""), patch.object(
        ai_agent, "_generar_con_gemini"
    ) as llm:
        expl = await redactar_explicacion(DATOS)

    llm.assert_not_called()
    assert expl.modelo == PLANTILLA
