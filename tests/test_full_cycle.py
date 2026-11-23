"""The whole circle on one synthetic project: GitHub API -> collector -> dataset -> evals -> console ->
web pages, JSON API, CLI, MCP tools and the shadow-mode simulation, with a fake model in the loop.

Every layer must tell the same story about the same failures: the numbers on the overview page are the
numbers the evals scored, the decision the MCP tool returns is the one the page shows.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from conftest import COMPILE_FAIL, NETWORK_FAIL, NOTHING, PLAYWRIGHT_FAIL, STAMP, SUREFIRE_FAIL
from fastapi.testclient import TestClient

from flaketriage import cli, collect, console, diagnose, evals, llm, mcp_server, triage, web

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
OTHER_FAIL = [
    x.replace("ThreatArsenalApiTest", "ScenarioApiTest").replace(
        "given_groupByPrimaryDomain_should_returnActionsInDomainOrder", "given_viewer_should_get_403"
    )
    for x in SUREFIRE_FAIL
]
TOUCH_FAIL = [
    x.replace("ThreatArsenalApiTest", "InjectApiTest").replace(
        "given_groupByPrimaryDomain_should_returnActionsInDomainOrder", "given_inject_should_save"
    )
    for x in SUREFIRE_FAIL
]
DEPENDENT_FAIL = [
    "##[group]Run report",
    "##[endgroup]",
    "Required producer did not succeed: Backend Compile",
    "##[error]Process completed with exit code 1.",
]


def day(k: int, hour: int) -> datetime:
    return NOW - timedelta(days=k) + timedelta(hours=hour - 12)


# (run id, days ago, hour, job name, log lines, later attempts' conclusions, changed files)
WORLD: list[tuple[int, int, int, str, list[str], list[str], list[str]]] = [
    (101, 5, 8, "pipeline / API Tests (elk8-1)", SUREFIRE_FAIL, ["success"], []),
    (102, 5, 9, "pipeline / Backend Compile", COMPILE_FAIL, ["failure"], []),
    (103, 5, 10, "pipeline / Front Install", NETWORK_FAIL, ["success"], []),
    (104, 4, 8, "pipeline / API Tests (elk8-1)", SUREFIRE_FAIL, ["success"], []),
    (105, 4, 9, "pipeline / E2E (chromium)", PLAYWRIGHT_FAIL, ["failure"], []),
    (106, 4, 10, "pipeline / Odd Script", NOTHING, ["failure"], []),
    (107, 4, 11, "pipeline / API Tests (elk8-2)", OTHER_FAIL, [], []),  # nobody re-ran it
    (108, 3, 8, "pipeline / API Tests (elk8-2)", OTHER_FAIL, ["failure"], []),
    (109, 3, 10, "pipeline / API Tests (elk8-2)", OTHER_FAIL, ["failure"], []),
    (110, 1, 8, "pipeline / API Tests (elk8-1)", SUREFIRE_FAIL, ["success"], []),  # flaked before -> retry
    (111, 1, 9, "pipeline / Backend Compile", COMPILE_FAIL, ["failure"], []),  # build -> do not retry
    (112, 1, 10, "pipeline / API Tests (elk8-2)", OTHER_FAIL, ["failure"], []),  # stuck test -> do not retry
    (113, 1, 11, "pipeline / Report", DEPENDENT_FAIL, ["failure"], []),  # dependent -> left out
    (
        114,
        1,
        12,
        "pipeline / API Tests (elk8-3)",
        TOUCH_FAIL,
        ["failure"],
        ["openaev-api/src/test/java/io/openaev/InjectApiTest.java"],
    ),
    (115, 1, 13, "pipeline / Odd Script", NOTHING, ["success"], []),  # unsure -> the model is asked
    (116, 0, 9, "pipeline / Backend Compile", COMPILE_FAIL, [], []),
]
HOLDOUT = (NOW - timedelta(days=1)).replace(hour=0).isoformat()


def job(jid: int, name: str, conclusion: str, start: datetime) -> dict[str, Any]:
    return {
        "id": jid,
        "name": name,
        "conclusion": conclusion,
        "started_at": start.isoformat().replace("+00:00", "Z"),
        "completed_at": (start + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "html_url": f"https://github.com/o/r/actions/runs/{jid // 10}/job/{jid}",
        "steps": [{"name": "Run tests", "conclusion": conclusion}],
        "run_attempt": 1,
        "run_id": jid // 10,
    }


def world() -> httpx.MockTransport:
    runs, jobs, logs, files = {}, {}, {}, {}
    for rid, ago, hour, name, lines, later, changed in WORLD:
        t = day(ago, hour)
        runs[rid] = {
            "id": rid,
            "name": "[Core] CI",
            "event": "pull_request",
            "head_branch": f"feat/{rid}",
            "head_sha": f"{rid:07d}abc",
            "run_attempt": 1 + len(later),
            "conclusion": later[-1] if later else "failure",
            "created_at": t.isoformat().replace("+00:00", "Z"),
            "html_url": f"https://github.com/o/r/actions/runs/{rid}",
            "pull_requests": [{"number": rid, "base": {"sha": "base"}}],
        }
        first = rid * 10 + 1
        jobs[(rid, 1)] = [job(first, name, "failure", t)]
        for a, c in enumerate(later, start=2):
            jobs[(rid, a)] = [job(rid * 10 + a, name, c, t + timedelta(hours=a))]
        logs[first] = "\n".join(STAMP + x for x in lines)
        files[rid] = [{"filename": f} for f in changed]

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/runs") and "/workflows/" in p:
            d = req.url.params.get("created")
            return httpx.Response(200, json={"workflow_runs": [r for r in runs.values() if r["created_at"][:10] == d]})
        if m := re.search(r"/runs/(\d+)/attempts/(\d+)/jobs$", p):
            return httpx.Response(200, json={"jobs": jobs.get((int(m[1]), int(m[2])), [])})
        if m := re.search(r"/jobs/(\d+)/logs$", p):
            text = logs.get(int(m[1]))
            return httpx.Response(200, content=text.encode()) if text else httpx.Response(410)
        if m := re.search(r"/actions/jobs/(\d+)$", p):
            jid = int(m[1])
            return httpx.Response(200, json=jobs[(jid // 10, 1)][0])
        if m := re.search(r"/actions/runs/(\d+)$", p):
            return httpx.Response(200, json=runs[int(m[1])])
        if "/compare/" in p:
            rid = int(re.search(r"\.\.\.(\d{7})", p)[1])  # type: ignore[index]
            return httpx.Response(200, json={"files": files.get(rid, [])})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def fake_model(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answers 'do not retry' and quotes the one line the odd script printed."""
    asked: list[str] = []

    def create(**req: Any) -> Any:
        text = req["messages"][0]["content"]
        asked.append(text)
        rows = re.findall(r"^(\d+)\| (.*)$", text, re.M)
        odd = [int(n) for n, t in rows if "something odd happened" in t]
        if odd:
            body = {
                "decision": "no_retry",
                "category": "regression",
                "confidence": 0.8,
                "summary": "The script fails the same way.",
                "evidence": [{"line": odd[0], "quote": "something odd happened"}],
            }
        else:  # a test with no history: say retry, but quote nothing -> the answer must be dropped
            body = {"decision": "retry", "category": "unclear", "confidence": 0.3, "summary": "Not clear.", "evidence": []}
        return SimpleNamespace(
            stop_reason="end_turn",
            model="claude-haiku-5-5",
            content=[SimpleNamespace(type="text", text=json.dumps(body))],
            usage=SimpleNamespace(input_tokens=900, output_tokens=120),
        )

    monkeypatch.setattr(anthropic, "Anthropic", lambda **_k: SimpleNamespace(messages=SimpleNamespace(create=create)))
    return asked


@pytest.fixture(scope="module")
def cycle(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("cycle")
    data = tmp / "data"
    mp.setenv("GITHUB_TOKEN", "test")
    mp.setenv("ANTHROPIC_API_KEY", "test")
    mp.delenv("FLAKE_REPLAY_ONLY", raising=False)
    mp.setattr(collect.time, "sleep", lambda _s: None)
    mp.setattr(llm, "CASSETTE", tmp / "cassette")
    mp.setattr(evals, "HOLDOUT_FROM", HOLDOUT)
    asked = fake_model(mp)
    meta = collect.collect("o/r", [1], 6, data, 100, transport=world())
    report = evals.run(data, True, None, False, 3.0, HOLDOUT)  # the model answers once, into the cassette
    mp.setenv("FLAKE_REPLAY_ONLY", "1")  # from here on everything replays
    store = console.Store(data, HOLDOUT, report=report)
    yield {"data": data, "meta": meta, "report": report, "store": store, "asked": asked, "mp": mp, "tmp": tmp}
    mp.undo()


def decision_of(store: console.Store, rid: int) -> triage.Decision:
    return store.by_id[rid * 10 + 1].final


def test_collector_saw_the_whole_world(cycle: dict[str, Any]) -> None:
    assert cycle["meta"]["runs_with_failure"] == len(WORLD) and cycle["meta"]["runs_skipped"] == 0
    labels = {r.f.job_id // 10: r.f.label for r in cycle["store"].rows}
    assert labels[107] == labels[116] == "unlabelled" and labels[110] == "flake" and labels[111] == "persists"


def test_every_rule_fires_where_it_should(cycle: dict[str, Any]) -> None:
    st = cycle["store"]
    expect = {
        101: ("unsure", "test_unknown"),  # no history yet; model asked? only on unsure test with label -> see below
        102: ("no_retry", "build"),
        103: ("retry", "infra"),
        104: ("retry", "flaked_before"),  # 101's retry had finished
        105: ("retry", "timeout"),
        110: ("retry", "flaked_before"),
        111: ("no_retry", "build"),
        112: ("no_retry", "stuck_tests"),  # 108 and 109 failed again on every retry
        114: ("no_retry", "change_touches_test"),
    }
    for rid, (dec, rule) in expect.items():
        r = st.by_id[rid * 10 + 1]
        assert (r.rules.decision, r.rules.rule) == (dec, rule), rid
    assert st.by_id[1131].dependent and st.by_id[1131].rules.rule == "dependent"


def test_the_model_only_decides_what_the_rules_left_unsure(cycle: dict[str, Any]) -> None:
    st = cycle["store"]
    asked = [r for r in st.rows if r.model == "answered"]
    assert asked and all(r.rules.decision == "unsure" for r in asked)
    odd = st.by_id[1151]
    assert odd.final.rule == "model" and odd.final.decision == "no_retry"
    assert odd.final.evidence[0].text == "something odd happened"
    assert all("Required producer" not in a for a in cycle["asked"])  # dependent jobs are never sent
    dropped = st.by_id[1011]  # the fake model answered "retry" for it without a quote
    assert dropped.model == "dropped" and dropped.final is dropped.rules and "dropped" not in dropped.final.reason


def test_console_numbers_are_the_evals_numbers(cycle: dict[str, Any]) -> None:
    st, rep = cycle["store"], cycle["report"]
    for period in ("dev", "holdout"):
        s, e = st.summary(period), rep["policies"]["rules+model"][period]
        assert (s["failures"], s["flakes"], s["persists"]) == (e["failures"], e["flakes"], e["persists"])
        assert (s["flakes_retried"], s["skipped"], s["missed"]) == (e["flakes_retried"], e["persist_skipped"], e["missed_flakes"])
        assert (s["wasted_min"], s["saved_min"]) == (e["retry_minutes_wasted"], e["retry_minutes_saved"])
        assert s["blind_wasted_min"] == rep["policies"]["blind"][period]["retry_minutes_wasted"]


def test_web_pages_tell_the_same_story(cycle: dict[str, Any]) -> None:
    st = cycle["store"]
    c = TestClient(web.create_app(st), base_url="https://testserver")
    s = st.summary("holdout")
    home = c.get("/").text
    assert f"{s['skipped']}<small>of {s['persists']}</small>" in home
    assert f"{s['flakes_retried']}<small>of {s['flakes']}</small>" in home
    page = c.get("/failures/1151").text
    assert "something odd happened" in page and 'class="ln cited"' in page and "Do not retry" in page
    assert c.get("/api/failures/1121").json()["rule"] == "stuck_tests"
    assert c.get("/api/summary").json() == s
    flaky = c.get("/api/tests?min_flakes=1").json()
    assert flaky[0]["test"].startswith("ThreatArsenalApiTest") and flaky[0]["flaked"] == 3
    test_page = c.get("/test", params={"name": flaky[0]["test"]}).text
    assert "Flaky: ThreatArsenalApiTest" in test_page
    listing = c.get("/failures", params={"rule": "build", "period": "holdout"}).text
    assert "/failures/1111" in listing and "/failures/1101" not in listing
    for path in ("/tests", "/quality", "/status", "/triage", "/failures?outcome=unlabelled"):
        assert c.get(path).status_code == 200, path


def test_triage_form_known_unknown_and_live(cycle: dict[str, Any]) -> None:
    st, mp = cycle["store"], cycle["mp"]
    c = TestClient(web.create_app(st), base_url="https://testserver")
    token = c.get("/triage").cookies.get(web.CSRF_COOKIE)
    known = c.post(
        "/triage", data={"url": "https://github.com/o/r/actions/runs/111/job/1111", "csrf": token}, follow_redirects=False
    )
    assert known.status_code == 303 and known.headers["location"] == "/failures/1111"
    off = c.post("/triage", data={"url": "https://github.com/o/r/actions/runs/999/job/9991", "csrf": token})
    assert off.status_code == 404 and "live triage is off" in off.text
    assert c.post("/triage", data={"url": "https://github.com/o/r/actions/runs/1/job/2", "csrf": "forged"}).status_code == 403
    assert c.post("/triage", data={"url": "javascript:alert(1)", "csrf": token}).status_code == 400

    mp.setenv("FLAKE_LIVE", "1")
    mp.setenv("FLAKE_LIVE_REPOS", "o/r")
    real = collect.GitHub
    mp.setattr(cli, "GitHub", lambda token, cache, patient=True: real(token, cycle["tmp"] / "live", world(), patient))
    live = TestClient(web.create_app(st), base_url="https://testserver")
    token = live.get("/triage").cookies.get(web.CSRF_COOKIE)
    other = live.post("/triage", data={"url": "https://github.com/x/y/actions/runs/1/job/2", "csrf": token})
    assert other.status_code == 400 and "covers o/r only" in other.text
    kept = st.by_id.pop(1121)  # pretend this job is new: fetched from the fake GitHub and triaged with all history
    try:
        r = live.post(
            "/triage", data={"url": "https://github.com/o/r/actions/runs/112/job/1121", "csrf": token}, follow_redirects=False
        )
        assert r.status_code == 303
        page = live.get("/failures/1121").text
        assert "Triaged live" in page and "Do not retry" in page  # ScenarioApiTest is stuck in the history
    finally:
        mp.delenv("FLAKE_LIVE")
        st.by_id[1121] = kept


def test_cli_and_mcp_agree_with_the_console(cycle: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    st, mp, data = cycle["store"], cycle["mp"], cycle["data"]
    mp.setattr(sys, "argv", ["flake-triage", "issues", "--data", str(data), "--min", "2"])
    cli.main()
    drafts = json.loads(capsys.readouterr().out)
    assert [d["test"].split(".")[0] for d in drafts] == ["ThreatArsenalApiTest"] and drafts[0]["flakes"] == 3

    mp.setattr(mcp_server, "_store", st)
    for jid in (1111, 1151, 1141):
        assert mcp_server.failure(jid)["decision"] == st.by_id[jid].final.decision
    assert mcp_server.triage_job("https://github.com/o/r/actions/runs/111/job/1111")["source"] == "collected data"
    assert "error" in mcp_server.triage_job("http://evil.example/o/r/actions/runs/1/job/2")
    assert mcp_server.flaky_tests(1)[0]["flaked"] == 3
    assert mcp_server.summary("holdout") == st.summary("holdout")
    assert "error" in mcp_server.summary("tomorrow") and "error" in mcp_server.failure(1)

    # live: a job that is not in the data is fetched from (the fake) GitHub and triaged with all history
    real = collect.GitHub
    mp.setattr(cli, "GitHub", lambda token, cache, patient=True: real(token, cycle["tmp"] / "mcp", world(), patient))
    mp.setenv("FLAKE_LIVE_REPOS", "o/r")
    url = "https://github.com/o/r/actions/runs/112/job/1121"
    row = st.by_id.pop(1121)
    try:
        assert "live triage is off" in mcp_server.triage_job(url)["error"]
        mp.setenv("FLAKE_LIVE", "1")
        live = mcp_server.triage_job(url)
        assert live["source"] == "live" and live["decision"] == "no_retry" and live["rule"] == "stuck_tests"
        assert "only" in mcp_server.triage_job("https://github.com/x/y/actions/runs/1/job/2")["error"]
        mp.setattr(cli, "fetch_failure", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")))
        assert "did not return" in mcp_server.triage_job(url)["error"]
        mp.delenv("GITHUB_TOKEN")
        assert "GITHUB_TOKEN" in mcp_server.triage_job(url)["error"]
    finally:
        st.by_id[1121] = row
        mp.setenv("GITHUB_TOKEN", "test")
        mp.delenv("FLAKE_LIVE", raising=False)


def test_simulation_matches_the_evals(cycle: dict[str, Any]) -> None:
    spec = importlib.util.spec_from_file_location("simulate", ROOT / "scripts" / "simulate.py")
    assert spec and spec.loader
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    res = sim.simulate(cycle["store"], "holdout")
    by = {v["label"]: v for v in res["variants"]}
    pol = cycle["report"]["policies"]
    assert by["Retry everything (today)"]["wasted_min"] == pol["blind"]["holdout"]["retry_minutes_wasted"]
    assert by["Rules"]["wasted_min"] == pol["rules"]["holdout"]["retry_minutes_wasted"]
    assert by["Rules + model"]["missed"] == pol["rules+model"]["holdout"]["missed_flakes"]
    assert triage.STUCK_AFTER == 2  # the sensitivity runs put the default back


def test_replay_offline_gives_the_same_report(cycle: dict[str, Any]) -> None:
    again = evals.run(cycle["data"], True, None, True, 3.0, HOLDOUT)
    assert again["policies"] == cycle["report"]["policies"]
    assert diagnose.PROMPT_VERSION and len(list((llm.CASSETTE / "triage").glob("*.json"))) == len(cycle["asked"])


def test_live_triage_never_learns_from_its_own_run(cycle: dict[str, Any]) -> None:
    st = cycle["store"]
    test = "ScenarioApiTest.given_viewer_should_get_403"
    key = next(t for t in st.history.persisted if t.endswith(test))
    assert st.history_for(112).persisted[key] == st.history.persisted[key] - 1  # run 112's own label is left out
    assert st.history_for(999) is st.history  # a run that is not in the data uses all history


def test_pages_work_before_any_eval_ran(cycle: dict[str, Any]) -> None:
    st = cycle["store"]
    saved, st.report = st.report, {}
    try:
        c = TestClient(web.create_app(st), base_url="https://testserver")
        for path in ("/", "/quality", "/status"):
            assert c.get(path).status_code == 200, path
        assert "have not been run" in c.get("/quality").text
    finally:
        st.report = saved


def test_fresh_check_scores_only_what_came_after_the_collection(cycle: dict[str, Any], tmp_path: Path) -> None:
    import shutil

    spec = importlib.util.spec_from_file_location("fresh_check", ROOT / "scripts" / "fresh_check.py")
    assert spec and spec.loader
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
    main = tmp_path / "main"
    shutil.copytree(cycle["data"], main)
    meta = json.loads((main / "meta.json").read_text(encoding="utf-8"))
    meta["collected_at"] = (day(1, 10) - timedelta(minutes=30)).isoformat()  # as if collected mid-day: runs 112-116 are new
    (main / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    import gzip

    from flaketriage import dataset

    cut = datetime.fromisoformat(meta["collected_at"])
    # run 111 was still running at collection time: not in the old data, but older than the cut, so not fresh
    older = [
        x
        for x in dataset.read_runs(main).splitlines()
        if datetime.fromisoformat(json.loads(x)["created_at"]) <= cut and json.loads(x)["id"] != 111
    ]
    (main / "runs.jsonl.gz").write_bytes(gzip.compress(("\n".join(older) + "\n").encode()))  # a collection then had only these
    res = fc.run(main, cycle["data"], True, 0.1)
    st = cycle["store"]
    new = [r for r in st.rows if datetime.fromisoformat(r.f.created_at) > cut]
    assert res["fresh_failures"] == len(new) and {r.f.run_id for r in new} == {112, 113, 114, 115, 116}
    scored = [r for r in new if r.scored]
    assert res["policies"]["rules"]["failures"] == len(scored)
    assert res["policies"]["rules+model"]["persist_skipped"] == sum(r.f.label == "persists" and not r.retried for r in scored)
    assert res["model_new_spend_usd"] == 0  # replay only: nothing was asked
