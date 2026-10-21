"""The review queue: one review at a time, validators grouped by model, every validator finishes.
Large PRs are split into parts (diffs.py); a time limit can cut slow reviews short (HOLD).
Front-ends follow progress through Events."""
import copy
import itertools
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, fields
from typing import Callable

from . import config, llm, models
from .agents import VALIDATORS
from .decision import decide
from .diffs import FileDiff, filter_files, pack, pack_with_context, split_files
from .findings import SEVERITIES, VERIFY_SCHEMA, parse_findings
from .state import State

log = logging.getLogger("vader.engine")

MAX_FINDINGS = 10        # per validator, after merging parts
KEEP_REVIEWS = 200       # finished reviews kept in memory
MAX_LISTED_FILES = 150   # file list given to every part
SAFETY_CHARS = 2000      # context headroom for chat formatting

VERIFY_TASK = ("Another reviewer reported the finding below about this change. Check it carefully against the "
               "code above before it is posted: work out step by step any fact it depends on (a date's "
               "weekday, what a called function does, how a library call behaves). Answer \"rejected\" if the "
               "code shows the finding is wrong or doesn't support it, and \"confirmed\" only if the code "
               "clearly shows the problem. Give the reason in one or two sentences.\n\nFinding: ")

SECTION = re.compile(r"^(TITLE|TICKET|DESCRIPTION|DIFF):[ \t]*", re.M)


def parse_sections(text: str) -> dict[str, str]:
    """Split `TITLE: ... DIFF: ...` input into fields. Unlabeled input is all diff."""
    parts = SECTION.split(text)
    if len(parts) == 1:
        return {"diff": text.strip()}
    out = {}
    for label, body in zip(parts[1::2], parts[2::2]):
        out[label.lower()] = body.strip()
    return out


@dataclass
class Review:
    source: str  # "slack" or "github"
    validators: list[str]
    title: str = ""
    description: str = ""
    ticket: str = ""
    diff: str = ""
    meta: dict = field(default_factory=dict)  # PR number, repo, url, author, copilot, ...
    reply_to: dict | None = None  # opaque to Vader; set by a front-end
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = "queued"  # queued -> running -> done
    model: str | None = None  # the active model, for validators without their own
    models: dict[str, str] = field(default_factory=dict)  # validator key -> model it ran on
    current: str | None = None
    results: list[dict] = field(default_factory=list)
    verdict: str | None = None
    verdict_reason: str | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    files: list[str] = field(default_factory=list)          # files reviewed
    skipped_files: list[dict] = field(default_factory=list) # [{"path", "reason"}] not reviewed
    parts: int = 1
    estimate_s: float | None = None
    time_limited: bool = False
    ci: list[dict] | None = None  # the PR commit's check runs; None = unknown
    context: dict[str, str] = field(default_factory=dict, repr=False)  # path -> full file after the change
    _parts: list[list[FileDiff]] = field(default_factory=list, repr=False)
    _calls_done: int = field(default=0, repr=False)

    @property
    def ci_status(self) -> str | None:
        """"passed", "failed", or None (no CI results, or still running)."""
        if not self.ci or any(c["status"] != "completed" for c in self.ci):
            return None
        bad = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
        return "failed" if any(c["conclusion"] in bad for c in self.ci) else "passed"

    def ci_line(self) -> str:
        if self.ci is None:
            return ""
        if not self.ci:
            return "CI: no checks ran for this commit."
        runs = ", ".join(f"{c['name']} {c['conclusion'] or c['status']}" for c in self.ci)
        meaning = {"passed": "every check passed, so the PR's tests run and pass (that doesn't prove the tests "
                             "themselves are right)",
                   "failed": "at least one check failed",
                   None: "still running"}[self.ci_status]
        return f"CI: {runs}. {meaning[0].upper() + meaning[1:]}."

    def header(self) -> str:
        """Everything a validator sees besides the diff itself."""
        lines = [f"TITLE: {self.title}" if self.title else "",
                 f"TICKET: {self.ticket}" if self.ticket else "",
                 f"DESCRIPTION:\n{self.description}" if self.description else "",
                 self.ci_line()]
        if self.parts > 1 or self.skipped_files:
            listed = self.files[:MAX_LISTED_FILES]
            more = len(self.files) - len(listed)
            lines.append(f"FILES IN THIS PR ({len(self.files)} reviewed):\n" + "\n".join(f"- {p}" for p in listed)
                         + (f"\n- ... and {more} more" if more else ""))
        if self.skipped_files:
            lines.append("NOT REVIEWED (generated, lockfile, or binary): "
                         + ", ".join(s["path"] for s in self.skipped_files[:30]))
        return "\n\n".join(x for x in lines if x)

    def prompt(self, part: int | None = None) -> str:
        if not self._parts:
            return "\n\n".join(x for x in [self.header(), f"DIFF:\n{self.diff}"] if x)
        note = ""
        if self.parts > 1:
            note = (f"NOTE: this PR is too large to review at once, so it is split into {self.parts} parts. This "
                    f"is part {part + 1} of {self.parts}. Files not shown here are reviewed in other parts, so do "
                    "not report something as missing only because it isn't in this part.")
        files = self._parts[part]
        diff = "\n".join(f.text for f in files)
        context = "\n\n".join(f"=== {f.path} ===\n{f.context}" for f in files if f.context)
        if context:
            context = ("CONTEXT (full contents of changed files after this change, so you can see the code "
                       "around the diff; review only what the DIFF changes):\n" + context)
        return "\n\n".join(x for x in [self.header(), note, context, f"DIFF:\n{diff}"] if x)

    def summary(self) -> dict:
        """A snapshot without the large text fields (events keep it as it was at that moment)."""
        skip = {"diff", "description", "ticket", "context", "_parts", "_calls_done"}
        return copy.deepcopy({f.name: getattr(self, f.name) for f in fields(self) if f.name not in skip})


class Events:
    """Numbered notifications for front-ends to poll. boot_id changes on every restart,
    so a front-end knows to reset its position."""

    def __init__(self, keep: int = 1000):
        self.boot_id = uuid.uuid4().hex[:8]
        self._items: deque[dict] = deque(maxlen=keep)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, type_: str, **data) -> None:
        with self._lock:
            self._items.append({"seq": next(self._seq), "type": type_, "time": time.time(), **data})

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._items if e["seq"] > seq]

    def latest(self) -> int:
        with self._lock:
            return self._items[-1]["seq"] if self._items else 0


class Stopped(Exception):
    """Vader is stopped and isn't accepting reviews."""


class Engine:
    def __init__(self, state: State, events: Events,
                 complete: Callable[..., str] = llm.complete,
                 prepare_model: Callable[[str], None] = models.prepare,
                 assignments: Callable[[], dict[str, str]] = models.assignments):
        self.state, self.events = state, events
        self._complete, self._prepare, self._assignments = complete, prepare_model, assignments
        self.before_review: Callable[[Review], None] | None = None  # e.g. fetch CI results for the PR
        self.on_done: Callable[[Review], None] | None = None  # e.g. post a comment on the PR
        self._queue: deque[Review] = deque()
        self._reviews: dict[str, Review] = {}  # insertion order = oldest first
        self._running: Review | None = None
        self._last_model: str | None = None  # the model left loaded by the previous review
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._work, name="vader-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, review: Review) -> int:
        """Queue a review; returns its position (1 = next up). Raises Stopped."""
        if not self.state.enabled:
            raise Stopped()
        unknown = [v for v in review.validators if v not in VALIDATORS]
        if unknown or not review.validators:
            raise ValueError(f"unknown validators: {unknown}")
        with self._cv:
            self._queue.append(review)
            self._reviews[review.id] = review
            self._prune()
            position = len(self._queue) + (1 if self._running else 0)
            self._cv.notify()
        self.events.add("review_queued", review=review.summary(), position=position)
        return position

    def _prune(self) -> None:
        """Forget the oldest finished reviews beyond KEEP_REVIEWS (caller holds the lock)."""
        extra = len(self._reviews) - KEEP_REVIEWS
        if extra <= 0:
            return
        for review_id in [r.id for r in self._reviews.values() if r.status == "done"][:extra]:
            del self._reviews[review_id]

    def get(self, review_id: str) -> Review | None:
        return self._reviews.get(review_id)

    def is_pending(self, pr_key: str, sha: str) -> bool:
        with self._cv:
            pending = list(self._queue) + ([self._running] if self._running else [])
        return any(r.meta.get("pr_key") == pr_key and r.meta.get("sha") == sha for r in pending)

    @property
    def busy(self) -> bool:
        return self._running is not None

    def wake(self) -> None:
        with self._cv:
            self._cv.notify()

    def status(self) -> dict:
        with self._cv:
            running, queued = self._running, list(self._queue)
        return {
            "running": None if not running else {
                "id": running.id, "title": running.title, "source": running.source,
                "model": running.models.get((running.current or "").split(" ")[0], running.model),
                "current": running.current, "done": len(running.results), "total": len(running.validators),
                "parts": running.parts, "estimate_s": running.estimate_s,
                "elapsed_s": round(time.time() - (running.started or time.time()))},
            "queue": [{"id": r.id, "title": r.title, "source": r.source} for r in queued],
        }

    def _work(self) -> None:
        while True:
            with self._cv:
                while not (self.state.enabled and self._queue):
                    self._cv.wait(timeout=5)
                review = self._queue.popleft()
                self._running = review
            try:
                self._run(review)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("review %s crashed", review.id)
                review.verdict, review.verdict_reason = "HOLD", "Vader crashed during the review; see its log"
                review.status, review.finished = "done", time.time()
                review.diff, review.context, review._parts = "", {}, []
                self._mark_reviewed(review)  # so the poller doesn't requeue it
                self.events.add("review_done", review=review.summary())
            finally:
                with self._cv:
                    self._running = None
            if self.on_done and review.status == "done":
                try:
                    self.on_done(review)
                except Exception:  # noqa: BLE001 - reporting must never stop the queue
                    log.exception("on_done hook failed for review %s", review.id)

    def _plan(self, review: Review) -> None:
        """Drop files nobody should review and split the rest into parts that fit the context."""
        kept, review.skipped_files = filter_files(split_files(review.diff))
        review.files = [f.path for f in kept]
        review.parts = 1
        longest_skill = max(len(VALIDATORS[k].instructions()) for k in review.validators)

        def budget() -> int:
            # llm.py estimates 1 token per 3 characters, so budget in the same units.
            chars = ((config.CONTEXT_TOKENS - config.OUTPUT_TOKENS) * 3 - longest_skill
                     - len(review.header()) - 800 - SAFETY_CHARS)  # 800: part note + CONTEXT heading
            return max(chars, 4000)

        def split() -> list[list[FileDiff]]:
            if not kept:
                return []
            if review.context:
                return pack_with_context(kept, review.context, budget())
            return pack(kept, budget())

        review._parts = split()
        review.parts = max(len(review._parts), 1)
        if review.parts > 1:  # leave room for the file list a split PR's header adds
            review._parts = split()
            review.parts = len(review._parts)

    def _deadline(self, review: Review) -> float | None:
        minutes = config.REVIEW_TIME_LIMIT_MINUTES
        return review.started + minutes * 60 if minutes and minutes > 0 else None

    def _run(self, review: Review) -> None:
        review.status, review.started = "running", time.time()
        if self.before_review:
            try:
                self.before_review(review)
            except Exception:  # noqa: BLE001 - extra evidence is optional
                log.exception("before_review hook failed for review %s", review.id)
        review.model = self.state.model
        assigned = self._assignments()
        review.models = {key: assigned.get(key, review.model) for key in review.validators}
        self._plan(review)
        self.events.add("review_started", review=review.summary())

        groups: dict[str, list[str]] = {}  # model -> its validators, in the order requested
        for key in review.validators:
            groups.setdefault(review.models[key], []).append(key)
        # Start with the model the previous review left loaded, so it isn't swapped out and back.
        order = sorted(groups, key=lambda m: m != self._last_model)

        for model in order:
            keys = groups[model]
            load_error = None
            deadline = self._deadline(review)
            if not review._parts:
                load_error = "nothing to review: every file was generated, a lockfile, or binary"
            elif deadline and time.time() > deadline:
                review.time_limited = True
                load_error = (f"not started: the review reached its {config.REVIEW_TIME_LIMIT_MINUTES:g}-minute "
                              "time limit first")
            else:
                try:
                    self._prepare(model)
                    self._last_model = model
                except Exception as e:  # noqa: BLE001
                    load_error = f"model {model} couldn't be loaded: {e}"
                    self._last_model = None
                    log.warning(load_error)
            self._run_group(model, keys, review, load_error)

        review.current = None
        review.verdict, review.verdict_reason = decide(review.results, ci_status=review.ci_status,
                                                     require_ci=config.REQUIRE_CI)
        if review.time_limited:
            review.verdict_reason += f"; stopped at the {config.REVIEW_TIME_LIMIT_MINUTES:g}-minute time limit"
        review.status, review.finished = "done", time.time()
        review.diff, review.context, review._parts = "", {}, []  # free the code
        self._mark_reviewed(review)
        self.events.add("review_done", review=review.summary())

    def _mark_reviewed(self, review: Review) -> None:
        """Record the PR commit so the poller skips it; only reviews by all six validators count."""
        if review.meta.get("pr_key") and set(review.validators) == set(VALIDATORS):
            self.state.mark_reviewed(review.meta["pr_key"], review.meta["sha"])

    def _run_group(self, model: str, keys: list[str], review: Review, load_error: str | None) -> None:
        """Run the validators that share one model, part by part so consecutive calls share a prompt
        start. Each validator is reported as soon as it finishes."""
        runs = {k: {"result": {"validator": k, "display_name": VALIDATORS[k].display_name,
                               "icon": VALIDATORS[k].icon, "model": model, "status": "failed", "findings": [],
                               "error": load_error, "dropped": 0, "omitted": 0, "parts_done": 0},
                    "findings": [], "errors": [], "seconds": 0.0} for k in keys}
        if not load_error:
            deadline = self._deadline(review)
            for i in range(review.parts):
                for key in keys:
                    if deadline and time.time() > deadline:
                        review.time_limited = True
                        break
                    self._run_part(model, key, review, i, runs[key])
                    if i == review.parts - 1:
                        self._report(review, runs.pop(key))
                if review.time_limited:
                    break
        for run in runs.values():  # not finished: a load error or the time limit
            if not load_error:
                run["errors"].append(f"not finished: the review reached its {config.REVIEW_TIME_LIMIT_MINUTES:g}-"
                                     f"minute time limit after {run['result']['parts_done']} of {review.parts} part(s)")
            self._report(review, run)

    def _run_part(self, model: str, key: str, review: Review, i: int, run: dict) -> None:
        review.current = key if review.parts == 1 else f"{key} (part {i + 1}/{review.parts})"
        result, label = run["result"], "" if review.parts == 1 else f"part {i + 1}: "
        start = time.time()
        try:
            parsed = self._ask(model, VALIDATORS[key], review, i)
            if parsed.failure:
                run["errors"].append(label + parsed.failure)
            else:
                run["findings"] += [{**f, "_part": i} for f in parsed.findings]  # _part: for the verify pass
                result["dropped"] += parsed.dropped
                result["parts_done"] += 1
        except llm.LlmError as e:
            run["errors"].append(f"{label}{e}")
        except Exception as e:  # noqa: BLE001 - one validator's crash must not stop the others
            log.exception("validator %s crashed", key)
            run["errors"].append(f"crashed: {e}")
        run["seconds"] += time.time() - start
        self._estimate(review, time.time() - start)

    def _report(self, review: Review, run: dict) -> None:
        result = run["result"]
        merged, result["rejected"] = self._verify(result["model"], review, merge_findings(run["findings"]))
        for f in merged:
            f.pop("_part", None)
        result["findings"], result["omitted"] = merged[:MAX_FINDINGS], max(len(merged) - MAX_FINDINGS, 0)
        if result["parts_done"] == review.parts:
            result["status"], result["error"] = "ok", None
        else:
            result["status"] = "partial" if result["parts_done"] else "failed"
            result["error"] = result["error"] or "; ".join(run["errors"]) or "not finished"
        result["seconds"] = round(run["seconds"], 1)
        review.results.append(result)
        self.events.add("validator_done", review=review.summary(), result=result)

    def _verify(self, model: str, review: Review, findings: list[dict]) -> tuple[list[dict], list[dict]]:
        """Re-check each VERIFY_SEVERITIES finding against the same code; only an explicit "rejected"
        drops it. Returns (kept, rejected)."""
        kept, rejected = [], []
        deadline = self._deadline(review)
        for f in findings:
            if f["severity"] not in config.VERIFY_SEVERITIES or (deadline and time.time() > deadline):
                kept.append(f)
                continue
            claim = {k: f.get(k) for k in ("severity", "category", "file", "line", "description")}
            start = time.time()
            try:
                reply = json.loads(self._complete(model, VERIFY_TASK + json.dumps(claim),
                                                  review.prompt(f.get("_part", 0)), schema=VERIFY_SCHEMA))
            except Exception as e:  # noqa: BLE001 - can't re-check, so keep the finding
                log.warning("couldn't verify a %s finding (%s); keeping it", f["severity"], e)
                reply = {}
            if reply.get("verdict") == "rejected":
                rejected.append({"severity": f["severity"], "description": f["description"],
                                 "reason": reply.get("reason", "")})
            else:
                kept.append(f)
            log.info("verify %s %s: %s (%.0fs)", f["severity"], f.get("category"), reply.get("verdict", "kept"),
                     time.time() - start)
        return kept, rejected

    def _ask(self, model: str, v, review: Review, part: int):
        """One model call for one part, parsed; re-asked PARSE_RETRIES times if unusable (each bad
        reply is saved to FAILED_REPLIES_DIR)."""
        for attempt in range(1 + config.PARSE_RETRIES):
            raw = self._complete(model, v.instructions(), review.prompt(part))
            parsed = parse_findings(raw)
            if not parsed.failure:
                return parsed
            self._save_failed_reply(review, v.key, part, attempt, model, parsed.failure, raw)
            log.warning("%s on %s gave an unusable reply (%s)%s", v.key, model, parsed.failure,
                        "; asking again" if attempt < config.PARSE_RETRIES else "")
        return parsed

    @staticmethod
    def _save_failed_reply(review: Review, key: str, part: int, attempt: int, model: str, why: str,
                           raw: str) -> None:
        if not config.FAILED_REPLIES_DIR:
            return
        try:
            config.FAILED_REPLIES_DIR.mkdir(parents=True, exist_ok=True)
            path = config.FAILED_REPLIES_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{review.id}-{key}-p{part + 1}-a{attempt + 1}.txt"
            path.write_text(f"model: {model}\nreason: {why}\nreview: {review.title}\n\n{raw}", encoding="utf-8")
        except OSError:
            log.exception("couldn't save the failed reply")

    def _estimate(self, review: Review, last_call_s: float) -> None:
        """Re-estimate the review after every call (time so far + the last call's duration per call
        left). Front-ends are told once, after the first call."""
        first = review.estimate_s is None
        review._calls_done += 1
        calls = review.parts * len(review.validators)
        elapsed = time.time() - (review.started or time.time())
        review.estimate_s = round(elapsed + last_call_s * max(calls - review._calls_done, 0))
        if not first:
            return
        limit = config.REVIEW_TIME_LIMIT_MINUTES * 60 if config.REVIEW_TIME_LIMIT_MINUTES > 0 else None
        if review.parts > 1 or review.estimate_s >= 120 or (limit and review.estimate_s > limit):
            self.events.add("review_estimate", review=review.summary(), estimate_s=review.estimate_s,
                            limit_s=limit, calls=calls)


def merge_findings(findings: list[dict]) -> list[dict]:
    """Merge findings from several parts: same file, line, and category (and description when
    there's no line) is one finding; the most severe copy wins. Sorted most severe first."""
    best: dict[tuple, dict] = {}
    for f in findings:
        key = (f.get("file"), f.get("line"), f.get("category"), f["description"] if not f.get("line") else None)
        if key not in best or SEVERITIES.index(f["severity"]) < SEVERITIES.index(best[key]["severity"]):
            best[key] = f
    return sorted(best.values(), key=lambda f: SEVERITIES.index(f["severity"]))
