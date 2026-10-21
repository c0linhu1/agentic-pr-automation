"""The written report of a finished review, as GitHub-flavored Markdown (for the PR comment)."""
from .engine import Review

VERDICT_ICON = {"BLOCK": "⛔", "HOLD": "✋", "AUTO-FIX": "🔧", "PASS": "✅"}
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪", "info": "ℹ️"}
GITHUB_LIMIT = 65_000  # GitHub's comment limit is 65,536 characters


def _minutes(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f} min"


def markdown(review: Review) -> str:
    verdict = review.verdict or "HOLD"
    took = (review.finished or 0) - (review.started or review.finished or 0)
    models = sorted(set(review.models.values())) or [review.model]
    sha = (review.meta.get("sha") or "")[:7]

    lines = [f"## {VERDICT_ICON.get(verdict, '')} Vader review: **{verdict}**", "",
             f"{review.verdict_reason}", "",
             f"Commit `{sha}` · {len(review.files)} file(s)"
             + (f" in {review.parts} parts" if review.parts > 1 else "")
             + f" · model(s): {', '.join(f'`{m}`' for m in models)} · {_minutes(took)}", ""]
    if review.ci is not None:
        lines += ["CI: " + (", ".join(f"`{c['name']}` {c['conclusion'] or c['status']}" for c in review.ci)
                            or "no checks ran"), ""]
    if review.skipped_files:
        lines += ["Not reviewed: " + ", ".join(f"`{s['path']}` ({s['reason']})" for s in review.skipped_files[:15]), ""]

    lines += ["| Validator | Result |", "|---|---|"]
    for r in review.results:
        if r["status"] == "failed":
            result = f"⚠️ failed: {r['error']}"
        else:
            result = f"{len(r['findings'])} finding(s)" if r["findings"] else "no findings"
            if r["status"] == "partial":
                result += f" (incomplete: {r['error']})"
        lines.append(f"| {r['display_name']} | {result.replace('|', '/')} |")

    findings = [(r["display_name"], f) for r in review.results for f in r["findings"]]
    if findings:
        lines += ["", "### Findings", ""]
        for name, f in findings:
            loc = f.get("file") or "PR-level"
            if f.get("line"):
                loc += f":{f['line']}"
            lines.append(f"- {SEV_ICON.get(f['severity'], '')} **{f['severity']}** · {name} · "
                         f"`{f.get('category', '?')}` · `{loc}`  \n  {f['description']}")
            if f.get("suggestedFix"):
                lines.append(f"  _Suggested fix:_ {f['suggestedFix']}")

    dropped = [(r["display_name"], x) for r in review.results for x in r.get("rejected") or []]
    if dropped:
        lines += ["", "<details><summary>Dropped after re-check</summary>", ""]
        lines += [f"- {name} ({x['severity']}): {x['description']}  \n  _Reason:_ {x['reason']}" for name, x in dropped]
        lines += ["", "</details>"]

    lines += ["", "<sub>Automated review by Vader's validators on local open-weight models. Findings can be "
              "wrong; a person decides.</sub>"]
    text = "\n".join(lines)
    return text if len(text) <= GITHUB_LIMIT else text[:GITHUB_LIMIT] + "\n\n_(truncated)_"
