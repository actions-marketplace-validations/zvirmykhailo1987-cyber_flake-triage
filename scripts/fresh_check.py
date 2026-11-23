# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Fresh check: the system on failures created after its data was collected.

data/openaev was collected on 2026-11-22 at 23:09 UTC; data/openaev-fresh holds what came later. This
replays the fresh failures after all earlier history exactly as the evals do and scores the three
policies on them only. Honest about what it is: no rule, threshold or prompt was changed after the fresh
data was collected (2026-11-23 10:38 UTC), but code did change between the two collections (rule ids,
the injection filter, the output-token limit; the evals stayed identical), and one live check had
already triaged a job of one of these runs. A late, small sanity check, not a pristine holdout.

    python scripts/fresh_check.py                  # asks the model where needed (cap: --max-usd), writes evals/results/fresh.json
    python scripts/fresh_check.py --replay-only --check   # CI: from the cassette, must match the committed file
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flaketriage import dataset, diagnose, evals, llm, triage

ROOT = Path(__file__).resolve().parents[1]


def after(when: str, cutoff: str) -> bool:
    """Compare as times: GitHub writes '...Z', the collector '+00:00' with microseconds."""
    return datetime.fromisoformat(when) > datetime.fromisoformat(cutoff)


def run(main_dir: Path, fresh_dir: Path, replay_only: bool, max_usd: float) -> dict[str, Any]:
    meta = json.loads((main_dir / "meta.json").read_text(encoding="utf-8"))
    cutoff = meta["collected_at"]
    known = [f for f in dataset.load(main_dir) if f.log.strip()]
    seen = {f.job_id for f in known}
    later = [f for f in dataset.load(fresh_dir) if f.log.strip() and f.job_id not in seen]
    fresh = [f for f in later if after(f.created_at, cutoff)]
    # failures from before the cut that the first collection missed (still running then) are history, not scored
    everything = known + later
    new_ids = {f.job_id for f in fresh}
    exclude = evals.dependent_ids(everything)
    spend = [0.0]
    policies: dict[str, Any] = {"blind": evals.blind, "rules": triage.decide}
    policies["rules+model"] = diagnose.with_model(replay_only=replay_only, budget=spend, max_usd=max_usd)
    out: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fresh_after": cutoff,
        "fresh_collected_at": json.loads((fresh_dir / "meta.json").read_text(encoding="utf-8"))["collected_at"],
        "fresh_failures": len(fresh),
        "labels": {k: sum(f.label == k for f in fresh) for k in ("flake", "persists", "unlabelled")},
        "policies": {},
    }
    for name, fn in policies.items():
        # scored like the evals: only failures with a known outcome (flake or persists)
        pairs = [(f, d) for f, d in triage.replay(everything, fn) if f.job_id in new_ids and f.label in ("flake", "persists")]
        out["policies"][name] = evals.score(pairs, exclude)
        if name == "rules+model":
            out["model_asked"] = sum(d.rule == "model" for _, d in pairs)
    out["model_new_spend_usd"] = round(spend[0], 4)
    # what the model answers for the fresh failures cost when they were asked (the cassette keeps the meta)
    costs = [json.loads(p.read_text(encoding="utf-8")) for p in (llm.CASSETTE / "triage").glob("*.json")]
    out["model_answers_cost_usd"] = round(
        sum(c["meta"]["cost_usd"] for c in costs if int(str(c.get("cache_id", "0")).split(":")[0]) in new_ids), 4
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--main", type=Path, default=ROOT / "data" / "openaev")
    ap.add_argument("--fresh", type=Path, default=ROOT / "data" / "openaev-fresh")
    ap.add_argument("--replay-only", action="store_true")
    ap.add_argument("--max-usd", type=float, default=0.5)
    ap.add_argument("--check", action="store_true", help="fail if the scores differ from the committed file")
    a = ap.parse_args()
    if not a.replay_only:
        llm.load_local_env()
    res = run(a.main, a.fresh, a.replay_only, a.max_usd)
    path = ROOT / "evals" / "results" / "fresh.json"
    if a.check:
        want = json.loads(path.read_text(encoding="utf-8"))
        keys = ("policies", "fresh_failures", "labels", "model_asked", "fresh_after", "model_answers_cost_usd")
        if any(json.loads(json.dumps(res[k])) != want.get(k) for k in keys):
            sys.exit("fresh check differs from fresh.json: re-run it and commit")
        print("fresh check matches", path.name)
        return
    path.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "policies"}, indent=1))
    for name, p in res["policies"].items():
        print(
            f"{name:<12} n={p['failures']:>4} flakes={p['flakes']:>3} retried={p['flakes_retried']:>3} "
            f"persist_skipped={p['persist_skipped']}/{p['persists']} wasted={p['retry_minutes_wasted']}min saved={p['retry_minutes_saved']}min"
        )


if __name__ == "__main__":
    main()
