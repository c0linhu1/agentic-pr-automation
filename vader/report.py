"""The written report of a finished review, as GitHub-flavored Markdown (for the PR comment)."""
from .decision import HOLD
from .engine import Review

VERDICT_ICON = {"BLOCK": "⛔", "HOLD": "✋", "MINOR": "🟡", "PASS": "✅"}
MERGE_ICON = {"merged": "🔀", "conflict": "❌", "skipped": "⏸️", "failed": "⚠️"}
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪", "info": "ℹ️"}
GITHUB_LIMIT = 65_000  # GitHub's comment limit is 65,536 characters


def _duration(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f} min"


def _result(r: dict) -> str:
    """One validator's cell in the results table."""
    if r["status"] == "failed":
        text = f"⚠️ failed: {r['error']}"
    else:
        text = f"{len(r['findings'])} finding(s)" if r["findings"] else "no findings"
        if r.get("omitted"):
            text += f", and {r['omitted']} lower-severity one(s) not shown"
        if r["status"] == "partial":
            text += f" (incomplete: {r['error']})"
    if r.get("dropped"):
        text += f" ({r['dropped']} malformed finding(s) dropped)"
    if r.get("note"):
        text += f" ({r['note']})"
    return " ".join(text.replace("|", "/").split())  # a pipe or line break would end the table row


def markdown(review: Review) -> str:
    verdict = review.verdict or HOLD
    took = (review.finished or 0) - (review.started or review.finished or 0)
    models = sorted(set(review.models.values())) or [review.model]
    sha = (review.meta.get("sha") or "")[:7]

    lines = [f"## {VERDICT_ICON.get(verdict, '')} Vader review: **{verdict}**", "",
             f"{review.verdict_reason}", "",
             f"Commit `{sha}` · {len(review.files)} file(s)"
             + (f" in {review.parts} parts" if review.parts > 1 else "")
             + f" · model(s): {', '.join(f'`{m}`' for m in models)} · {_duration(took)}", ""]
    if review.merge:
        lines += [f"{MERGE_ICON.get(review.merge['status'], '')} {review.merge['message']}", ""]
    if review.ci is not None:
        lines += ["CI: " + (", ".join(f"`{c['name']}` {c['conclusion'] or c['status']}" for c in review.ci)
                            or "no checks ran"), ""]
    if review.skipped_files:
        skipped = ", ".join(f"`{s['path']}` ({s['reason']})" for s in review.skipped_files[:15])
        lines += [f"Not reviewed: {skipped}", ""]

    lines += ["| Validator | Result |", "|---|---|"]
    lines += [f"| {r['display_name']} | {_result(r)} |" for r in review.results]

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
        lines += ["", "<details><summary>Dropped after checking (quote not found, or rejected by the verify pass)"
                      "</summary>", ""]
        for name, x in dropped:
            lines.append(f"- {name} ({x['severity']}): {x['description']}  \n  _Reason:_ {x['reason']}")
            if x.get("evidence"):  # so a person can see whether the drop was right
                quoted = " ".join(x["evidence"].split()).replace("`", "'")
                lines.append(f"  _It quoted:_ `{quoted[:200]}`" + ("…" if len(quoted) > 200 else ""))
        lines += ["", "</details>"]

    lines += ["", "<sub>Automated review by Vader's validators on local open-weight models. Findings can be "
              "wrong; a person decides.</sub>"]
    text = "\n".join(lines)
    return text if len(text) <= GITHUB_LIMIT else text[:GITHUB_LIMIT] + "\n\n_(truncated)_"
