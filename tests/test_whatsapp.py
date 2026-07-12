"""El canal de WhatsApp: la puerta pública y el alcance del agente.

El webhook es la única ruta de la API sin JWT, así que lo que se prueba acá no es que
"el bot conteste bonito" (eso depende del LLM y no es testeable), sino las dos cosas de
las que depende que este canal sea seguro y útil:

1. **Nadie entra sin firma válida ni sin vínculo.** Un POST sin la firma de Twilio se
   rechaza, y un teléfono no vinculado no obtiene ni un dato — recibe la bienvenida.
2. **El alcance nuevo es el que se pidió.** Recomendar del catálogo entra; predecir
   precios y ejecutar órdenes siguen fuera.
"""

from decimal import Decimal

import pytest

from src.controllers.whatsapp_controller import _codigo_del_mensaje
from src.services import whatsapp
from src.services.agent_graph import _fuera_de_alcance
from src.services.guardrails import ContextoPermitido, validar

# El ejemplo canónico de la documentación de Twilio: si nuestro HMAC coincide con esta
# firma, coincide con el que Twilio calcula de verdad. Es el test que no se puede
# "arreglar" cambiando el código — el valor esperado lo fija ellos, no nosotros.
# (docs.twilio.com/usage/security — "Validating Signatures from Twilio")
TOKEN_TWILIO = "12345"
URL_TWILIO = "https://example.com/myapp.php?foo=1&bar=2"
# Deliberadamente DESORDENADOS: el algoritmo exige ordenarlos, y un dict que ya llegara
# ordenado dejaría pasar una implementación que no ordena nada.
PARAMS_TWILIO = {
    "Digits": "1234",
    "To": "+18005551212",
    "From": "+14158675310",
    "Caller": "+14158675310",
    "CallSid": "CA1234567890ABCDE",
}
FIRMA_TWILIO = "L/OH5YylLD5NRKLltdqwSvS0BnU="


# ===========================================================================
# 1. La firma: quién tiene derecho a hablarle al webhook
# ===========================================================================


def test_la_firma_coincide_con_el_ejemplo_oficial_de_twilio() -> None:
    """Si esto falla, el algoritmo está mal y NINGÚN mensaje real entraría."""
    assert whatsapp.firma_esperada(URL_TWILIO, PARAMS_TWILIO, TOKEN_TWILIO) == FIRMA_TWILIO
    assert whatsapp.firma_valida(URL_TWILIO, PARAMS_TWILIO, FIRMA_TWILIO, TOKEN_TWILIO)


def test_un_parametro_alterado_invalida_la_firma() -> None:
    """⭐ El ataque que importa: cambiar el remitente para leer la cartera de otro."""
    suplantado = {**PARAMS_TWILIO, "From": "+593999999999"}
    assert not whatsapp.firma_valida(URL_TWILIO, suplantado, FIRMA_TWILIO, TOKEN_TWILIO)


def test_sin_token_o_sin_firma_no_se_valida_nada() -> None:
    """Un token vacío no puede significar 'pasa todo': significa 'no puedo verificar'."""
    assert not whatsapp.firma_valida(URL_TWILIO, PARAMS_TWILIO, FIRMA_TWILIO, "")
    assert not whatsapp.firma_valida(URL_TWILIO, PARAMS_TWILIO, "", TOKEN_TWILIO)


def test_la_url_es_parte_de_la_firma() -> None:
    """Si TWILIO_WEBHOOK_URL no coincide con la de la consola, nada valida — a propósito."""
    otra = "https://example.com/myapp.php?foo=1&bar=3"
    assert not whatsapp.firma_valida(otra, PARAMS_TWILIO, FIRMA_TWILIO, TOKEN_TWILIO)


# ===========================================================================
# 2. Teléfonos y códigos
# ===========================================================================


def test_normalizar_telefono_quita_el_canal_y_el_formato() -> None:
    assert whatsapp.normalizar_telefono("whatsapp:+593999999999") == "+593999999999"
    assert whatsapp.normalizar_telefono("whatsapp:+593 99-999.9999") == "+593999999999"
    assert whatsapp.normalizar_telefono("+593999999999") == "+593999999999"


def test_un_telefono_que_no_es_e164_no_pasa() -> None:
    """Lo que no se puede normalizar no se busca en la base: se descarta."""
    assert whatsapp.normalizar_telefono("whatsapp:0999999999") is None  # sin código de país
    assert whatsapp.normalizar_telefono("") is None
    assert whatsapp.normalizar_telefono("whatsapp:+0abc") is None


def test_el_telefono_se_enmascara_en_los_logs() -> None:
    assert whatsapp.enmascarar("+593999999999") == "+593•••9999"


@pytest.mark.parametrize(
    "mensaje",
    ["VINCULAR 123456", "vincular 123456", "  Vincular  123-456 ", "link 123456"],
)
def test_el_codigo_se_extrae_de_las_formas_en_que_la_gente_escribe(mensaje: str) -> None:
    assert _codigo_del_mensaje(mensaje) == "123456"


@pytest.mark.parametrize(
    "mensaje",
    [
        "123456",  # sin el prefijo no es un intento de vinculación, es una pregunta
        "vincular 12345",  # cinco dígitos
        "¿qué inversiones tengo?",
        "vincular",
    ],
)
def test_lo_que_no_es_un_codigo_no_se_confunde_con_uno(mensaje: str) -> None:
    assert _codigo_del_mensaje(mensaje) is None


# ===========================================================================
# 3. El formato de salida
# ===========================================================================


def test_los_guillemets_no_llegan_nunca_al_usuario() -> None:
    """⭐ Se ven como ruido en un globo de chat; salen como comillas rectas."""
    salida = whatsapp.formatear('Te conviene «Depósito a Plazo Fijo» de «Banco Loja».')
    assert "«" not in salida and "»" not in salida
    assert salida == 'Te conviene "Depósito a Plazo Fijo" de "Banco Loja".'


def test_el_markdown_se_traduce_a_lo_que_whatsapp_sabe_pintar() -> None:
    """WhatsApp no renderiza markdown: un '**' se lee literal, con los dos asteriscos."""
    salida = whatsapp.formatear("## Tus opciones\n\n**Fondo** de Banco Loja\n- tasa 8,5%")
    assert salida == "*Tus opciones*\n\n*Fondo* de Banco Loja\n• tasa 8,5%"


def test_el_titulo_no_se_come_el_parrafo_de_abajo() -> None:
    """En MULTILINE un `\\s*` final se traga el salto y fusiona las dos líneas."""
    assert whatsapp.formatear("# Titulo\n\nUn parrafo") == "*Titulo*\n\nUn parrafo"


def test_formatear_es_idempotente() -> None:
    """Se aplica en la única puerta de salida; pasar dos veces no debe deformar nada."""
    una = whatsapp.formatear("**Hola** «mundo»\n- uno\n- dos")
    assert whatsapp.formatear(una) == una


def test_el_formato_se_aplica_tambien_en_el_twiml() -> None:
    """La garantía vive en la salida, no en la confianza de que el LLM obedezca el prompt."""
    xml = whatsapp.twiml("Te conviene el «Fondo» y **nada más**")
    assert "«" not in xml and "**" not in xml


# ===========================================================================
# 4. TwiML: la respuesta que Twilio entrega
# ===========================================================================


def test_el_texto_se_escapa_para_no_romper_el_xml() -> None:
    """⭐ Un '&' sin escapar rompe el XML y Twilio no entrega NADA: silencio, sin error."""
    xml = whatsapp.twiml("Renta fija & variable <no es> un producto")
    assert "&amp;" in xml and "&lt;no es&gt;" in xml
    assert "& variable" not in xml


def test_un_texto_largo_se_parte_en_varios_globos_sin_cortar_una_vinuta() -> None:
    """WhatsApp corta a los 1600; partimos nosotros para elegir dónde."""
    largo = "\n".join(f"• Producto {i} con su tasa y su plazo" for i in range(80))
    xml = whatsapp.twiml(largo)

    globos = xml.count("<Message>")
    assert globos > 1, "Un texto de más de 1500 caracteres tiene que partirse."
    # Ningún globo excede el límite, y ninguno empieza a media viñeta.
    import re

    for cuerpo in re.findall(r"<Message>(.*?)</Message>", xml, re.S):
        assert len(cuerpo) <= whatsapp.LIMITE_MENSAJE
        assert not cuerpo.startswith("Producto"), "Se cortó a mitad de una viñeta."


def test_un_texto_corto_va_en_un_solo_globo() -> None:
    xml = whatsapp.twiml("Tu perfil es moderado.")
    assert xml.count("<Message>") == 1


# ===========================================================================
# 4. El alcance nuevo: recomendar sí, adivinar no
# ===========================================================================


@pytest.mark.parametrize(
    "pregunta",
    [
        "¿qué inversiones tengo?",
        "¿dónde me conviene invertir?",
        "¿cuál producto me da mejor tasa?",
        "recomiéndame algo a 180 días",
        "¿qué es la renta fija?",
        "¿por qué importa la calificación del banco?",
        "compárame mis dos subcuentas",
        "¿puedo invertir en acciones?",  # se contesta: "el banco no las ofrece, pero…"
    ],
)
def test_lo_que_el_usuario_pidio_que_el_bot_responda_entra_en_alcance(pregunta: str) -> None:
    """El pedido explícito: preguntar por la cuenta y por dónde invertir tiene que pasar."""
    assert not _fuera_de_alcance(pregunta)


@pytest.mark.parametrize(
    "pregunta",
    [
        "¿el dólar va a subir el próximo mes?",
        "predice el precio del bitcoin",
        "¿cuánto valdrá mi fondo en un año?",
        "dame un pronóstico del mercado",
        "cómprame 100 dólares de ese fondo",
        "invierte por mí",
        "tradúceme esto al inglés",
        "escribe un código en Python",
    ],
)
def test_predecir_y_ejecutar_siguen_fuera_de_alcance(pregunta: str) -> None:
    """⭐ Abrir el alcance a recomendaciones no abre la puerta a adivinar el futuro."""
    assert _fuera_de_alcance(pregunta)


def test_una_recomendacion_del_catalogo_pasa_el_guardarrail() -> None:
    """El alcance se abrió, pero el anti-alucinación sigue en pie: cifras solo de la base."""
    ctx = ContextoPermitido(
        numeros={Decimal(180), Decimal("8.5"), Decimal(5000)},
        instrumentos={"Depósito a Plazo Fijo 180 días"},
        instituciones={"Banco Pichincha"},
        calificaciones={"AAA"},
    )
    bueno = (
        "Te conviene el Depósito a Plazo Fijo 180 días de Banco Pichincha (AAA): "
        "paga 8,5% referencial a 180 días y el mínimo es USD 5.000."
    )
    assert validar(bueno, ctx).ok

    inventado = (
        "Te conviene el Fondo Tecnológico Global de Banco Fantasma (AA+): rinde 14% anual."
    )
    veredicto = validar(inventado, ctx)
    assert not veredicto.ok
    assert len(veredicto.motivos) >= 3  # producto, emisor, calificación y número
