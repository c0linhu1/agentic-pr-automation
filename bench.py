"""Model benchmark: speed, bugs caught (bench_cases.yaml), false alarms, and failed replies.

    python bench.py [--models a,b] [--runs 3] [--cases name,...] [--resume]

Sends exactly what Vader sends, through the same OpenAI-compatible call, so it works with any model
server. With MODEL_RUNTIME=ollama it also loads each model first (timing the load) and reports memory.
Results go to bench_results/. Speed only means something on the target hardware; catch rates carry over."""
import argparse
import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from vader import config, llm, models
from vader.agents import VALIDATORS
from vader.diffs import split_files
from vader.engine import parse_sections
from vader.findings import SCHEMA, SEVERITIES, agreed, code_lines, ground, parse_findings

ROOT = Path(__file__).resolve().parent
PROGRESS = ROOT / "bench_results" / "progress.json"  # the run so far, saved after each case


def ask(model: str, instructions: str, content: str,
        schema: dict = SCHEMA) -> tuple[list[dict] | None, int, float, int, bool]:
    """(findings, or None for a failed reply; findings dropped by the quote and weekday checks; seconds;
    output tokens; whether it had to answer without thinking). The same checks as a review, and the same
    retry without thinking; a reply that doesn't parse counts as failed (a review would ask again)."""
    start, think = time.time(), True
    try:
        try:
            resp = llm.request(model, instructions, content, schema=schema)
            llm.check_reply(model, resp, llm.prompt_tokens(instructions, content))
        except llm.OutputLimit as e:
            print(f"    {e}; asking again without thinking")
            think = False
            resp = llm.request(model, instructions, content, schema=schema, think=False)
            llm.check_reply(model, resp, llm.prompt_tokens(instructions, content))
    except llm.LlmError as e:
        print(f"    failed: {e}")
        return None, 0, time.time() - start, 0, not think
    seconds = time.time() - start
    parsed = parse_findings(resp.choices[0].message.content or "")
    wrote = getattr(resp.usage, "completion_tokens", 0) or 0
    if parsed.failure:
        return None, 0, seconds, wrote, not think
    sections = parse_sections(content)
    code = {f.path: code_lines(f.text) for f in split_files(sections.get("diff", ""))}
    pr_text = "\n".join(sections.get(k, "") for k in ("title", "ticket", "description"))
    kept = []
    for f in parsed.findings:
        why = ground(f, code, pr_text)
        if why:
            print(f"    dropped {f['severity']} {f.get('category')}: {why}. Quoted: {f.get('evidence')!r}")
        else:
            kept.append(f)
    return kept, len(parsed.findings) - len(kept), seconds, wrote, not think


def says(finding: dict, words: list) -> bool:
    """The finding says one of `words`; with a list of lists, one word from each list."""
    text = f"{finding.get('description') or ''} {finding.get('suggestedFix') or ''}".lower()
    groups = words if words and isinstance(words[0], list) else [words]
    return all(any(w.lower() in text for w in group) for group in groups)


def bench_model(model: str, cases: list[dict], runs: int, stats: dict | None = None,
                save: Callable[[dict], None] = lambda stats: None, asks: dict[str, int] | None = None,
                only: list[str] | None = None) -> dict:
    """Runs the cases on one model. `stats` carries on a stopped run (its finished cases are skipped);
    `save` is called after each case. `asks`: validators asked several times per run, as Vader's
    VALIDATOR_ASKS does; each ask is also scored on its own, so both are measured on the same calls."""
    asks = asks or {}
    stats = stats or {"model": model, "load_s": None, "memory_gb": None, "call_s": [], "tokens": 0,
                      "failed": 0, "calls": 0, "unquoted": 0, "no_thinking": 0, "cases": {}}
    cases = [c for c in cases if c["name"] not in stats["cases"]]
    if not cases:
        return stats
    if config.MODEL_RUNTIME == "ollama":  # load first, so the load isn't counted as a review's time
        for m in models.loaded() or []:
            models.unload(m["name"])
        start = time.time()
        models.ensure_only(model)
        stats["load_s"] = round(time.time() - start, 1)
        stats["memory_gb"] = next((m["size_gb"] for m in models.loaded() or [] if m["name"] == model), None)

    for case in cases:
        keys = (only or list(VALIDATORS)) if case["validator"] == "all" else [case["validator"]]
        expect = set(case.get("expect") or [])
        words = case.get("expect_words") or []
        # A catch reported below this severity doesn't count: the bug is too serious to file as minor.
        severe_enough = SEVERITIES[:SEVERITIES.index(case.get("min_severity", "info")) + 1]
        # Without a planted bug, anything more severe than this is a false alarm.
        too_severe = SEVERITIES[:SEVERITIES.index(case.get("worst_allowed", "info"))]

        def caught(findings):
            return bool(expect) and any(f.get("category") in expect and f["severity"] in severe_enough
                                        and (not words or says(f, words)) for f in findings)

        def alarm(findings):
            return not expect and any(f["severity"] in too_severe for f in findings)

        hits = alarms = tries = 0
        # A validator asked several times: its findings combined, and combined without the minor findings
        # most asks didn't report (Vader sends those to the verify pass, which isn't run here)
        combined = {"hits": 0, "alarms": 0, "tries": 0}
        filtered = {"hits": 0, "alarms": 0, "tries": 0}
        seen = Counter()
        misses = []  # what the model wrote on each ask that missed the bug or raised a false alarm
        for _ in range(runs):
            for key in keys:
                together, answered = [], 0
                for n in range(asks.get(key, 1)):
                    findings, unquoted, seconds, wrote, fell_back = ask(model, VALIDATORS[key].instructions(),
                                                                        case["input"], VALIDATORS[key].schema())
                    stats["calls"] += 1
                    stats["unquoted"] += unquoted
                    stats["no_thinking"] += fell_back
                    tries += 1
                    stats["call_s"].append(seconds)
                    stats["tokens"] += wrote
                    if findings is None:
                        stats["failed"] += 1
                        continue
                    together, answered = together + [{**f, "_ask": n} for f in findings], answered + 1
                    seen.update(f"{f['severity']} {f.get('category', '?')}" for f in findings)
                    hits, alarms = hits + caught(findings), alarms + alarm(findings)
                    if (expect and not caught(findings)) or alarm(findings):
                        misses.append([f"{f['severity']} {f.get('category', '?')}: "
                                       f"{' '.join(f['description'].split())[:300]}" for f in findings]
                                      or ["(no findings)"])
                if asks.get(key, 1) > 1 and answered:
                    # "filtered" is as if the verify pass rejected all of those, "combined" as if it kept them
                    kept = [f for f in agreed([dict(f) for f in together], asks[key]) if not f.get("_verify")]
                    for tally, fs in ((combined, together), (filtered, kept)):
                        tally["tries"] += 1
                        tally["hits"] += caught(fs)
                        tally["alarms"] += alarm(fs)
        stats["cases"][case["name"]] = {"planted": bool(expect), "hits": hits, "alarms": alarms, "tries": tries,
                                        "seen": dict(seen), "misses": misses,
                                        "combined": combined if combined["tries"] else None,
                                        "filtered": filtered if filtered["tries"] else None}
        save(stats)
        line = f"caught {hits}/{tries}" if expect else f"false alarms {alarms}/{tries}"
        for label, k in (("combined", combined), ("filtered", filtered)):
            if k["tries"]:
                line += f"; {label}: {k['hits'] if expect else k['alarms']}/{k['tries']}"
        print(f"  {model} · {case['name']}: {line}")
    models.unload(model)
    return stats


def report(results: list[dict], runs: int) -> str:
    def mean(xs):
        return statistics.mean(xs) if xs else 0

    def one(cs, k):  # per single ask
        return f"{sum(c[k] for c in cs)}/{sum(c['tries'] for c in cs)}"

    def many(cs, k, how):  # the asks combined, or filtered
        return f"{sum(c[how][k] for c in cs if c.get(how))}/{sum(c[how]['tries'] for c in cs if c.get(how))}"

    lines = [f"# Vader model benchmark · {datetime.now():%Y-%m-%d %H:%M}", "",
             f"{runs} run(s) per case on {config.LLM_BASE_URL} ({config.MODEL_RUNTIME}). Speed is only meaningful "
             "on the target hardware. Output tokens/s includes reading the prompt; with a generic server the first "
             "call also includes loading the model.", "",
             "| Model | Load s | Memory GB | Output tok/s | s/validator | Est. full review s "
             "| Bugs caught | False alarms (clean) | Failed replies | Dropped by the quote/weekday checks "
             "| Ran out thinking |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        planted = [c for c in r["cases"].values() if c["planted"]]
        clean = [c for c in r["cases"].values() if not c["planted"]]
        caught = f"{sum(c['hits'] for c in planted)}/{sum(c['tries'] for c in planted)}" if planted else "-"
        alarms = f"{sum(c['alarms'] for c in clean)}/{sum(c['tries'] for c in clean)}" if clean else "-"
        per, total = mean(r["call_s"]), sum(r["call_s"])
        lines.append(f"| `{r['model']}` | {r['load_s'] or '-'} | {r['memory_gb'] or '-'} "
                     f"| {r['tokens'] / total if total else 0:.1f} | {per:.1f} | {per * len(VALIDATORS):.0f} "
                     f"| {caught} | {alarms} | {r['failed']}/{r['calls']} | {r['unquoted']} "
                     f"| {r['no_thinking']}/{r['calls']} |")
    for r in results:
        both = [c for c in r["cases"].values() if c.get("combined")]
        if both:
            planted = [c for c in both if c["planted"]]
            clean = [c for c in both if not c["planted"]]
            how =("filtered (without the findings below high that most asks didn't report: Vader sends those to "
                   "the verify pass, which isn't run here, so the truth is between this and combined)")
            lines += ["", f"**`{r['model']}`, asked several times ({r.get('asks')}), on the {len(both)} "
                          f"case(s) that used it:** bugs caught {one(planted, 'hits')} per single ask, "
                          f"{many(planted, 'hits', 'combined')} with the asks combined, "
                          f"{many(planted, 'hits', 'filtered')} {how}; false alarms {one(clean, 'alarms')} per "
                          f"single ask, {many(clean, 'alarms', 'combined')} combined, "
                          f"{many(clean, 'alarms', 'filtered')} {how.split(' (')[0]}."]
    lines += ["", "## Per case", "", "| Model | Case | Result | Findings reported (all runs) |", "|---|---|---|---|"]
    for r in results:
        for name, c in r["cases"].items():
            res = f"caught {c['hits']}/{c['tries']}" if c["planted"] else f"false alarms {c['alarms']}/{c['tries']}"
            for how in ("combined", "filtered"):
                if k := c.get(how):
                    res += f"; {how}: {k['hits'] if c['planted'] else k['alarms']}/{k['tries']}"
            found = ", ".join(f"{k} ×{n}" for k, n in sorted(c["seen"].items())) or "none"
            lines.append(f"| `{r['model']}` | {name} | {res} | {found} |")
    missed = [(r["model"], name, c["misses"]) for r in results for name, c in r["cases"].items() if c.get("misses")]
    if missed:
        lines += ["", "## What the model wrote on misses and false alarms", ""]
        for model, name, misses in missed:
            lines.append(f"**`{model}` · {name}**")
            for i, findings in enumerate(misses, 1):
                lines += [f"- Miss {i}:"] + [f"  - {f}" for f in findings]
            lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", help="comma-separated; default: every enabled model in models.yaml the server has")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--cases", help="comma-separated case names; default: all")
    p.add_argument("--resume", action="store_true",
                   help="carry on from a run that was stopped, skipping the cases it finished (same --runs)")
    p.add_argument("--asks", default="",
                   help='ask validators several times per run and also score their findings combined, as '
                        'VALIDATOR_ASKS does in Vader: "correctness=3"')
    p.add_argument("--only", help="run just these validators on cases meant for all of them (comma-separated)")
    p.add_argument("--cases-file", default="bench_cases.yaml", help="the cases to run, e.g. a held-out set")
    args = p.parse_args()
    asks = {k.strip(): max(1, int(n)) for k, _, n in (s.partition("=") for s in args.asks.split(",") if "=" in s)}

    cases = yaml.safe_load((ROOT / args.cases_file).read_text(encoding="utf-8"))["cases"]
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["name"] in wanted]

    have = models.available()
    if have is None:
        raise SystemExit(f"Can't reach the model server at {config.LLM_BASE_URL}. Start it first.")
    if args.models:
        targets = [models.normalize(m) for m in args.models.split(",")]
    else:
        _, entries = models.catalog()
        targets = [e.name for e in entries if e.enabled and e.name in have]
    missing = [m for m in targets if m not in have]
    if missing:
        raise SystemExit(f"Not on the model server: {', '.join(missing)} (pull or download them there first)")

    PROGRESS.parent.mkdir(exist_ok=True)
    # A resumed run must score its remaining cases the same way as the ones it already did
    settings = {"runs": args.runs, "asks": args.asks, "only": args.only, "cases_file": args.cases_file}
    progress = {**settings, "results": {}}
    if args.resume:
        saved = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else None
        if not saved:
            raise SystemExit(f"No stopped run to resume ({PROGRESS} is missing).")
        if changed := [f"--{k.replace('_', '-')}" for k in settings if saved.get(k) != settings[k]]:
            raise SystemExit(f"The stopped run used different {', '.join(changed)}; resume with the same "
                             f"settings: {', '.join(f'{k}={saved.get(k)!r}' for k in settings)}")
        progress["results"] = saved["results"]
        print(f"Resuming: {sum(len(r['cases']) for r in saved['results'].values())} case(s) already done")

    def save(stats: dict) -> None:
        progress["results"][stats["model"]] = stats
        PROGRESS.write_text(json.dumps(progress), encoding="utf-8")

    results = []
    for model in targets:
        print(f"== {model}")
        t0 = time.time()
        stats = bench_model(model, cases, args.runs, progress["results"].get(model), save, asks,
                            args.only.split(",") if args.only else None)
        stats["asks"] = args.asks or None
        results.append(stats)
        print(f"   done in {time.time() - t0:.0f}s")

    text = report(results, args.runs)
    out = ROOT / "bench_results" / f"bench-{datetime.now():%Y%m%d-%H%M}.md"
    out.write_text(text, encoding="utf-8")
    PROGRESS.unlink(missing_ok=True)
    # The Windows console can't print every character in the report
    sys.stdout.reconfigure(errors="replace")
    print("\n" + text + f"\nSaved to {out}")


if __name__ == "__main__":
    main()
