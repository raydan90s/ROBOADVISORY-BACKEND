"""Gateway hacia la banca: SIMULADO.

Qué es y qué no es
------------------
Cursar una orden real contra un banco es mover el dinero de una persona: exige convenio
firmado, credenciales del cliente en la banca del emisor y un canal certificado. Nada de
eso existe en este proyecto y este módulo no finge que exista. Lo que hace es responder
como respondería ese canal —con una referencia por instrucción— para que **todo lo que sí
es real** (quién puede cursar una orden, contra qué instituciones, cuánto se cobra y quién
respondió por ella) se pueda ejecutar, probar y demostrar de punta a punta.

La honestidad no es un comentario: `investment_orders.is_simulated` nace en `true`, viaja
al cliente en `Orden.is_simulated` y la app lo pinta en pantalla. Una orden simulada nunca
se puede confundir con una real ni mirando la base ni mirando el celular.

Por qué determinista
--------------------
Misma disciplina que el respaldo de `market_data.py`: la referencia se siembra con el
identificador de la línea, no con el reloj ni con `uuid4()`. La misma orden produce
siempre la misma referencia, así que la demo no cambia de números entre un ensayo y la
presentación, y un test puede afirmar contra un valor fijo.

Por qué no falla
----------------
`order_status` tiene 'failed' porque un canal real rechaza órdenes (fondos, cupos,
ventanas horarias) y la base tiene que saber representarlo. El simulador no lo usa: en una
demo en vivo, un fallo aleatorio no demuestra robustez, arruina la corrida. El día que
haya integración real, esta es la única función que cambia — la regla de negocio no.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class AcuseBancario:
    """Lo que el banco devuelve por UNA instrucción."""

    item_id: str
    bank_reference: str


# Un alfabeto sin vocales no produce palabras por accidente: una referencia jamás va a
# leerse como algo que signifique nada. Sin 0/O ni 1/I: en el comprobante de la demo
# alguien va a intentar leerlas en voz alta.
_ALFABETO = "23456789BCDFGHJKLMNPQRSTVWXYZ"


def _bloque(semilla: str, largo: int) -> str:
    """Un bloque estable de la referencia, derivado de la semilla."""
    digest = hashlib.sha256(semilla.encode()).digest()
    return "".join(_ALFABETO[b % len(_ALFABETO)] for b in digest[:largo])


def referencia(item_id: str, institucion: str | None = None) -> str:
    """La referencia que 'devuelve el banco' para una línea. Estable por línea.

    Formato `BRK-XXXX-YYYY`: el prefijo dice quién originó la instrucción (nosotros), y
    los dos bloques salen del id de la línea. Que sea reconocible a simple vista es
    deliberado — nadie debería confundir esto con un comprobante bancario real.
    """
    semilla = f"{item_id}|{institucion or ''}"
    return f"BRK-{_bloque(semilla, 4)}-{_bloque(semilla[::-1], 4)}"


def cursar(items: list[tuple[str, str | None]]) -> list[AcuseBancario]:
    """Manda las N instrucciones y devuelve los N acuses.

    `items` son pares `(item_id, nombre_institucion)`. El orden de la respuesta espeja el
    de la entrada.

    No consulta la base ni escribe en ella: recibe datos ya armados y devuelve acuses,
    igual que `ai_agent`/`agent_graph` reciben contexto y devuelven texto. Quien persiste
    es el controller.
    """
    return [
        AcuseBancario(item_id=item_id, bank_reference=referencia(item_id, institucion))
        for item_id, institucion in items
    ]
