# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""The one place that talks to the Claude API: text in, schema-valid JSON out, every answer kept.

Answers live in a cassette (``evals/llm_cache``) keyed by purpose, prompt version, model and a cache
id, so evals re-run offline and for free, CI never calls the API, and anyone can read exactly what
the model said about each failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from flaketriage.paths import HOME

DEFAULT_MODEL = os.environ.get("FLAKE_MODEL", "claude-haiku-5-5")
CASSETTE = Path(os.environ.get("FLAKE_CASSETTE", HOME / "evals" / "llm_cache"))
# USD per million tokens (input, output), first-party API list prices, Nov 2026
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
_DROP = {"default", "maxLength", "minLength", "title", "examples", "minimum", "maximum"}


class LLMUnavailable(RuntimeError):
    """No key, no network, a refusal or a malformed answer: the caller keeps the rules' decision."""


def load_local_env() -> None:
    """Read ANTHROPIC_API_KEY from this project's git-ignored .env.local, if one is there."""
    path = HOME / ".env.local"
    if path.exists() and "ANTHROPIC_API_KEY" not in os.environ:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()


def strict_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [strict_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: strict_schema(v) for k, v in schema.items() if k not in _DROP}
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def _key(purpose: str, prompt_version: str, model: str, cache_id: str) -> Path:
    digest = hashlib.sha256(f"{purpose}|{prompt_version}|{model}|{cache_id}".encode()).hexdigest()[:24]
    return CASSETTE / purpose / f"{digest}.json"


def call_json(
    *,
    purpose: str,
    prompt_version: str,
    system: str,
    text: str,
    schema: dict[str, Any],
    cache_id: str,
    model: str | None = None,
    replay_only: bool = False,
    max_tokens: int = 1500,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (answer, meta); meta has tokens, ms, cost and whether it came from the cassette."""
    model = model or DEFAULT_MODEL
    path = _key(purpose, prompt_version, model, cache_id)
    if path.exists():
        hit = json.loads(path.read_text(encoding="utf-8"))
        return hit["answer"], {**hit["meta"], "cached": True}
    if replay_only or os.environ.get("FLAKE_REPLAY_ONLY") == "1":
        raise LLMUnavailable("answer not in the cassette and model calls are off")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        raise LLMUnavailable("no API key configured")
    import anthropic

    t0 = time.perf_counter()
    try:
        msg = anthropic.Anthropic(max_retries=3).messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": text}],
            output_config={"format": {"type": "json_schema", "schema": strict_schema(schema)}},
        )
        if msg.stop_reason == "refusal":
            raise LLMUnavailable("the model refused")
        if msg.stop_reason == "max_tokens":
            raise LLMUnavailable(f"answer cut at {max_tokens} tokens")
        answer: dict[str, Any] = json.loads("".join(b.text for b in msg.content if b.type == "text"))
    except LLMUnavailable:
        raise
    except anthropic.APIError as exc:
        raise LLMUnavailable(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMUnavailable(f"malformed answer: {exc}") from exc
    ms = int((time.perf_counter() - t0) * 1000)
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    meta = {"model": msg.model, **usage, "ms": ms, "cost_usd": round(cost_usd(model, **usage), 6), "cached": False}
    try:  # keeping the answer is a convenience; a read-only disk must not lose a paid answer
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"purpose": purpose, "prompt_version": prompt_version, "cache_id": cache_id, "answer": answer, "meta": meta},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        meta["cassette"] = "not written"
    return answer, meta
