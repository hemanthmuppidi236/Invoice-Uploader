"""
Claude API client and structured-output plumbing.

Every AI call in this app returns strict JSON (prompt §8: "Ask for structured
JSON only"), so this module wraps the Messages API with a helper that takes a
Pydantic model, sends its schema as an `output_config.format` constraint, and
validates the response back into that model.

Why raw `output_config.format` rather than the SDK's `messages.parse()`
convenience: we also need `output_config.effort` on the same request, so the
whole `output_config` object is built here explicitly. Nothing in the pipeline
depends on the SDK inferring a schema for us.

Model choice is in config (`CLAUDE_MODEL`, default claude-opus-5). Adaptive
thinking is on: picking a concrete cost code off a scanned invoice is a
judgement call, and a wrong code is a wrong bill in BuilderTrend.
"""

import base64
import json
import logging
from typing import Any, Optional, Type, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# The Messages API caps a request at 32 MB, and base64 inflates by ~4/3, so
# the raw PDF has to stay under roughly 24 MB. Refuse earlier with a clear
# message rather than letting the API return an opaque 413.
MAX_PDF_BYTES = 20 * 1024 * 1024

_client: Optional[anthropic.Anthropic] = None


class ClaudeNotConfigured(RuntimeError):
    """Raised when an AI path is reached with no ANTHROPIC_API_KEY set."""


class ClaudeOutputInvalid(RuntimeError):
    """The model returned JSON that does not satisfy the requested schema.

    Carries the raw text so the caller can store it on the invoice/flag for
    diagnosis — prompt §7.1 requires failing loudly with the error text
    rather than silently skipping.
    """

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def get_client() -> anthropic.Anthropic:
    global _client
    if not settings.claude_enabled:
        raise ClaudeNotConfigured(
            "ANTHROPIC_API_KEY is not set; AI extraction and mix design "
            "parsing are unavailable."
        )
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


# ─── Content block helpers ────────────────────────────────────────────


def pdf_block(pdf_bytes: bytes) -> dict:
    """A base64 PDF document block.

    Document blocks go BEFORE the text block in the content array so the
    model reads the document as the subject of the instruction that follows.
    """
    if not pdf_bytes:
        raise ValueError("pdf_block called with empty bytes")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError(
            f"PDF is {len(pdf_bytes) / 1_048_576:.1f} MB; the limit is "
            f"{MAX_PDF_BYTES / 1_048_576:.0f} MB. Split or downsample it."
        )
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            # No newlines in the base64 payload — the API rejects them.
            "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
        },
    }


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


# ─── Strict schema generation ─────────────────────────────────────────

# JSON Schema keywords the structured-outputs validator does not accept.
# Pydantic emits several of these from Field(...) constraints, so they are
# stripped rather than forbidden at the model level — that keeps the Python
# models free to carry their own validation.
_UNSUPPORTED_KEYWORDS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "default",
    "examples",
}


def to_strict_schema(model: Type[BaseModel]) -> dict:
    """Render a Pydantic model as a schema the API will accept.

    Three transformations:
      1. Strip unsupported constraint keywords (see above).
      2. Set `additionalProperties: false` on every object.
      3. Mark every property required — optionality is expressed by allowing
         null in the type, which is what `Optional[...]` already produces.

    The result is still validated client-side by the Pydantic model itself,
    so stripping a constraint here loses nothing.
    """
    return _strictify(model.model_json_schema())


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(n) for n in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_KEYWORDS:
            continue
        out[key] = _strictify(value)

    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        props = out.get("properties")
        if isinstance(props, dict):
            out["required"] = list(props.keys())

    return out


# ─── The call ─────────────────────────────────────────────────────────


def structured_call(
    *,
    output_model: Type[T],
    system: str,
    content: list[dict],
    max_tokens: Optional[int] = None,
    effort: Optional[str] = None,
) -> tuple[T, dict]:
    """Send one message and validate the response into `output_model`.

    Returns (parsed, meta) where meta carries the model id and token counts
    for logging to invoice_suggestions.

    Raises ClaudeNotConfigured, ClaudeOutputInvalid, or the SDK's own typed
    exceptions (RateLimitError, APIStatusError, ...) — callers flag the
    invoice with the error text rather than retrying blindly.
    """
    client = get_client()

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens or settings.claude_max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort or settings.claude_effort,
            "format": {
                "type": "json_schema",
                "schema": to_strict_schema(output_model),
            },
        },
        messages=[{"role": "user", "content": content}],
    )

    meta = {
        "model": response.model,
        "prompt_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "stop_reason": response.stop_reason,
    }

    # Check stop_reason before reading content: a refusal returns HTTP 200
    # with an empty content array, and max_tokens returns truncated JSON.
    if response.stop_reason == "refusal":
        raise ClaudeOutputInvalid(
            "The model declined this document "
            f"({getattr(response.stop_details, 'category', 'unspecified')})."
        )
    if response.stop_reason == "max_tokens":
        raise ClaudeOutputInvalid(
            "Response hit the output token limit before the JSON was "
            "complete. Raise CLAUDE_MAX_TOKENS or split the document."
        )

    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw.strip():
        raise ClaudeOutputInvalid("The model returned no text content.", raw)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ClaudeOutputInvalid(f"Response was not valid JSON: {e}", raw)

    try:
        parsed = output_model.model_validate(payload)
    except ValidationError as e:
        raise ClaudeOutputInvalid(f"Response did not match the schema: {e}", raw)

    log.info(
        "claude call ok model=%s in=%s out=%s schema=%s",
        meta["model"],
        meta["prompt_tokens"],
        meta["output_tokens"],
        output_model.__name__,
    )
    return parsed, meta
