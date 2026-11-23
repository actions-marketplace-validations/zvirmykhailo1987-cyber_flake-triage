# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Collect failed CI jobs from a public repository's GitHub Actions history.

For every run of the chosen workflows that failed at least once, it stores each attempt's jobs, a
bounded excerpt of each failed job's log, and the files the change touched. Everything is cached on
disk, so a second run only asks GitHub for what is new, and the dataset outlives GitHub's 90-day log
retention.

    GITHUB_TOKEN=... python -m flaketriage.collect --repo OpenAEV-Platform/openaev --days 45

Read-only: it never re-runs, cancels or comments. Any token works (logs of public repos need one).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from flaketriage.paths import DATA

API = "https://api.github.com"


class GitHub:
    def __init__(self, token: str, cache: Path, transport: httpx.BaseTransport | None = None, patient: bool = True) -> None:
        """``patient``: the collector waits out rate limits and outages; a web request (patient=False) must
        answer within seconds, so it tries twice and gives up instead of sleeping."""
        self.cache = cache
        self.patient = patient
        self.http = httpx.Client(
            transport=transport,
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "flake-triage-collector",
            },
            timeout=60 if patient else 15,
            follow_redirects=True,
        )
        self.calls = 0

    def _get(self, url: str, **params: Any) -> httpx.Response:
        for attempt in range(8 if self.patient else 2):
            try:
                r = self.http.get(url, params=params or None)
            except httpx.TransportError:  # dropped connection, DNS hiccup: wait and try again
                time.sleep(min(60 if self.patient else 1, 2**attempt))
                continue
            self.calls += 1
            if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                if not self.patient:
                    raise RuntimeError("GitHub rate limit reached")
                wait = max(5, int(r.headers.get("x-ratelimit-reset", "0")) - int(time.time()) + 2)
                print(f"  rate limit: sleeping {wait}s", file=sys.stderr)
                time.sleep(min(wait, 3700))
                continue
            if r.status_code >= 500:
                time.sleep(min(60 if self.patient else 1, 2**attempt))
                continue
            return r
        raise RuntimeError(f"GitHub kept failing for {url}")

    def json(self, url: str, **params: Any) -> Any:
        r = self._get(url, **params)
        r.raise_for_status()
        return r.json()

    def cached_json(self, name: str, url: str, **params: Any) -> Any:
        path = self.cache / f"{name}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        data = self.json(url, **params)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def log_text(self, repo: str, job_id: int, max_bytes: int | None = None) -> str | None:
        """The job's full log; with ``max_bytes``, streamed and cut to its last ``max_bytes`` (the end of a
        failed step is where the cause is), so a huge log cannot fill the memory of a web worker."""
        if max_bytes is None:
            r = self._get(f"/repos/{repo}/actions/jobs/{job_id}/logs")
            if r.status_code in (404, 410):  # expired or never produced
                return None
            r.raise_for_status()
            return r.content.decode("utf-8", errors="replace")
        with self.http.stream("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs") as r:
            self.calls += 1
            if r.status_code in (404, 410):
                return None
            r.raise_for_status()
            tail = b""
            for chunk in r.iter_bytes():
                tail = (tail + chunk)[-max_bytes:]
        return tail.decode("utf-8", errors="replace")


def runs_since(gh: GitHub, repo: str, workflow_id: int, since: datetime) -> list[dict[str, Any]]:
    """All completed runs since ``since``, one query per day: the API stops at 1,000 results per query."""
    out: list[dict[str, Any]] = []
    day = since.date()
    today = datetime.now(UTC).date()
    while day <= today:
        page = 1
        while True:
            args = {"per_page": 100, "page": page, "created": f"{day:%Y-%m-%d}", "status": "completed"}
            url = f"/repos/{repo}/actions/workflows/{workflow_id}/runs"
            # people re-run failed jobs days later: a day's run list is cached only once it is a week old
            cached = day < today - timedelta(days=7)
            data = gh.cached_json(f"runs/{workflow_id}-{day}-{page}", url, **args) if cached else gh.json(url, **args)
            batch = data.get("workflow_runs", [])
            out += batch
            if len(batch) < 100 or page >= 10:
                break
            page += 1
        day += timedelta(days=1)
    return out


def slim_run(r: dict[str, Any]) -> dict[str, Any]:
    prs = r.get("pull_requests") or []
    return {
        "id": r["id"],
        "workflow": r["name"],
        "event": r["event"],
        "branch": r["head_branch"],
        "sha": r["head_sha"],
        "attempts": r["run_attempt"],
        "conclusion": r["conclusion"],
        "created_at": r["created_at"],
        "url": r["html_url"],
        "base_sha": prs[0]["base"]["sha"] if prs else None,
        "pr": prs[0]["number"] if prs else None,
    }


def slim_job(j: dict[str, Any]) -> dict[str, Any]:
    started, done = j.get("started_at"), j.get("completed_at")
    secs = None
    if started and done:
        secs = int((datetime.fromisoformat(done) - datetime.fromisoformat(started)).total_seconds())
    return {
        "id": j["id"],
        "name": j["name"],
        "conclusion": j["conclusion"],
        "seconds": secs,
        "started_at": started,
        "completed_at": done,
        "runner": j.get("runner_name") or "",
        "url": j.get("html_url"),
        "failed_steps": [s["name"] for s in j.get("steps") or [] if s.get("conclusion") == "failure"],
    }


def collect(
    repo: str, workflows: list[int], days: int, out: Path, max_logs: int, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("set GITHUB_TOKEN (any token; logs of public repos need one)")
    cache = out / "cache"
    gh = GitHub(token, cache, transport)
    since = datetime.now(UTC) - timedelta(days=days)
    runs: list[dict[str, Any]] = []
    for wf in workflows:
        runs += [slim_run(r) for r in runs_since(gh, repo, wf, since)]
    touched = [r for r in runs if r["attempts"] > 1 or r["conclusion"] == "failure"]
    print(f"{len(runs)} runs, {len(touched)} with a failure", file=sys.stderr)
    logs_dir = out / "raw"  # full logs, gzipped, git-ignored: excerpts are derived from them
    logs_dir.mkdir(parents=True, exist_ok=True)
    n_logs = 0
    records = []
    skipped = 0
    for i, run in enumerate(sorted(touched, key=lambda r: r["created_at"])):
        try:
            records.append(collect_run(gh, repo, run, logs_dir, max_logs - n_logs))
            n_logs = sum(1 for _ in logs_dir.glob("*.gz"))
        except (RuntimeError, httpx.HTTPError) as exc:  # one unreachable run must not lose the others
            skipped += 1
            print(f"  skipped run {run['id']}: {exc}", file=sys.stderr)
        if i % 10 == 0:
            print(f"  {i}/{len(touched)} runs · {gh.calls} API calls · {n_logs} logs", file=sys.stderr)
    return finish(out, repo, workflows, days, runs, records, gh.calls, skipped)


def collect_run(gh: GitHub, repo: str, run: dict[str, Any], logs_dir: Path, budget: int) -> dict[str, Any]:
    """One run: every attempt's jobs, a log per failed job (while GitHub still keeps it), changed files."""
    attempts = []
    for a in range(1, run["attempts"] + 1):
        data = gh.cached_json(f"jobs/{run['id']}-{a}", f"/repos/{repo}/actions/runs/{run['id']}/attempts/{a}/jobs", per_page=100)
        attempts.append([slim_job(j) for j in data.get("jobs", [])])
    fetched = 0
    gone = 0  # GitHub keeps logs for a limited time; three expired logs in a row -> skip the rest of the run
    for jobs in attempts:
        for j in jobs:
            if j["conclusion"] != "failure":
                continue
            path = logs_dir / f"{j['id']}.log.gz"
            if path.exists() or fetched >= budget or gone >= 3:
                continue
            text = gh.log_text(repo, j["id"])
            fetched += 1
            gone = gone + 1 if text is None else 0
            path.write_bytes(gzip.compress((text or "").encode("utf-8")))
    if run["base_sha"]:
        files = gh.cached_json(f"compare/{run['id']}", f"/repos/{repo}/compare/{run['base_sha']}...{run['sha']}")
    else:
        files = gh.cached_json(f"commit/{run['sha']}", f"/repos/{repo}/commits/{run['sha']}")
    changed = [f["filename"] for f in files.get("files", [])][:300]
    return {**run, "jobs": attempts, "changed_files": changed}


def finish(
    out: Path,
    repo: str,
    workflows: list[int],
    days: int,
    runs: list[dict[str, Any]],
    records: list[dict[str, Any]],
    calls: int,
    skipped: int,
) -> dict[str, Any]:
    body = "".join(json.dumps(r) + "\n" for r in records).encode("utf-8")
    (out / "runs.jsonl.gz").write_bytes(gzip.compress(body, mtime=0))  # mtime=0: same data, same bytes
    meta = {
        "repo": repo,
        "workflows": workflows,
        "days": days,
        "collected_at": datetime.now(UTC).isoformat(),
        "runs_seen": len(runs),
        "runs_with_failure": len(records),
        "runs_skipped": skipped,
        "api_calls": calls,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default="OpenAEV-Platform/openaev")
    ap.add_argument("--workflow", type=int, action="append", help="workflow id (repeatable)")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--max-logs", type=int, default=3000)
    ap.add_argument("--out", type=Path, default=DATA / "openaev")
    a = ap.parse_args()
    print(json.dumps(collect(a.repo, a.workflow or [245579469, 247641507], a.days, a.out, a.max_logs), indent=1))


if __name__ == "__main__":
    main()
