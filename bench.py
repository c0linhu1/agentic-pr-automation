"""Model benchmark: speed, memory, bugs caught (bench_cases.yaml), false alarms, and JSON failures.

    python bench.py [--models a,b] [--runs 5] [--cases name,...]

Talks to Ollama directly, one model at a time; results go to bench_results/. Speed only means
something on the target hardware; catch rates carry over."""
import argparse
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from vader import config, llm, models
from vader.agents import VALIDATORS
from vader.findings import SCHEMA, parse_findings

ROOT = Path(__file__).resolve().parent
BAD = ("critical", "high", "medium")
CACHED_BELOW = 300  # prompt tokens: fewer processed means the rest came from the cache


def chat(model: str, system: str, user: str) -> dict:
    body = {
        "model": model, "stream": False, "format": SCHEMA,  # same Finding schema Vader enforces
        "messages": llm.messages(system, user),  # same layout Vader uses, so timings match
        "options": {"temperature": 0.1, "num_ctx": config.CONTEXT_TOKENS, "num_predict": config.OUTPUT_TOKENS},
    }
    effort = models.reasoning_effort(model)
    if effort:
        body["think"] = effort  # Ollama's name for reasoning effort
    r = httpx.post(f"{config.OLLAMA_URL}/api/chat", timeout=httpx.Timeout(10, read=900), json=body)
    r.raise_for_status()
    return r.json()


def bench_model(model: str, cases: list[dict], runs: int) -> dict:
    for m in models.loaded() or []:
        models.unload(m["name"])
    stats = {"model": model, "load_s": None, "vram_gb": None, "gen_tps": [], "prompt_tps": [], "call_s": [],
             "json_fail": 0, "calls": 0, "cases": {}}

    for case in cases:
        keys = list(VALIDATORS) if case["validator"] == "all" else [case["validator"]]
        expect = set(case.get("expect") or [])
        hits = alarms = tries = 0
        for _ in range(runs):
            for key in keys:
                resp = chat(model, VALIDATORS[key].instructions(), case["input"])
                stats["calls"] += 1
                tries += 1
                if stats["load_s"] is None:
                    stats["load_s"] = round(resp.get("load_duration", 0) / 1e9, 1)
                    loaded = models.loaded() or []
                    stats["vram_gb"] = next((m["vram_gb"] for m in loaded if m["name"] == model), None)
                if resp.get("eval_duration"):
                    stats["gen_tps"].append(resp["eval_count"] / resp["eval_duration"] * 1e9)
                # Cached prompts skew prompt speed; only count full reads.
                if resp.get("prompt_eval_duration") and resp.get("prompt_eval_count", 0) > CACHED_BELOW:
                    stats["prompt_tps"].append(resp["prompt_eval_count"] / resp["prompt_eval_duration"] * 1e9)
                stats["call_s"].append(resp.get("total_duration", 0) / 1e9)

                parsed = parse_findings(resp["message"]["content"])
                if parsed.failure:
                    stats["json_fail"] += 1
                    continue
                if expect and any(f.get("category") in expect for f in parsed.findings):
                    hits += 1
                if not expect and any(f["severity"] in BAD for f in parsed.findings):
                    alarms += 1
        stats["cases"][case["name"]] = {"planted": bool(expect), "hits": hits, "alarms": alarms, "tries": tries}
        print(f"  {model} · {case['name']}: " + (f"caught {hits}/{tries}" if expect else f"false alarms {alarms}/{tries}"))
    models.unload(model)
    return stats


def report(results: list[dict], runs: int) -> str:
    mean = lambda xs: statistics.mean(xs) if xs else 0  # noqa: E731
    lines = [f"# Vader model benchmark · {datetime.now():%Y-%m-%d %H:%M}", "",
             f"{runs} run(s) per case. Speed is only meaningful on the target hardware.", "",
             "| Model | Load s | Memory GB | Gen tok/s | Prompt tok/s | s/validator | Est. full review s "
             "| Bugs caught | False alarms (clean) | JSON failures |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        planted = [c for c in r["cases"].values() if c["planted"]]
        clean = [c for c in r["cases"].values() if not c["planted"]]
        caught = f"{sum(c['hits'] for c in planted)}/{sum(c['tries'] for c in planted)}" if planted else "-"
        alarms = f"{sum(c['alarms'] for c in clean)}/{sum(c['tries'] for c in clean)}" if clean else "-"
        per = mean(r["call_s"])
        lines.append(f"| `{r['model']}` | {r['load_s']} | {r['vram_gb']} | {mean(r['gen_tps']):.1f} "
                     f"| {mean(r['prompt_tps']):.0f} | {per:.1f} | {per * len(VALIDATORS):.0f} | {caught} | {alarms} "
                     f"| {r['json_fail']}/{r['calls']} |")
    lines += ["", "## Per case", "", "| Model | Case | Result |", "|---|---|---|"]
    for r in results:
        for name, c in r["cases"].items():
            res = f"caught {c['hits']}/{c['tries']}" if c["planted"] else f"false alarms {c['alarms']}/{c['tries']}"
            lines.append(f"| `{r['model']}` | {name} | {res} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", help="comma-separated; default: every enabled, pulled model in models.yaml")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--cases", help="comma-separated case names; default: all")
    args = p.parse_args()

    cases = yaml.safe_load((ROOT / "bench_cases.yaml").read_text(encoding="utf-8"))["cases"]
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["name"] in wanted]

    have = models.pulled()
    if have is None:
        raise SystemExit(f"Can't reach Ollama at {config.OLLAMA_URL}. Start it first.")
    if args.models:
        targets = [models.normalize(m) for m in args.models.split(",")]
    else:
        _, entries = models.catalog()
        targets = [e.name for e in entries if e.enabled and e.name in have]
    missing = [m for m in targets if m not in have]
    if missing:
        raise SystemExit(f"Not pulled: {', '.join(missing)} (ollama pull <name>)")

    results = []
    for model in targets:
        print(f"== {model}")
        t0 = time.time()
        results.append(bench_model(model, cases, args.runs))
        print(f"   done in {time.time() - t0:.0f}s")

    text = report(results, args.runs)
    out = ROOT / "bench_results" / f"bench-{datetime.now():%Y%m%d-%H%M}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print("\n" + text + f"\nSaved to {out}")


if __name__ == "__main__":
    main()
