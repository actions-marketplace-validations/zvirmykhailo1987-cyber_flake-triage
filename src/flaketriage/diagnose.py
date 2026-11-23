# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""The model layer: a second opinion only where the rules said ``unsure``, held to the log.

The model reads the excerpt, the failed step, the files the change touched and what the history
knows about the failing tests, and answers in a fixed JSON shape. Code then checks every quoted line
against the excerpt. An answer with no verified quote is dropped and the rules' fallback (retry, the
team's current policy) stands. The model never re-runs a job and never decides on its own evidence.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from flaketriage import llm, signals
from flaketriage.dataset import Failure
from flaketriage.excerpt import lines_of
from flaketriage.triage import NO_RETRY, RETRY, UNSURE, Decision, History, decide

PROMPT_VERSION = "v2"
MAX_LINES = 220
# lines too generic to carry a reason: separators, Maven boilerplate, the generic exit line
BORING = re.compile(r"^[\s\[\]A-Z]*[-=*#_]{8,}\s*$|Process completed with exit code|Re-run Maven|\[Help 1\]|BUILD FAILURE\s*$")
# A narrow filter, not a defence: a log line that plainly addresses a model is not accepted as evidence.
# It is easy to word around; what actually bounds an injected log is that a model answer counts only
# with an exact quote of a real line, never overrides a rule, and the tool only suggests (read-only).
ADDRESSED = re.compile(
    r"(?i:\b(ignore|disregard) (all |any )?(the )?(previous|prior|above) instructions?\b)"
    r"|(?i:\b(AI|LLM|triage bot)\b.{0,40}\b(answer|reply|decide|classify)\b)"
    r"|(?i:\bdecision\s*[=:]\s*(retry|no_retry)\b)"
    r"|(?:^|\]\s)\W*SYSTEM:\s"
)
SYSTEM = """You triage one failed CI job of an open-source project (Java/Spring API, React front end,
Playwright end-to-end tests) for its engineers. Decide whether re-running the job on the same commit
is likely to pass.

- "retry": the failure looks independent of the change: infrastructure, a race or timing problem,
  test data left by another test, an order-dependent test, an external service.
- "no_retry": the change most likely broke it: an assertion about behaviour the change touches, a
  compile or contract error, a deterministic error that will repeat.
Quote the log lines that carry your reason, exactly as written, with their line numbers. If the log
does not tell, say so with low confidence instead of guessing. Write the summary for an engineer who
has 20 seconds: what failed, why you think so."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": [RETRY, NO_RETRY]},
        "category": {"type": "string", "enum": ["infrastructure", "flaky_test", "regression", "unclear"]},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {"type": "object", "properties": {"line": {"type": "integer"}, "quote": {"type": "string"}}},
        },
    },
}


def prompt(f: Failure, d: Decision, history: History) -> str:
    rows = f.log.splitlines()
    if len(rows) > MAX_LINES:  # keep the start of the errors and the end
        rows = [*rows[: MAX_LINES - 60], "…", *rows[-60:]]
    tests = (
        "\n".join(
            f"- {t}: flaked {history.flaked.get(t, 0)}x, persisted {history.persisted.get(t, 0)}x before" for t in d.tests[:10]
        )
        or "- (no named test)"
    )
    changed = "\n".join(f"- {c}" for c in f.changed_files[:60]) or "- (unknown)"
    return (
        f"JOB: {f.job}\nFAILED STEP: {', '.join(f.failed_steps) or '?'}\nEVENT: {f.event} on {f.branch}\n"
        f"RULES SAID: {d.reason}\n\nFAILING TESTS AND HISTORY:\n{tests}\n\nFILES THE CHANGE TOUCHED:\n{changed}\n\n"
        f"LOG EXCERPT (line number | text):\n" + "\n".join(rows)
    )


def verified(answer: dict[str, Any], log: str) -> list[signals.Signal]:
    """Quotes that really are on the cited line (whitespace-insensitive, at least 12 characters)."""
    have = lines_of(log)
    out = []
    for ev in answer.get("evidence") or []:
        line, quote = ev.get("line"), " ".join(str(ev.get("quote", "")).split())
        text = " ".join(have.get(line, "").split()) if isinstance(line, int) else ""
        if (
            len(re.findall(r"[A-Za-z0-9]", quote)) >= 12
            and quote in text
            and not BORING.search(text)
            and not ADDRESSED.search(text)
        ):
            out.append(signals.Signal("model", "quoted", line, have[line].strip()[:240]))
    return out


class BudgetExceeded(RuntimeError):
    """The model calls of this run have cost more than allowed: stop before the next one."""


def with_model(  # type: ignore[no-untyped-def]
    model: str | None = None, replay_only: bool = False, budget: list[float] | None = None, max_usd: float | None = None
):
    """A ``decide`` function for :func:`triage.replay` that asks the model on ``unsure`` failures."""

    def run(f: Failure, history: History) -> Decision:
        d = decide(f, history)
        if d.decision != UNSURE or not f.log.strip() or d.kind == signals.DEPENDENT:  # dependent: ask about its producer
            return d
        if budget is not None and max_usd is not None and budget[0] >= max_usd:
            raise BudgetExceeded(f"model calls cost ${budget[0]:.2f}, the cap is ${max_usd}")
        text = prompt(f, d, history)
        try:
            ans, meta = llm.call_json(
                purpose="triage",
                prompt_version=PROMPT_VERSION,
                system=SYSTEM,
                text=text,
                schema=SCHEMA,
                # the prompt itself is in the key: a new excerpt cut or more history asks again
                cache_id=f"{f.job_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}",
                model=model,
                replay_only=replay_only,
                max_tokens=3000,  # 1500 cut 15 of 255 answers in the first run (long evidence lists)
            )
        except llm.LLMUnavailable as exc:
            d.reason += f" Model unavailable ({exc}); fallback: retry."
            return d
        if budget is not None and not meta.get("cached"):
            budget[0] += float(meta.get("cost_usd", 0))
        ev = verified(ans, f.log)
        if ans.get("decision") not in (RETRY, NO_RETRY):
            d.reason += " Model answer dropped: no valid decision; fallback: retry."
            return d
        if not ev:
            d.reason += " Model answer dropped: no quote matched the log; fallback: retry."
            return d
        return Decision(
            ans["decision"],
            d.kind,
            "Model: " + str(ans.get("summary", ""))[:600],
            ev,
            d.tests,
            d.prior_flakes,
            source=f"model:{ans.get('category', 'unclear')}:{float(ans.get('confidence', 0)):.2f}",
            signature=d.signature,
            rule="model",
        )

    return run
