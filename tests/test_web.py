"""The web console under hostile or odd input: escaping, CSRF, parameters, addresses, limits, GitHub failures."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_full_cycle import cycle  # noqa: F401  (the synthetic project, built once per module)

from flaketriage import cli, console, web


@pytest.fixture
def client(cycle: dict[str, Any]) -> TestClient:  # noqa: F811
    return TestClient(web.create_app(cycle["store"]), base_url="https://testserver")


def test_security_headers_everywhere(client: TestClient) -> None:
    for path in ("/", "/failures", "/nope", "/api/summary"):
        r = client.get(path)
        csp = r.headers["content-security-policy"]
        assert "script-src 'none'" in csp and "frame-ancestors 'none'" in csp and "style-src 'self'" in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


def test_report_gets_its_own_narrow_policy(client: TestClient) -> None:
    from flaketriage.paths import HOME

    if not (HOME / "docs" / "report.html").exists():
        pytest.skip("the report is not built in this checkout")
    r = client.get("/report")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script" not in csp


def test_log_text_is_escaped(cycle: dict[str, Any], client: TestClient) -> None:  # noqa: F811
    row = cycle["store"].by_id[1061]
    old = row.f.log
    row.f.log = old + "99| <script>alert('x')</script><img src=x onerror=alert(1)>\n"
    try:
        page = client.get("/failures/1061").text
        assert "<script>alert" not in page and "<img src=x" not in page
        assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in page
    finally:
        row.f.log = old


def test_odd_parameters_never_break_a_page(client: TestClient) -> None:
    for path in (
        "/failures?page_n=-5",
        "/failures?page_n=999999",
        "/failures?decision=<x>&kind=evil&outcome=x&rule=x&period=forever",
        "/failures?q=" + "a" * 10_000,
        "/failures?job=nonexistent",
        "/tests?min_flakes=-3",
        "/tests?min_flakes=100000",
    ):
        assert client.get(path).status_code == 200, path
    assert client.get("/failures?page_n=abc").status_code == 404
    assert client.get("/failures/not-a-number").status_code == 404
    assert client.get("/api/failures/not-a-number").json() == {"error": "invalid parameter"}
    assert client.get("/api/summary?period=x").status_code == 400
    assert client.get("/test?name=" + "x" * 5000).status_code == 404


def test_unknown_pages_are_html_and_api_errors_are_json(client: TestClient) -> None:
    r = client.get("/no/such/page")
    assert r.status_code == 404 and "no page at this address" in r.text and "<html" in r.text
    assert client.get("/api/failures/1").json()["error"].startswith("No failure")


def post(client: TestClient, url: str, csrf: str | None = None) -> Any:
    token = client.get("/triage").cookies.get(web.CSRF_COOKIE)
    return client.post("/triage", data={"url": url, "csrf": token if csrf is None else csrf}, follow_redirects=False)


def test_triage_rejects_lookalike_addresses(client: TestClient) -> None:
    for url in (
        "https://github.com.evil.example/o/r/actions/runs/1/job/2",
        "https://evil.example/github.com/o/r/actions/runs/1/job/2",
        "http://github.com/o/r/actions/runs/1/job/2",
        "github.com/o/r/actions/runs/1/job/2",
        "https://github.com/o/r/pull/7",
        "",
    ):
        assert post(client, url).status_code == 400, url


def test_triage_without_cookie_or_with_a_forged_token_is_refused(client: TestClient) -> None:
    assert post(client, "https://github.com/o/r/actions/runs/111/job/1111", csrf="").status_code == 403
    fresh = TestClient(client.app, base_url="https://testserver")
    r = fresh.post("/triage", data={"url": "https://github.com/o/r/actions/runs/111/job/1111", "csrf": "x"})
    assert r.status_code == 403 and "expired" in r.text


def test_live_triage_limits_and_github_failures(cycle: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("FLAKE_LIVE", "1")
    monkeypatch.setenv("FLAKE_LIVE_REPOS", "o/r")
    monkeypatch.setenv("FLAKE_LIVE_PER_IP", "2")

    def broken(url: str, token: str, patient: bool = True) -> Any:
        raise RuntimeError("GitHub is down")

    monkeypatch.setattr(cli, "fetch_failure", broken)
    c = TestClient(web.create_app(cycle["store"]), base_url="https://testserver")
    first = post(c, "https://github.com/o/r/actions/runs/900/job/9001")
    assert first.status_code == 502 and "GitHub did not return this job" in first.text
    post(c, "https://github.com/o/r/actions/runs/900/job/9002")
    third = post(c, "https://github.com/o/r/actions/runs/900/job/9003")
    assert third.status_code == 429 and "Too many requests" in third.text


def test_rate_limiter_window_day_and_memory() -> None:
    rl = web.RateLimiter(per_ip=2, window_s=60, daily=5, max_ips=3)
    assert rl.allow("a", 0) is None and rl.allow("a", 1) is None
    assert rl.allow("a", 2) is not None  # third in the window
    assert rl.allow("a", 100) is None  # window passed
    for ip in "bcd":
        rl.allow(ip, 200)
    assert len(rl.hits) <= 3  # bounded memory
    assert rl.allow("e", 300) is not None  # the daily cap (5) is reached


def test_excerpt_view_marks_cited_lines_and_gaps(cycle: dict[str, Any]) -> None:  # noqa: F811
    row = cycle["store"].by_id[1111]
    view = web.excerpt_view(row)
    cited = [x for x in view if x.get("cited")]
    assert cited and all(x["n"] in {s.line for s in row.final.evidence} for x in cited)
    assert all(("gap" in x) != ("n" in x) for x in view)


def test_daily_chart_geometry() -> None:
    days = [
        {"day": f"2026-11-{d:02d}", "blind_min": d * 100, "wasted_min": d * 40, "failures": 1, "missed": 0} for d in range(10, 20)
    ]
    ch = web.chart_days(days, "2026-11-15T00:00:00+00:00")
    assert len(ch["bars"]) == 10 and ch["cut"] is not None
    assert all(b["hw"] <= b["hb"] and b["yb"] >= 0 for b in ch["bars"])
    assert ch["ticks"][-1]["v"] >= 1900
    empty = web.chart_days([], "2026-11-15T00:00:00+00:00")
    assert empty["bars"] == [] and empty["cut"] is None


def test_formatting_helpers() -> None:
    assert web.minutes(None) == "—" and web.minutes(30) == "0.5 min" and web.minutes(1200) == "20 min"
    assert web.num(1234567) == "1 234 567" and web.num(None) == "—"
    assert web.pct(0.9534) == "95.3%" and web.pct(None) == "—"
    assert web.short_test("tests_e2e/tests/infra/a.spec.ts > Suite > t") == "a.spec.ts > Suite > t"
    assert web.short_test("io.x.FooTest.m") == "io.x.FooTest.m"


def test_store_views(cycle: dict[str, Any]) -> None:  # noqa: F811
    st: console.Store = cycle["store"]
    rules = {x["rule"]: x for x in st.by_rule("holdout")}
    assert rules["build"]["right_rate"] == 1.0 and rules["build"]["decision"] == "no_retry"
    assert "dependent" not in rules  # left out of every score
    assert st.filter(q="1111") and all(r.f.job_id == 1111 for r in st.filter(q="1111"))
    assert st.filter(decision="no_retry", outcome="flake", period="dev") == [r for r in st.period("dev") if r.missed]
    assert sum(d["failures"] for d in st.daily()) == st.summary("all")["failures"]


def test_a_github_http_error_is_a_502_page_not_a_crash(cycle: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import httpx

    from flaketriage import collect

    monkeypatch.setenv("FLAKE_LIVE", "1")
    monkeypatch.setenv("FLAKE_LIVE_REPOS", "o/r")
    gone = httpx.MockTransport(lambda _r: httpx.Response(404, json={"message": "Not Found"}))
    real = collect.GitHub
    monkeypatch.setattr(cli, "GitHub", lambda token, cache, patient=True: real(token, cache, gone, patient))
    c = TestClient(web.create_app(cycle["store"]), base_url="https://testserver")
    r = post(c, "https://github.com/o/r/actions/runs/900/job/9009")
    assert r.status_code == 502 and "GitHub did not return this job (HTTPStatusError)" in r.text


def test_a_job_from_another_run_is_refused(cycle: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import httpx

    from flaketriage import collect

    other = httpx.MockTransport(lambda r: httpx.Response(200, json={"id": 9009, "run_id": 1, "name": "x"}))
    real = collect.GitHub
    monkeypatch.setattr(cli, "GitHub", lambda token, cache, patient=True: real(token, cache, other, patient))
    with pytest.raises(ValueError, match="does not belong to run 900"):
        cli.fetch_failure("https://github.com/o/r/actions/runs/900/job/9009", "t")


def test_the_impatient_client_neither_sleeps_nor_reads_a_whole_huge_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import httpx

    from flaketriage import collect

    monkeypatch.setattr(collect.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError(f"slept {s}s")))
    limited = httpx.MockTransport(
        lambda _r: httpx.Response(429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"})
    )
    with pytest.raises(RuntimeError, match="rate limit"):
        collect.GitHub("t", tmp_path, limited, patient=False).json("/x")
    big = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"a" * 5000 + b"THE END"))
    tail = collect.GitHub("t", tmp_path, big, patient=False).log_text("o/r", 1, max_bytes=100)
    assert tail is not None and len(tail) == 100 and tail.endswith("THE END")


def test_job_addresses_are_parsed_strictly() -> None:
    ok = "https://github.com/OpenAEV-Platform/openaev/actions/runs/1/job/2"
    assert cli.parse_job_url(ok) == ("OpenAEV-Platform/openaev", 1, 2)
    for bad in (
        "https://github.com/evil?x=github.com/OpenAEV-Platform/openaev/actions/runs/1/job/3",
        "https://user@github.com/o/r/actions/runs/1/job/2",
        "https://github.com:8443/o/r/actions/runs/1/job/2",
        "https://github.com/o/r/actions/runs/1/job/2/../../x",
        "https://[::1/",
    ):
        assert cli.parse_job_url(bad) is None, bad
