"""
Zentraler Client für lokale Sprachmodelle.

Alle KI-Funktionen von molo (Event-Suche, Tagging, Scraper-Generierung,
Termin-Erkennung) laufen über dieses Modul. Es spricht die REST-API von
Ollama (https://ollama.com) an. Standardmodell ist ``gpt-oss:20b``; auf
leistungsfähiger Hardware kann ``gpt-oss:120b`` gesetzt werden.

Konfiguration (Django-Settings bzw. Umgebungsvariablen, siehe ``.env.example``):

    LLM_BASE_URL       URL des Ollama-Servers          (http://localhost:11434)
    LLM_MODEL          Standardmodell                  (gpt-oss:20b)
    LLM_CODEGEN_MODEL  Modell für Scraper-Generierung  (= LLM_MODEL)
    LLM_THINK          Reasoning-Stufe low|medium|high (low)
    LLM_TIMEOUT        Timeout pro Anfrage in Sekunden (120)
    LLM_NUM_CTX        Kontextlänge in Tokens          (8192)
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

import requests
from django.conf import settings

logger = logging.getLogger("molo.llm")

JsonSchema = Dict[str, Any]


class LLMError(Exception):
    """Fehler bei der Kommunikation mit dem Sprachmodell."""


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def base_url() -> str:
    return str(_setting("LLM_BASE_URL", "http://localhost:11434")).rstrip("/")


def default_model() -> str:
    return _setting("LLM_MODEL", "gpt-oss:20b")


def codegen_model() -> str:
    return _setting("LLM_CODEGEN_MODEL", "") or default_model()


def is_available(timeout: float = 5) -> bool:
    """True, wenn der Ollama-Server erreichbar ist."""
    try:
        response = requests.get(f"{base_url()}/api/tags", timeout=timeout)
        return response.ok
    except requests.RequestException:
        return False


def chat(
    messages: List[Dict[str, str]],
    *,
    model: Optional[str] = None,
    schema: Optional[Union[JsonSchema, str]] = None,
    think: Optional[Union[bool, str]] = None,
    temperature: Optional[float] = 0.1,
    timeout: Optional[float] = None,
    num_ctx: Optional[int] = None,
) -> str:
    """
    Schickt eine Chat-Anfrage an das Modell und gibt den Antworttext zurück.

    ``schema`` kann ein JSON-Schema (dict) oder ``"json"`` sein; das Modell
    antwortet dann garantiert in diesem Format (Ollama Structured Outputs).
    """
    model = model or default_model()
    think = _setting("LLM_THINK", "low") if think is None else think
    options: Dict[str, Any] = {"num_ctx": num_ctx or int(_setting("LLM_NUM_CTX", 8192))}
    if temperature is not None:
        options["temperature"] = temperature

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": options,
    }
    if think not in (None, ""):
        payload["think"] = think
    if schema is not None:
        payload["format"] = schema

    try:
        response = requests.post(
            f"{base_url()}/api/chat",
            json=payload,
            timeout=timeout or float(_setting("LLM_TIMEOUT", 120)),
        )
    except requests.RequestException as exc:
        raise LLMError(f"Sprachmodell nicht erreichbar ({base_url()}): {exc}") from exc

    if not response.ok:
        raise LLMError(f"Sprachmodell antwortet mit HTTP {response.status_code}: {response.text[:300]}")

    data = response.json()
    content = (data.get("message") or {}).get("content", "")
    logger.debug(
        "LLM %s: %s Prompt-Tokens, %s Antwort-Tokens",
        model, data.get("prompt_eval_count"), data.get("eval_count"),
    )
    return content.strip()


def generate(system: str, user: str, **kwargs: Any) -> str:
    """Kurzform für eine Anfrage aus System- und Nutzer-Prompt."""
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        **kwargs,
    )


def generate_json(system: str, user: str, schema: Optional[JsonSchema] = None, **kwargs: Any) -> Any:
    """
    Wie ``generate``, liefert aber geparstes JSON. Ohne ``schema`` wird nur
    gültiges JSON erzwungen, mit ``schema`` zusätzlich dessen Struktur.
    """
    text = generate(system, user, schema=schema or "json", **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Antwort ist kein gültiges JSON: {text[:300]}") from exc


_CODE_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str:
    """Holt Python-Code aus einer Modellantwort (mit oder ohne Markdown-Codeblock)."""
    blocks = _CODE_FENCE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"
