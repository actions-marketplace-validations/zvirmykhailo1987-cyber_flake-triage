"""Collector, CLI, model layer and evals against a fake GitHub API and a fake model: no network."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from conftest import NETWORK_FAIL, STAMP, SUREFIRE_FAIL, make

from flaketriage import cli, collect, dataset, diagnose, evals, llm, triage

TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


def job(jid: int, name: str, conclusion: str) -> dict[str, Any]:
    return {
        "id": jid,
        "name": name,
        "conclusion": conclusion,
        "started_at": f"{TODAY}T{jid % 24:02d}:00:00Z",
        "completed_at": f"{TODAY}T{jid % 24:02d}:10:00Z",
        "html_url": f"https://github.com/o/r/actions/runs/100/job/{jid}",
        "steps": [{"name": "Run tests", "conclusion": conclusion}],
        "run_attempt": 1,
        "run_id": 100,
    }


RUN = {
    "id": 100,
    "name": "[Core] CI",
    "event": "pull_request",
    "head_branch": "feat/x",
    "head_sha": "abc1234",
    "run_attempt": 2,
    "conclusion": "success",
    "created_at": f"{TODAY}T00:00:01Z",
    "html_url": "https://github.com/o/r/actions/runs/100",
    "pull_requests": [{"number": 7, "base": {"sha": "base1"}}],
}
SUREFIRE_LOG = "\n".join(STAMP + x for x in SUREFIRE_FAIL)


def fake_github(calls: list[str], flaky: dict[str, int] | None = None) -> httpx.MockTransport:
    """A tiny GitHub: one run, a test job that failed then passed, a gate job, an expired log."""
    flaky = flaky if flaky is not None else {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        calls.append(path)
        if n := flaky.get(path):  # fail this path n times first
            flaky[path] = n - 1
            return httpx.Response(503)
        if path.endswith("/runs") and "workflows" in path:
            runs = [RUN] if req.url.params.get("created") == TODAY else []
            return httpx.Response(200, json={"workflow_runs": runs})
        if path.endswith("/actions/runs/100/jobs"):  # latest attempt of the run
            return httpx.Response(
                200,
                json={"jobs": [job(11, "API Tests", "failure"), job(12, "Pipeline Gate", "failure"), job(13, "Lint", "success")]},
            )
        if path.endswith("/attempts/1/jobs"):
            return httpx.Response(200, json={"jobs": [job(11, "API Tests", "failure"), job(12, "Pipeline Gate", "failure")]})
        if path.endswith("/attempts/2/jobs"):
            return httpx.Response(200, json={"jobs": [job(21, "API Tests", "success"), job(22, "Pipeline Gate", "success")]})
        if path.endswith("/jobs/11/logs"):
            return httpx.Response(200, content=SUREFIRE_LOG.encode())
        if path.endswith("/logs"):
            return httpx.Response(410)
        if path.endswith("/actions/jobs/11"):
            return httpx.Response(200, json=job(11, "API Tests", "failure"))
        if path.endswith("/actions/runs/100"):
            return httpx.Response(200, json=RUN)
        if "/compare/" in path or "/commits/" in path:
            return httpx.Response(200, json={"files": [{"filename": "openaev-api/src/test/ThreatArsenalApiTest.java"}]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect.time, "sleep", lambda _s: None)


@pytest.fixture
def collected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: None) -> Path:
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    meta = collect.collect("o/r", [1], 1, tmp_path, 100, transport=fake_github([]))
    assert meta["runs_with_failure"] == 1 and meta["runs_skipped"] == 0
    return tmp_path


def test_collect_writes_runs_and_logs(collected: Path) -> None:
    rows = [json.loads(x) for x in dataset.read_runs(collected).splitlines()]
    assert rows[0]["changed_files"] == ["openaev-api/src/test/ThreatArsenalApiTest.java"]
    assert (collected / "raw" / "11.log.gz").exists()
    fs = dataset.load(collected)
    assert [(f.job, f.label) for f in fs] == [("API Tests", "flake")]  # the gate job is dropped
    assert "ThreatArsenalApiTest" in fs[0].log and fs[0].retry_seconds == 600
    assert (collected / f"excerpts-{dataset.EXCERPT_VERSION}" / "11.txt").exists()
    assert dataset.load(collected)[0].log == fs[0].log  # second load reads the stored excerpt


def test_collect_uses_cache_on_second_pass(collected: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    collect.collect("o/r", [1], 1, collected, 100, transport=fake_github(calls))
    assert not any("/attempts/" in c or c.endswith("/logs") for c in calls)  # jobs cached, logs already on disk


def test_collect_needs_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        collect.collect("o/r", [1], 1, tmp_path, 10)


def test_collect_skips_a_run_github_keeps_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: None) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    flaky = {"/repos/o/r/actions/runs/100/attempts/1/jobs": 99}
    meta = collect.collect("o/r", [1], 1, tmp_path, 100, transport=fake_github([], flaky))
    assert meta["runs_skipped"] == 1 and meta["runs_with_failure"] == 0


def test_github_retries_5xx_rate_limit_and_dropped_connections(tmp_path: Path, no_sleep: None) -> None:
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("reset")
        if state["n"] == 2:
            return httpx.Response(500)
        if state["n"] == 3:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"})
        return httpx.Response(200, json={"ok": True})

    gh = collect.GitHub("t", tmp_path, httpx.MockTransport(handler))
    assert gh.json("/x") == {"ok": True} and state["n"] == 4
    assert gh.cached_json("a/b", "/x") == {"ok": True}
    assert gh.cached_json("a/b", "/never-called") == {"ok": True}  # from disk


def test_github_gives_up(tmp_path: Path, no_sleep: None) -> None:
    gh = collect.GitHub("t", tmp_path, httpx.MockTransport(lambda _r: httpx.Response(502)))
    with pytest.raises(RuntimeError):
        gh.json("/x")
    gone = collect.GitHub("t", tmp_path, httpx.MockTransport(lambda _r: httpx.Response(404)))
    assert gone.log_text("o/r", 1) is None


def test_expired_logs_stop_the_run_early(tmp_path: Path) -> None:
    calls: list[str] = []
    gh = collect.GitHub("t", tmp_path / "c", fake_github(calls))
    run = collect.slim_run(RUN)
    run["attempts"] = 1
    many = [job(30 + i, f"Job {i}", "failure") for i in range(6)]

    def jobs(_name: str, _url: str, **_p: Any) -> dict[str, Any]:
        return {"jobs": many}

    gh.cached_json = jobs  # type: ignore[method-assign]
    gh.json = lambda *_a, **_k: {"files": []}  # type: ignore[method-assign]
    (tmp_path / "raw").mkdir()
    rec = collect.collect_run(gh, "o/r", {**run, "base_sha": None}, tmp_path / "raw", 100)
    assert sum(c.endswith("/logs") for c in calls) == 3  # three expired in a row, the other three skipped
    assert len(rec["jobs"][0]) == 6


def test_collect_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(collect, "collect", lambda *a: seen.setdefault("a", a) and {"ok": 1})
    monkeypatch.setattr(sys, "argv", ["x", "--days", "3", "--out", str(tmp_path)])
    collect.main()
    assert seen["a"][1] == [245579469, 247641507] and seen["a"][2] == 3


# ---------- model layer ----------


@pytest.fixture
def cassette(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(llm, "CASSETTE", tmp_path / "cassette")
    monkeypatch.delenv("FLAKE_REPLAY_ONLY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    return tmp_path / "cassette"


class FakeClient:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **req: Any) -> Any:
        self.requests.append(req)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def message(text: str, stop: str = "end_turn") -> Any:
    return SimpleNamespace(
        stop_reason=stop,
        model="claude-haiku-5-5",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
    )


def use(monkeypatch: pytest.MonkeyPatch, reply: Any) -> FakeClient:
    client = FakeClient(reply)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_k: client)
    return client


def call(**kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    args = {"purpose": "t", "prompt_version": "v1", "system": "s", "text": "x", "schema": diagnose.SCHEMA, "cache_id": "1"}
    return llm.call_json(**{**args, **kw})


def test_llm_answers_and_keeps_the_answer(cassette: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = use(monkeypatch, message('{"decision": "retry"}'))
    ans, meta = call()
    assert ans == {"decision": "retry"} and meta["cached"] is False
    assert meta["cost_usd"] == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6)
    schema = client.requests[0]["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(diagnose.SCHEMA["properties"])
    assert schema["properties"]["evidence"]["items"]["additionalProperties"] is False
    use(monkeypatch, RuntimeError("must not be called"))
    again, meta2 = call()
    assert again == ans and meta2["cached"] is True


@pytest.mark.parametrize(
    "reply",
    [
        message("{}", stop="refusal"),
        message('{"decision"', stop="max_tokens"),
        message("not json"),
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
    ],
)
def test_llm_failures_become_unavailable(cassette: Path, monkeypatch: pytest.MonkeyPatch, reply: Any) -> None:
    use(monkeypatch, reply)
    with pytest.raises(llm.LLMUnavailable):
        call()
    assert not cassette.exists() or not any(cassette.rglob("*.json"))


def test_llm_off_without_key_or_in_replay(cassette: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, RuntimeError("must not be called"))
    with pytest.raises(llm.LLMUnavailable):
        call(replay_only=True)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(llm.LLMUnavailable, match="no API key"):
        call()


def test_llm_read_only_disk_keeps_the_answer(cassette: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, message('{"decision": "retry"}'))
    cassette.parent.mkdir(parents=True, exist_ok=True)
    cassette.write_text("a file where a folder should be", encoding="utf-8")
    ans, meta = call()
    assert ans == {"decision": "retry"} and meta["cassette"] == "not written"


def test_load_local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "HOME", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env.local").write_text("OTHER=1\nANTHROPIC_API_KEY= sk-test \n", encoding="utf-8")
    llm.load_local_env()
    assert llm.os.environ["ANTHROPIC_API_KEY"] == "sk-test"


def unsure() -> dataset.Failure:
    return make(
        [
            "##[group]Run ./script.sh",
            "##[endgroup]",
            "Something odd: worker 3 stopped responding",
            "##[error]Process completed with exit code 2.",
        ]
    )


def test_model_decides_unsure_only_with_a_verified_quote(cassette: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = unsure()
    line = next(n for n, t in diagnose.lines_of(f.log).items() if "worker 3" in t)
    good = {
        "decision": "retry",
        "category": "infrastructure",
        "confidence": 0.8,
        "summary": "Worker died.",
        "evidence": [{"line": line, "quote": "worker 3 stopped responding"}],
    }
    use(monkeypatch, message(json.dumps(good)))
    spend = [0.0]
    d = diagnose.with_model(budget=spend)(f, triage.History())
    assert d.decision == "retry" and d.source == "model:infrastructure:0.80" and d.evidence[0].line == line
    assert spend[0] > 0

    bad = {**good, "decision": "no_retry", "evidence": [{"line": line, "quote": "a line that is not in the log at all"}]}
    use(monkeypatch, message(json.dumps(bad)))
    f2 = unsure()
    f2.job_id = 2
    d2 = diagnose.with_model()(f2, triage.History())
    assert d2.decision == triage.UNSURE and "no quote matched" in d2.reason


def test_model_not_asked_when_rules_know(cassette: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, RuntimeError("must not be called"))
    d = diagnose.with_model()(make(NETWORK_FAIL), triage.History())
    assert d.decision == triage.RETRY and d.source == "rules"
    f = unsure()
    d2 = diagnose.with_model(replay_only=True)(f, triage.History())
    assert d2.decision == triage.UNSURE and "Model unavailable" in d2.reason


def test_prompt_is_bounded_and_carries_history() -> None:
    f = unsure()
    f.log = "".join(f"{i}| line {i}\n" for i in range(1, 500))
    h = triage.History()
    h.learn(["A.b"], "flake")
    d = triage.Decision(triage.UNSURE, "test", "why", tests=["A.b"])
    p = diagnose.prompt(f, d, h)
    assert "A.b: flaked 1x" in p and "499| line 499" in p and "300| line 300" not in p


# ---------- evals and CLI ----------


def test_score_and_policies() -> None:
    flake_retried = (make(NETWORK_FAIL, "flake", job_id=1), triage.Decision(triage.RETRY, "infra", ""))
    flake_missed = (make(NETWORK_FAIL, "flake", job_id=2, retry_seconds=300), triage.Decision(triage.NO_RETRY, "build", ""))
    persist_saved = (make(NETWORK_FAIL, "persists", job_id=3, retry_seconds=1200), triage.Decision(triage.NO_RETRY, "build", ""))
    persist_wasted = (make(NETWORK_FAIL, "persists", job_id=4, retry_seconds=600), triage.Decision(triage.UNSURE, "test", ""))
    dependent = (make(NETWORK_FAIL, "flake", job_id=5), triage.Decision(triage.RETRY, "any", ""))  # e.g. blind's decision
    s = evals.score([flake_retried, flake_missed, persist_saved, persist_wasted, dependent], exclude={5})
    assert s["failures"] == 4 and s["dependent_excluded"] == 1 and s["runs"] == 4
    assert s["flake_recall"] == 0.5 and s["missed_flakes"] == 1 and s["missed_flake_minutes"] == 5
    assert s["persist_skip_rate"] == 0.5 and s["retry_minutes_saved"] == 20 and s["retry_minutes_wasted"] == 10
    assert s["no_retry_precision"] == 0.5
    assert evals.score([])["flake_recall"] is None
    assert evals.wilson(0, 0) is None
    ci = evals.wilson(8, 10)
    assert ci is not None and ci[0] < 0.8 < ci[1]


def test_every_policy_is_scored_on_the_same_failures() -> None:
    dep = make(["##[group]Run x", "##[endgroup]", "Required producer did not succeed: Backend Compile", "##[error]exit 1"])
    assert evals.dependent_ids([dep, make(NETWORK_FAIL, job_id=2)]) == {1}


def test_split_by_time() -> None:
    old = make(NETWORK_FAIL, when="2026-11-01T10:00:00+00:00")
    new = make(NETWORK_FAIL, when="2026-11-20T10:00:00+00:00", job_id=2)
    dev, hold, cut = evals.split([old, new])
    assert dev == [old] and hold == [new] and cut.startswith("2026-11-16")


def test_eval_run_and_main(
    collected: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(evals, "EVALS", tmp_path / "evals")
    monkeypatch.setattr(sys, "argv", ["x", "--data", str(collected)])
    evals.main()
    out = json.loads(next((tmp_path / "evals" / "results").glob("eval-*.json")).read_text(encoding="utf-8"))
    assert out["labelled_with_log"] == 1 and set(out["policies"]) == {"blind", "rules"}
    assert out["policies"]["blind"]["all"]["flake_recall"] == 1.0
    monkeypatch.setattr(sys, "argv", ["x", "--data", str(collected), "--verify"])
    evals.main()  # same data, same code: the numbers match the stored report
    latest = tmp_path / "evals" / "results" / "latest.json"
    stored = json.loads(latest.read_text(encoding="utf-8"))
    stored["policies"]["rules"]["all"]["flake_recall"] = 0.5
    latest.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(SystemExit, match="differ"):
        evals.main()
    res = evals.run(collected, with_model=True, model=None, replay_only=True, max_usd=3.0)
    assert "rules+model" in res["policies"] and res["model"]["new_spend_usd"] == 0
    assert "blind" in capsys.readouterr().out


def test_eval_budget_stop(collected: Path, monkeypatch: pytest.MonkeyPatch, cassette: Path) -> None:
    spend = [3.5]  # as if earlier model calls had already cost $3.50
    with pytest.raises(diagnose.BudgetExceeded):
        diagnose.with_model(budget=spend, max_usd=3.0)(unsure(), triage.History())

    def spender(model: Any = None, replay_only: bool = False, budget: list[float] | None = None, max_usd: float = 0) -> Any:
        def run(f: dataset.Failure, h: triage.History) -> triage.Decision:
            raise diagnose.BudgetExceeded("cap")

        return run

    monkeypatch.setattr(evals.diagnose, "with_model", spender)
    with pytest.raises(SystemExit, match="budget exceeded"):
        evals.run(collected, with_model=True, model=None, replay_only=False, max_usd=3.0)


def test_cli_job_and_issues(
    collected: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    real = collect.GitHub
    monkeypatch.setattr(cli, "GitHub", lambda token, cache, patient=True: real(token, tmp_path / "c", fake_github([]), patient))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("FLAKE_REPLAY_ONLY", "1")
    monkeypatch.setattr(llm, "load_local_env", lambda: None)
    url = "https://github.com/o/r/actions/runs/100/job/11"
    monkeypatch.setattr(sys, "argv", ["x", "job", url, "--no-model"])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "no_retry" and "ThreatArsenalApiTest" in out["reason"]  # the change touched the test
    monkeypatch.setattr(sys, "argv", ["x", "job", url, "--summary"])
    cli.main()
    md = capsys.readouterr().out
    assert md.startswith("### 🛑") and "read-only" in md
    monkeypatch.setattr(sys, "argv", ["x", "issues", "--data", str(collected), "--min", "1"])
    cli.main()
    drafts = json.loads(capsys.readouterr().out)
    assert drafts[0]["title"].startswith("Flaky: ThreatArsenalApiTest.") and drafts[0]["flakes"] == 1
    with pytest.raises(SystemExit):
        cli.fetch_failure("https://example.com/not-a-job", "t")
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setattr(sys, "argv", ["x", "job", url])
    with pytest.raises(SystemExit):
        cli.main()


def test_model_answers_that_say_nothing_are_dropped(cassette: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = unsure()
    have = diagnose.lines_of(f.log)
    exit_line = next(n for n, t in have.items() if "exit code" in t)
    boring = {
        "decision": "no_retry",
        "category": "regression",
        "confidence": 0.9,
        "summary": "x",
        "evidence": [{"line": exit_line, "quote": "Process completed with exit code 2."}],
    }
    use(monkeypatch, message(json.dumps(boring)))
    assert diagnose.with_model()(f, triage.History()).decision == triage.UNSURE
    line = next(n for n, t in have.items() if "worker 3" in t)
    invalid = {**boring, "decision": "maybe", "evidence": [{"line": line, "quote": "worker 3 stopped responding"}]}
    use(monkeypatch, message(json.dumps(invalid)))
    g = unsure()
    g.job_id = 7
    d = diagnose.with_model()(g, triage.History())
    assert d.decision == triage.UNSURE and "no valid decision" in d.reason


def test_model_not_asked_about_dependent_jobs_and_prompt_is_in_the_key(cassette: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = use(monkeypatch, message('{"decision": "retry", "evidence": []}'))
    dep = make(["##[group]Run x", "##[endgroup]", "Required producer did not succeed: Backend Compile", "##[error]exit 1"])
    diagnose.with_model()(dep, triage.History())
    assert client.requests == []
    f = unsure()
    diagnose.with_model()(f, triage.History())
    h = triage.History()
    h.learn(["x"], "flake")
    f.log += "99| one more line of log\n"  # a different excerpt: a different prompt, asked again
    diagnose.with_model()(f, h)
    assert len(client.requests) == 2


def test_run_scores_blind_and_rules_on_the_same_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    dep = make(
        ["##[group]Run x", "##[endgroup]", "Required producer did not succeed: Backend Compile", "##[error]exit 1"], "persists"
    )
    other = make(NETWORK_FAIL, "flake", job_id=2)
    monkeypatch.setattr(evals.dataset, "load", lambda _root: [dep, other])
    res = evals.run(Path("."), with_model=False, model=None, replay_only=True, max_usd=1.0)
    for name in ("blind", "rules"):
        assert res["policies"][name]["all"]["failures"] == 1 and res["policies"][name]["all"]["dependent_excluded"] == 1


def test_cli_run_triages_every_failed_job_of_a_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = collect.GitHub
    monkeypatch.setattr(cli, "GitHub", lambda token, cache, patient=True: real(token, tmp_path / "c", fake_github([]), patient))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(llm, "load_local_env", lambda: None)
    out, summary = tmp_path / "d.json", tmp_path / "summary.md"
    summary.write_text("earlier steps\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["x", "run", "--repo", "o/r", "--run-id", "100", "--no-model", "--json-out", str(out), "--summary-file", str(summary)],
    )
    cli.main()
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert [r["job"] for r in rows] == ["API Tests"]  # the gate job and the passed job are left out
    assert rows[0]["decision"] == "no_retry" and "summary_md" not in rows[0]
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("earlier steps") and "Do not retry" in text  # appended, never overwritten
    monkeypatch.setattr(sys, "argv", ["x", "run", "--repo", "bad repo", "--run-id", "1"])
    with pytest.raises(SystemExit):
        cli.main()


def test_cli_run_reports_a_job_github_does_not_return(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = collect.GitHub
    monkeypatch.setattr(cli, "GitHub", lambda token, cache, patient=True: real(token, tmp_path / "c", fake_github([]), patient))
    monkeypatch.setattr(cli, "fetch_failure", lambda *_a, **_k: (_ for _ in ()).throw(httpx.HTTPError("gone")))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(llm, "load_local_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["x", "run", "--repo", "o/r", "--run-id", "100", "--no-model"])
    with pytest.raises(SystemExit, match="could be triaged"):  # nothing decided: the step must not look green
        cli.main()
    assert "Not triaged" in capsys.readouterr().out  # but what happened is still in the summary


def test_failed_jobs_follows_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        jobs = [
            {"id": i, "name": f"API Tests ({i})", "conclusion": "failure"} for i in range((page - 1) * 100, min(page * 100, 150))
        ]
        return httpx.Response(200, json={"total_count": 150, "jobs": jobs})

    real = collect.GitHub
    monkeypatch.setattr(
        cli, "GitHub", lambda token, cache, patient=True: real(token, tmp_path, httpx.MockTransport(handler), patient)
    )
    assert len(cli.failed_jobs("o/r", 1, "t")) == 150  # a matrix of 150 jobs is not cut at the first page


def test_log_and_model_text_cannot_plant_markdown() -> None:
    f = make(SUREFIRE_FAIL, job="API [Tests](http://evil)")
    d = triage.Decision("no_retry", "test", "Model: see ![x](http://e/p.png) <img src=x>", tests=["A`B.c"])
    md = cli.as_markdown(f, d)
    import re

    assert not re.search(r"(?<!\\)\]\(", md) and not re.search(r"(?<!\\)<img", md) and "![" not in md  # only escaped forms
    assert "`` A'B.c ``" in md
