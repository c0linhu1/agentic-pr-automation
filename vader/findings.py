"""Parses and validates validator output against the shared Finding schema.
Output that doesn't parse counts as a FAILED review, never as a clean one."""
import json
import re
from dataclasses import dataclass

SEVERITIES = ["critical", "high", "medium", "low", "info"]
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)

# The reply shape, enforced by the server as a JSON schema (structured outputs).
SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": SEVERITIES},
            "category": {"type": "string"},
            "file": {"type": ["string", "null"]},
            "line": {"type": ["integer", "null"]},
            "description": {"type": "string"},
            "suggestedFix": {"type": ["string", "null"]},
        },
        "required": ["severity", "category", "file", "line", "description", "suggestedFix"],
        "additionalProperties": False,
    }}},
    "required": ["findings"],
    "additionalProperties": False,
}


# The reply shape for the verify pass.
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["confirmed", "rejected"]}, "reason": {"type": "string"}},
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


@dataclass
class Parsed:
    findings: list[dict]
    failure: str | None = None  # set when the review must count as failed
    dropped: int = 0  # malformed findings that were thrown away


def parse_findings(raw: str) -> Parsed:
    try:
        data = json.loads(FENCE.sub("", raw).strip())
    except json.JSONDecodeError:
        return Parsed([], "output was not valid JSON")
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return Parsed([], "JSON had no findings list")

    findings, dropped = [], 0
    for f in items:
        if not isinstance(f, dict) or str(f.get("severity", "")).lower() not in SEVERITIES or not f.get("description"):
            dropped += 1
            continue
        f["severity"] = f["severity"].lower()
        findings.append(f)
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return Parsed(findings, None, dropped)
