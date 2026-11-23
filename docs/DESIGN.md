# Design notes

Why the tool is built the way it is. Numbers live in the README and in `evals/results/latest.json`;
this file explains the choices behind them.

## The question

A CI job failed. Re-running it costs runner minutes and a wait; not re-running it, when the failure
was a flake, costs an engineer who re-runs by hand later or, worse, chases a bug that is not there.
Many projects answer with a blind policy: retry every failed job once or twice. The tool asks the
question per job and answers *retry*, *do not retry*, or *unsure*, always with the log lines behind it.

## Ground truth without labelling anything by hand

GitHub keeps every attempt of a workflow run. When a job failed on attempt 1 and the same job, on
the same commit, passed on a later attempt, a retry would have helped: **flake**. When every later
attempt failed too, a retry only spent minutes: **persists**. When nobody re-ran it, or the re-run
was cancelled, the outcome is unknown and the failure is left out of the scores (**unlabelled**).

Only the first failure of a job in a run is a sample, so a job that failed three times counts once.
Aggregate gate jobs ("Pipeline Gate", summaries) are dropped: they fail because another job failed.

The labels come from what happened *after* the failure; the triage never sees them when it decides.

## Rules first, a model only where the rules do not know

The deterministic layer reads the excerpt and classifies the failure:

| Kind | Example | Decision |
|---|---|---|
| build | compile error, type check, lint, a missing library in an image | do not retry: a retry runs the same code |
| dependent | "Required producer did not succeed: Backend Compile" | unsure: triage the producer instead |
| infra | `ECONNRESET`, registry 5xx, runner lost, artifact download | retry |
| timeout | Playwright `Timeout 15000ms exceeded`, Awaitility | retry |
| test | a named failing test | history and the change decide (below) |
| unknown | nothing recognisable | unsure |

Two things refine that from history, learned in time order:

* the same failure (job family + kind + what) that **failed again on every retry at least twice and
  never passed** is treated as deterministic: do not retry. This is how the tool learns, without a
  hand-written rule, that a heap-space `OutOfMemoryError` in one API test shard is a real problem
  in that project while a dropped connection is not;
* a failing test that **flaked before** is retried; a failing test whose file **the change touched**
  is not.

The model (Claude, a small model by default) is asked only about failures the rules call *unsure*,
and only with the excerpt, the failed step, the changed files and the history of the failing tests.
It answers in a fixed JSON schema and must quote log lines with their numbers. Code checks every
quote against the excerpt; an answer with no verified quote is dropped and the rules' fallback
(retry, the team's current policy) stands. The model never overrides a decision the rules made.

## The excerpt

A job log is 0.1-10 MB. GitHub marks steps with `##[start-action …]` / `##[end-action …;outcome=failure]`;
the excerpt is cut from the **first** failed step that contains no other failed step (later failures,
such as "no coverage file to upload", are consequences). Inside it:

* GitHub echoes each step's script between `##[group]Run` and `##[endgroup]`. Those lines are the
  script, not events (`echo "Timed out after …"` is not a timeout), so they never enter the excerpt;
* lines where the build tool or the runner says "this failed" are always kept, with context;
* lines that merely look like errors are capped and de-duplicated by shape, because applications log
  expected `ERROR`s during their own tests;
* the step's last lines are kept.

Line numbers are the original ones, so a quote can be checked and a human can find it in the log.

## Evaluation that cannot peek

* **Replay in time order.** Each failure is decided with the history known at its start time. Its
  own label enters the history only at the recorded moment the retry that decided it finished.
  People often re-run a failed job hours or days later, so an estimate is not good enough: an
  earlier version estimated it and an independent review found 43 labels learned too early. A
  failure without a recorded end teaches nothing.
* **Holdout.** The split date (`HOLDOUT_FROM`, 2026-11-18) was set on 2026-11-22, when the data was
  collected and before any rule was tuned on it. Rules and the model prompt were written and adjusted
  looking only at failures before it. It is not a pristine one-shot test: the independent review of the
  first version saw aggregate holdout numbers before its defects were fixed, and the model run was
  completed once more after 15 answers were cut by the output-token limit (nothing else changed).
* **Same failures for every policy.** Dependent jobs are not scored as decisions of their own (their
  outcome is their producer's). Which jobs are dependent is read from the log once, and the same set
  is removed from every policy, the blind one included; the report says how many. Failures without a log
  (GitHub keeps logs for a limited time) are not scored.
* **Correlated samples.** Shards of one matrix often fail together for one reason. The report gives
  the number of distinct runs next to the number of failures, and the confidence intervals are
  computed per failure, so they are optimistic; read them as such.

Policies compared on the same failures: **blind** (retry every failed job, the common default),
**rules**, and **rules + model**. Per policy: flakes still retried (recall), flakes that would no
longer be retried (missed), persisting failures no longer retried, and the CI minutes those retries
took.

* **Minutes.** A retry's cost is the runner time of the later attempts of that job, up to the one
  that passed. When only failed jobs are re-run, GitHub lists the jobs that already passed again under
  the new attempt with the same times; those copies are not counted. A flake that is no longer
  retried automatically is assumed to be re-run by hand: its minutes are reported as
  `missed_flake_minutes`, next to the minutes saved.

## Found by review, fixed

An adversarial review of the first version (by a separate agent that saw the code and the data, not
the author's conclusions) found, among others: blind scored on more failures than the rules; labels
learned before they were known; case-insensitive patterns that read `BeanDefinitionValueResolver`
as an npm `ERESOLVE` and a Playwright code frame `toBeVisible({ timeout: 10_000 })` as a timeout;
passing Playwright tests read as failing; a change to `login.tsx` counted as touching
`login.spec.ts`; any 12-character line accepted as a model's evidence; the spend cap checked only
after the whole run. Each now has a test and a mutation in `scripts/mutation_check.py`. The review
saw aggregate holdout numbers of the first version; the fixes are corrections of defects, not
thresholds tuned to them.

## What it deliberately does not do

It does not re-run, cancel, comment or open issues. It prints a decision and its evidence; a team
that wants it to gate an automatic retry reads the JSON in a workflow step it writes and reviews
(see `docs/example-workflow.yml`). `flake-triage issues` drafts "Flaky: <test>" issues from the
history and never posts them.

## The console, the MCP server and live triage

The web console (`flaketriage.web`) and the MCP server (`flaketriage.mcp_server`) read one in-memory
store (`flaketriage.console`) that replays every failure exactly as the evals do, with the model's
answers from the cassette. So a number on a page is the number in `evals/results/latest.json`;
`tests/test_full_cycle.py` checks that on a synthetic project end to end (fake GitHub API, collector,
evals, console, pages, JSON API, CLI, MCP tools, simulation).

The pages run no script at all (`script-src 'none'`); charts are SVG drawn on the server. Log text is
attacker-controlled (it comes from public CI logs) and is only ever rendered escaped. The one form,
"Triage a job", uses a double-submit CSRF cookie (`SameSite=Strict`).

Live triage of a job that is not in the data is off unless `FLAKE_LIVE=1` and a read-only
`GITHUB_TOKEN` are set, and then:

* only `https://github.com/OWNER/REPO/actions/runs/RUN/job/JOB` is accepted, parsed strictly (scheme,
  host and the whole path), for repositories in `FLAKE_LIVE_REPOS`;
* the job must belong to the run in the address, so one job's log is never paired with another run's
  changed files;
* the GitHub client does not wait out rate limits or outages in a web request (two tries, 15 s
  timeout), and the log is streamed and cut to its last 30 MB;
* history excludes labels that came from the same run, so a later attempt is never told its own outcome;
* model calls share a spend cap per process (`FLAKE_LIVE_MAX_USD`, default $1), and requests are
  rate-limited per client address. Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` to the proxy, or
  every visitor shares one address and one limit. The CSRF cookie is `Secure` by default: serve the
  console over HTTPS, or set `FLAKE_COOKIE_SECURE=0` for a local run over plain HTTP.

## Prompt injection through a log

A failing job's log can contain anything a test, a dependency or a commit message prints, including
text written for a model. What bounds that is structural: a model answer counts only with an exact
quote of a real line, it is asked only where the rules are unsure, it never overrides a rule, and the
tool only suggests. On top of that, lines that plainly address a model ("ignore previous instructions",
"SYSTEM: ...", "decision: retry") are not accepted as evidence; this filter is narrow and easy to word
around, and it is not presented as a defence. `scripts/stress.py` plants such instructions next to the
failing line of real failures and asks the model with and without them, with the same history.

## Second review

A second independent review (of the console, MCP server, simulation and scripts) found, among others:
a GitHub HTTP error crashing live triage with a 500; a job address that could pair one job's log with
another run's files; live requests that could block for an hour on a rate limit; the MCP tool without
the console's live-triage limits; the simulation counting engineers' notes only on failures with a
known outcome; wording that called the holdout "tested once"; and an injection filter that also
dropped real log lines. All are fixed, with tests and mutations.

A third review (of the fresh check, the GitHub Action and the README's numbers) found: a fresh check
described as unseen although one live check had already triaged a job of one of its runs and code had
changed between the two collections (now stated as such); unlabelled fresh failures counted in its
scores; the Action running without history, so two rules could never fire (now a `history` input,
`bundled` for OpenAEV); only the first 100 jobs of a run read; a rate-limit wait that could time out the
step and lose every verdict (now no waiting, and each verdict is written as soon as it is decided);
model and log text written into the job summary as Markdown (now escaped); no spend cap and a green exit
when nothing could be triaged; and README numbers older than the files they cite. All are fixed, with
tests and mutations.
