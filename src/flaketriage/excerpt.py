# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Cut a CI log down to the lines that explain a failure, keeping their original line numbers.

A job log is 0.1-10 MB; the reason it failed is a few dozen lines inside the step that failed.
GitHub marks steps with ``##[start-action ...]`` / ``##[end-action ...;outcome=failure]``, so the
excerpt is taken from the innermost failed step: every line there that looks like an error, a few
lines of context, and the step's last lines. Without markers it falls back to the whole log, cut
off before the post-job cleanup. Lines are kept verbatim (minus timestamp and colour codes) and
prefixed with their line number, so a diagnosis can quote a line and code can check it is there.
"""

from __future__ import annotations

import re

VERSION = "e7"  # bump when the cut changes: stored excerpts are re-derived from the raw logs
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ?")
START = re.compile(r"^##\[start-action display=(.*?);id=([^\]]+)\]")
END_FAIL = re.compile(r"^##\[end-action id=([^;]+);outcome=failure")
# strong: the build tool or the runner saying "this failed" - always kept
STRONG = re.compile(
    r"##\[error\]|BUILD FAILURE|<<< (FAILURE|ERROR)!|Tests run:.*(Failures|Errors): [1-9]|"
    r"^\[ERROR\] (Failures|Errors|Tests run|  \w)|"
    r"^\[ERROR\] Failed to execute goal|\s✘\s|^\s+\d+\) \[\w+\] ›|\bFAIL\s+\S+\.(spec|test)\.|failed to solve|"
    r"Test timeout of|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|No space left|OutOfMemory|exit code 137|"
    r"lost communication|The runner has received a shutdown|COMPILATION ERROR|error TS\d+",
)
# weak: anything that looks like an error; applications log expected errors in tests, so these are capped
WEAK = re.compile(
    r"\[ERROR\]|\bFAILED\b|✖|Error:|Exception\b|Caused by:|Timeout|timed out|Killed|exit code [1-9]|rate limit|"
    r"502 Bad Gateway|503 Service|expect\(|Expected|Received:",
    re.IGNORECASE,
)
WEAK_BUDGET = 60
NOISE = re.compile(
    r"\[ERROR\] *$|Re-run Maven using|To see the full stack trace|For more information about the errors|"
    r"\[Help 1\]|See dump files|^\s+at (org\.junit|java\.base|org\.springframework|jdk\.internal)"
)
CONTEXT = 3
TAIL = 30
MAX_LINES = 300
MAX_CHARS = 400


def clean(line: str) -> str:
    return ANSI.sub("", STAMP.sub("", line)).rstrip()[:MAX_CHARS]


def failed_span(lines: list[str]) -> tuple[int, int]:
    """(start, end) of the first innermost failed step, or the whole log before cleanup if there are no markers."""
    starts: dict[str, int] = {}
    spans = []
    for i, line in enumerate(lines):
        if m := START.match(line):
            starts[m.group(2)] = i
        elif (m := END_FAIL.match(line)) and m.group(1) in starts:
            spans.append((starts[m.group(1)], i))
    if spans:  # the first failed step that holds no other failed step: later failures are its consequences
        inner = [s for s in spans if not any(o != s and s[0] <= o[0] and o[1] <= s[1] for o in spans)]
        return min(inner)
    end = next((i for i, x in enumerate(lines) if x.startswith("Post job cleanup.")), len(lines))
    return 0, max(0, end - 1)  # inclusive, like the marker spans


def excerpt(text: str) -> str:
    lines = [clean(x) for x in text.splitlines()]
    lo, hi = failed_span(lines)
    keep: set[int] = {lo} if lo < len(lines) else set()
    in_script = False  # GitHub echoes each step's script between "##[group]Run" and "##[endgroup]"
    weak_seen: set[str] = set()
    weak: set[int] = set()
    script: set[int] = set()
    for i in range(lo, min(hi + 1, len(lines))):
        line = lines[i]
        if line.startswith("##[group]Run "):
            in_script = True
            keep.add(i)
            continue
        if line.startswith("##[endgroup]"):
            in_script = False
            script.add(i)
            continue
        if in_script:  # the script's own text ("echo Timed out ...") is not something that happened
            script.add(i)
            continue
        if NOISE.search(line):
            continue
        if STRONG.search(line):
            keep.update(range(max(lo, i - CONTEXT), min(hi + 1, i + CONTEXT + 1)))
        elif WEAK.search(line):
            shape = re.sub(r"[0-9a-f]{6,}|\d+", "#", line)[:120]  # the same message with other ids/ports: once
            if shape not in weak_seen and len(weak) < WEAK_BUDGET:
                weak_seen.add(shape)
                weak.update(range(max(lo, i - 1), min(hi + 1, i + 2)))
    keep.update(i for i in range(max(lo, hi - TAIL), min(hi + 1, len(lines))) if i not in script)
    keep |= weak
    chosen = sorted(keep - script)  # context around an error never reaches back into a script echo
    if len(chosen) > MAX_LINES:  # the first errors and the step's end carry the cause; the middle repeats
        tail = [i for i in chosen if i >= hi - TAIL]
        chosen = [i for i in chosen if i < hi - TAIL][: MAX_LINES - len(tail)] + tail
    out, prev = [], -2
    for i in chosen:
        if i != prev + 1:
            out.append("…")
        out.append(f"{i + 1}| {lines[i]}")
        prev = i
    return "\n".join(out) + "\n"


def lines_of(ex: str) -> dict[int, str]:
    """Line number -> text, as kept in an excerpt."""
    out = {}
    for row in ex.splitlines():
        num, sep, rest = row.partition("| ")
        if sep and num.isdigit():
            out[int(num)] = rest
    return out
