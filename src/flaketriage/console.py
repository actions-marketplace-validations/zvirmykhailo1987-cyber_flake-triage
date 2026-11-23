# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""What the web console and the MCP server show, computed once from the collected data.

Every failure with a log is replayed in time order exactly as the evals replay it: the rules decide
with the history known at that moment, and the model's answer is read from the cassette (never a live
call here). So a number on a page is the number in ``evals/results/latest.json``, and a decision shown
for one failure is the decision the evals scored for it.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flaketriage import dataset, diagnose, evals, llm, triage
from flaketriage.dataset import Failure
from flaketriage.paths import DATA, EVALS, HOME
from flaketriage.triage import NO_RETRY, Decision

DROPPED = "Model answer dropped"


@dataclass
class Row:
    f: Failure
    rules: Decision
    final: Decision  # the rules, or the model where it answered with a verified quote
    model: str  # answered | dropped | not_asked
    dependent: bool
    period: str  # dev | holdout

    @property
    def retried(self) -> bool:
        return self.final.decision != NO_RETRY  # unsure -> retry, the team's current policy

    @property
    def outcome(self) -> str:
        return self.f.label

    @property
    def scored(self) -> bool:
        """Counted in the evals: a known outcome and not a job that only failed because another did."""
        return self.f.label in ("flake", "persists") and not self.dependent

    @property
    def wasted_s(self) -> int:
        return self.f.retry_seconds if self.f.label == "persists" and self.retried else 0

    @property
    def saved_s(self) -> int:
        return self.f.retry_seconds if self.f.label == "persists" and not self.retried else 0

    @property
    def missed(self) -> bool:
        return self.f.label == "flake" and not self.retried


@dataclass
class TestStats:
    name: str
    flaked: int = 0
    persisted: int = 0
    not_rerun: int = 0
    first: str = ""
    last: str = ""
    jobs: Counter[str] = field(default_factory=Counter)
    rows: list[int] = field(default_factory=list)  # job ids, newest first

    @property
    def failures(self) -> int:
        return self.flaked + self.persisted + self.not_rerun


def _rows(failures: list[Failure], holdout_from: str) -> list[Row]:
    dep = evals.dependent_ids(failures)
    rules = triage.replay(failures)
    model = triage.replay(failures, diagnose.with_model(replay_only=True))
    cut = datetime.fromisoformat(holdout_from)
    out = []
    for (f, r), (g, m) in zip(rules, model, strict=True):
        if f.job_id != g.job_id:  # both replays walk the same failures in the same order
            raise RuntimeError("rules and model replays went out of step")
        if m.source.startswith("model:"):
            final, state = m, "answered"
        elif DROPPED in m.reason:
            final, state = r, "dropped"
        else:
            final, state = r, "not_asked"
        period = "holdout" if datetime.fromisoformat(f.created_at) >= cut else "dev"
        out.append(Row(f, r, final, state, f.job_id in dep, period))
    return out


class Store:
    """All rows plus the aggregates the pages need. Built once; read-only afterwards."""

    def __init__(
        self, root: Path | None = None, holdout_from: str = evals.HOLDOUT_FROM, report: dict[str, Any] | None = None
    ) -> None:
        self.root = root or DATA / "openaev"
        self.holdout_from = holdout_from
        t0 = datetime.now(UTC)
        allf = dataset.load(self.root)
        self.total_failures = len(allf)
        with_log = [f for f in allf if f.log.strip()]
        self.without_log = len(allf) - len(with_log)
        self.rows = sorted(_rows(with_log, holdout_from), key=lambda r: (r.f.created_at, r.f.job_id), reverse=True)
        self.by_id = {r.f.job_id: r for r in self.rows}
        self.history = triage.learned(with_log)  # for triaging a new failure after all of these
        self.tests = self._tests()
        meta = self.root / "meta.json"
        self.meta: dict[str, Any] = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
        # the committed result files describe the committed dataset only, never another one loaded here
        own = self.root.resolve() == (DATA / "openaev").resolve()
        default = self._json(EVALS / "results" / "latest.json") if own else {}
        self.report: dict[str, Any] = report if report is not None else default
        self.simulation: dict[str, Any] = self._json(HOME / "docs" / "simulation.json") if own else {}
        self.fresh: dict[str, Any] = self._json(EVALS / "results" / "fresh.json") if own else {}
        self.cassette = self._cassette()
        self.run_ids = {r.f.run_id for r in self.rows}
        self.live_spend = [0.0]  # model cost of live triage in this process, capped per call of triage_live
        self.built_in_s = round((datetime.now(UTC) - t0).total_seconds(), 1)

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _cassette(self) -> dict[str, Any]:
        files = list((llm.CASSETTE / "triage").glob("*.json"))
        metas = [json.loads(p.read_text(encoding="utf-8"))["meta"] for p in files]
        ms = sorted(m["ms"] for m in metas)
        return {
            "answers": len(metas),
            "cost_usd": round(sum(m["cost_usd"] for m in metas), 4),
            "median_ms": ms[len(ms) // 2] if ms else None,
            "model": metas[0]["model"] if metas else None,
        }

    def _tests(self) -> dict[str, TestStats]:
        out: dict[str, TestStats] = {}
        for r in self.rows:  # newest first
            for t in r.final.tests:
                s = out.setdefault(t, TestStats(t))
                if r.f.label == "flake":
                    s.flaked += 1
                elif r.f.label == "persists":
                    s.persisted += 1
                else:
                    s.not_rerun += 1
                s.first = r.f.created_at
                s.last = s.last or r.f.created_at
                s.jobs[r.f.job] += 1
                s.rows.append(r.f.job_id)
        return out

    # ── live triage (web form and MCP tool) ─────────────────────────────────────────────────────

    def history_for(self, run_id: int) -> triage.History:
        """All collected history, except labels that came from this very run (a later attempt of a
        collected run would otherwise be told its own outcome)."""
        if run_id not in self.run_ids:
            return self.history
        return triage.learned([r.f for r in self.rows if r.f.run_id != run_id])

    def triage_live(self, f: Failure, *, model: bool, max_usd: float) -> Row:
        history = self.history_for(f.run_id)
        rules = triage.decide(f, history)
        final, state = rules, "not_asked"
        if model:
            try:
                m = diagnose.with_model(budget=self.live_spend, max_usd=max_usd)(f, history)
            except diagnose.BudgetExceeded:
                m = rules
            if m.source.startswith("model:"):
                final, state = m, "answered"
        return Row(f, rules, final, state, False, "live")

    # ── views ──────────────────────────────────────────────────────────────────────────────────

    def period(self, name: str) -> list[Row]:
        return self.rows if name == "all" else [r for r in self.rows if r.period == name]

    def summary(self, period: str = "holdout") -> dict[str, Any]:
        rows = [r for r in self.period(period) if r.scored]
        flakes = [r for r in rows if r.f.label == "flake"]
        persists = [r for r in rows if r.f.label == "persists"]
        blind = sum(r.f.retry_seconds for r in persists)
        return {
            "period": period,
            "failures": len(rows),
            "runs": len({r.f.run_id for r in rows}),
            "flakes": len(flakes),
            "persists": len(persists),
            "flakes_retried": sum(r.retried for r in flakes),
            "missed": sum(r.missed for r in flakes),
            "skipped": sum(not r.retried for r in persists),
            "blind_wasted_min": round(blind / 60),
            "wasted_min": round(sum(r.wasted_s for r in rows) / 60),
            "saved_min": round(sum(r.saved_s for r in rows) / 60),
            "decisions": dict(Counter(r.final.decision for r in rows)),
            "model_answered": sum(r.model == "answered" for r in rows),
        }

    def daily(self, period: str = "all") -> list[dict[str, Any]]:
        days: dict[str, dict[str, Any]] = defaultdict(lambda: {"failures": 0, "blind_s": 0, "wasted_s": 0, "missed": 0})
        for r in self.period(period):
            if not r.scored:
                continue
            d = days[r.f.created_at[:10]]
            d["failures"] += 1
            d["blind_s"] += r.f.retry_seconds if r.f.label == "persists" else 0
            d["wasted_s"] += r.wasted_s
            d["missed"] += int(r.missed)
        return [
            {
                "day": k,
                "failures": v["failures"],
                "blind_min": round(v["blind_s"] / 60),
                "wasted_min": round(v["wasted_s"] / 60),
                "missed": v["missed"],
            }
            for k, v in sorted(days.items())
        ]

    def by_rule(self, period: str = "all") -> list[dict[str, Any]]:
        """How often each rule fired on scored failures and how often its decision was right."""
        agg: dict[str, Counter[str]] = defaultdict(Counter)
        for r in self.period(period):
            if r.scored:
                agg[r.final.rule or "?"][f"{r.final.decision}:{r.f.label}"] += 1
        out = []
        for rule, c in agg.items():
            n = sum(c.values())
            decision = max(("retry", "no_retry", "unsure"), key=lambda d: c[f"{d}:flake"] + c[f"{d}:persists"])
            if rule == "model":
                right = c["no_retry:persists"] + c["retry:flake"]
            elif decision == NO_RETRY:
                right = c["no_retry:persists"]
            elif decision == "retry":
                right = c["retry:flake"]
            else:
                right = None
            out.append(
                {
                    "rule": rule,
                    "label": triage.RULES.get(rule, rule),
                    "decision": "mixed" if rule == "model" else decision,
                    "n": n,
                    "flakes": c[f"{decision}:flake"] if rule != "model" else c["retry:flake"] + c["no_retry:flake"],
                    "persists": c[f"{decision}:persists"] if rule != "model" else c["retry:persists"] + c["no_retry:persists"],
                    "right": right,
                    "right_rate": round(right / n, 3) if right is not None and n else None,
                }
            )
        return sorted(out, key=lambda x: -x["n"])

    def filter(
        self,
        *,
        period: str = "all",
        decision: str = "",
        kind: str = "",
        outcome: str = "",
        rule: str = "",
        job: str = "",
        q: str = "",
    ) -> list[Row]:
        q = q.lower().strip()
        out = []
        for r in self.period(period):
            if decision and r.final.decision != decision:
                continue
            if kind and r.final.kind != kind:
                continue
            if outcome and r.f.label != outcome:
                continue
            if rule and r.final.rule != rule:
                continue
            if job and triage.family(r.f.job) != job:
                continue
            if q and q not in r.f.job.lower() and not any(q in t.lower() for t in r.final.tests) and q != str(r.f.job_id):
                continue
            out.append(r)
        return out

    def families(self) -> list[str]:
        return sorted({triage.family(r.f.job) for r in self.rows})

    def flaky_tests(self, minimum: int = 2) -> list[TestStats]:
        return sorted((t for t in self.tests.values() if t.flaked >= minimum), key=lambda t: (-t.flaked, t.name))
