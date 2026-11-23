# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Break the code on purpose and check that the tests notice. Writes docs/mutation-results.json.

Coverage says a line ran. This says a test fails when the line is wrong. Each mutation removes one
guarantee from a temporary copy of the project, and the whole suite runs against that copy. A mutation
nothing catches is reported as "survived", not hidden: the fix is a missing test, never deleting the
mutation.

    python scripts/mutation_check.py           # all mutations (~1 suite run each, stops at the first failure)
    python scripts/mutation_check.py --check   # only verify every mutation still applies to the code
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess  # nosec B404 — runs this repo's own test suite only
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
SKIP = {
    ".venv",
    "data",
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "docs",
    ".coverage",
    "evals",
    ".env.local",
    "htmlcov",
}

MUTATIONS = [
    (
        "A build error is retried",
        "src/flaketriage/triage.py",
        'return Decision(NO_RETRY, kind, f"Build error',
        'return Decision(RETRY, kind, f"Build error',
    ),
    (
        "An infrastructure error is not retried",
        "src/flaketriage/triage.py",
        'return Decision(RETRY, kind, f"Infrastructure',
        'return Decision(NO_RETRY, kind, f"Infrastructure',
    ),
    ("A test that flaked before is not retried", "src/flaketriage/triage.py", "        if prior:\n", "        if False:\n"),
    (
        "A failing test the change touched is retried",
        "src/flaketriage/triage.py",
        "if touched := touched_tests(r, f.changed_files):",
        "if touched := []:",
    ),
    ("Replay learns an outcome before it was known", "src/flaketriage/triage.py", "if p[0] <= now", "if True"),
    (
        "History counts a persisting test as a flake",
        "src/flaketriage/triage.py",
        'if label == "flake":\n                self.flaked',
        'if label in ("flake", "persists"):\n                self.flaked',
    ),
    (
        "A job that failed because another did is judged on its own log",
        "src/flaketriage/signals.py",
        "for kind in (DEPENDENT, BUILD, INFRA, TIMEOUT, TEST):",
        "for kind in (BUILD, INFRA, TIMEOUT, TEST):",
    ),
    (
        "A network error outranks a build error",
        "src/flaketriage/signals.py",
        "for kind in (DEPENDENT, BUILD, INFRA, TIMEOUT, TEST):",
        "for kind in (DEPENDENT, INFRA, BUILD, TIMEOUT, TEST):",
    ),
    (
        "A passing retry is not labelled a flake",
        "src/flaketriage/dataset.py",
        'elif any(x["conclusion"] == "success" for x in later):',
        "elif False:",
    ),
    (
        "A cancelled retry is labelled persists",
        "src/flaketriage/dataset.py",
        'elif all(x["conclusion"] == "failure" for x in later):',
        "elif True:",
    ),
    ("Gate jobs become samples", "src/flaketriage/dataset.py", " or GATE.search(name):", ":"),
    ("A job that failed twice counts twice", "src/flaketriage/dataset.py", " or name in seen or GATE", " or GATE"),
    (
        "The step's own script is read as events",
        "src/flaketriage/excerpt.py",
        "if in_script:  # the script's own text",
        "if False:  # the script's own text",
    ),
    (
        "The failed step is ignored: post-job noise is read",
        "src/flaketriage/excerpt.py",
        "    if spans:  # the first failed step",
        "    if False:  # the first failed step",
    ),
    (
        "Dependent jobs are scored",
        "src/flaketriage/evals.py",
        "pairs = [(f, d) for f, d in pairs if f.job_id not in exclude]",
        "pass",
    ),
    (
        "Unsure counted as not retried in scoring",
        "src/flaketriage/evals.py",
        "return d.decision != triage.NO_RETRY",
        "return d.decision == triage.RETRY",
    ),
    (
        "A model quote is accepted without checking the log",
        "src/flaketriage/diagnose.py",
        "            and quote in text\n",
        "            and True\n",
    ),
    (
        "The model overrides a decision the rules already made",
        "src/flaketriage/diagnose.py",
        "if d.decision != UNSURE or not f.log.strip() or",
        "if not f.log.strip() or",
    ),
    (
        "Cassette never replayed (every eval pays again)",
        "src/flaketriage/llm.py",
        "    if path.exists():\n        hit",
        "    if False:\n        hit",
    ),
    ("A model refusal is treated as an answer", "src/flaketriage/llm.py", 'if msg.stop_reason == "refusal":', "if False:"),
    ("Schema not closed for structured output", "src/flaketriage/llm.py", 'out["additionalProperties"] = False', "pass"),
    (
        "Replay-only mode still calls the API",
        "src/flaketriage/llm.py",
        'if replay_only or os.environ.get("FLAKE_REPLAY_ONLY") == "1":',
        "if False:",
    ),
    ("Expired logs are fetched one by one to the end", "src/flaketriage/collect.py", " or gone >= 3:", ":"),
    (
        "One unreachable run aborts the whole collection",
        "src/flaketriage/collect.py",
        "except (RuntimeError, httpx.HTTPError) as exc:  # one unreachable",
        "except ZeroDivisionError as exc:  # one unreachable",
    ),
    (
        "The disk cache is never read",
        "src/flaketriage/collect.py",
        "        if path.exists():\n            return json.loads",
        "        if False:\n            return json.loads",
    ),
    (
        "A failure that always failed again is still retried",
        "src/flaketriage/triage.py",
        "    if sig and (n := history.stuck(sig)):",
        "    if False and (n := history.stuck(sig)):",
    ),
    ("One persisting retry is already a pattern", "src/flaketriage/triage.py", "STUCK_AFTER = 2 ", "STUCK_AFTER = 1 "),
    (
        "A failure that once passed on retry still counts as stuck",
        "src/flaketriage/triage.py",
        "return n if n >= STUCK_AFTER and not self.flaked.get(key) else 0",
        "return n if n >= STUCK_AFTER else 0",
    ),
    (
        "Shards of one matrix are different jobs",
        "src/flaketriage/triage.py",
        'return re.sub(r"\\s*\\([^)]*\\)\\s*$", "", job).strip()',
        "return job.strip()",
    ),
    (
        "Tests that always failed again are still retried",
        "src/flaketriage/triage.py",
        "if not prior and all(stuck):",
        "if False:",
    ),
    (
        "Replay never learns signatures",
        "src/flaketriage/triage.py",
        "history.learn(item[1], item[2], item[3])",
        "history.learn(item[1], item[2])",
    ),
    (
        "The last failed step is read, not the first",
        "src/flaketriage/excerpt.py",
        "        return min(inner)",
        "        return max(inner)",
    ),
    (
        "Blind is scored on more failures than the rules",
        "src/flaketriage/evals.py",
        "    exclude = dependent_ids(with_log)\n",
        "    exclude: set[int] = set()\n",
    ),
    (
        "A label without a recorded end is learned at once",
        "src/flaketriage/triage.py",
        "    return datetime.max.replace(tzinfo=datetime.fromisoformat(f.created_at).tzinfo)",
        "    return datetime.fromisoformat(f.created_at)",
    ),
    ("A copied job counts as a retry", "src/flaketriage/dataset.py", 'if x.get("started_at") and stamp in seen:', "if False:"),
    (
        "A flake's minutes include attempts after it passed",
        "src/flaketriage/dataset.py",
        '                later = later[: next(i for i, x in enumerate(later) if x["conclusion"] == "success") + 1]\n',
        "",
    ),
    (
        "Source-code lines are read as events",
        "src/flaketriage/signals.py",
        "if not test and not CODE_FRAME.search(text):",
        "if not test:",
    ),
    (
        "A test line is also read as infra/timeout/build",
        "src/flaketriage/signals.py",
        "if not test and not CODE_FRAME.search(text):",
        "if not CODE_FRAME.search(text):",
    ),
    (
        "Touching a source file counts as touching its test",
        "src/flaketriage/triage.py",
        "for c in changed if TEST_FILE.search(c)}",
        "for c in changed}",
    ),
    ("A boilerplate line counts as a quote", "src/flaketriage/diagnose.py", "            and not BORING.search(text)\n", ""),
    (
        "A model answer with an invalid decision is used",
        "src/flaketriage/diagnose.py",
        'if ans.get("decision") not in (RETRY, NO_RETRY):',
        "if False:",
    ),
    ("The model is asked about dependent jobs", "src/flaketriage/diagnose.py", " or d.kind == signals.DEPENDENT:", ":"),
    ("The spend cap is checked only at the end", "src/flaketriage/diagnose.py", "and budget[0] >= max_usd:", "and False:"),
    (
        "The cassette ignores the prompt",
        "src/flaketriage/diagnose.py",
        'cache_id=f"{f.job_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}",',
        "cache_id=str(f.job_id),",
    ),
    (
        "A planted instruction in the log counts as evidence",
        "src/flaketriage/diagnose.py",
        "            and not ADDRESSED.search(text)\n",
        "",
    ),
    (
        "'Unsure' is no longer retried",
        "src/flaketriage/console.py",
        "return self.final.decision != NO_RETRY",
        'return self.final.decision == "retry"',
    ),
    (
        "Dependent jobs are scored",
        "src/flaketriage/console.py",
        'in ("flake", "persists") and not self.dependent',
        'in ("flake", "persists")',
    ),
    (
        "A dropped model answer is shown as a decision",
        "src/flaketriage/console.py",
        'final, state = r, "dropped"',
        'final, state = m, "dropped"',
    ),
    (
        "The triage form skips the CSRF check",
        "src/flaketriage/web.py",
        "if not cookie or not hmac.compare_digest(cookie.encode(), csrf.encode()):",
        "if False:",
    ),
    (
        "Any address whose path looks like a job is fetched, whatever the host",
        "src/flaketriage/cli.py",
        'if parts.scheme != "https" or parts.netloc.lower() != "github.com" or not m:',
        "if not m:",
    ),
    ("Live triage fetches any repository", "src/flaketriage/web.py", "if repo.lower() not in live_repos:", "if False:"),
    ("A job from another run is accepted", "src/flaketriage/cli.py", 'if job.get("run_id") != run_id:', "if False:"),
    (
        "A GitHub HTTP error crashes the live triage page",
        "src/flaketriage/web.py",
        "except (SystemExit, KeyError, OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:",
        "except (SystemExit, KeyError, OSError, RuntimeError, ValueError) as exc:",
    ),
    (
        "Live triage learns from its own run",
        "src/flaketriage/console.py",
        "        if run_id not in self.run_ids:\n",
        "        if True:\n",
    ),
    ("The rate limit lets one more through", "src/flaketriage/web.py", "if len(q) >= self.per_ip:", "if len(q) > self.per_ip:"),
    ("Pages may run scripts", "src/flaketriage/web.py", "script-src 'none'", "script-src 'self'"),
    (
        "The MCP tool fetches when live triage is off",
        "src/flaketriage/mcp_server.py",
        'if os.environ.get("FLAKE_LIVE") != "1":',
        "if False:",
    ),
    (
        "The simulation leaves its threshold changed",
        "scripts/simulate.py",
        "        triage.STUCK_AFTER = old\n",
        "        pass\n",
    ),
    (
        "The fresh check also scores old failures",
        "scripts/fresh_check.py",
        "fresh = [f for f in later if after(f.created_at, cutoff)]",
        "fresh = list(later)",
    ),
    ("A run's gate jobs are triaged", "src/flaketriage/cli.py", ' and not dataset.GATE.search(j.get("name", ""))', ""),
    (
        "The job summary is overwritten",
        "src/flaketriage/cli.py",
        'with a.summary_file.open("a", encoding="utf-8") as fh:',
        'with a.summary_file.open("w", encoding="utf-8") as fh:',
    ),
    (
        "Model text is written into the summary as Markdown",
        "src/flaketriage/cli.py",
        "return MD_SPECIAL.sub(",
        "return text or MD_SPECIAL.sub(",
    ),
    (
        "A backtick in a test name breaks the code span",
        "src/flaketriage/cli.py",
        """.join(text.replace("`", "'").split())""",
        """.join(text.split())""",
    ),
    (
        "Only the first page of a run's jobs is read",
        "src/flaketriage/cli.py",
        'if len(jobs) >= data.get("total_count", 0) or not data.get("jobs"):',
        "if True:",
    ),
    (
        "A run nobody could triage looks green",
        "src/flaketriage/cli.py",
        'if rows and all("error" in x for x in rows):',
        "if False:",
    ),
]


def copy_project(target: Path) -> None:
    shutil.copytree(ROOT, target, ignore=lambda _d, names: [n for n in names if n in SKIP], dirs_exist_ok=True)


def run_tests(cwd: Path) -> tuple[bool, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    # fixed argv (this interpreter + pytest), no shell
    done = subprocess.run(  # nosec B603
        [PYTHON, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    found = re.findall(r"^FAILED (\S+)", done.stdout, re.M) or re.findall(r"^ERROR (\S+)", done.stdout, re.M)
    return done.returncode == 0, found[0] if found else ""


def apply(text: str, old: str, new: str, name: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"Mutation no longer applies exactly once, fix the script: {name}")
    return text.replace(old, new, 1)


def main() -> None:
    for name, relative, old, new in MUTATIONS:
        apply((ROOT / relative).read_text(encoding="utf-8"), old, new, name)
    if "--check" in sys.argv:
        print(len(MUTATIONS), "mutations apply")
        return
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "copy"
        copy_project(work)
        passed, failing = run_tests(work)
        if not passed:
            raise SystemExit(f"The unmodified copy does not pass ({failing}), so nothing here could be trusted.")
        for name, relative, old, new in MUTATIONS:
            path = work / relative
            original = path.read_text(encoding="utf-8")
            path.write_text(apply(original, old, new, name), encoding="utf-8", newline="\n")
            passed, caught_by = run_tests(work)
            path.write_text(original, encoding="utf-8", newline="\n")
            results.append({"bug": name, "file": relative, "caught": not passed, "caught_by": caught_by if not passed else None})
            print(("caught  " if not passed else "SURVIVED"), name, caught_by, flush=True)
    summary = {
        "measured": dt.date.today().isoformat(),
        "bugs_injected": len(results),
        "caught": sum(row["caught"] for row in results),
        "results": results,
    }
    (ROOT / "docs" / "mutation-results.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    print(summary["caught"], "of", summary["bugs_injected"])


if __name__ == "__main__":
    main()
