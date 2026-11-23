# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""MCP server: the triage as tools for another agent (Claude Code fixing CI, a release bot, a chat assistant).

Run:  python -m flaketriage.mcp_server            (stdio)
      python -m flaketriage.mcp_server --http     (streamable HTTP on 127.0.0.1:8771, no auth: loopback only)

Every tool is read-only. ``triage_job`` fetches a job from GitHub only when ``GITHUB_TOKEN`` is set and
the repository is in ``FLAKE_LIVE_REPOS``; otherwise it answers for jobs in the collected data.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from flaketriage import cli, llm, triage
from flaketriage.console import Row, Store

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
LIVE = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
server = MCPServer(
    name="flake-triage",
    instructions=(
        "Answers whether a failed GitHub Actions job is worth re-running: 'retry', 'no_retry' or 'unsure', "
        "always with the log lines it rests on. 'unsure' means: follow the team's current policy (retry). "
        "Never re-run a job, comment or open an issue on the strength of these tools alone; show the evidence "
        "to a person. flaky_tests lists tests that failed and then passed on a re-run, with an issue draft."
    ),
)
_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def _row(row: Row) -> dict[str, Any]:
    d = row.final
    return {
        "job_id": row.f.job_id,
        "job": row.f.job,
        "url": row.f.url,
        "decision": d.decision,
        "rule": d.rule,
        "rule_says": triage.RULES.get(d.rule, d.rule),
        "reason": d.reason,
        "decided_by": "model" if d.rule == "model" else "rules",
        "failing_tests": d.tests[:10],
        "evidence": [{"line": s.line, "text": s.text} for s in d.evidence[:8]],
        "what_happened": {"flake": "passed on a re-run", "persists": "failed again on every re-run", "unlabelled": "not re-run"}[
            row.f.label
        ],
    }


@server.tool(annotations=LIVE)
def triage_job(url: str) -> dict[str, Any]:
    """Triage one failed job by its address: https://github.com/OWNER/REPO/actions/runs/RUN/job/JOB."""
    parsed = cli.parse_job_url(url)
    if not parsed:
        return {"error": "expected https://github.com/OWNER/REPO/actions/runs/RUN/job/JOB"}
    repo, _run_id, job_id = parsed
    st = store()
    row = st.by_id.get(job_id)
    if row is not None:
        return {**_row(row), "source": "collected data"}
    if os.environ.get("FLAKE_LIVE") != "1":
        return {"error": "this job is not in the collected data and live triage is off (FLAKE_LIVE=1 turns it on)"}
    repos = {r.strip().lower() for r in os.environ.get("FLAKE_LIVE_REPOS", "OpenAEV-Platform/openaev").split(",")}
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return {"error": "live triage needs a read-only GITHUB_TOKEN"}
    if repo.lower() not in repos:
        return {"error": f"live triage covers {', '.join(sorted(repos))} only"}
    try:
        f = cli.fetch_failure(url, token, patient=False)
    except (SystemExit, KeyError, OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
        return {"error": f"GitHub did not return this job ({type(exc).__name__})"}
    llm.load_local_env()
    model = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("FLAKE_REPLAY_ONLY") != "1"
    row = st.triage_live(f, model=model, max_usd=float(os.environ.get("FLAKE_LIVE_MAX_USD", "1.0")))
    return {**_row(row), "source": "live"}


@server.tool(annotations=READ_ONLY)
def failure(job_id: int) -> dict[str, Any]:
    """The decision for one collected failure, with its evidence and what happened when it was re-run."""
    row = store().by_id.get(int(job_id))
    return _row(row) if row is not None else {"error": "no failure with this job id in the collected data"}


@server.tool(annotations=READ_ONLY)
def flaky_tests(min_flakes: int = 3, limit: int = 20) -> list[dict[str, Any]]:
    """Tests that failed and then passed on a re-run of the same commit at least ``min_flakes`` times."""
    st = store()
    out = []
    for t in st.flaky_tests(max(1, min(int(min_flakes), 50)))[: max(1, min(int(limit), 100))]:
        out.append(
            {
                "test": t.name,
                "flaked": t.flaked,
                "failed_again": t.persisted,
                "not_rerun": t.not_rerun,
                "first_seen": t.first,
                "last_seen": t.last,
                "jobs": dict(t.jobs.most_common(5)),
            }
        )
    return out


@server.tool(annotations=READ_ONLY)
def summary(period: str = "holdout") -> dict[str, Any]:
    """Minutes saved, flakes kept and failed-again retries skipped, for 'holdout', 'dev' or 'all'."""
    if period not in ("holdout", "dev", "all"):
        return {"error": "period is holdout, dev or all"}
    return store().summary(period)


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(description="flake-triage as MCP tools")
    ap.add_argument("--http", action="store_true", help="streamable HTTP on 127.0.0.1 instead of stdio")
    ap.add_argument("--port", type=int, default=8771)
    a = ap.parse_args()
    if a.http:
        server.run("streamable-http", host="127.0.0.1", port=a.port)
    else:
        server.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
