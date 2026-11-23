# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Decide, for one failed job, whether a retry is worth its CI minutes - by rules, before any model.

Order of evidence (first match wins):

1. a build, type, lint or migration error  -> do not retry: a retry never changes the code;
2. the same failure in the same job failed again on every retry before (at least twice, never passed)
   -> do not retry: history says this one is deterministic, whatever its kind;
3. a network, registry, runner or service-container error -> retry;
4. a timeout -> retry;
5. failing tests that failed again on every retry before, never passed -> do not retry;
6. failing tests that have flaked before -> retry;
7. failing tests whose file the change touched -> do not retry: the change probably broke them;
8. anything else -> ``unsure``: the deterministic layer does not know. The policy for ``unsure`` is the
   team's current one (retry), unless the model layer has a cited reason to decide otherwise.

History is only what earlier failures showed once their retries had finished (see :func:`replay`).
The triage never re-runs anything itself; it returns a decision with the log lines behind it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from flaketriage import signals
from flaketriage.dataset import Failure

RETRY, NO_RETRY, UNSURE = "retry", "no_retry", "unsure"
# rule id -> what it says, for people reading a decision (the console and the MCP tools show these)
RULES = {
    "build": "Build, type, lint or migration error",
    "stuck_signature": "Same failure failed again on every retry before",
    "infra": "Network, registry, runner or service error",
    "timeout": "A step or test ran out of time",
    "stuck_tests": "Failing tests failed again on every retry before",
    "flaked_before": "Failing tests flaked before",
    "change_touches_test": "The change touches the failing test",
    "test_unknown": "Test failure, no history, no link to the change",
    "no_pattern": "No known failure pattern",
    "dependent": "Failed because another job failed",
    "no_log": "No log kept",
    "model": "Model, with quoted log lines",
}
STUCK_AFTER = 2  # failed again on retry this many times, never passed -> treat as deterministic


@dataclass
class Decision:
    decision: str  # retry | no_retry | unsure
    kind: str
    reason: str
    evidence: list[signals.Signal] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    prior_flakes: int = 0
    source: str = "rules"  # rules | model
    signature: str = ""  # job family | kind | what: how history recognises the same failure again
    rule: str = ""  # which rule decided (RULES below); the model layer sets "model"


class History:
    """What earlier failures taught: how often each test (or failure signature) flaked or persisted.
    Fed in time order only."""

    def __init__(self) -> None:
        self.flaked: dict[str, int] = defaultdict(int)
        self.persisted: dict[str, int] = defaultdict(int)

    def learn(self, tests: list[str], label: str, signature: str = "") -> None:
        for t in [*tests, *([signature] if signature else [])]:
            if label == "flake":
                self.flaked[t] += 1
            elif label == "persists":
                self.persisted[t] += 1

    def prior_flakes(self, tests: list[str]) -> int:
        return sum(self.flaked.get(t, 0) for t in tests)

    def stuck(self, key: str) -> int:
        """How many times this failed again on retry, if that happened at least STUCK_AFTER times and it
        never once passed on a retry; otherwise 0."""
        n = self.persisted.get(key, 0)
        return n if n >= STUCK_AFTER and not self.flaked.get(key) else 0


def family(job: str) -> str:
    """'pipeline / API Tests (elk8-6)' -> 'pipeline / API Tests': the shards of one matrix are one job."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", job).strip()


def signature_of(f: Failure, r: signals.Reading, kind: str) -> str:
    s = r.first(kind)
    return f"{family(f.job)} | {kind} | {s.what}" if s is not None and kind in (signals.INFRA, signals.TIMEOUT) else ""


TEST_FILE = re.compile(r"\.(spec|test)\.[jt]sx?$|Tests?\.java$|IT\.java$")


def _stem(path: str) -> str:
    """'e2e/login.spec.ts' -> 'login'; 'src/test/java/io/x/FooTest.java' -> 'FooTest'; 'io.x.FooTest' -> 'FooTest'."""
    name = PurePosixPath(path.replace("\\", "/")).name
    m = re.search(r"\.(spec|test)\.[jt]sx?$|\.java$", name)
    if m:
        return name[: m.start()]
    return name.rsplit(".", 1)[-1] if "/" not in path else name


def touched_tests(reading: signals.Reading, changed: list[str]) -> list[str]:
    """Failing tests whose own test file the change touched. Touching 'login.tsx' does not touch
    'login.spec.ts': only changed test files count."""
    changed_stems = {_stem(c) for c in changed if TEST_FILE.search(c)}
    return [t for t, where in zip(reading.tests, reading.test_files, strict=True) if _stem(where) in changed_stems]


def _decide(f: Failure, r: signals.Reading, kind: str, history: History) -> Decision:
    if not f.log.strip():
        return Decision(UNSURE, signals.UNKNOWN, "No log: GitHub no longer keeps it.", rule="no_log")
    if kind == signals.DEPENDENT:
        s = r.evidence(signals.DEPENDENT)
        return Decision(UNSURE, kind, f"Failed because another job failed ({s.what}): triage that one.", [s], rule="dependent")
    if kind == signals.BUILD:
        s = r.evidence(signals.BUILD)
        return Decision(NO_RETRY, kind, f"Build error ({s.what}): a retry runs the same code.", [s], r.tests, rule="build")
    sig = signature_of(f, r, kind)
    if sig and (n := history.stuck(sig)):
        s = r.evidence(kind)
        return Decision(
            NO_RETRY,
            kind,
            f"{s.what.capitalize()} in this job failed again on every retry, {n} times before.",
            [s],
            r.tests,
            rule="stuck_signature",
        )
    if kind == signals.INFRA:
        s = r.evidence(signals.INFRA)
        return Decision(RETRY, kind, f"Infrastructure ({s.what}), not the code.", [s], r.tests, rule="infra")
    if kind == signals.TIMEOUT:
        s = r.evidence(signals.TIMEOUT)
        return Decision(RETRY, kind, f"Timeout ({s.what}).", [s], r.tests, rule="timeout")
    if kind == signals.TEST:
        ev = [s for s in r.signals if s.kind == signals.TEST][:5]
        prior = history.prior_flakes(r.tests)
        stuck = [history.stuck(t) for t in r.tests]
        if not prior and all(stuck):
            return Decision(
                NO_RETRY,
                kind,
                f"These tests failed again on every retry before ({sum(stuck)} times), never passed.",
                ev,
                r.tests,
                rule="stuck_tests",
            )
        if prior:
            return Decision(RETRY, kind, f"These tests flaked {prior} time(s) before.", ev, r.tests, prior, rule="flaked_before")
        if touched := touched_tests(r, f.changed_files):
            return Decision(
                NO_RETRY,
                kind,
                f"The change touches the failing test: {', '.join(touched[:3])}.",
                ev,
                r.tests,
                rule="change_touches_test",
            )
        return Decision(
            UNSURE, kind, "Test failure with no flake history and no link to the change.", ev, r.tests, rule="test_unknown"
        )
    return Decision(UNSURE, kind, "No known failure pattern in the log.", [], r.tests, rule="no_pattern")


def decide(f: Failure, history: History) -> Decision:
    r = signals.read(f.log)
    kind = signals.kind_of(r)
    d = _decide(f, r, kind, history)
    d.signature = signature_of(f, r, kind) if f.log.strip() else ""
    return d


def known_at(f: Failure) -> datetime:
    """When the outcome of a failure is known: when the retry that decided its label finished.
    Re-runs are often started hours or days later, so the recorded time is used; without one, never."""
    if f.known_at:
        return datetime.fromisoformat(f.known_at)
    return datetime.max.replace(tzinfo=datetime.fromisoformat(f.created_at).tzinfo)


def replay(failures: list[Failure], decide_fn: Callable[[Failure, History], Decision] = decide) -> list[tuple[Failure, Decision]]:
    """Decide each failure in time order. A label is learned only once it would have been known in
    real life (after the retries finished), never at the moment of deciding: no peeking."""
    history = History()
    pending: list[tuple[datetime, list[str], str, str]] = []
    out = []
    for f in sorted(failures, key=lambda x: x.created_at):
        now = datetime.fromisoformat(f.created_at)
        for item in [p for p in pending if p[0] <= now]:
            history.learn(item[1], item[2], item[3])
            pending.remove(item)
        d = decide_fn(f, history)
        out.append((f, d))
        pending.append((known_at(f), d.tests, f.label, d.signature))
    return out


def learned(failures: list[Failure]) -> History:
    """Everything a set of labelled failures teaches, for triaging a new failure after them."""
    h = History()
    for f, d in replay(failures):
        h.learn(d.tests, f.label, d.signature)
    return h
