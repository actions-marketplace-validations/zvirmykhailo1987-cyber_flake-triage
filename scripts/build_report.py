# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Build docs/report.html from evals/results/latest.json and the committed data.

Every number on the page is read here, none is typed. Charts are static SVG drawn at build time by a
chart library with a strict axis contract (passed with --charts); the page itself runs no script.

    python scripts/build_report.py --charts PATH/charts.js --css PATH/yadro.css PATH/mova.css
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import subprocess
from collections import Counter
from pathlib import Path

from flaketriage import dataset, diagnose, evals, triage

ROOT = Path(__file__).resolve().parents[1]


def outcomes(root: Path) -> Counter[tuple[str, str]]:
    """Holdout decisions of the rules against what happened, on the same failures the evals score."""
    fs = [f for f in dataset.load(root) if f.log.strip()]
    dep = evals.dependent_ids(fs)
    out: Counter[tuple[str, str]] = Counter()
    for f, d in triage.replay(fs):
        if f.job_id in dep or f.label == "unlabelled" or f.created_at < evals.HOLDOUT_FROM[:10]:
            continue
        out[(d.decision, f.label)] += 1
    return out


def model_outcomes(root: Path) -> Counter[tuple[str, str, str]]:
    """(period, model decision, label) for the failures the rules left unsure: the model's own part."""
    fs = [f for f in dataset.load(root) if f.log.strip()]
    dep = evals.dependent_ids(fs)
    out: Counter[tuple[str, str, str]] = Counter()
    rules = triage.replay(fs)
    model = triage.replay(fs, diagnose.with_model(replay_only=True))
    for (f, d), (g, m) in zip(rules, model, strict=True):
        assert f.job_id == g.job_id
        if f.job_id in dep or f.label == "unlabelled" or d.decision != "unsure":
            continue
        period = "holdout" if f.created_at >= evals.HOLDOUT_FROM[:10] else "dev"
        out[(period, m.decision, f.label)] += 1
    return out


def fresh_and_simulation() -> str:
    """The fresh check and the shadow-mode simulation, from their committed result files."""
    out = []
    fresh = ROOT / "evals/results/fresh.json"
    if fresh.exists():
        fr = json.loads(fresh.read_text(encoding="utf-8"))
        rows = "".join(
            f'<tr><td>{label}</td><td class="n">{p["failures"]} ({p["runs"]})</td><td class="n">{p["flakes_retried"]} of {p["flakes"]}</td>'
            f'<td class="n">{p["persist_skipped"]} of {p["persists"]}</td><td class="n">{fmt(p["retry_minutes_wasted"])} min</td></tr>'
            for key, label in (("blind", "Retry everything"), ("rules", "Rules"), ("rules+model", "Rules + model"))
            if (p := fr["policies"].get(key))
        )
        out.append(
            "<h2>Fresh check</h2>\n"
            f'<p class="prygl">The same system on {fr["fresh_failures"]} failures created after the data was collected '
            f"({html.escape(fr['fresh_after'][:16].replace('T', ' '))} UTC), with all earlier history. No rule, threshold or "
            "prompt changed after they were collected; code did change between the two collections without changing any "
            "evaluated decision, and one live check had triaged a job of one of these runs. Very small: a sanity check, "
            "not a measurement.</p>\n"
            '<div class="tabl"><table><tr><th>Policy</th><th class="n">Scored (runs)</th><th class="n">Flakes still retried</th>'
            f'<th class="n">Failed-again retries skipped</th><th class="n">Minutes still wasted</th></tr>{rows}</table></div>\n'
        )
    sim = ROOT / "docs/simulation.json"
    if sim.exists():
        sm = json.loads(sim.read_text(encoding="utf-8"))
        rows = "".join(
            f'<tr><td>{html.escape(v["label"])}{" <em>(post hoc)</em>" if v["post_hoc"] else ""}</td><td class="n">{fmt(v["wasted_min"])}</td>'
            f'<td class="n">{v["missed"]} ({v["missed_min"]} min)</td><td class="n">{v["notes_per_day"]}</td></tr>'
            for v in sm["variants"]
        )
        out.append(
            "<h2>Shadow-mode simulation</h2>\n"
            f'<p class="prygl">{html.escape(sm["summary"])}</p>\n'
            '<div class="tabl"><table><tr><th>Variant</th><th class="n">Minutes still wasted</th><th class="n">Flakes left</th>'
            f'<th class="n">Runs a day with a "do not retry" note</th></tr>{rows}</table></div>\n'
            f'<p class="prygl">{html.escape(sm["caveat"])}</p>\n'
        )
    return "\n".join(out)


def charts(lib: Path, specs: list[dict[str, object]]) -> list[str]:
    js = (
        f"const CH=require({json.dumps(str(lib))});"
        "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
        "process.stdout.write(JSON.stringify(JSON.parse(s).map(o=>CH[o.fn](o.opts))))})"
    )
    run = subprocess.run(["node", "-e", js], input=json.dumps(specs), capture_output=True, text=True, encoding="utf-8")
    if run.returncode:
        raise SystemExit(run.stderr)
    return list(json.loads(run.stdout))


def fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ")


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def build(lib: Path, css: list[Path], font: Path | None) -> str:
    rep = json.loads((ROOT / "evals/results/latest.json").read_text(encoding="utf-8"))
    b, r = rep["policies"]["blind"]["holdout"], rep["policies"]["rules"]["holdout"]
    rd = rep["policies"]["rules"]["dev"]
    oc = outcomes(ROOT / "data/openaev")
    assert sum(v for (_, lab), v in oc.items() if lab == "persists") == r["persists"]
    assert sum(v for (_, lab), v in oc.items() if lab == "flake") == r["flakes"]
    m, md = rep["policies"]["rules+model"]["holdout"], rep["policies"]["rules+model"]["dev"]
    mo = model_outcomes(ROOT / "data/openaev")
    assert sum(v for (p, _, _), v in mo.items() if p == "holdout") == oc[("unsure", "persists")] + oc[("unsure", "flake")]
    asked = {p: sum(v for (q, _, _), v in mo.items() if q == p) for p in ("dev", "holdout")}
    calls = [json.loads(p.read_text(encoding="utf-8"))["meta"] for p in (ROOT / "evals/llm_cache/triage").glob("*.json")]
    extra = fresh_and_simulation()
    cost = sum(c["cost_usd"] for c in calls)
    ms = sorted(c["ms"] for c in calls)[len(calls) // 2]

    top = max(b["retry_minutes_wasted"], 1)
    step = 2000
    hi = -(-top // step) * step
    names = {"no_retry": "Do not retry", "retry": "Retry", "unsure": "Unsure"}
    bars = []
    for dec in ("no_retry", "retry", "unsure"):
        bars.append({"label": names[dec], "value": oc[(dec, "persists")], "cat": "failed again"})
        bars.append({"label": names[dec], "value": oc[(dec, "flake")], "cat": "passed on retry"})
    hi2 = -(-max(x["value"] for x in bars) // 50) * 50
    svg_min, svg_dec = charts(
        lib,
        [
            {
                "fn": "bar",
                "opts": {
                    "dani": [
                        {"label": "Retry everything", "value": b["retry_minutes_wasted"]},
                        {"label": "Rules", "value": r["retry_minutes_wasted"]},
                        {"label": "Rules + model", "value": m["retry_minutes_wasted"], "aktsent": True},
                    ],
                    "vis": {"domain": [0, hi], "unit": "minutes", "ticks": list(range(0, hi + 1, step))},
                    "w": 720,
                    "h": 360,
                },
            },
            {
                "fn": "bar",
                "opts": {
                    "dani": bars,
                    "vis": {"domain": [0, hi2], "unit": "failures", "ticks": list(range(0, hi2 + 1, 50))},
                    "legend": {"failed again": "Failed again on retry", "passed on retry": "Passed on retry (flake)"},
                    "barvy": {"failed again": "var(--ch-bar)", "passed on retry": "var(--akcent)"},
                    "w": 960,
                    "h": 400,
                },
            },
        ],
    )
    styles = "\n".join(p.read_text(encoding="utf-8") for p in css)
    face = ""
    if font:
        data = base64.b64encode(font.read_bytes()).decode()
        face = f"@font-face{{font-family:'IBM Plex Sans';src:url(data:font/woff2;base64,{data}) format('woff2');font-weight:100 700;font-display:swap}}"
    e = html.escape
    saved = r["retry_minutes_saved"]
    page = f"""<!doctype html>
<html lang="en" data-tema="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flake triage report</title>
<meta name="description" content="Should a failed CI job be re-run? Rules measured against 45 days of real re-runs.">
<meta name="robots" content="noindex, nofollow">
<style>
{styles}
{face}
:root{{--hair:color-mix(in oklch,var(--tilo,#1f2227) 13%,transparent);--f-tilo:'IBM Plex Sans',system-ui,'Segoe UI',sans-serif;--f-dysplei:var(--f-tilo)}}
html,body{{background:var(--poverhnya,#fff);color:var(--tilo,#1f2227)}}
body{{margin:0;font-family:var(--f-tilo);font-size:16px;line-height:1.55;letter-spacing:var(--trekink,0)}}
main{{max-width:1040px;margin:0 auto;padding:48px 16px 64px}}
.gryf{{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--prygl,#5d6066)}}
h1{{font-size:clamp(30px,4.6vw,46px);line-height:1.2;padding-bottom:2px;margin:12px 0 16px;letter-spacing:var(--trekink-zaholovok,-.01em);max-width:22ch}}
h2{{font-size:clamp(21px,2.6vw,27px);line-height:1.2;margin:56px 0 8px;letter-spacing:var(--trekink-zaholovok,-.01em)}}
.lid{{font-size:18px;max-width:64ch;color:var(--tilo)}}
.prygl{{color:var(--prygl,#5d6066)}}
.metryky{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:32px 0 8px}}
.kartka{{background:var(--poverhnya);border:1px solid var(--hair);border-radius:var(--r,12px);padding:20px;box-shadow:var(--tin-1,0 1px 2px rgb(0 0 0 / .05))}}
.kartka .znach{{font-size:34px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.1}}
.kartka .znach.akc{{color:var(--akcent-tekst)}}
.kartka .pid{{font-size:14px;color:var(--prygl,#5d6066);margin-top:6px}}
figure{{margin:16px 0 0;padding:16px;border:1px solid var(--hair);border-radius:var(--r,12px)}}
figure svg{{width:100%;height:auto;display:block}}
figcaption{{font-size:14px;color:var(--prygl,#5d6066);margin-top:8px}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:15px;margin-top:16px}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--hair);vertical-align:top}}
th{{font-weight:600;font-size:14px;color:var(--prygl,#5d6066)}}
td.n,th.n{{text-align:right;white-space:nowrap}}
.tabl{{overflow-x:auto}}
ul.mezhi li{{margin:6px 0;max-width:72ch}}
code{{font-family:var(--f-kod);font-size:.92em}}
footer{{margin-top:56px;padding-top:16px;border-top:1px solid var(--hair);font-size:14px;color:var(--prygl,#5d6066)}}
@media print{{main{{padding:0}} figure,.kartka{{break-inside:avoid}}}}
</style>
</head>
<body>
<main>
<div class="gryf">flake-triage · evaluation report · {e(rep["at"][:10])}</div>
<h1>Should this failed CI job be re-run?</h1>
<p class="lid">Rules that read a failed job's log, with a model only where they are unsure. Of {fmt(rep["all_failures"])} job failures in
45 days of public CI history of OpenAEV, the {fmt(rep["policies"]["blind"]["dev"]["failures"] + rep["policies"]["blind"]["holdout"]["failures"])}
that someone re-ran and whose log was still kept are scored against what the re-run showed. The holdout
(failures from {e(rep["holdout_from"][:10])}) was set when the data was collected; the rules and the prompt were
written on earlier failures.</p>

<div class="metryky">
  <div class="kartka"><div class="znach akc">{pct(r["persist_skip_rate"])}</div>
    <div class="pid">of retries that would fail again are skipped ({r["persist_skipped"]} of {r["persists"]})</div></div>
  <div class="kartka"><div class="znach">{pct(r["flake_recall"])}</div>
    <div class="pid">of flakes are still retried ({r["flakes_retried"]} of {r["flakes"]})</div></div>
  <div class="kartka"><div class="znach">{fmt(saved)} min</div>
    <div class="pid">of CI time saved on the holdout; {r["missed_flake_minutes"]} min of manual re-runs for the {r["missed_flakes"]} missed flakes</div></div>
  <div class="kartka"><div class="znach">{pct(r["no_retry_precision"])}</div>
    <div class="pid">of "do not retry" decisions were right</div></div>
</div>
<p class="prygl">Holdout: {r["failures"]} failures in {r["runs"]} runs. 95% intervals (Wilson, per failure, optimistic
because shards of one run are correlated): flakes retried {pct(r["flake_recall_ci95"][0])}–{pct(r["flake_recall_ci95"][1])},
retries skipped {pct(r["persist_skip_ci95"][0])}–{pct(r["persist_skip_ci95"][1])}.</p>

<h2>Minutes spent on retries that failed again</h2>
<p class="prygl">Holdout, same {r["failures"]} failures for both policies. {pct(r["persists"] / r["failures"])} of the
failures someone re-ran failed again.</p>
<figure>{svg_min}<figcaption>Runner minutes of later attempts that failed again. Copies of jobs that GitHub lists
again under a new attempt are not counted.</figcaption></figure>

<h2>What the rules decided, and what happened</h2>
<figure>{svg_dec}<figcaption>Holdout, {r["failures"]} failures. "Unsure" is retried, as the team does today: it is
the part the model layer is for.</figcaption></figure>

<h2>The model layer, on what the rules left unsure</h2>
<p class="lid">For the {asked["holdout"]} holdout failures the rules could not decide, Claude Haiku read the excerpt,
the failed step, the changed files and the test history, and had to quote log lines; code checked every quote.</p>
<div class="metryky">
  <div class="kartka"><div class="znach akc">{pct(m["persist_skip_rate"])}</div>
    <div class="pid">of retries that would fail again are skipped, rules and model together ({m["persist_skipped"]} of {m["persists"]})</div></div>
  <div class="kartka"><div class="znach">{pct(m["flake_recall"])}</div>
    <div class="pid">of flakes are still retried ({m["flakes_retried"]} of {m["flakes"]}); {m["missed_flake_minutes"]} min of manual re-runs for the {m["missed_flakes"]} missed</div></div>
  <div class="kartka"><div class="znach">{fmt(m["retry_minutes_saved"])} min</div>
    <div class="pid">of CI time saved on the holdout, of {fmt(b["retry_minutes_wasted"])}</div></div>
  <div class="kartka"><div class="znach">${cost:.2f}</div>
    <div class="pid">for all {len(calls)} model answers (scored failures, failures nobody re-ran, the fresh check); median {ms / 1000:.1f} s</div></div>
</div>
<div class="tabl"><table>
<tr><th>Model's own decisions on failures the rules left unsure</th><th class="n">Failed again</th><th class="n">Passed on retry (flake)</th></tr>
<tr><td>Holdout: do not retry</td><td class="n">{mo[("holdout", "no_retry", "persists")]}</td><td class="n">{mo[("holdout", "no_retry", "flake")]}</td></tr>
<tr><td>Holdout: retry</td><td class="n">{mo[("holdout", "retry", "persists")]}</td><td class="n">{mo[("holdout", "retry", "flake")]}</td></tr>
<tr><td>Dev: do not retry</td><td class="n">{mo[("dev", "no_retry", "persists")]}</td><td class="n">{mo[("dev", "no_retry", "flake")]}</td></tr>
<tr><td>Dev: retry</td><td class="n">{mo[("dev", "retry", "persists")]}</td><td class="n">{mo[("dev", "retry", "flake")]}</td></tr>
</table></div>
<p class="prygl">The trade is real: the model doubles the flakes that are not retried ({r["missed_flakes"]} to
{m["missed_flakes"]} on the holdout) to skip {m["persist_skipped"] - r["persist_skipped"]} more retries that fail again.
On the small dev period it did worse ({md["flakes_retried"]} of {md["flakes"]} flakes retried), so its holdout result
should be read as one measurement, not a guarantee. A team that wants fewer missed flakes can ignore the model's
"do not retry" and keep the rules alone.</p>

<h2>Both periods</h2>
<div class="tabl"><table>
<tr><th>Policy and period</th><th class="n">Failures (runs)</th><th class="n">Flakes still retried</th><th class="n">Failed-again retries skipped</th><th class="n">Minutes saved</th></tr>
<tr><td>Rules, dev (written here)</td><td class="n">{rd["failures"]} ({rd["runs"]})</td><td class="n">{rd["flakes_retried"]} of {rd["flakes"]}</td><td class="n">{rd["persist_skipped"]} of {rd["persists"]}</td><td class="n">{fmt(rd["retry_minutes_saved"])} of {fmt(rd["retry_minutes_saved"] + rd["retry_minutes_wasted"])}</td></tr>
<tr><td>Rules, holdout (held out)</td><td class="n">{r["failures"]} ({r["runs"]})</td><td class="n">{r["flakes_retried"]} of {r["flakes"]}</td><td class="n">{r["persist_skipped"]} of {r["persists"]}</td><td class="n">{fmt(saved)} of {fmt(b["retry_minutes_wasted"])}</td></tr>
<tr><td>Rules + model, dev</td><td class="n">{md["failures"]} ({md["runs"]})</td><td class="n">{md["flakes_retried"]} of {md["flakes"]}</td><td class="n">{md["persist_skipped"]} of {md["persists"]}</td><td class="n">{fmt(md["retry_minutes_saved"])} of {fmt(md["retry_minutes_saved"] + md["retry_minutes_wasted"])}</td></tr>
<tr><td>Rules + model, holdout</td><td class="n">{m["failures"]} ({m["runs"]})</td><td class="n">{m["flakes_retried"]} of {m["flakes"]}</td><td class="n">{m["persist_skipped"]} of {m["persists"]}</td><td class="n">{fmt(m["retry_minutes_saved"])} of {fmt(b["retry_minutes_wasted"])}</td></tr>
</table></div>
{extra}
<h2>Limits</h2>
<ul class="mezhi">
<li>One project, 45 days. The patterns fit a Java/Spring and TypeScript/Playwright monorepo.</li>
<li>Dev is small: GitHub keeps job logs for a limited time, so most early failures have no log left.</li>
<li>Only failures someone re-ran have a label; failures nobody re-ran are probably real more often.</li>
<li>{r["dependent_excluded"]} holdout failures only failed because another job did; they are left out of every policy.</li>
<li>The model was run once, with one prompt, written before any model answer was seen. Its first run cut 15 of
255 answers at 1 500 tokens; those were asked again with a 3 000 limit, nothing else changed.</li>
</ul>

<footer>Generated by <code>scripts/build_report.py</code> from <code>evals/results/latest.json</code> and the committed
data. Not affiliated with the OpenAEV project.</footer>
</main>
</body>
</html>
"""
    return page


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--charts", type=Path, required=True)
    ap.add_argument("--css", type=Path, nargs="*", default=[])
    ap.add_argument("--font", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "docs/report.html")
    a = ap.parse_args()
    a.out.write_text(build(a.charts, a.css, a.font), encoding="utf-8")
    print(a.out)


if __name__ == "__main__":
    main()
