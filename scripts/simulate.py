# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Shadow-mode simulation: the triage installed in OpenAEV's CI from the first collected day.

Day by day, every failure with a known outcome gets the decision the triage would have posted at that
moment (history only from retries already finished). The simulation counts what a team would have
lived with: CI minutes still spent on retries that failed again, flakes left for a manual re-run, and
how many "do not retry" notes engineers would have read per day.

The first three variants are the evaluated policies. Variants marked ``post_hoc`` change a setting
after the holdout was seen; they are sensitivity, not a tuned result, and the defaults are not changed.

    python scripts/simulate.py            # writes docs/simulation.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from flaketriage import evals, triage
from flaketriage.console import Row, Store

ROOT = Path(__file__).resolve().parents[1]


def model_confidence(row: Row) -> float:
    parts = row.final.source.split(":")
    return float(parts[2]) if len(parts) == 3 else 1.0


def run(
    rows: list[Row], everyone: list[Row], retried: Callable[[Row], bool], label: str, post_hoc: bool = False
) -> dict[str, Any]:
    """Minutes and missed flakes on ``rows`` (known outcome); notes on ``everyone`` (every failed job with a
    log in the period, re-run or not: engineers read the note before anyone knows the outcome)."""
    days: dict[str, set[int]] = {}  # runs with at least one "do not retry": one summary per run
    for r in everyone:
        day = r.f.created_at[:10]
        days.setdefault(day, set())
        if not retried(r):
            days[day].add(r.f.run_id)
    wasted = missed = missed_s = 0
    for r in rows:
        keep = retried(r)
        if r.f.label == "persists" and keep:
            wasted += r.f.retry_seconds
        if r.f.label == "flake" and not keep:
            missed += 1
            missed_s += r.f.retry_seconds
    return {
        "label": label,
        "post_hoc": post_hoc,
        "wasted_min": round(wasted / 60),
        "missed": missed,
        "missed_min": round(missed_s / 60),
        "notes_per_day": round(mean(len(v) for v in days.values()), 1) if days else 0,
        "busiest_day_notes": max((len(v) for v in days.values()), default=0),
    }


def rules_with_stuck_after(st: Store, n: int) -> dict[int, triage.Decision]:
    """Rules replayed with another STUCK_AFTER threshold (the default is 2)."""
    old = triage.STUCK_AFTER
    try:
        triage.STUCK_AFTER = n
        return {f.job_id: d for f, d in triage.replay([r.f for r in st.rows])}
    finally:
        triage.STUCK_AFTER = old


def simulate(st: Store, period: str = "holdout") -> dict[str, Any]:
    rows = sorted((r for r in st.period(period) if r.scored), key=lambda r: (r.f.created_at, r.f.job_id))
    everyone = [r for r in st.period(period) if not r.dependent]
    stuck1, stuck3 = rules_with_stuck_after(st, 1), rules_with_stuck_after(st, 3)
    variants = [
        run(rows, everyone, lambda r: True, "Retry everything (today)"),
        run(rows, everyone, lambda r: r.rules.decision != triage.NO_RETRY, "Rules"),
        run(rows, everyone, lambda r: r.final.decision != triage.NO_RETRY, "Rules + model"),
        run(
            rows,
            everyone,
            lambda r: r.final.decision != triage.NO_RETRY or (r.final.rule == "model" and model_confidence(r) < 0.7),
            "Rules + model, model's 'do not retry' only at confidence ≥ 0.7",
            post_hoc=True,
        ),
        run(
            rows,
            everyone,
            lambda r: stuck1[r.f.job_id].decision != triage.NO_RETRY,
            "Rules, 'stuck' after 1 failed retry",
            post_hoc=True,
        ),
        run(
            rows,
            everyone,
            lambda r: stuck3[r.f.job_id].decision != triage.NO_RETRY,
            "Rules, 'stuck' after 3 failed retries",
            post_hoc=True,
        ),
    ]
    blind = variants[0]["wasted_min"]
    for v in variants:
        v["saved_min"] = blind - v["wasted_min"]
    first, last = rows[0].f.created_at[:10], rows[-1].f.created_at[:10]
    best = variants[2]
    return {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "period": period,
        "from": first,
        "to": last,
        "failures": len(rows),
        "variants": variants,
        "summary": (
            f"Replayed day by day from {first} to {last} as if the triage had been posting its decision on every failed job "
            f"({len(everyone)} with a log). With rules and model, engineers would have seen a 'do not retry' note on about "
            f"{best['notes_per_day']} runs a day (at most {best['busiest_day_notes']}). On the {len(rows)} failures with a known "
            f"outcome, {best['missed']} flakes would have needed a manual re-run ({best['missed_min']} min) while "
            f"{best['saved_min']:,} CI minutes were not spent."
        ),
        "caveat": (
            "Post-hoc rows change a setting after the holdout was seen: they show how sensitive the result is, and the "
            "defaults were not changed because of them. Minutes are runner minutes of the retries GitHub recorded. Notes "
            "count every failed job with a log, re-run or not; the model answered every 'unsure' one of them (answers "
            "for failures nobody re-ran are in the cassette too, but no score is computed on them)."
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "simulation.json")
    ap.add_argument("--period", default="holdout", choices=("holdout", "dev", "all"))
    ap.add_argument("--check", action="store_true", help="fail if the variants differ from the committed file")
    a = ap.parse_args()
    st = Store()
    res = simulate(st, a.period)
    if a.check:
        old = json.loads(a.out.read_text(encoding="utf-8"))
        want = {k: v for k, v in old.items() if k != "at"}
        if {k: v for k, v in {**res, "holdout_from": evals.HOLDOUT_FROM}.items() if k != "at"} != want:
            raise SystemExit(f"simulation differs from {a.out.name}: re-run it and commit")
        print("simulation matches", a.out.name)
        return
    res["holdout_from"] = evals.HOLDOUT_FROM
    a.out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(res["summary"])
    for v in res["variants"]:
        print(
            f"{v['label']:<70} wasted={v['wasted_min']:>5} missed={v['missed']:>3} ({v['missed_min']} min) notes/day={v['notes_per_day']}"
        )


if __name__ == "__main__":
    main()
