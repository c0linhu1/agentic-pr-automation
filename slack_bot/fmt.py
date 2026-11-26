"""Turns Vader's replies and events into Slack messages."""
SEV_ICON = {"critical": ":red_circle:", "high": ":large_orange_circle:", "medium": ":large_yellow_circle:",
            "low": ":white_circle:", "info": ":information_source:"}
VERDICT_ICON = {"BLOCK": ":no_entry:", "HOLD": ":raised_hand:", "MINOR": ":large_yellow_circle:",
                "PASS": ":white_check_mark:"}
MERGE_ICON = {"merged": ":twisted_rightwards_arrows:", "conflict": ":x:", "skipped": ":double_vertical_bar:",
              "failed": ":warning:"}
LIMIT = 39_000  # Slack takes up to 40,000 characters a message
REPORT_TOP = 8  # most serious findings repeated in the end-of-review report

HELP = """*Vader* runs six validators on a change and gives a verdict. Commands:
`/vader status` · `/vader stop` · `/vader start`
`/vader auto on` · `/vader auto off` (automatic PR reviews)
`/vader models` · `/vader model <name>`
`/vader pr <number or link>` (one GitHub PR, all six validators) · `/vader pr <number> <validator>`
`/vader review <input>` (all six validators) · `/vader run <validator> <input>`
Validators: security, correctness, qa, architecture, compliance, performance
Input can be a bare diff, or labeled sections (new lines with Shift+Enter):
`TITLE:` · `TICKET:` · `DESCRIPTION:` · `DIFF:`"""


def unescape(text: str) -> str:
    """Slack sends &, <, and > in message and command text as &amp;, &lt;, and &gt;; pasted code needs them
    back."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def esc(text) -> str:
    """Text from a PR or a model, made safe to post: <!channel>, <@user>, and <url|label> show as written
    instead of pinging people or hiding links."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cut(text: str) -> str:
    return text if len(text) <= LIMIT else text[:LIMIT] + "\n_(truncated)_"


def status(s: dict) -> str:
    lines = [f"*Vader* is {':large_green_circle: running' if s['enabled'] else ':red_circle: stopped'}"
             f" · automation {'on' if s['automation'] else 'off'} · model `{s['model']}`"]
    server, loaded = s.get("model_server") or {}, s.get("loaded")
    if not server.get("reachable", True):
        lines.append(f":warning: Vader can't reach the model server at {esc(server.get('url', '?'))}")
    elif loaded is None:
        lines.append("Loaded: up to the model server")
    else:
        lines.append("Loaded: " + (", ".join(f"`{m['name']}` ({m['size_gb']} GB)" for m in loaded) or "nothing yet"))
    run = s.get("running")
    if run:
        lines.append(f":gear: Reviewing _{esc(run['title'] or run['id'])}_: {run['done']}/{run['total']} validators "
                     f"done{', now ' + run['current'] if run['current'] else ''} · {minutes(run['elapsed_s'])}"
                     + (f" of ~{minutes(run['estimate_s'])}" if run.get("estimate_s") else ""))
    queued = "".join(f"\n  • _{esc(q['title'] or q['id'])}_ ({q['source']})" for q in s["queue"])
    lines.append(f"Queue: {len(s['queue'])} waiting{queued}")
    gh = s["github"]
    lines.append("GitHub: " + (", ".join(gh["repos"]) if gh["configured"] else "not configured"))
    lines.append("Auto-merge: " + (f"PASS → `{esc(s['auto_merge'])}`" if s.get("auto_merge") else "off")
                 + " · Closing linked issues: " + ("on" if s.get("close_issues") else "off"))
    return "\n".join(lines)


def issue_closed(e: dict) -> str:
    how = "was merged" if e["merged"] else "was closed without merging"
    return (f":white_check_mark: Closed issue <{e['url']}|{e['repo']}#{e['issue']}> _{esc(e['title'])}_: "
            f"PR #{e['pr']} {how}.")


def merge(status: str, message: str) -> str:
    """What happened when Vader tried to merge a PR that passed (the message can include the PR's branch name)."""
    return f"{MERGE_ICON.get(status, '')} {esc(message)}"


def models(m: dict) -> str:
    lines = [f"Active model: `{m['active']}` (for validators without their own in models.yaml)"]
    if m.get("validators"):
        lines.append("Validators: " + " · ".join(f"{k} `{model}`" for k, model in m["validators"].items()))
    for e in m["models"]:
        flags = []
        if not e["enabled"]:
            flags.append("disabled")
        if e["available"] is False:
            flags.append("not on the model server")
        mark = ":arrow_right:" if e["name"] == m["active"] else "•"
        lines.append(f"{mark} `{e['name']}` _{e['tier']}_" + (f" ({', '.join(flags)})" if flags else "")
                     + (f": {e['note']}" if e["note"] else ""))
    return "\n".join(lines)


def minutes(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f} min"


def model_list(review: dict) -> str:
    """`a` for one model; `a` (security, qa), `b` (correctness) for several."""
    groups: dict[str, list[str]] = {}
    for key in review["validators"]:
        groups.setdefault(review.get("models", {}).get(key, review["model"]), []).append(key)
    if len(groups) == 1:
        return f"`{next(iter(groups))}`"
    return ", ".join(f"`{m}` ({', '.join(keys)})" for m, keys in groups.items())


def started(review: dict) -> str:
    text = (f":gear: Vader started the review on {model_list(review)} with {len(review['validators'])} validator(s)"
            f" · {len(review['files'])} file(s)")
    if review["parts"] > 1:
        text += f" in {review['parts']} parts (too large to review at once)"
    if review.get("ci") is not None:
        text += "\nCI: " + (", ".join(f"{esc(c['name'])} {c['conclusion'] or c['status']}" for c in review["ci"])
                            or "no checks ran")
    skipped = review["skipped_files"]
    if skipped:
        shown = ", ".join(f"`{esc(s['path'])}` ({s['reason']})" for s in skipped[:8])
        text += f"\nNot reviewed: {shown}" + (f" and {len(skipped) - 8} more" if len(skipped) > 8 else "")
    return text


def estimate(estimate_s: float, limit_s: float | None, calls: int) -> str:
    text = f":stopwatch: Estimated about {minutes(estimate_s)} ({calls} model call(s))"
    if limit_s and estimate_s > limit_s:
        text += (f". That's over the {minutes(limit_s)} limit, so whatever isn't done by then will be skipped "
                 "and the verdict will be HOLD.")
    return text


def validator_result(r: dict) -> str:
    model = f" · `{r['model']}`" if r.get("model") else ""
    if r["status"] == "failed":
        return (f":warning: Review FAILED: {esc(r['error'])}. This counts as missing evidence, not a clean "
                f"review.{model}")
    if not r["findings"]:
        text = f":white_check_mark: No findings{model}"
    else:
        lines = [f"{len(r['findings'])} finding(s) · {r['seconds']}s{model}"]
        for f in r["findings"]:
            loc = f.get("file") or "PR-level"
            if f.get("line"):
                loc += f":{f['line']}"
            lines.append(f"{SEV_ICON[f['severity']]} *{f['severity']}* `{esc(f.get('category', '?'))}` "
                         f"{esc(loc)}\n{esc(f['description'])}")
            if f.get("suggestedFix"):
                lines.append(f"    _fix:_ {esc(f['suggestedFix'])}")
        text = "\n".join(lines)
    for x in r.get("rejected") or []:
        text += (f"\n:wastebasket: _Dropped after checking ({x['severity']}): {esc(x['description'][:150])} "
                 f"Reason: {esc(x['reason'])}_")
    if r.get("omitted"):
        text += f"\n_{r['omitted']} lower-severity finding(s) not shown_"
    if r.get("dropped"):
        text += f"\n:warning: {r['dropped']} malformed finding(s) dropped"
    if r["status"] == "partial":
        text += f"\n:warning: Incomplete ({esc(r['error'])}). This counts as missing evidence."
    if r.get("note"):
        text += f"\n:information_source: {esc(r['note'][0].upper() + r['note'][1:])}"
    return cut(text)


def verdict(review: dict) -> str:
    """The end-of-review report: verdict, one line per validator, and the most serious findings."""
    v = review["verdict"]
    seconds = round((review["finished"] or 0) - (review["started"] or review["finished"] or 0))
    lines = [f"{VERDICT_ICON.get(v, '')} *Verdict: {v}*: {review['verdict_reason']} · {model_list(review)} · "
             f"{minutes(seconds)}"]
    m = review.get("meta") or {}
    if m.get("url"):
        lines[0] += f" · <{m['url']}|{m['repo']}#{m['number']}>"

    lines.append("*Summary*")
    for r in review["results"]:
        if r["status"] == "failed":
            status = f":warning: failed: {esc(r['error'])}"
        elif not r["findings"]:
            status = ":white_check_mark: no findings"
        else:
            worst = r["findings"][0]["severity"]  # findings are sorted most severe first
            status = f"{SEV_ICON[worst]} {len(r['findings'])} finding(s), worst *{worst}*"
        if r["status"] == "partial":
            status += " (incomplete)"
        if r.get("note"):
            status += " (answered without thinking)"
        lines.append(f"{r.get('icon', '')} {r['display_name']}: {status}")

    serious = [(r["display_name"], f) for r in review["results"] for f in r["findings"]
               if f["severity"] in ("critical", "high")]
    if serious:
        lines.append("*Most serious*")
        for name, f in serious[:REPORT_TOP]:
            loc = (f.get("file") or "PR-level") + (f":{f['line']}" if f.get("line") else "")
            desc = f["description"] if len(f["description"]) <= 200 else f["description"][:200] + "…"
            lines.append(f"{SEV_ICON[f['severity']]} *{f['severity']}* · {name} · `{esc(loc)}`: {esc(desc)}")
        if len(serious) > REPORT_TOP:
            lines.append(f"_…and {len(serious) - REPORT_TOP} more in the thread_")
    if review["results"]:
        lines.append("_Each validator's full findings are in this thread._")
    return cut("\n".join(lines))


def pr_header(review: dict, position: int) -> str:
    m = review["meta"]
    return (f":mag: PR <{m['url']}|{m['repo']}#{m['number']}> _{esc(review['title'])}_ by `{esc(m['author'])}`"
            + (f"\nLinked ticket: #{m['linked_issue']}" if m.get("linked_issue") else "\nNo linked ticket")
            + f"\nQueued for review (position {position})")
