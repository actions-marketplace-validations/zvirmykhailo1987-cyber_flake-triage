# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Turn collected runs into labelled failures.

The label comes from what happened *after* the failure, which the triage never sees:

* ``flake``   - the same job, on the same commit, passed when GitHub re-ran it: a retry helps;
* ``persists`` - every later attempt of that job failed too: a retry only spends CI minutes;
* ``unlabelled`` - nobody re-ran it (superseded by a newer push, or retries ran out before).

Only the first failure of each job in a run becomes a sample, so a job that failed three times counts
once. Aggregate gate jobs ("Pipeline Gate") are dropped: they fail because another job failed.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flaketriage.excerpt import VERSION as EXCERPT_VERSION
from flaketriage.excerpt import excerpt

GATE = re.compile(r"gate\b|summary|report results|notify", re.IGNORECASE)


@dataclass
class Failure:
    job_id: int
    run_id: int
    attempt: int
    job: str
    workflow: str
    event: str
    branch: str
    sha: str
    created_at: str
    seconds: int | None
    failed_steps: list[str]
    changed_files: list[str]
    url: str
    label: str  # flake | persists | unlabelled
    retry_seconds: int = 0  # CI time the later attempts of this job took (what a blind retry spent)
    known_at: str | None = None  # when the last retry that decided the label finished (None: not recorded)
    log: str = field(default="", repr=False)

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.job}"


def base_name(job: str) -> str:
    """'pipeline / 🧪 API Tests (elk8-4)' -> same; strip only the attempt-independent prefix noise."""
    return job.strip()


def _distinct(later: list[dict[str, Any]], first: dict[str, Any]) -> list[dict[str, Any]]:
    """Later attempts of a job, each run once. When only failed jobs are re-run, GitHub lists the
    jobs that already passed again under the new attempt with the same times: a copy, not a run."""
    seen = {(first.get("started_at"), first.get("completed_at"))} if first.get("started_at") else set()
    out = []
    for x in later:
        stamp = (x.get("started_at"), x.get("completed_at"))
        if x.get("started_at") and stamp in seen:
            continue
        seen.add(stamp)
        out.append(x)
    return out


def failures_of(run: dict[str, Any]) -> list[Failure]:
    attempts: list[list[dict[str, Any]]] = run["jobs"]
    out: list[Failure] = []
    seen: set[str] = set()
    for a, jobs in enumerate(attempts, start=1):
        for j in jobs:
            name = base_name(j["name"])
            if j["conclusion"] != "failure" or name in seen or GATE.search(name):
                continue
            seen.add(name)
            later = _distinct([x for jobs2 in attempts[a:] for x in jobs2 if base_name(x["name"]) == name], j)
            if not later:
                label = "unlabelled"
            elif any(x["conclusion"] == "success" for x in later):
                label = "flake"
                later = later[: next(i for i, x in enumerate(later) if x["conclusion"] == "success") + 1]
            elif all(x["conclusion"] == "failure" for x in later):
                label = "persists"
            else:  # cancelled / skipped later: we do not know
                label = "unlabelled"
            ends = [x["completed_at"] for x in later if x.get("completed_at")]
            out.append(
                Failure(
                    job_id=j["id"],
                    run_id=run["id"],
                    attempt=a,
                    job=name,
                    workflow=run["workflow"],
                    event=run["event"],
                    branch=run["branch"],
                    sha=run["sha"],
                    created_at=run["created_at"],
                    seconds=j["seconds"],
                    failed_steps=j["failed_steps"],
                    changed_files=run.get("changed_files", []),
                    url=j["url"] or run["url"],
                    label=label,
                    retry_seconds=sum(x["seconds"] or 0 for x in later),
                    known_at=max(ends) if ends and len(ends) == len(later) else None,
                )
            )
    return out


def read_runs(root: Path) -> str:
    """The collected runs: ``runs.jsonl.gz`` as committed, or a plain ``runs.jsonl``."""
    gz = root / "runs.jsonl.gz"
    if gz.exists():
        return gzip.decompress(gz.read_bytes()).decode("utf-8")
    return (root / "runs.jsonl").read_text(encoding="utf-8")


def load(root: Path, with_logs: bool = True) -> list[Failure]:
    rows = [json.loads(x) for x in read_runs(root).splitlines() if x.strip()]
    out = [f for r in rows for f in failures_of(r)]
    if with_logs:
        for f in out:
            ex = root / f"excerpts-{EXCERPT_VERSION}" / f"{f.job_id}.txt"
            raw = root / "raw" / f"{f.job_id}.log.gz"
            if ex.exists():
                f.log = ex.read_text(encoding="utf-8")
            elif raw.exists():
                f.log = excerpt(gzip.decompress(raw.read_bytes()).decode("utf-8", errors="replace"))
                ex.parent.mkdir(parents=True, exist_ok=True)
                ex.write_text(f.log, encoding="utf-8")
    return sorted(out, key=lambda f: (f.created_at, f.job_id))
