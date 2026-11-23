# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Deterministic reading of a failed job's log excerpt: which tests failed and what kind of failure it is.

No model here. Each signal records the line it came from, so every claim the triage makes can be
traced to a line of the log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from flaketriage.excerpt import lines_of

# Kinds, from "a retry cannot help" to "a retry is the right move"
BUILD = "build"  # compile, type check, lint, formatting, dependency resolution in code
TEST = "test"  # an assertion or error inside a named test
INFRA = "infra"  # network, registry, runner, service container, disk, out of memory
TIMEOUT = "timeout"  # a step or test ran out of time
DEPENDENT = "dependent"  # failed because another job failed: triage that job instead
UNKNOWN = "unknown"
DEPENDENT_RX = re.compile(r"Required producer did not succeed: (?!\$\{)(.+)|(\d+)/\d+ upstream job\(s\) failed")

# Patterns are case-sensitive: "(?i)" marks the few that are not. Case-insensitive matching made
# "BeanDefinitionValueResolver" an ERESOLVE and "ModuleNotFoundError" an ENOTFOUND.
INFRA_RX = [
    (r"\bE(CONNRESET|CONNREFUSED|TIMEDOUT|AI_AGAIN|NOTFOUND)\b|socket hang up|Connection reset by peer", "network"),
    (
        r"502 Bad Gateway|503 Service Unavailable|504 Gateway Time-?out|Internal Server Error.*(registry|npmjs|maven|docker)",
        "upstream 5xx",
    ),
    (r"toomanyrequests|(?i:rate limit exceeded)|API rate limit|429 Too Many Requests", "rate limit"),
    (r"failed to (pull|resolve|fetch).*(image|manifest)|manifest unknown|pull access denied|TLS handshake timeout", "image pull"),
    (r"No space left on device|disk quota exceeded", "disk full"),
    (r"OutOfMemoryError|JavaScript heap out of memory|exit code 137|\bKilled\b|OOMKilled", "out of memory"),
    (r"The runner has received a shutdown signal|lost communication with the server|runner .* (offline|lost)", "runner lost"),
    (
        r"Container '\w[\w-]*' failed to start|is unhealthy|dependency failed to start|(?i:health ?check (failed|timed out))",
        "service container",
    ),
    (r"Could not transfer artifact|Failed to read artifact descriptor", "artifact download"),
    (r"Polling exceeded \d+ attempts|Unable to get scan results|unexpected empty event loop", "external service or tool crash"),
    (r"Unable to download|Failed to download|unable to access 'https://", "download"),
]
TIMEOUT_RX = [
    (r"Test timeout of \d+ms exceeded|Timeout of \d+ms exceeded|exceeded timeout of \d+", "test timeout"),
    (r"TimeoutError|[Tt]imed out after|Timed out waiting|waiting for .* timed out", "wait timeout"),
    (r"The job running on runner .* has exceeded the maximum execution time", "job timeout"),
    (r"ConditionTimeoutException", "await timeout"),
]
BUILD_RX = [
    (r"COMPILATION ERROR|cannot find symbol|error: \S+\.java:\d+|\[ERROR\] .*\.java:\[\d+,\d+\]", "java compile"),
    (r"error TS\d+:|Type error:", "type check"),
    (r"✖ \d+ problems? \(\d+ errors?|spotless.*violations|Run 'mvn spotless:apply'", "lint/format"),
    (r"Non-resolvable (parent|import) POM|\bERESOLVE\b", "dependencies"),
    (r"You have \d+ Checkstyle violations?|PMD Failure|Missing license header", "static check"),
    (r"ModuleNotFoundError|No module named|Cannot find module|ERR_MODULE_NOT_FOUND", "missing module"),
    (r"FlywayException|FlywayValidateException|Migration .* failed|Validate failed: Migrations", "migration"),
    (r"error while loading shared libraries|exit code: 127", "image build: missing library or command"),
]
# "Could not resolve dependencies" is deliberately in neither list: a missing artifact can be a typo in
# the pom (build) or a release still propagating to the mirror (a retry passes). Both happened.

# Lines that are source code, not events: a Playwright code frame ("  61 |   await expect(...)"), a
# stack frame, a diff of the test file.
CODE_FRAME = re.compile(r"^\s*>?\s*\d+ \|\s|^\s+at [\w.$<>]+\(|^\s*\|\s")

# tests: Maven surefire (method or whole class), Playwright, Vitest/Jest
SUREFIRE = re.compile(
    r"\[ERROR\] ([\w.$]+?)\.([a-z_]\w*)(?:\([^)]*\))?(?:\[[^\]]*\])? -- Time elapsed: [\d.,]+ s <<< (FAILURE|ERROR)!"
)
SUREFIRE_CLASS = re.compile(r"\[ERROR\] ((?:[a-z_]\w*\.)+([A-Z][\w$]*)) -- Time elapsed: [\d.,]+ s <<< (FAILURE|ERROR)!")
PLAYWRIGHT = re.compile(r"(?:✘|^\s*\d+\) \[\w+\] ›)\s*.*?([\w./-]+\.(?:spec|test)\.[jt]sx?):(\d+):\d+ › (.+?)(?:\s+\(\d|$)")
VITEST = re.compile(r"(?:FAIL|×|✗)\s+([\w./-]+\.(?:spec|test)\.[jt]sx?)\s*(?:>|›)\s*(.+?)\s*(?:\d+ms)?$")


@dataclass
class Signal:
    kind: str
    what: str
    line: int
    text: str


@dataclass
class Reading:
    signals: list[Signal] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)  # "Class.method" or "file.spec.ts > title"
    test_files: list[str] = field(default_factory=list)  # paths or class names that locate the tests

    def kinds(self) -> set[str]:
        return {s.kind for s in self.signals}

    def first(self, kind: str) -> Signal | None:
        return next((s for s in self.signals if s.kind == kind), None)

    def evidence(self, kind: str) -> Signal:
        """The first signal of a kind that :func:`kind_of` returned, so one always exists."""
        if (s := self.first(kind)) is None:
            raise ValueError(f"no {kind} signal in this reading")
        return s


def _match(rx: list[tuple[str, str]], kind: str, num: int, text: str) -> Signal | None:
    for pat, what in rx:
        if re.search(pat, text):
            return Signal(kind, what, num, text.strip()[:240])
    return None


def read(log: str) -> Reading:
    out = Reading()
    seen_tests: set[str] = set()
    for num, text in lines_of(log).items():
        if m := DEPENDENT_RX.search(text):
            out.signals.append(Signal(DEPENDENT, (m.group(1) or "upstream jobs").strip()[:120], num, text.strip()[:240]))
            continue
        test, where = None, ""
        if m := SUREFIRE.search(text):
            test, where = f"{m.group(1).rsplit('.', 1)[-1]}.{m.group(2)}", m.group(1)
        elif m := SUREFIRE_CLASS.search(text):  # the whole class failed, e.g. its Spring context did not load
            test, where = m.group(2), m.group(1)
        elif m := PLAYWRIGHT.search(text):
            test, where = f"{m.group(1)} > {m.group(3).strip()}", m.group(1)
        elif m := VITEST.search(text):
            test, where = f"{m.group(1)} > {m.group(2).strip()}", m.group(1)
        # a line that names a failing test, or a line of source code, says nothing about infra, time or build:
        # "shouldHandleTimeoutError <<< FAILURE!", "61 |  await expect(row).toBeVisible({ timeout: 10_000 })"
        if not test and not CODE_FRAME.search(text):
            for rx, kind in ((INFRA_RX, INFRA), (TIMEOUT_RX, TIMEOUT), (BUILD_RX, BUILD)):
                if sig := _match(rx, kind, num, text):
                    out.signals.append(sig)
                    break
        if test and test not in seen_tests:
            seen_tests.add(test)
            out.tests.append(test)
            out.test_files.append(where)
            out.signals.append(Signal(TEST, "failing test", num, text.strip()[:240]))
    return out


def kind_of(r: Reading) -> str:
    """One kind for the job. A job that only failed because another did is DEPENDENT; otherwise a build
    error outranks everything (a retry never fixes code)."""
    k = r.kinds()
    for kind in (DEPENDENT, BUILD, INFRA, TIMEOUT, TEST):
        if kind in k:
            return kind
    return UNKNOWN
