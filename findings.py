"""Parses and validates reviewer output against the shared Finding schema,
and formats it for Slack. Output that doesn't parse counts as a FAILED review,
never as a clean one."""
import json
import re

SEVERITIES = ["critical", "high", "medium", "low", "info"]
ICONS = {"critical": ":red_circle:", "high": ":large_orange_circle:", "medium": ":large_yellow_circle:",
         "low": ":white_circle:", "info": ":information_source:"}
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_findings(raw: str) -> tuple[list[dict], str | None]:
    """Returns (findings, error). error is set when the review should count as failed."""
    try:
        data = json.loads(FENCE.sub("", raw).strip())
    except json.JSONDecodeError:
        return [], "output was not valid JSON"
    items = data.get("findings") if isinstance(data, dict) else data if isinstance(data, list) else None
    if not isinstance(items, list):
        return [], "JSON had no findings list"

    findings, dropped = [], 0
    for f in items:
        if not isinstance(f, dict) or str(f.get("severity", "")).lower() not in SEVERITIES or not f.get("description"):
            dropped += 1
            continue
        f["severity"] = f["severity"].lower()
        findings.append(f)
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return findings, (f"{dropped} malformed finding(s) dropped" if dropped else None)


def to_slack(agent_name: str, model: str, findings: list[dict], error: str | None) -> str:
    header = f"*{agent_name}* ({model})"
    if error and not findings and "dropped" not in error:
        return f":warning: {header}: review FAILED, {error}. This counts as missing evidence, not a clean review."
    lines = [f"{header}: {len(findings)} finding(s)" if findings else f":white_check_mark: {header}: no findings"]
    for f in findings:
        loc = f.get("file") or "PR-level"
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(f"{ICONS[f['severity']]} *{f['severity']}* `{f.get('category', '?')}` {loc}\n{f['description']}")
        if f.get("suggestedFix"):
            lines.append(f"    _fix:_ {f['suggestedFix']}")
    if error:
        lines.append(f":warning: {error}")
    return "\n".join(lines)
