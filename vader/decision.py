"""The release verdict, a pure function of the validators' results:
BLOCK on any critical; HOLD on any high, a failed validator, or missing required CI;
MINOR on only medium/low or failed CI; otherwise PASS."""
from collections import Counter

BLOCK, HOLD, MINOR, PASS = "BLOCK", "HOLD", "MINOR", "PASS"


def decide(results: list[dict], ci_status: str | None = None, require_ci: bool = False) -> tuple[str, str]:
    """results: [{"status": "ok" | "partial" | "failed", "findings": [...]}, ...]
    "partial" means a validator reviewed only some parts of a large PR (an error or the time
    limit stopped it); its findings still count, and it also counts as missing evidence.
    ci_status: "passed", "failed", or None (unknown: no CI connected, still running, or unreadable)."""
    if not results:
        return HOLD, "no validators ran"

    sev = Counter(f["severity"] for r in results for f in r["findings"])
    failed = sum(r["status"] != "ok" for r in results)

    if sev["critical"]:
        return BLOCK, f"{sev['critical']} critical finding(s)"

    hold = []
    if sev["high"]:
        hold.append(f"{sev['high']} high finding(s)")
    if failed:
        hold.append(f"{failed} validator(s) didn't finish, so evidence is missing")
    if require_ci and ci_status is None:
        hold.append("CI results are missing")
    if hold:
        return HOLD, "; ".join(hold)

    minor = []
    if sev["medium"] or sev["low"]:
        minor.append(f"{sev['medium']} medium and {sev['low']} low finding(s)")
    if ci_status == "failed":
        minor.append("CI failed")
    if minor:
        return MINOR, "; ".join(minor)

    return PASS, "no findings above info" + ("; CI passed" if ci_status == "passed" else "; no CI results")
