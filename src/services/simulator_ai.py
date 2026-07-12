"""Recomendación del simulador: **el motor elige, la IA explica**.

Misma regla que el resto del proyecto, aplicada a una pantalla nueva: cada cifra (tasa,
interés, monto final, mínimo) sale de Postgres — son literalmente las filas que
`/api/catalog/rates?monto=` le devolvió al front y que el usuario está viendo. El LLM no
elige la opción ni multiplica nada: recibe las opciones ya calculadas, la que el motor
marcó como recomendada y la que el usuario tocó, y solo lo pone en palabras.

Quién recomienda es `elegir_recomendado`, no el modelo. Y lo que el modelo escribe pasa
por el MISMO `guardrails.validar` que valida las propuestas:

    generar → validar → reintentar una vez → caer a la recomendación determinista

Un número que no esté en las filas hace que el texto se descarte entero. Nunca se
"corrige" un texto alucinado: se tira y se muestra el determinista.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.models.catalog import TasaInstrumento
from src.services.guardrails import ContextoPermitido, validar
from src.services.llm_provider import crear_llm, hay_api_key, modelo_activo

log = logging.getLogger(__name__)

PLANTILLA = "plantilla-determinista"

# El simulador explora, no ordena. El disclaimer no lo escribe el modelo (se le olvidaría):
# se anexa siempre, venga el texto de donde venga.
#
# Ojo con la redacción: `validar_lexico` rechaza «garantiz*» y «asegur*` sin entender la
# negación, así que un disclaimer que dijera "no garantiza rentabilidad" se rechazaría a sí
# mismo. Se dice lo mismo con otras palabras, como en el resto del proyecto.
DISCLAIMER = (
    "Es una simulación referencial: los retornos no son una promesa y un asesor "
    "autorizado revisa la propuesta antes de ejecutarla."
)


def _usd(monto: float | Decimal | None) -> str:
    """USD 12.000 · USD 719,18 — el MISMO formato que pinta el front (`utils/formato.ts`).

    Que coincidan no es cosmético: el prompt le pide al modelo copiar los montos tal cual,
    y el guardarraíl compara con tolerancia de un centavo. Si acá escribiéramos "USD 719"
    donde la tarjeta dice "USD 719,18", estaríamos pidiéndole al modelo que redondee — y
    luego rechazándolo por hacerlo.
    """
    if monto is None:
        return "—"
    entero, decimales = f"{abs(Decimal(str(monto))):,.2f}".split(".")
    agrupado = entero.replace(",", ".")
    cola = "" if decimales == "00" else f",{decimales}"
    signo = "-" if Decimal(str(monto)) < 0 else ""
    return f"{signo}USD {agrupado}{cola}"


def _pct(valor: float | None) -> str:
    """8.5 → «8,5%» (y 8.0 → «8%»): igual que `porcentaje()` del front."""
    if valor is None:
        return "—"
    limpio = f"{valor:.2f}".rstrip("0").rstrip(".")
    return f"{limpio.replace('.', ',')}%"


def _norm(s: str) -> str:
    """Sin tildes y en minúsculas, para buscar menciones sin depender de la ortografía."""
    plano = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(plano.lower().split())


# ===========================================================================
# La simulación: las filas de Postgres + quién recomienda qué
# ===========================================================================


@dataclass(frozen=True)
class Simulacion:
    """Todo lo que la IA tiene permitido ver. Sale entero de `/api/catalog/rates`."""

    monto: float
    plazo_dias: int | None
    perfil: str | None
    tasas: list[TasaInstrumento]
    # La que marcó `catalog_controller.elegir_recomendado`: es la MISMA fila que el front
    # destaca en la tarjeta, no una segunda opinión de este módulo.
    recomendado: TasaInstrumento | None
    # La opción que el usuario tocó en la lista, si cambió de banco o de fondo.
    seleccionado: TasaInstrumento | None


@dataclass
class Recomendacion:
    """El texto y su expediente: quién lo escribió, si pasó el guardarraíl, qué citó."""

    texto: str
    modelo: str
    guardrail_passed: bool
    retry_count: int = 0
    motivos: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)


# ===========================================================================
# Lo que el texto tiene derecho a decir
# ===========================================================================


def contexto_permitido(sim: Simulacion) -> ContextoPermitido:
    """El conjunto cerrado de números, productos, bancos y calificaciones citables."""
    numeros: set[Decimal] = {Decimal(100), Decimal(str(sim.monto))}
    if sim.plazo_dias is not None:
        numeros.add(Decimal(sim.plazo_dias))

    instrumentos: set[str] = set()
    instituciones: set[str] = set()
    calificaciones: set[str] = set()

    for t in sim.tasas:
        instrumentos.add(t.producto)
        instituciones.add(t.institucion)
        calificaciones.add(t.calificacion)
        numeros.add(Decimal(t.rating_tier))

        for valor in (
            t.tasa_anual,
            t.plazo_dias,
            t.monto_minimo,
            t.interes_estimado,
            t.monto_final,
        ):
            if valor is None:
                continue
            exacto = Decimal(str(valor))
            numeros.add(exacto)
            # El modelo puede escribir "USD 719" donde la fila dice 719,18. Redondear un
            # monto no es alucinar: la cifra sigue siendo la de la base. Inventarse otra sí.
            numeros.add(Decimal(round(exacto)))

        # El `rationale` de la regla de elegibilidad se le muestra al modelo para que pueda
        # explicar POR QUÉ un banco no es elegible; si el texto de la regla trae dígitos,
        # citarlos es citar la base, no inventar.
        if t.motivo_no_elegible:
            numeros.update(
                Decimal(d)
                for d in "".join(
                    c if c.isdigit() else " " for c in t.motivo_no_elegible
                ).split()
            )

    return ContextoPermitido(
        numeros=numeros,
        instrumentos=instrumentos,
        instituciones=instituciones,
        calificaciones=calificaciones,
    )


def fuentes_citadas(sim: Simulacion, texto: str) -> list[dict[str, Any]]:
    """Source chips: SOLO las opciones que este texto nombró de verdad.

    Se exige el producto Y su banco porque dos bancos venden depósitos con el mismo
    nombre; sin el emisor, el chip apuntaría a la fila equivocada.
    """
    t = _norm(texto)
    chips: list[dict[str, Any]] = []

    for tasa in sim.tasas:
        if _norm(tasa.producto) not in t or _norm(tasa.institucion) not in t:
            continue
        etiqueta = f"{tasa.producto} · {tasa.institucion} · {_pct(tasa.tasa_anual)}"
        if tasa.monto_final is not None:
            etiqueta += f" · {_usd(tasa.monto_final)}"
        chips.append(
            {"table": "instruments", "record_id": tasa.code, "label": etiqueta}
        )

    return chips


# ===========================================================================
# La recomendación determinista: fallback y, a la vez, piso de calidad
# ===========================================================================


def recomendacion_determinista(sim: Simulacion) -> str:
    """La misma recomendación, escrita sin LLM. Pasa el guardarraíl por construcción."""
    if sim.recomendado is None:
        return (
            f"Con {_usd(sim.monto)} no hay ninguna opción del catálogo que tu perfil "
            f"admita y cuyo monto mínimo alcances. Prueba con un monto mayor o mira el "
            f"comparador para ver la regla que te bloquea cada emisor. {DISCLAIMER}"
        )

    r = sim.recomendado
    horizonte = f" a {r.plazo_dias} días" if r.plazo_dias is not None else ""
    texto = (
        f"Con {_usd(sim.monto)}, la mejor opción que tu perfil admite es "
        f"{r.producto} de {r.institucion} ({r.calificacion}): tasa referencial "
        f"{_pct(r.tasa_anual)}{horizonte}, un interés estimado de "
        f"{_usd(r.interes_estimado)} y un monto final de {_usd(r.monto_final)}."
    )

    s = sim.seleccionado
    if s is not None and s.code != r.code:
        if s.elegible is False:
            texto += (
                f" {s.producto} de {s.institucion} ({s.calificacion}) es lo que tienes "
                f"seleccionado, pero tu perfil no lo admite: {s.motivo_no_elegible}"
            )
        elif s.cumple_monto_minimo is False:
            texto += (
                f" {s.producto} de {s.institucion} pide un mínimo de "
                f"{_usd(s.monto_minimo)}, que este monto no alcanza."
            )
        else:
            texto += (
                f" Lo que tienes seleccionado, {s.producto} de {s.institucion} "
                f"({s.calificacion}), paga {_pct(s.tasa_anual)} y termina en "
                f"{_usd(s.monto_final)}: la diferencia frente a la recomendada es el "
                f"trade-off entre tasa y calificación del emisor."
            )

    return f"{texto} {DISCLAIMER}"


# ===========================================================================
# El LLM
# ===========================================================================

_SISTEMA = """Eres el asistente de un simulador de inversiones de un banco ecuatoriano.
El MOTOR DE REGLAS del banco YA calculó cada cifra y YA eligió la opción recomendada. Tu
único trabajo es EXPLICAR esa recomendación en español claro.

REGLAS ABSOLUTAS (si rompes una, tu respuesta se descarta):
1. FUENTE DE VERDAD = los DATOS. NO inventes ni recalcules ningún número: copia las
   cifras TAL CUAL aparecen, decimales incluidos. No sumes, no estimes, no redondees.
2. NO elijas una opción distinta a la que los DATOS marcan como RECOMENDADA. Si el
   usuario tiene otra SELECCIONADA, compárala con la recomendada usando los DATOS (tasa,
   calificación del emisor, plazo, mínimo) y di con honestidad qué gana y qué cede. No lo
   empujes ni lo regañes: la decisión es suya.
3. NO menciones ningún producto, banco ni calificación que no esté en los DATOS. Copia el
   nombre de cada producto ENTERO y EXACTO («Depósito a Plazo Fijo 360 días», nunca «el
   DPF» ni «Depósito» a secas), y nómbralo siempre con su banco.
4. NO uses «fondo» ni «depósito» como palabra genérica («ese fondo», «el depósito rinde»).
   El validador lee TODA frase que empiece por «Fondo» o «Depósito» como el nombre de un
   producto y, si no coincide con uno del catálogo, descarta tu respuesta entera. Cuando
   no estés nombrando uno puntual, escribe «ese producto» o «esa opción».
5. NUNCA prometas rentabilidad ni niegues el riesgo, tampoco para negarlo: las palabras
   "garantizado(s)", "asegurado", "seguro", "sin riesgo" y "vas a ganar" están prohibidas
   en cualquier contexto, incluso dentro de una frase que diga que NO hay garantía. Los
   retornos son referenciales; dilo con otras palabras.
6. Toda CIFRA de los DATOS (montos, tasas, plazos, días, porcentajes) va SIEMPRE en
   dígitos, copiada tal cual: «360 días», nunca «trescientos sesenta días». Una cantidad
   escrita en palabras se descarta. La ÚNICA excepción son los conteos chicos de la
   conversación («las dos opciones», «ambos productos»), que van en letras porque no son
   datos.
7. Montos en formato ecuatoriano: USD 12.000 (el punto separa miles).
8. FORMATO OBLIGATORIO — se lee en un teléfono, MÁXIMO 85 PALABRAS en total:
   Primera frase: «Te conviene [producto] ([banco]):» y LA razón más fuerte CON sus
   cifras (su tasa y su interés estimado o monto final).
   Luego, SOLO si el usuario tiene seleccionada una opción distinta a la recomendada,
   la línea «Si eliges [producto seleccionado] ([banco]):» seguida de exactamente dos
   viñetas de UNA línea que hablan de ESA opción seleccionada (nunca de la recomendada):
   • Ganas: …
   • Cedes: …
   Sin esa línea de contexto las viñetas se leen como si fueran de la recomendada y
   confunden. NADA más: sin introducción, sin párrafo de cierre, sin resumen final.
9. CONCRETO, no vago: cada afirmación lleva su cifra de los DATOS pegada («USD 1.597,81
   frente a USD 1.134,25», no «un interés mayor»). Cada cifra aparece UNA sola vez, en
   la línea que la necesita. Vago se rechaza igual que inventado.
10. Si comparas un depósito con un fondo, la diferencia de naturaleza de la tasa (la del
    depósito queda pactada al contratar; la del fondo es referencial y puede variar) va
    DENTRO de las viñetas, en pocas palabras. Es el trade-off más importante."""


def _linea(t: TasaInstrumento) -> str:
    # La naturaleza de la tasa es parte del dato: pactada (depósito) vs referencial
    # (fondo). Dársela al modelo es lo que le permite explicar ese trade-off sin inventar.
    if t.product_type == "deposito_plazo":
        partes = [f"tasa {_pct(t.tasa_anual)} pactada al contratar"]
    else:
        partes = [f"tasa {_pct(t.tasa_anual)} referencial, puede variar"]
    partes.append(
        f"plazo {t.plazo_dias} días" if t.plazo_dias is not None else "sin plazo fijo"
    )
    if t.monto_minimo is not None:
        partes.append(f"mínimo {_usd(t.monto_minimo)}")
    if t.interes_estimado is not None:
        partes.append(f"interés estimado {_usd(t.interes_estimado)}")
    if t.monto_final is not None:
        partes.append(f"monto final {_usd(t.monto_final)}")

    if t.elegible is False:
        estado = f" — NO ELEGIBLE para su perfil: {t.motivo_no_elegible}"
    elif t.cumple_monto_minimo is False:
        estado = " — ELEGIBLE, pero el monto simulado NO alcanza su mínimo"
    else:
        estado = " — ELEGIBLE y disponible con este monto"

    return (
        f"- {t.producto} ({t.institucion}, calificación {t.calificacion}): "
        f"{', '.join(partes)}{estado}"
    )


def _prompt(sim: Simulacion) -> str:
    opciones = "\n".join(_linea(t) for t in sim.tasas)
    horizonte = f"{sim.plazo_dias} días" if sim.plazo_dias is not None else "no declarado"
    perfil = sim.perfil or "sin perfilar todavía"

    recomendada = (
        f"{sim.recomendado.producto} ({sim.recomendado.institucion})"
        if sim.recomendado
        else "ninguna: con este monto, nada del catálogo es elegible y alcanza el mínimo"
    )
    seleccionada = (
        f"{sim.seleccionado.producto} ({sim.seleccionado.institucion})"
        if sim.seleccionado
        else "ninguna: está viendo la recomendada"
    )

    pregunta = (
        "Explica por qué el motor recomienda esa opción con este monto y este horizonte, "
        "y qué gana y qué cede el usuario si se queda con la que tiene seleccionada."
        if sim.seleccionado is not None
        and sim.recomendado is not None
        and sim.seleccionado.code != sim.recomendado.code
        else "Explica por qué el motor recomienda esa opción con este monto y este "
        "horizonte, y qué está eligiendo el usuario al tomarla (tasa frente a "
        "calificación del emisor)."
    )

    return f"""DATOS DE LA SIMULACIÓN (son los ÚNICOS números y nombres que puedes usar):
Monto simulado: {_usd(sim.monto)}
Horizonte elegido: {horizonte}
Perfil del inversionista: {perfil}

Opciones del catálogo, con lo que el motor calculó para ESTE monto:
{opciones}

OPCIÓN RECOMENDADA POR EL MOTOR: {recomendada}
OPCIÓN SELECCIONADA POR EL USUARIO: {seleccionada}

{pregunta}"""


async def recomendar(sim: Simulacion, provider: str | None = None) -> Recomendacion:
    """Genera → valida → reintenta una vez → cae a la determinista. Nunca inventa un número."""
    ctx = contexto_permitido(sim)
    prompt = _prompt(sim)

    def _determinista(motivos: list[str], intentos: int) -> Recomendacion:
        texto = recomendacion_determinista(sim)
        return Recomendacion(
            texto=texto,
            modelo=PLANTILLA,
            guardrail_passed=True,
            retry_count=intentos,
            motivos=motivos,
            sources=fuentes_citadas(sim, texto),
        )

    if not hay_api_key(provider):
        log.warning("Sin API key del proveedor de IA: el simulador usa la recomendación determinista.")
        return _determinista([], 0)

    correccion = ""
    ultimos_motivos: list[str] = []

    for intento in range(2):  # el original y UN reintento
        mensajes = [("system", _SISTEMA), ("human", prompt)]
        if correccion:
            mensajes.append(("human", correccion))

        try:
            # `crear_llm` va DENTRO del try: también revienta si el proveedor del .env no
            # tiene su paquete instalado, y eso no puede tumbar el simulador con un 500.
            # Que la IA no esté disponible degrada la pantalla, no la rompe.
            llm = crear_llm(provider=provider)
            respuesta = await llm.ainvoke(mensajes)
            texto = str(respuesta.content).strip()
        except Exception as exc:  # API caída, cuota agotada, timeout, paquete ausente…
            log.warning("El proveedor de IA falló en el simulador (intento %s): %s", intento + 1, exc)
            return _determinista([f"El proveedor de IA no respondió: {exc}"], intento)

        palabras = len(texto.split())

        # El disclaimer lo ponemos nosotros: no depende de que el modelo se acuerde.
        if "asesor autorizado" not in texto.lower():
            texto = f"{texto}\n\n{DISCLAIMER}"

        veredicto = validar(texto, ctx)
        # El largo solo fuerza el reintento: un texto válido pero verboso en el segundo
        # intento se acepta igual — mejor largo y fiel que caer a la plantilla por estilo.
        muy_largo = palabras > 100 and intento == 0
        if veredicto.ok and not muy_largo:
            return Recomendacion(
                texto=texto,
                modelo=modelo_activo(provider),
                guardrail_passed=True,
                retry_count=intento,
                sources=fuentes_citadas(sim, texto),
            )

        motivos = list(veredicto.motivos)
        if muy_largo:
            motivos.append(
                f"Respuesta demasiado larga ({palabras} palabras): el formato pide "
                "máximo 85, para leerse en un teléfono."
            )
        ultimos_motivos = motivos
        log.warning(
            "Recomendación del simulador rechazada (intento %s): %s",
            intento + 1,
            motivos,
        )
        correccion = (
            "Tu respuesta anterior fue RECHAZADA por el validador del banco:\n"
            + "\n".join(f"- {m}" for m in motivos)
            + "\nReescríbela respetando el FORMATO OBLIGATORIO (máximo 85 palabras) y "
            "usando EXCLUSIVAMENTE los números, productos y bancos de los DATOS."
        )

    # Dos rechazos: al usuario no se le muestra nada que el modelo haya inventado.
    return _determinista(ultimos_motivos, 1)
