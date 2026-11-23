# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""End to end against the real processes and the real data, every way in:

1. the web console over HTTP: every page, headers, the numbers against latest.json, a load burst;
2. the MCP server over its stdio protocol, as another agent would start it;
3. the command line: issue drafts against docs/issue-drafts.json;
4. live: a failed job of OpenAEV that is NOT in the collected data, fetched from GitHub now and triaged
   by the CLI (needs GITHUB_TOKEN; skipped and reported as skipped without one).

    python scripts/e2e_check.py --url http://127.0.0.1:8070     # writes evals/results/e2e.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess  # nosec B404 - runs this repo's own CLI and MCP server
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8", "FLAKE_REPLAY_ONLY": "1"}
results: list[dict[str, Any]] = []


def check(name: str, ok: bool, info: str = "", skipped: bool = False) -> bool:
    results.append({"check": name, "ok": bool(ok), "skipped": skipped, "info": info})
    print(f"{'SKIP' if skipped else 'PASS' if ok else 'FAIL'}  {name}" + (f"  ({info})" if info else ""))
    return ok


def web(base: str) -> None:
    latest = json.loads((ROOT / "evals/results/latest.json").read_text(encoding="utf-8"))
    hold = latest["policies"]["rules+model"]["holdout"]
    c = httpx.Client(base_url=base, timeout=30)
    summary = c.get("/api/summary").json()
    check(
        "API summary = latest.json (holdout, rules+model)",
        (summary["flakes_retried"], summary["skipped"], summary["wasted_min"])
        == (hold["flakes_retried"], hold["persist_skipped"], hold["retry_minutes_wasted"]),
        f"{summary['skipped']}/{summary['persists']} skipped",
    )
    home = c.get("/").text
    check("overview shows the evaluated numbers", f"{hold['persist_skipped']}<small>of {hold['persists']}</small>" in home)
    tests = c.get("/api/tests?min_flakes=3").json()
    drafts = json.loads((ROOT / "docs/issue-drafts.json").read_text(encoding="utf-8"))
    check(
        "flaky tests = issue drafts (min 3)",
        [t["test"] for t in tests][:3] == [d["test"] for d in drafts][:3],
        f"{len(tests)} tests",
    )
    sample = c.get("/failures?period=holdout&rule=model").text
    ids = re.findall(r'href="/failures/(\d+)"', sample)[:3]
    pages = [
        "/",
        "/failures",
        "/failures?period=holdout&decision=no_retry",
        "/tests",
        "/triage",
        "/quality",
        "/status",
        "/report",
        "/health",
        f"/test?name={tests[0]['test']}",
        *[f"/failures/{i}" for i in ids],
    ]
    bad = []
    for p in pages:
        r = c.get(p)
        if r.status_code != 200 or "content-security-policy" not in r.headers:
            bad.append(f"{p}:{r.status_code}")
        if p != "/report" and re.search(
            r"<script|https?://(?!github\.com|127\.0\.0\.1|localhost)[\w.-]+/",
            r.text.replace('xmlns="http://www.w3.org/2000/svg"', ""),
        ):
            bad.append(f"{p}:external-or-script")
    check(f"{len(pages)} pages answer, with CSP, no script, no third-party URL", not bad, ", ".join(bad))
    check("unknown page is a 404 page", c.get("/no/such").status_code == 404)

    pool = httpx.Client(base_url=base, timeout=60, limits=httpx.Limits(max_connections=16))  # keep-alive, as a browser

    def hit(i: int) -> float:
        t0 = time.perf_counter()
        r = pool.get(pages[i % len(pages)])
        assert r.status_code == 200
        return (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    with ThreadPoolExecutor(16) as ex:
        ms = sorted(ex.map(hit, range(400)))
    wall = time.perf_counter() - t0
    p50, p95 = statistics.median(ms), ms[int(len(ms) * 0.95)]
    check(
        "load: 400 requests, 16 at a time, all 200", len(ms) == 400, f"p50 {p50:.0f} ms, p95 {p95:.0f} ms, {400 / wall:.0f} req/s"
    )


async def mcp() -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    latest = json.loads((ROOT / "evals/results/latest.json").read_text(encoding="utf-8"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "flaketriage.mcp_server"], env=ENV, cwd=str(ROOT))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        tools = sorted(t.name for t in (await s.list_tools()).tools)
        check(
            "MCP lists its four read-only tools", tools == ["failure", "flaky_tests", "summary", "triage_job"], ", ".join(tools)
        )
        out = json.loads((await s.call_tool("summary", {"period": "holdout"})).content[0].text)
        check("MCP summary = latest.json", out["skipped"] == latest["policies"]["rules+model"]["holdout"]["persist_skipped"])
        fl = json.loads((await s.call_tool("flaky_tests", {"min_flakes": 3, "limit": 1})).content[0].text)
        check(
            "MCP flaky_tests top test",
            fl["test"].startswith("ThreatArsenalApiTest")
            if isinstance(fl, dict)
            else fl[0]["test"].startswith("ThreatArsenalApiTest"),
        )
        bad = json.loads((await s.call_tool("triage_job", {"url": "https://evil.example/x"})).content[0].text)
        check("MCP refuses a non-GitHub address", "error" in bad)


def cli_issues() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "flaketriage.cli", "issues", "--data", "data/openaev", "--min", "3"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENV,
        cwd=ROOT,
        check=False,
    )
    drafts = json.loads(out.stdout or "[]")
    want = json.loads((ROOT / "docs/issue-drafts.json").read_text(encoding="utf-8"))
    check(
        "CLI issues = docs/issue-drafts.json", [d["title"] for d in drafts] == [d["title"] for d in want], f"{len(drafts)} drafts"
    )


def live() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        check("live: a new OpenAEV job from GitHub", True, "no GITHUB_TOKEN", skipped=True)
        return
    known = {int(p.stem) for p in (ROOT / "data/openaev/excerpts-e7").glob("*.txt")}
    gh = httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    runs = (
        gh.get("/repos/OpenAEV-Platform/openaev/actions/runs", params={"status": "failure", "per_page": 20})
        .json()
        .get("workflow_runs", [])
    )
    candidates = []
    for run in runs:
        for j in (
            gh.get(f"/repos/OpenAEV-Platform/openaev/actions/runs/{run['id']}/jobs", params={"filter": "latest"})
            .json()
            .get("jobs", [])
        ):
            if j["conclusion"] == "failure" and j["id"] not in known and "gate" not in j["name"].lower():
                candidates.append(j["html_url"])
    if not candidates:
        check("live: a new OpenAEV job from GitHub", True, "no new failed job right now", skipped=True)
        return
    env = {**ENV, "GITHUB_TOKEN": token}
    env.pop("FLAKE_REPLAY_ONLY")  # the model may be asked for real (Haiku, ~$0.001)
    tried = 0
    for url in candidates[:6]:  # a job that only failed because another did says little: prefer one with its own cause
        t0 = time.perf_counter()
        out = subprocess.run(
            [sys.executable, "-m", "flaketriage.cli", "job", url, "--history", "data/openaev"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=ROOT,
            check=False,
        )
        secs = time.perf_counter() - t0
        tried += 1
        try:
            d = json.loads(out.stdout)
        except json.JSONDecodeError:
            check("live: a new OpenAEV job from GitHub", False, (out.stderr or out.stdout)[-300:])
            return
        if d.get("rule") != "dependent":
            break
    lines_ok = all(isinstance(e.get("line"), int) for e in d.get("evidence", []))
    check(
        "live: a new OpenAEV job from GitHub",
        d["decision"] in ("retry", "no_retry", "unsure") and lines_ok,
        f"{d['job']} -> {d['decision']} ({d['source']}, {d.get('rule')}) in {secs:.0f} s, {tried} tried: {url}",
    )
    results[-1]["live"] = {
        "url": url,
        "decision": d["decision"],
        "rule": d.get("rule"),
        "reason": d["reason"][:400],
        "evidence": d.get("evidence", [])[:3],
    }


def action() -> None:
    """What action.yml does, on a real failed run of OpenAEV: install the package (not editable) into a
    clean virtualenv with the locked dependencies, then `flake-triage run` with a JSON file and a summary."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        check("action: a whole failed run, from a clean install", True, "no GITHUB_TOKEN", skipped=True)
        return
    import tempfile
    import venv

    gh = httpx.Client(base_url="https://api.github.com", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    runs = (
        gh.get("/repos/OpenAEV-Platform/openaev/actions/runs", params={"status": "failure", "per_page": 5})
        .json()
        .get("workflow_runs", [])
    )
    if not runs:
        check("action: a whole failed run, from a clean install", True, "no failed run right now", skipped=True)
        return
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        venv.create(t / "venv", with_pip=True)
        py = t / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        inst = subprocess.run(
            [str(py), "-m", "pip", "install", "-q", "-c", str(ROOT / "requirements.lock"), str(ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        if inst.returncode:
            check("action: a whole failed run, from a clean install", False, inst.stderr[-300:])
            return
        summary, output = t / "summary.md", t / "output.txt"
        summary.write_text("", encoding="utf-8")
        # the exact shell of the action's triage step, run by bash with the env the action gives it
        import yaml

        step = next(
            s
            for s in yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))["runs"]["steps"]
            if s.get("id") == "triage"
        )
        env = {
            **os.environ,
            "PATH": str(py.parent) + os.pathsep + os.environ.get("PATH", ""),
            "GITHUB_TOKEN": token,
            "ANTHROPIC_API_KEY": "",  # rules only: no paid call in a check
            "RUNNER_TEMP": str(t),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_ACTION_PATH": str(ROOT),
            "FLAKE_REPO": "OpenAEV-Platform/openaev",
            "FLAKE_RUN_ID": str(runs[0]["id"]),
            "FLAKE_HISTORY": "bundled",
            "FLAKE_MAX_USD": "0.5",
            "PYTHONIOENCODING": "utf-8",
        }
        env.pop("PYTHONPATH", None)
        # Git Bash on Windows (System32\bash.exe is a WSL launcher), the runner's bash elsewhere
        bash = next((b for b in (r"C:\Program Files\Git\bin\bash.exe", "/bin/bash") if Path(b).exists()), "bash")
        t0 = time.perf_counter()
        out = subprocess.run(
            [bash, "-e", "-c", step["run"]], capture_output=True, text=True, encoding="utf-8", env=env, cwd=t, check=False
        )
        secs = time.perf_counter() - t0
        decisions = output.read_text(encoding="utf-8").strip().removeprefix("decisions=") if output.exists() else ""
        rows = json.loads(Path(decisions).read_text(encoding="utf-8")) if decisions and Path(decisions).exists() else []
        decided = [r for r in rows if "decision" in r]
        ok = out.returncode == 0 and bool(rows) and summary.read_text(encoding="utf-8").startswith("### ")
        check(
            "action: a whole failed run, from a clean install",
            ok,
            f"run {runs[0]['id']}: {len(decided)} of {len(rows)} failed jobs decided in {secs:.0f} s"
            + ("" if ok else f"; {(out.stderr or out.stdout)[-200:]}"),
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8070")
    ap.add_argument("--out", type=Path, default=ROOT / "evals/results/e2e.json")
    a = ap.parse_args()
    web(a.url)
    asyncio.run(mcp())
    cli_issues()
    live()
    action()
    ok = all(r["ok"] for r in results)
    a.out.write_text(
        json.dumps(
            {"at": datetime.now(UTC).isoformat(timespec="seconds"), "all_ok": ok, "checks": results}, indent=1, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    skipped = [r["check"] for r in results if r["skipped"]]
    print(("ALL PASS" if ok else "SOME FAILED") + (f" ({len(skipped)} skipped)" if skipped else ""))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
