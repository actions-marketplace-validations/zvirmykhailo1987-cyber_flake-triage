# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Triage one failed job, or draft issues for the tests that keep flaking.

    flake-triage job https://github.com/OWNER/REPO/actions/runs/RUN/job/JOB   # decision + evidence (JSON)
    flake-triage job ... --summary >> "$GITHUB_STEP_SUMMARY"                  # the same, as Markdown
    flake-triage job ... --history data/openaev                               # with the history the evals use
    flake-triage issues --data data/openaev --min 2                           # draft issues, never posted

Read-only by design: it prints. Re-running, commenting or opening issues stays with a person or with
a workflow step the team writes and reviews.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from flaketriage import dataset, diagnose, llm, triage
from flaketriage.collect import API, GitHub
from flaketriage.excerpt import excerpt

JOB_PATH = re.compile(r"/([\w.-]+/[\w.-]+)/actions/runs/(\d+)/job/(\d+)/?")
MAX_LOG_BYTES = 30_000_000


def parse_job_url(url: str) -> tuple[str, int, int] | None:
    """(owner/repo, run id, job id) for exactly https://github.com/OWNER/REPO/actions/runs/RUN/job/JOB."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    m = JOB_PATH.fullmatch(parts.path)
    if parts.scheme != "https" or parts.netloc.lower() != "github.com" or not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def fetch_failure(url: str, token: str, patient: bool = True) -> dataset.Failure:
    parsed = parse_job_url(url)
    if not parsed:
        raise SystemExit("expected a job URL: https://github.com/OWNER/REPO/actions/runs/RUN/job/JOB")
    repo, run_id, job_id = parsed
    gh = GitHub(token, Path(os.environ.get("RUNNER_TEMP", ".")) / ".flake-cache", patient=patient)
    job = gh.json(f"/repos/{repo}/actions/jobs/{job_id}")
    if job.get("run_id") != run_id:  # one address must not pair one job's log with another run's changes
        raise ValueError(f"job {job_id} does not belong to run {run_id}")
    run = gh.json(f"/repos/{repo}/actions/runs/{run_id}")
    text = gh.log_text(repo, job_id, max_bytes=MAX_LOG_BYTES) or ""
    prs = run.get("pull_requests") or []
    files = (
        gh.json(f"/repos/{repo}/compare/{prs[0]['base']['sha']}...{run['head_sha']}")
        if prs
        else gh.json(f"/repos/{repo}/commits/{run['head_sha']}")
    )
    return dataset.Failure(
        job_id=job_id,
        run_id=run_id,
        attempt=job.get("run_attempt", 1),
        job=job["name"],
        workflow=run["name"],
        event=run["event"],
        branch=run["head_branch"],
        sha=run["head_sha"],
        created_at=run["created_at"],
        seconds=None,
        failed_steps=[s["name"] for s in job.get("steps") or [] if s.get("conclusion") == "failure"],
        changed_files=[f["filename"] for f in files.get("files", [])][:300],
        url=f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}",
        label="unlabelled",
        log=excerpt(text),
    )


MD_SPECIAL = re.compile(r"([\\`*_\[\]()<>!#|~])")


def md_text(text: str) -> str:
    """Log and model text as plain text in Markdown: no links, images, HTML or emphasis can be planted."""
    return MD_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def code(text: str) -> str:
    """A code span that a backtick inside the text cannot break out of."""
    return "`` " + " ".join(text.replace("`", "'").split()) + " ``"


def as_markdown(f: dataset.Failure, d: triage.Decision) -> str:
    verdict = {
        "retry": "🔁 Retry is likely to pass",
        "no_retry": "🛑 Do not retry: likely a real failure",
        "unsure": "❔ Unsure: retry per current policy",
    }[d.decision]
    rows = "\n".join(f"- line {s.line}: {code(s.text[:200])}" for s in d.evidence[:6]) or "- (none)"
    tests = ", ".join(code(t) for t in d.tests[:5]) or "—"
    return (
        f"### {verdict}\n\n**{md_text(f.job)}** · {d.kind} · decided by {d.source}\n\n{md_text(d.reason)}\n\n"
        f"**Failing tests:** {tests}\n\n**Evidence from the log:**\n{rows}\n\n"
        "<sub>flake-triage: read-only. It does not re-run, comment or open issues.</sub>\n"
    )


def issue_drafts(root: Path, minimum: int) -> list[dict[str, Any]]:
    """One draft per test that flaked at least ``minimum`` times, with every run as evidence."""
    return issue_drafts_from(triage.replay([f for f in dataset.load(root) if f.log.strip()]), minimum)


def issue_drafts_from(pairs: list[tuple[dataset.Failure, triage.Decision]], minimum: int) -> list[dict[str, Any]]:
    """The same drafts from failures already decided, oldest first."""
    seen: dict[str, list[tuple[dataset.Failure, triage.Decision]]] = defaultdict(list)
    for f, d in pairs:
        if f.label == "flake":
            for t in d.tests:
                seen[t].append((f, d))
    out = []
    for test, hits in sorted(seen.items(), key=lambda kv: -len(kv[1])):
        if len(hits) < minimum:
            continue
        first = hits[0][1].evidence[0].text if hits[0][1].evidence else ""
        runs = "\n".join(f"- {f.created_at[:10]} · {f.job} · [{f.sha[:7]}]({f.url}) · passed on retry" for f, _ in hits[:15])
        out.append(
            {
                "title": f"Flaky: {test}",
                "test": test,
                "flakes": len(hits),
                "body": (
                    f"`{test}` failed and then passed on a re-run of the same commit {len(hits)} times.\n\n"
                    f"First failing line:\n```\n{first}\n```\n\nRuns:\n{runs}\n\n"
                    "_Draft generated from public CI history. Not posted; a maintainer decides._"
                ),
            }
        )
    return out


def failed_jobs(repo: str, run_id: int, token: str) -> list[str]:
    """Addresses of the failed jobs in the latest attempt of a run, without aggregate gate jobs."""
    gh = GitHub(token, Path(os.environ.get("RUNNER_TEMP", ".")) / ".flake-cache", patient=False)
    jobs: list[dict[str, Any]] = []
    for page in range(1, 11):  # a big matrix has more than 100 jobs; the API pages them
        data = gh.json(f"/repos/{repo}/actions/runs/{run_id}/jobs", filter="latest", per_page=100, page=page)
        jobs += data.get("jobs", [])
        if len(jobs) >= data.get("total_count", 0) or not data.get("jobs"):
            break
    return [
        f"https://github.com/{repo}/actions/runs/{run_id}/job/{j['id']}"
        for j in jobs
        if j.get("conclusion") == "failure" and not dataset.GATE.search(j.get("name", ""))
    ]


def triage_run(
    repo: str, run_id: int, token: str, decide: Any, history: triage.History, emit: Any = None
) -> list[dict[str, Any]]:
    """Every failed job of one run, decided; a job GitHub does not return is reported, not fatal. Each
    verdict is passed to ``emit`` as soon as it is decided, so a step that times out keeps what it had."""
    out = []
    for url in failed_jobs(repo, run_id, token):
        try:
            f = fetch_failure(url, token, patient=False)
            d = decide(f, history)
        except diagnose.BudgetExceeded:
            d = triage.decide(f, history)  # the model budget is spent: rules for the rest
        except (SystemExit, KeyError, OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            row = {"url": url, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            out.append(row)
            if emit:
                emit(f"### ⚠️ Not triaged\n\n{url}: {md_text(row['error'])}\n\n")
            continue
        out.append({"job": f.job, "url": f.url, **asdict(d)})
        if emit:
            emit(as_markdown(f, d))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Should this failed CI job be retried?")
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("job", help="triage one failed job")
    j.add_argument("url")
    j.add_argument("--summary", action="store_true", help="print Markdown instead of JSON")
    j.add_argument("--no-model", action="store_true", help="rules only (without an API key the model is skipped anyway)")
    j.add_argument("--history", type=Path, help="collected data to learn flake history from (as in the evals)")
    r = sub.add_parser("run", help="triage every failed job of one workflow run")
    r.add_argument("--repo", required=True, help="OWNER/REPO")
    r.add_argument("--run-id", type=int, required=True)
    r.add_argument("--json-out", type=Path, help="write the decisions here as JSON")
    r.add_argument("--summary-file", type=Path, help="append the Markdown verdicts here (e.g. $GITHUB_STEP_SUMMARY)")
    r.add_argument("--no-model", action="store_true")
    r.add_argument("--max-usd", type=float, default=0.5, help="model spend cap for this run (then rules only)")
    r.add_argument(
        "--history", type=Path, help="collected data to learn flake history from; without it two history rules cannot fire"
    )
    i = sub.add_parser("issues", help="draft issues for tests that keep flaking")
    i.add_argument("--data", type=Path, default=Path("data/openaev"))
    i.add_argument("--min", type=int, default=2)
    a = ap.parse_args()
    if a.cmd == "issues":
        print(json.dumps(issue_drafts(a.data, a.min), indent=1, ensure_ascii=False))
        return
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("set GITHUB_TOKEN")
    llm.load_local_env()
    decide = triage.decide if a.no_model else diagnose.with_model()
    # without history the rules that learn from past retries cannot fire: the evals measure them with it
    history = triage.learned([x for x in dataset.load(a.history) if x.log.strip()]) if a.history else triage.History()
    if a.cmd == "run":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", a.repo):
            raise SystemExit("--repo is OWNER/REPO")

        def emit(md: str) -> None:
            if a.summary_file:
                with a.summary_file.open("a", encoding="utf-8") as fh:
                    fh.write(md)
            else:
                sys.stdout.write(md)

        if not a.no_model:
            decide = diagnose.with_model(budget=[0.0], max_usd=a.max_usd)
        rows = triage_run(a.repo, a.run_id, token, decide, history, emit)
        if not rows:
            emit("No failed job in this run.\n")
        if a.json_out:
            a.json_out.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
        if rows and all("error" in x for x in rows):
            raise SystemExit("no failed job of this run could be triaged")
        return
    f = fetch_failure(a.url, token)
    d = decide(f, history)
    if a.summary:
        sys.stdout.write(as_markdown(f, d))
    else:
        print(json.dumps({"job": f.job, "url": f.url, **asdict(d)}, indent=1, ensure_ascii=False))


_ = API  # re-exported for callers that build their own client


if __name__ == "__main__":
    main()
