# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Score triage policies against what GitHub's own retries showed.

Only failures with a known outcome count: ``flake`` (the retry passed) and ``persists`` (every retry
failed), with a log to read. Split by time: ``dev`` is everything before the holdout cut, ``holdout``
the last days, never looked at while writing rules or prompts.

Policies compared on the same failures:

* ``blind``  - what the repository does today: retry every failed job (up to twice);
* ``rules``  - :mod:`flaketriage.triage`, ``unsure`` -> retry;
* ``rules+model`` - the model decides the ``unsure`` ones, held to quoted log lines.

Numbers per policy: flakes caught (retried, recall), wasted retries (retried but failed again),
missed flakes (not retried although a retry would have passed - an engineer re-runs by hand),
and CI minutes a retry spent on failures that persisted anyway.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from flaketriage import dataset, diagnose, llm, signals, triage
from flaketriage.paths import DATA, EVALS

HOLDOUT_DAYS = 4
# Set on 2026-11-22, when the data was collected and before any rule was tuned on it: everything from this
# moment on is the holdout. Rules and the prompt were written looking only at failures before it. Aggregate
# holdout numbers of a first version were seen once, by an independent review, before its defects were fixed.
HOLDOUT_FROM = "2026-11-18T00:00:00+00:00"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% interval per failure. Shards of one run are correlated, so read it as optimistic."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 3), round(min(1.0, c + h), 3))


def dependent_ids(failures: list[dataset.Failure]) -> set[int]:
    """Jobs that failed only because another job did. Decided from the log, once, for every policy:
    each policy is scored on exactly the same failures."""
    return {f.job_id for f in failures if signals.kind_of(signals.read(f.log)) == signals.DEPENDENT}


def score(pairs: list[tuple[dataset.Failure, triage.Decision]], exclude: set[int] | None = None) -> dict[str, Any]:
    exclude = exclude or set()
    dependent = sum(f.job_id in exclude for f, _ in pairs)
    pairs = [(f, d) for f, d in pairs if f.job_id not in exclude]  # their outcome is another job's
    flakes = [(f, d) for f, d in pairs if f.label == "flake"]
    persist = [(f, d) for f, d in pairs if f.label == "persists"]

    def retried(d: triage.Decision) -> bool:
        return d.decision != triage.NO_RETRY  # unsure -> retry, the team's current policy

    caught = sum(retried(d) for _, d in flakes)
    wasted = [(f, d) for f, d in persist if retried(d)]
    saved = [(f, d) for f, d in persist if not retried(d)]
    missed = len(flakes) - caught
    return {
        "failures": len(pairs),
        "runs": len({f.run_id for f, _ in pairs}),  # matrix shards of one run fail together: fewer independent samples
        "dependent_excluded": dependent,
        "flakes": len(flakes),
        "persists": len(persist),
        "flakes_retried": caught,
        "flake_recall": round(caught / len(flakes), 3) if flakes else None,
        "flake_recall_ci95": wilson(caught, len(flakes)),
        "missed_flakes": missed,
        "persist_skipped": len(saved),
        "persist_skip_rate": round(len(saved) / len(persist), 3) if persist else None,
        "persist_skip_ci95": wilson(len(saved), len(persist)),
        "retry_minutes_wasted": round(sum(f.retry_seconds for f, _ in wasted) / 60),
        "retry_minutes_saved": round(sum(f.retry_seconds for f, _ in saved) / 60),
        # a flake not retried automatically is re-run by hand later: the same minutes, plus a person's wait
        "missed_flake_minutes": round(sum(f.retry_seconds for f, d in flakes if not retried(d)) / 60),
        "decisions": dict(Counter(d.decision for _, d in pairs)),
        "no_retry_precision": round(len(saved) / (len(saved) + missed), 3) if (len(saved) + missed) else None,
    }


def blind(f: dataset.Failure, _: triage.History) -> triage.Decision:
    return triage.Decision(triage.RETRY, "any", "Retry everything (current policy).")


def split(failures: list[dataset.Failure], frozen: str | None = None) -> tuple[list[dataset.Failure], list[dataset.Failure], str]:
    if frozen:
        cut = datetime.fromisoformat(frozen)
    else:  # a new repository: the last HOLDOUT_DAYS days
        last = max(datetime.fromisoformat(f.created_at) for f in failures)
        cut = (last - timedelta(days=HOLDOUT_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    dev = [f for f in failures if datetime.fromisoformat(f.created_at) < cut]
    hold = [f for f in failures if datetime.fromisoformat(f.created_at) >= cut]
    return dev, hold, cut.isoformat()


def run(
    root: Path, with_model: bool, model: str | None, replay_only: bool, max_usd: float, holdout_from: str | None = HOLDOUT_FROM
) -> dict[str, Any]:
    allf = dataset.load(root)
    labelled = [f for f in allf if f.label in ("flake", "persists")]
    with_log = [f for f in labelled if f.log.strip()]
    _, _, cut = split(with_log, holdout_from)
    spend = [0.0]
    policies: dict[str, Any] = {"blind": blind, "rules": triage.decide}
    if with_model:
        policies["rules+model"] = diagnose.with_model(model=model, replay_only=replay_only, budget=spend, max_usd=max_usd)
    out: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repo_data": str(root.name),
        "all_failures": len(allf),
        "labelled": len(labelled),
        "labelled_with_log": len(with_log),
        "labels": dict(Counter(f.label for f in allf)),
        "holdout_from": cut,
        "policies": {},
    }
    exclude = dependent_ids(with_log)
    for name, fn in policies.items():
        # history is learned over ALL labelled failures in time order (logless ones teach nothing: no tests)
        try:
            pairs = [(f, d) for f, d in triage.replay(with_log, fn)]
        except diagnose.BudgetExceeded as exc:
            raise SystemExit(f"budget exceeded: {exc}") from exc
        when = datetime.fromisoformat(cut)
        dev = [(f, d) for f, d in pairs if datetime.fromisoformat(f.created_at) < when]
        hold = [(f, d) for f, d in pairs if datetime.fromisoformat(f.created_at) >= when]
        out["policies"][name] = {"dev": score(dev, exclude), "holdout": score(hold, exclude), "all": score(pairs, exclude)}
        if name == "rules+model":
            out["model"] = {"name": model or llm.DEFAULT_MODEL, "new_spend_usd": round(spend[0], 4)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Score triage policies on collected CI history.")
    ap.add_argument("--data", type=Path, default=DATA / "openaev")
    ap.add_argument("--model", action="store_true", help="include the model layer (uses the cassette first)")
    ap.add_argument("--model-name", default=None)
    ap.add_argument("--replay-only", action="store_true", help="never call the API; cassette only")
    ap.add_argument("--max-usd", type=float, default=3.0)
    ap.add_argument("--verify", action="store_true", help="fail if the numbers differ from evals/results/latest.json")
    a = ap.parse_args()
    llm.load_local_env()
    res = run(a.data, a.model, a.model_name, a.replay_only, a.max_usd)
    latest = EVALS / "results" / "latest.json"
    if a.verify:
        want = json.loads(latest.read_text(encoding="utf-8"))
        diff = [k for k in ("labelled_with_log", "holdout_from", "policies") if json.loads(json.dumps(res[k])) != want[k]]
        if diff:
            raise SystemExit(f"numbers differ from {latest.name} in: {', '.join(diff)} (re-run the evals and commit)")
        print("evals match", latest.name)
        return
    (EVALS / "results").mkdir(parents=True, exist_ok=True)
    path = EVALS / "results" / f"eval-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    for out in (path, latest):
        out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "policies"}, indent=1))
    for name, p in res["policies"].items():
        for sp in ("dev", "holdout"):
            s = p[sp]
            print(
                f"{name:12} {sp:8} n={s['failures']:4} flakes={s['flakes']:3} recall={s['flake_recall']} "
                f"missed={s['missed_flakes']:3} persist_skipped={s['persist_skipped']}/{s['persists']} "
                f"saved={s['retry_minutes_saved']}min wasted={s['retry_minutes_wasted']}min"
            )
    print("->", path)


if __name__ == "__main__":
    main()
