"""Proveedor de IA intercambiable: un solo lugar donde se decide QUÉ modelo se usa.

El resto del backend (el agente y la redacción de propuestas) nunca instancia un
cliente de LLM directamente: pide uno acá con `crear_llm()`. Así, cambiar de Gemini a
OpenAI o Anthropic es cambiar **una variable del `.env`** (`AI_PROVIDER`), sin tocar
código.

    AI_PROVIDER=google      GEMINI_API_KEY=...     GEMINI_MODEL=gemini-flash-lite-latest
    AI_PROVIDER=openai      OPENAI_API_KEY=...     OPENAI_MODEL=gpt-4o-mini
    AI_PROVIDER=anthropic   ANTHROPIC_API_KEY=...  ANTHROPIC_MODEL=claude-haiku-4-5

Los paquetes de cada proveedor se importan de forma perezosa: la app arranca aunque
solo esté instalado el de Gemini. Si eliges un proveedor cuyo paquete falta, el error
te dice exactamente qué instalar.

Regla del proyecto intacta: el proveedor solo REDACTA. Los números salen de la base y
`guardrails.validar` valida el texto, sea cual sea el modelo detrás.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# pyrefly: ignore [missing-import]
from dotenv import dotenv_values

from src.config.settings import settings


def _valor_vivo(attr: str) -> str | None:
    """Lee una variable reflejando el estado ACTUAL, no el del arranque.

    Prioridad: `.env` en disco (dev: editar y ver el cambio sin reiniciar) → variable
    de entorno real (prod: Render). Devuelve None si no está en ninguno de los dos.
    """
    try:
        archivo = dotenv_values(".env")
    except Exception:
        archivo = {}
    if attr in archivo:
        return (archivo[attr] or "").strip()
    entorno = os.getenv(attr)
    return entorno.strip() if entorno is not None else None


def _api_key_viva(attr: str) -> str:
    """La API key ACTUAL. Sin fallback al valor del arranque a propósito: si borras la
    key del `.env`, tiene que quedar vacía (= no disponible), no la vieja cacheada."""
    valor = _valor_vivo(attr)
    return valor if valor is not None else ""


def _config_valor(attr: str) -> str:
    """Un ajuste no sensible (proveedor activo, modelo): estado vivo, y si no está en
    ningún lado, el default cargado por settings al arrancar."""
    valor = _valor_vivo(attr)
    return valor if valor is not None else (str(getattr(settings, attr, "") or ""))


@dataclass(frozen=True)
class ProveedorConfig:
    """El proveedor activo, ya resuelto a (nombre, key, modelo)."""

    nombre: str
    api_key: str
    modelo: str


# ---------------------------------------------------------------------------
# Fábricas por proveedor (import perezoso: solo se importa el que se use)
# ---------------------------------------------------------------------------


def _crear_google(modelo: str, api_key: str, temperature: float) -> Any:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=modelo, google_api_key=api_key, temperature=temperature
    )


def _crear_openai(modelo: str, api_key: str, temperature: float) -> Any:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # paquete no instalado
        raise RuntimeError(
            "AI_PROVIDER=openai requiere el paquete: pip install langchain-openai"
        ) from exc

    return ChatOpenAI(model=modelo, api_key=api_key, temperature=temperature)


def _crear_anthropic(modelo: str, api_key: str, temperature: float) -> Any:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:
        raise RuntimeError(
            "AI_PROVIDER=anthropic requiere el paquete: pip install langchain-anthropic"
        ) from exc

    return ChatAnthropic(model=modelo, api_key=api_key, temperature=temperature)


# nombre de proveedor → (fábrica, nombre-de-setting-de-key, nombre-de-setting-de-modelo)
_PROVEEDORES: dict[str, tuple[Callable[[str, str, float], Any], str, str]] = {
    "google": (_crear_google, "GEMINI_API_KEY", "GEMINI_MODEL"),
    "openai": (_crear_openai, "OPENAI_API_KEY", "OPENAI_MODEL"),
    "anthropic": (_crear_anthropic, "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
}


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def _config_activa(provider: str | None = None) -> ProveedorConfig:
    """Resuelve un proveedor a su key y su modelo.

    Sin `provider`, usa el de `AI_PROVIDER` (el default del `.env`). Con `provider`, usa
    ese — es el override que manda el front para elegir el modelo en tiempo real.
    """
    nombre = (provider or _config_valor("AI_PROVIDER") or "google").strip().lower()
    entrada = _PROVEEDORES.get(nombre)
    if entrada is None:
        disponibles = ", ".join(_PROVEEDORES)
        raise RuntimeError(
            f"Proveedor '{nombre}' no es válido. Usa uno de: {disponibles}."
        )
    _, key_attr, modelo_attr = entrada
    return ProveedorConfig(
        nombre=nombre,
        api_key=_api_key_viva(key_attr),
        modelo=_config_valor(modelo_attr),
    )


def proveedor_activo(provider: str | None = None) -> str:
    """Nombre del proveedor ('google', 'openai', 'anthropic')."""
    return _config_activa(provider).nombre


def modelo_activo(provider: str | None = None) -> str:
    """Modelo — lo que se registra en `llm_interactions.model`."""
    return _config_activa(provider).modelo


def hay_api_key(provider: str | None = None) -> bool:
    """True si el proveedor tiene una API key configurada.

    Tolerante: un proveedor desconocido (typo) devuelve False en vez de reventar, para
    que el agente caiga con gracia a la explicación determinista.
    """
    try:
        return bool(_config_activa(provider).api_key)
    except RuntimeError:
        return False


def listar_proveedores() -> list[dict[str, Any]]:
    """Catálogo de proveedores para el front: cuál está activo por default y cuáles
    tienen key. **Nunca** devuelve las keys, solo si existen."""
    default = (_config_valor("AI_PROVIDER") or "google").strip().lower()
    salida: list[dict[str, Any]] = []
    for nombre in _PROVEEDORES:
        cfg = _config_activa(nombre)
        salida.append(
            {
                "id": nombre,
                "modelo": cfg.modelo,
                "disponible": bool(cfg.api_key),
                "es_default": nombre == default,
            }
        )
    return salida


def crear_llm(temperature: float | None = None, provider: str | None = None) -> Any:
    """Devuelve un chat model de LangChain listo para `.ainvoke(mensajes)`.

    `mensajes` es la lista `[("system", ...), ("human", ...), ...]` que ya usan tanto
    el agente como la redacción de propuestas — es el formato común de LangChain, así
    que los llamadores no cambian al cambiar de proveedor.
    """
    cfg = _config_activa(provider)
    if not cfg.api_key:
        raise RuntimeError(
            f"El proveedor '{cfg.nombre}' no tiene API key. "
            f"Configúrala en el .env o elige otro proveedor."
        )
    fabrica = _PROVEEDORES[cfg.nombre][0]
    temp = settings.AI_TEMPERATURE if temperature is None else temperature
    return fabrica(cfg.modelo, cfg.api_key, temp)
