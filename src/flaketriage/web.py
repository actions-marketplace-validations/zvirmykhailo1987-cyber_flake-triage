# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Web console for the people who keep CI green: which failures were worth a retry, the log lines
behind every decision, the tests that keep flaking, how good each rule is, and a form to triage one
job. Read-only: it never re-runs a job, comments or opens an issue.

    uvicorn flaketriage.web:create_app --factory --port 8070

Live triage of a job that is not in the collected data needs ``FLAKE_LIVE=1`` and a read-only
``GITHUB_TOKEN``; it is limited to the repositories in ``FLAKE_LIVE_REPOS`` and rate-limited.
"""

from __future__ import annotations

import hmac
import math
import os
import secrets
import threading
import time
from collections import Counter, OrderedDict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from flaketriage import __version__, cli, llm, triage
from flaketriage.console import Row, Store
from flaketriage.excerpt import lines_of
from flaketriage.paths import HOME

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
CSRF_COOKIE = "ft_csrf"
PAGE = 50
DECISION_LABEL = {"retry": "Retry", "no_retry": "Do not retry", "unsure": "Unsure"}
OUTCOME_LABEL = {"flake": "Passed on retry", "persists": "Failed again", "unlabelled": "Not re-run"}
KIND_LABEL = {
    "build": "Build",
    "test": "Test",
    "infra": "Infrastructure",
    "timeout": "Timeout",
    "dependent": "Dependent",
    "unknown": "Unknown",
}


def minutes(seconds: int | None) -> str:
    if not seconds:
        return "—"
    m = seconds / 60
    return f"{m:.0f} min" if m >= 10 else f"{m:.1f} min"


def num(n: float | None) -> str:
    return "—" if n is None else f"{n:,.0f}".replace(",", " ")


def pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def when(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M")


def when_short(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %H:%M")


def short_test(name: str) -> str:
    """'tests_e2e/tests/infra/x.spec.ts > Suite > title' -> 'x.spec.ts > Suite > title'."""
    head, sep, tail = name.partition(" > ")
    return head.rsplit("/", 1)[-1] + sep + tail


templates.env.filters.update(minutes=minutes, num=num, pct=pct, when=when, when_short=when_short, short_test=short_test)
templates.env.globals.update(
    DECISION_LABEL=DECISION_LABEL, OUTCOME_LABEL=OUTCOME_LABEL, KIND_LABEL=KIND_LABEL, RULES=triage.RULES, num=num
)


class RateLimiter:
    """Per-IP sliding window and a global daily cap, bounded memory (an LRU of addresses)."""

    def __init__(self, per_ip: int, window_s: int, daily: int, max_ips: int = 10_000) -> None:
        self.per_ip, self.window_s, self.daily, self.max_ips = per_ip, window_s, daily, max_ips
        self.hits: OrderedDict[str, deque[float]] = OrderedDict()
        self.day, self.count = date.today(), 0
        self.lock = threading.Lock()

    def allow(self, ip: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        with self.lock:
            if date.today() != self.day:
                self.day, self.count = date.today(), 0
            if self.count >= self.daily:
                return "The daily limit of live triage is reached. Try again tomorrow."
            q = self.hits.pop(ip, deque())
            while q and now - q[0] > self.window_s:
                q.popleft()
            self.hits[ip] = q
            while len(self.hits) > self.max_ips:
                self.hits.popitem(last=False)
            if len(q) >= self.per_ip:
                return "Too many requests in a short time. Try again in a few minutes."
            q.append(now)
            self.count += 1
            return None


def excerpt_view(row: Row) -> list[dict[str, Any]]:
    """The excerpt as numbered lines, cited lines marked, gaps between kept fragments shown."""
    cited = {s.line for s in row.final.evidence} | {s.line for s in row.rules.evidence}
    out: list[dict[str, Any]] = []
    prev = None
    for n, text in sorted(lines_of(row.f.log).items()):
        if prev is not None and n > prev + 1:
            out.append({"gap": n - prev - 1})
        out.append({"n": n, "text": text, "cited": n in cited})
        prev = n
    return out


def chart_days(days: list[dict[str, Any]], holdout: str, width: int = 960, height: int = 240) -> dict[str, Any]:
    """Geometry of the daily chart (bars drawn in the template, so the CSP needs no inline style)."""
    pad_l, pad_r, pad_t, pad_b = 52, 8, 12, 34
    top = max((d["blind_min"] for d in days), default=0)
    step = 500 if top <= 2500 else 1000
    hi = max(step, math.ceil(top / step) * step)
    n = max(len(days), 1)
    slot = (width - pad_l - pad_r) / n
    bw = max(2.0, slot * 0.72)
    ph = height - pad_t - pad_b
    bars = []
    for i, d in enumerate(days):
        x = pad_l + i * slot + (slot - bw) / 2
        hb = ph * d["blind_min"] / hi
        hw = ph * d["wasted_min"] / hi
        bars.append(
            {
                "x": round(x, 1),
                "w": round(bw, 1),
                "yb": round(pad_t + ph - hb, 1),
                "hb": round(hb, 1),
                "yw": round(pad_t + ph - hw, 1),
                "hw": round(hw, 1),
                "day": d["day"],
                "label": d["day"][5:].replace("-", "/") if i % max(1, n // 9) == 0 else "",
                "holdout": d["day"] >= holdout[:10],
                "title": f"{d['day']}: {d['blind_min']} min on retries that failed again; {d['wasted_min']} min with the triage",
            }
        )
    ticks = [{"y": round(pad_t + ph - ph * v / hi, 1), "v": v} for v in range(0, hi + 1, step)]
    cut = next((b["x"] - (slot - bw) / 2 for b in bars if b["holdout"]), None)
    return {
        "w": width,
        "h": height,
        "bars": bars,
        "ticks": ticks,
        "x0": pad_l,
        "x1": width - pad_r,
        "base": pad_t + ph,
        "cut": cut,
    }


def create_app(store: Store | None = None) -> FastAPI:
    llm.load_local_env()
    app = FastAPI(title="Flake triage", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    st = store or Store()
    live = os.environ.get("FLAKE_LIVE") == "1" and bool(os.environ.get("GITHUB_TOKEN"))
    live_repos = {
        r.strip().lower() for r in os.environ.get("FLAKE_LIVE_REPOS", "OpenAEV-Platform/openaev").split(",") if r.strip()
    }
    live_model = live and bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("FLAKE_REPLAY_ONLY") != "1"
    secure_cookie = os.environ.get("FLAKE_COOKIE_SECURE", "1") == "1"
    live_max_usd = float(os.environ.get("FLAKE_LIVE_MAX_USD", "1.0"))  # model spend of live triage per process
    limiter = RateLimiter(int(os.environ.get("FLAKE_LIVE_PER_IP", "5")), 3600, int(os.environ.get("FLAKE_LIVE_DAILY", "100")))
    recent: OrderedDict[int, Row] = OrderedDict()  # live triage results, kept in memory only

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Any:
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        if request.url.path == "/report":  # a self-contained file: inline styles and an embedded font, no script
            resp.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; "
                "frame-ancestors 'none'; base-uri 'none'"
            )
        else:
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        return resp

    def page(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        base = {
            "path": request.url.path,
            "live": live,
            "version": __version__,
            "data_until": st.rows[0].f.created_at if st.rows else "",
        }
        return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status)

    def row_of(job_id: int) -> Row:
        row = st.by_id.get(job_id) or recent.get(job_id)
        if row is None:
            raise HTTPException(404, "No failure with this job id in the collected data.")
        return row

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "There is no page at this address."
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": message}, status_code=exc.status_code)
        return page(request, "error.html", status=exc.status_code, code=exc.status_code, message=message)

    @app.exception_handler(RequestValidationError)
    async def bad_input(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "invalid parameter"}, status_code=400)
        return page(request, "error.html", status=404, code=404, message="There is no page at this address.")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        hold = st.summary("holdout")
        attention: list[Row] = []
        seen: set[str] = set()
        for r in st.rows:  # newest first, one row per test
            if r.final.decision == "no_retry" and r.final.tests and r.final.tests[0] not in seen:
                seen.add(r.final.tests[0])
                attention.append(r)
            if len(attention) == 6:
                break
        return page(
            request,
            "home.html",
            s=hold,
            dev=st.summary("dev"),
            chart=chart_days(st.daily(), st.holdout_from),
            flaky=st.flaky_tests(3)[:6],
            attention=attention,
            report=st.report,
        )

    @app.get("/failures", response_class=HTMLResponse)
    def failures(
        request: Request,
        period: str = "all",
        decision: str = "",
        kind: str = "",
        outcome: str = "",
        rule: str = "",
        job: str = "",
        q: str = "",
        page_n: int = 1,
    ) -> HTMLResponse:
        period = period if period in ("all", "dev", "holdout") else "all"
        decision = decision if decision in DECISION_LABEL else ""
        kind = kind if kind in KIND_LABEL else ""
        outcome = outcome if outcome in OUTCOME_LABEL else ""
        rule = rule if rule in triage.RULES else ""
        q = q[:120]
        rows = st.filter(period=period, decision=decision, kind=kind, outcome=outcome, rule=rule, job=job, q=q)
        pages = max(1, math.ceil(len(rows) / PAGE))
        page_n = min(max(1, page_n), pages)
        params = {
            k: v
            for k, v in {
                "period": period,
                "decision": decision,
                "kind": kind,
                "outcome": outcome,
                "rule": rule,
                "job": job,
                "q": q,
            }.items()
            if v and v != "all"
        }
        return page(
            request,
            "failures.html",
            rows=rows[(page_n - 1) * PAGE : page_n * PAGE],
            total=len(rows),
            counts=Counter(r.final.decision for r in rows),
            page_n=page_n,
            pages=pages,
            f={"period": period, "decision": decision, "kind": kind, "outcome": outcome, "rule": rule, "job": job, "q": q},
            families=st.families(),
            qs=lambda **kw: "?" + urlencode({**params, **kw}),
        )

    @app.get("/failures/{job_id}", response_class=HTMLResponse)
    def failure(request: Request, job_id: int) -> HTMLResponse:
        row = row_of(job_id)
        history = [
            {
                "test": t,
                "flaked": st.history.flaked.get(t, 0),
                "persisted": st.history.persisted.get(t, 0),
                "known": t in st.tests,
            }
            for t in row.final.tests[:10]
        ]
        return page(
            request,
            "failure.html",
            r=row,
            lines=excerpt_view(row),
            history=history,
            markdown=cli.as_markdown(row.f, row.final),
            live_row=job_id in recent and job_id not in st.by_id,
        )

    @app.get("/tests", response_class=HTMLResponse)
    def tests(request: Request, min_flakes: int = 2) -> HTMLResponse:
        min_flakes = min(max(1, min_flakes), 50)
        return page(request, "tests.html", tests=st.flaky_tests(min_flakes), min_flakes=min_flakes)

    @app.get("/test", response_class=HTMLResponse)
    def test(request: Request, name: str) -> HTMLResponse:
        t = st.tests.get(name)
        if t is None:
            raise HTTPException(404, "This test never failed in the collected data.")
        rows = [st.by_id[i] for i in t.rows]
        drafts = [d for d in cli.issue_drafts_from([(r.f, r.final) for r in reversed(rows)], 1) if d["test"] == name]
        return page(request, "test.html", t=t, rows=rows, draft=drafts[0] if drafts else None)

    @app.get("/triage", response_class=HTMLResponse)
    def triage_form(request: Request) -> HTMLResponse:
        token = secrets.token_urlsafe(24)
        resp = page(request, "triage.html", csrf=token, error=None, url="", examples=examples())
        resp.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="strict", secure=secure_cookie, max_age=3600)
        return resp

    def examples() -> list[Row]:
        picks: dict[str, Row] = {}
        for r in st.rows:
            if r.scored and r.period == "holdout":
                picks.setdefault(f"{r.final.rule}:{r.f.label}", r)
        order = ("flaked_before:flake", "build:persists", "model:persists", "infra:flake", "change_touches_test:persists")
        return [picks[k] for k in order if k in picks]

    @app.post("/triage", response_class=HTMLResponse)
    def triage_post(request: Request, url: str = Form(""), csrf: str = Form("")) -> Response:
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not cookie or not hmac.compare_digest(cookie.encode(), csrf.encode()):
            raise HTTPException(403, "The form expired. Open it again and resend.")
        url = url.strip()[:300]

        def again(error: str, status: int = 400) -> HTMLResponse:
            return page(request, "triage.html", status=status, csrf=cookie, error=error, url=url, examples=examples())

        parsed = cli.parse_job_url(url)
        if not parsed:
            return again("Paste the address of a job: https://github.com/OWNER/REPO/actions/runs/RUN/job/JOB")
        repo, _run_id, job_id = parsed
        if job_id in st.by_id or job_id in recent:
            return Response(status_code=303, headers={"Location": f"/failures/{job_id}"})
        if not live:
            return again("This job is not in the collected data, and live triage is off on this server.", 404)
        if repo.lower() not in live_repos:
            return again(f"Live triage covers {', '.join(sorted(live_repos))} only.")
        ip = request.client.host if request.client else "?"
        if blocked := limiter.allow(ip):
            return again(blocked, 429)
        try:
            f = cli.fetch_failure(url, os.environ["GITHUB_TOKEN"], patient=False)
        except (SystemExit, KeyError, OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            return again(f"GitHub did not return this job ({type(exc).__name__}). Is it public, finished, and in this run?", 502)
        row = st.triage_live(f, model=live_model, max_usd=live_max_usd)
        recent[job_id] = row
        while len(recent) > 200:
            recent.popitem(last=False)
        return Response(status_code=303, headers={"Location": f"/failures/{job_id}"})

    @app.get("/quality", response_class=HTMLResponse)
    def quality(request: Request) -> HTMLResponse:
        return page(
            request,
            "quality.html",
            rep=st.report,
            rules_hold=st.by_rule("holdout"),
            rules_dev=st.by_rule("dev"),
            sim=st.simulation,
            fresh=st.fresh,
            cassette=st.cassette,
        )

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request) -> HTMLResponse:
        kinds = Counter(r.final.kind for r in st.rows)
        labels = Counter(r.f.label for r in st.rows)
        return page(
            request, "status.html", st=st, kinds=kinds, labels=labels, live_model=live_model, live_repos=sorted(live_repos)
        )

    @app.get("/report")
    def report() -> FileResponse:
        path = HOME / "docs" / "report.html"
        if not path.exists():
            raise HTTPException(404, "The report has not been built.")
        return FileResponse(path, media_type="text/html")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "failures": len(st.rows), "version": __version__}

    # ── JSON API: the same data for scripts and dashboards ──────────────────────────────────────

    def as_json(row: Row) -> dict[str, Any]:
        d = row.final
        return {
            "job_id": row.f.job_id,
            "job": row.f.job,
            "url": row.f.url,
            "created_at": row.f.created_at,
            "decision": d.decision,
            "kind": d.kind,
            "rule": d.rule,
            "reason": d.reason,
            "source": d.source,
            "tests": d.tests,
            "evidence": [{"line": s.line, "text": s.text} for s in d.evidence],
            "outcome": row.f.label,
            "rules_decision": row.rules.decision,
            "period": row.period,
        }

    @app.get("/api/summary")
    def api_summary(period: str = "holdout") -> dict[str, Any]:
        if period not in ("all", "dev", "holdout"):
            raise HTTPException(400, "period is all, dev or holdout")
        return st.summary(period)

    @app.get("/api/failures/{job_id}")
    def api_failure(job_id: int) -> dict[str, Any]:
        return as_json(row_of(job_id))

    @app.get("/api/tests")
    def api_tests(min_flakes: int = 2) -> list[dict[str, Any]]:
        return [
            {
                "test": t.name,
                "flaked": t.flaked,
                "failed_again": t.persisted,
                "not_rerun": t.not_rerun,
                "first": t.first,
                "last": t.last,
                "jobs": dict(t.jobs),
            }
            for t in st.flaky_tests(min(max(1, min_flakes), 50))
        ]

    return app


def main() -> None:
    import uvicorn

    uvicorn.run("flaketriage.web:create_app", factory=True, host="127.0.0.1", port=int(os.environ.get("PORT", "8070")))


if __name__ == "__main__":
    main()
