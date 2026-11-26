"""What happens to a validator's reply, in order: parse it against the Finding schema (a reply that doesn't
parse is a FAILED review, never a clean one), check each finding's quote against the code it was shown,
then, with repeated asks, combine the asks' findings."""
import copy
import json
import re
from dataclasses import dataclass
from datetime import date

SEVERITIES = ["critical", "high", "medium", "low", "info"]
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)

# The reply shape, enforced by the server as a JSON schema (structured outputs). The model writes the
# fields in this order, so it quotes the code and describes the problem before it rates the severity.
FIELDS = ["file", "line", "evidence", "description", "severity", "category", "suggestedFix"]
SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "file": {"type": ["string", "null"]},
            "line": {"type": ["integer", "null"]},
            "evidence": {"type": ["string", "null"]},
            "description": {"type": "string"},
            "severity": {"type": "string", "enum": SEVERITIES},
            "category": {"type": "string"},
            "suggestedFix": {"type": ["string", "null"]},
        },
        "required": FIELDS,
        "additionalProperties": False,
    }}},
    "required": ["findings"],
    "additionalProperties": False,
}


def schema_for(categories: tuple[str, ...]) -> dict:
    """The reply shape with `category` limited to these names, so a validator can't invent one. With no
    names (a skill without a Scope table), any category is allowed."""
    if not categories:
        return SCHEMA
    schema = copy.deepcopy(SCHEMA)
    schema["properties"]["findings"]["items"]["properties"]["category"]["enum"] = list(categories)
    return schema


# The reply shape for the verify pass: the reason first, so the verdict follows from it.
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "verdict": {"type": "string", "enum": ["confirmed", "rejected"]}},
    "required": ["reason", "verdict"],
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


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")
ELLIPSIS = {"...", "…", "// ...", "# ..."}
MIN_QUOTE_CHARS = 4  # a quote of only `}` or `);` shows nothing
WORD = re.compile(r"\w+")
GAP = re.compile(r"\.\.\.|…")
MIN_WORDS = 3  # matching by words needs at least this many, or nearly anything would match


@dataclass
class CodeLine:
    text: str          # whitespace collapsed, case folded
    number: int | None  # line in the file after the change; None for removed lines
    changed: bool       # added or removed by the diff


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def code_lines(diff: str, context: str = "") -> tuple[list[CodeLine], list[CodeLine]]:
    """A file's lines as the validators saw them: (the diff's lines, the full file's lines)."""
    lines, new = [], None
    for raw in diff.splitlines():
        if m := HUNK.match(raw):
            new = int(m[1])
        elif new is None:  # before the first hunk: git headers, or a pasted diff without hunk headers
            if not raw.startswith(("diff --git", "index ", "--- a/", "+++ b/", "--- /dev/null", "+++ /dev/null")):
                lines.append(CodeLine(_norm(raw[1:] if raw[:1] in "+- " else raw), None, raw[:1] in "+-"))
        elif raw.startswith("-"):
            lines.append(CodeLine(_norm(raw[1:]), None, True))
        elif not raw.startswith("\\"):  # "\ No newline at end of file"
            lines.append(CodeLine(_norm(raw[1:]), new, raw.startswith("+")))
            new += 1
    return lines, [CodeLine(_norm(t), i, False) for i, t in enumerate(context.splitlines(), 1)]


def _quote(evidence) -> list[set[str]]:
    """The quote's lines, each with and without a pasted diff marker. Empty if it shows nothing."""
    out = []
    for raw in str(evidence or "").splitlines():
        text = _norm(raw)
        if text and text not in ELLIPSIS:
            out.append({text, _norm(raw.strip()[1:])} if raw.strip()[:1] in "+-" else {text})
    return out if sum(len(max(q, key=len)) for q in out) >= MIN_QUOTE_CHARS else []


def _same_words(evidence: str, lines: list[CodeLine]) -> tuple[bool, int | None]:
    """For a quote rewrapped onto fewer lines or shortened with "...": its words (names, keywords, values) in
    order, ignoring spacing and punctuation. Each piece between gaps must appear unbroken, the pieces in order.
    Made-up code fails: its words aren't there in that order."""
    words, where = [], []
    for c in lines:
        for w in WORD.findall(c.text):
            words.append(w)
            where.append(c.number)
    pieces = [p for p in (WORD.findall(_norm(s)) for s in GAP.split(evidence)) if p]
    if sum(map(len, pieces)) < MIN_WORDS:
        return False, None
    start, first = 0, None
    for piece in pieces:
        at = next((i for i in range(start, len(words) - len(piece) + 1) if words[i:i + len(piece)] == piece), None)
        if at is None:
            return False, None
        first = at if first is None else first
        start = at + len(piece)
    return True, where[first]


def _find(evidence: str, quote: list[set[str]], lines: list[CodeLine], near: int | None) -> tuple[bool, int | None]:
    """Whether the quote is in these lines, and the file line its first line is on."""
    def hits(q):
        return [c for c in lines if any(v and v in c.text for v in q)]
    if not all(hits(q) for q in quote):
        return _same_words(evidence, lines)
    numbered = [c for c in hits(quote[0]) if c.number]
    if not numbered:
        return True, None
    if near:
        return True, min(numbered, key=lambda c: abs(c.number - near)).number
    return True, next((c for c in numbered if c.changed), numbered[0]).number


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DAY = r"(?P<day>(?:mon|tues|wednes|thurs|fri|satur|sun)day)"
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE = (r"(?:(?P<y1>\d{4})-(?P<m1>\d{1,2})-(?P<d1>\d{1,2})"                       # 2028-04-13
         r"|\(\s*(?P<y2>\d{4})\s*,\s*(?P<m2>\d{1,2})\s*,\s*(?P<d2>\d{1,2})\s*\)"    # DateOnly(2028, 4, 13)
         rf"|(?P<mon3>{_MON})\s+(?P<d3>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y3>\d{{4}})"   # April 13, 2028
         rf"|(?P<d4>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon4>{_MON}),?\s+(?P<y4>\d{{4}}))")  # 13 April 2028
# "2028-04-13 is a Thursday", "2028-04-13 (Thursday)", "Thursday, April 13, 2028". "is not a" doesn't match.
DATE_CLAIMS = [re.compile(_DATE + r"\)?\s*[(,]?\s*(?:which\s+)?(?:is|was|falls\s+on|lands\s+on)?\s*(?:an?\s+)?"
                          + _DAY + r"\b", re.I),
               re.compile(r"\b" + _DAY + r",?\s+(?:the\s+)?" + _DATE, re.I)]


def wrong_weekday(finding: dict) -> str | None:
    """A weekday the finding gets wrong for a date it names, worked out here rather than trusted
    (models get these wrong often). None when every weekday it states is right, or it states none."""
    text = " ".join(str(finding.get(k) or "") for k in ("description", "suggestedFix"))
    for pattern in DATE_CLAIMS:
        for m in pattern.finditer(text):
            g = {k: v for k, v in m.groupdict().items() if v}
            n = next(i for i in "1234" if "d" + i in g)
            month = int(g["m" + n]) if "m" + n in g else MONTHS.index(g["mon" + n][:3].lower()) + 1
            try:
                actual = date(int(g["y" + n]), month, int(g["d" + n]))
            except ValueError:  # not a real date; nothing to check
                continue
            said = g["day"].lower()
            if WEEKDAYS[actual.weekday()] != said:
                return (f"it says {actual.isoformat()} is a {said.title()}, but it's a "
                        f"{WEEKDAYS[actual.weekday()].title()}")
    return None


def ground(finding: dict, code: dict[str, tuple[list[CodeLine], list[CodeLine]]], pr_text: str = "") -> str | None:
    """Check a finding against what the validator was shown: its quote must be in the code, or in the PR's
    title, ticket, and description (`pr_text`, for claims about the PR), and any weekday it states for a date
    must be right. A quote from the code sets the finding's file to the PR's path and its line to where the
    quote is. Returns why it should be dropped, or None to keep it. A finding with no quote is kept as it is
    unless it gets a weekday wrong."""
    if why := wrong_weekday(finding):
        return why
    named = str(finding.get("file") or "").strip().removeprefix("a/").removeprefix("b/")
    matches = [p for p in code if named and (p == named or p.endswith("/" + named))]
    path = matches[0] if len(matches) == 1 else None
    if path:
        finding["file"] = path
    evidence = str(finding.get("evidence") or "")
    quote = _quote(evidence)
    if not quote:
        return None
    order = ([path] if path else []) + [p for p in code if p != path]
    for p in order:
        diff, context = code[p]
        for lines in (diff, context):
            found, line = _find(evidence, quote, lines, finding.get("line") if p == path else None)
            if found:
                finding["file"] = p
                if line:
                    finding["line"] = line
                return None
    # A quote that joins lines from several files (a setting and the code that reads it): each line must be in one
    def first_place(q):
        for p in order:
            for lines in code[p]:
                for c in lines:
                    if any(v and v in c.text for v in q):
                        return p, c
        return None

    where = [first_place(q) for q in quote]
    if len(quote) > 1 and all(where):
        p, c = where[0]
        finding["file"] = p
        if c.number:
            finding["line"] = c.number
        return None
    if pr_text and _find(evidence, quote, [CodeLine(_norm(t), None, False) for t in pr_text.splitlines()], None)[0]:
        return None
    return (f"the code it quotes isn't in {path}" if path else
            f"the code it quotes isn't in the PR{f' (it names {named})' if named else ''}")


SAME_ISSUE_LINES = 3  # two asks pointing within this many lines of each other in a file mean the same thing


def _same_issue(a: dict, b: dict) -> bool:
    if a.get("file") != b.get("file"):
        return False
    if a.get("line") and b.get("line"):
        return abs(a["line"] - b["line"]) <= SAME_ISSUE_LINES
    return a.get("category") == b.get("category")


def agreed(findings: list[dict], asks: int) -> list[dict]:
    """With several asks (each finding tagged `_ask`), marks `_verify` a finding below high that most asks
    didn't report: it may be a one-off false alarm, so the verify pass re-checks it rather than it being
    dropped (a real bug is sometimes caught by one ask only). Critical and high ones are re-checked anyway."""
    if asks < 2:
        return findings
    for f in findings:
        if (f["severity"] not in ("critical", "high")
                and len({g.get("_ask") for g in findings if _same_issue(f, g)}) <= asks // 2):
            f["_verify"] = True
    return findings


def merge_asks(findings: list[dict]) -> list[dict]:
    """One finding per issue across repeated asks: copies of it from other asks of the same part become the
    most severe copy, as that copy wrote it. Two findings from the same ask are never merged. When a finding
    could join several, it joins the one with the closest line."""
    def gap(f, g):
        return abs(f["line"] - g["line"]) if f.get("line") and g.get("line") else 0

    kept: list[tuple[dict, set]] = []  # (the copy kept, the asks merged into it)
    for f in sorted(findings, key=lambda f: SEVERITIES.index(f["severity"])):
        match = min((k for k in kept if k[0].get("_part") == f.get("_part") and f.get("_ask") not in k[1]
                     and _same_issue(f, k[0])), key=lambda k: gap(f, k[0]), default=None)
        if match:
            match[1].add(f.get("_ask"))
        else:
            kept.append((f, {f.get("_ask")}))
    return [f for f, _ in kept]
