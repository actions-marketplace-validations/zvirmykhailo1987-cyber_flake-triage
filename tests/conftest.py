from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from flaketriage.dataset import Failure
from flaketriage.excerpt import excerpt

STAMP = "2026-11-22T13:58:19.3042770Z "

SUREFIRE_FAIL = [
    "##[group]Run ./.github/actions/api-tests",
    "##[endgroup]",
    "[INFO] Running io.openaev.api.threat_arsenal.ThreatArsenalApiTest",
    "[ERROR] Tests run: 17, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 7.236 s <<< FAILURE! -- in Search Threat Arsenal Actions",
    "[ERROR] io.openaev.api.threat_arsenal.ThreatArsenalApiTest.given_groupByPrimaryDomain_should_returnActionsInDomainOrder -- Time elapsed: 0.346 s <<< FAILURE!",
    "org.opentest4j.AssertionFailedError: expected: <[a, b]> but was: <[b, a]>",
    "[ERROR] Tests run: 1211, Failures: 1, Errors: 0, Skipped: 0",
    "[ERROR] BUILD FAILURE",
    "##[error]Process completed with exit code 1.",
]
COMPILE_FAIL = [
    "##[group]Run mvn -B install",
    "##[endgroup]",
    "[ERROR] COMPILATION ERROR :",
    "[ERROR] /home/runner/work/openaev/openaev/openaev-api/src/main/java/io/openaev/Foo.java:[12,8] cannot find symbol",
    "[ERROR] BUILD FAILURE",
    "##[error]Process completed with exit code 1.",
]
NETWORK_FAIL = [
    "##[group]Run yarn install --immutable",
    "##[endgroup]",
    "➤ YN0001: │ RequestError: socket hang up",
    "error Error: connect ECONNRESET 104.16.0.35:443",
    "##[error]Process completed with exit code 1.",
]
PLAYWRIGHT_FAIL = [
    "##[group]Run yarn test:e2e",
    "##[endgroup]",
    "  ✘  12 [chromium] › tests/atomic-testings/atomic.spec.ts:41:7 › Atomic testing › should create an atomic testing (31.2s)",
    "    Error: expect(locator).toBeVisible() failed",
    "    Test timeout of 30000ms exceeded.",
    "##[error]Process completed with exit code 1.",
]
NOTHING = ["##[group]Run ./script.sh", "##[endgroup]", "something odd happened", "##[error]Process completed with exit code 2."]


def log(lines: list[str]) -> str:
    return excerpt("\n".join(STAMP + x for x in lines))


def make(
    lines: list[str],
    label: str = "flake",
    changed: list[str] | None = None,
    when: str = "2026-11-20T10:00:00Z",
    job_id: int = 1,
    **kw: Any,
) -> Failure:
    return Failure(
        job_id=job_id,
        run_id=job_id * 10,
        attempt=1,
        job=kw.pop("job", "pipeline / API Tests"),
        workflow="[Core] CI",
        event="pull_request",
        branch="feat/x",
        sha="abc",
        created_at=when,
        seconds=600,
        failed_steps=["Run tests"],
        changed_files=changed or [],
        url="https://example/job",
        label=label,
        known_at=kw.pop("known_at", (datetime.fromisoformat(when) + timedelta(hours=1)).isoformat()),
        retry_seconds=kw.pop("retry_seconds", 600),
        log=log(lines),
    )


@pytest.fixture
def fixtures() -> dict[str, list[str]]:
    return {
        "surefire": SUREFIRE_FAIL,
        "compile": COMPILE_FAIL,
        "network": NETWORK_FAIL,
        "playwright": PLAYWRIGHT_FAIL,
        "nothing": NOTHING,
    }
