"""Vader's HTTP API on localhost (run: python -m vader.service). The only program that talks to the
model server and GitHub; front-ends must send the shared X-Vader-Key."""
import logging
import secrets
import threading
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import config, models
from .agents import VALIDATORS, check_skills, resolve
from .engine import Engine, Events, Review, Stopped, parse_sections
from .github import GitHubPoller, parse_pr_ref
from .state import State

log = logging.getLogger("vader")

STOPPED = "Vader is stopped; nothing new is accepted until it's started"


class ControlBody(BaseModel):
    action: str  # "start" | "stop"


class AutomationBody(BaseModel):
    enabled: bool


class ModelBody(BaseModel):
    name: str


class ReviewBody(BaseModel):
    text: str = ""  # labeled TITLE/TICKET/DESCRIPTION/DIFF text, or a bare diff
    title: str = ""
    description: str = ""
    ticket: str = ""
    diff: str = ""
    validators: list[str] | None = None  # default: all six
    reply_to: dict | None = None


class PrBody(BaseModel):
    ref: str                              # "42", "#42", "owner/repo#42", or the PR's URL
    validators: list[str] | None = None   # default: all six
    reply_to: dict | None = None


class ReplyToBody(BaseModel):
    reply_to: dict


def create_app(state: State, events: Events, engine: Engine, poller: GitHubPoller | None,
               api_key: str, start_threads: bool = True) -> FastAPI:

    @asynccontextmanager
    async def lifespan(_app):
        if start_threads:
            engine.start()
            if poller:
                poller.start()
        yield

    app = FastAPI(title="Vader", lifespan=lifespan)

    def require_key(x_vader_key: str = Header(default="")):
        if not secrets.compare_digest(x_vader_key, api_key):
            raise HTTPException(401, "missing or wrong X-Vader-Key")

    auth = [Depends(require_key)]

    @app.get("/status", dependencies=auth)
    def status():
        return {"enabled": state.enabled, "automation": state.automation, "model": state.model,
                "model_server": {"url": config.LLM_BASE_URL, "runtime": config.MODEL_RUNTIME,
                                 "reachable": models.available() is not None},
                "loaded": models.loaded(), "github": {"configured": bool(poller and poller.configured),
                                                      "repos": config.GITHUB_REPOS},
                "auto_merge": config.AUTO_MERGE_BRANCH or None, "close_issues": config.CLOSE_LINKED_ISSUES,
                **engine.status()}

    @app.post("/control", dependencies=auth)
    def control(body: ControlBody):
        if body.action not in ("start", "stop"):
            raise HTTPException(400, "action must be start or stop")
        state.set(enabled=body.action == "start")
        engine.wake()
        return {"enabled": state.enabled, "running": engine.status()["running"]}

    @app.post("/automation", dependencies=auth)
    def automation(body: AutomationBody):
        state.set(automation=body.enabled)
        return {"automation": state.automation,
                "github_configured": bool(poller and poller.configured)}

    @app.get("/models", dependencies=auth)
    def list_models():
        default, entries = models.catalog()
        have = models.available()
        assigned = models.assignments()
        return {"active": state.model, "default": default, "loaded": models.loaded(),
                "validators": {k: assigned.get(k, state.model) for k in VALIDATORS}, "models": [
            {"name": e.name, "tier": e.tier, "enabled": e.enabled, "note": e.note,
             "available": None if have is None else e.name in have} for e in entries]}

    @app.post("/model", dependencies=auth)
    def switch_model(body: ModelBody):
        if engine.busy:
            raise HTTPException(409, "a review is running; switch models when it finishes")
        problem = models.check_selectable(body.name)
        if problem:
            raise HTTPException(400, problem)
        name = models.normalize(body.name)
        state.set(model=name)

        def preload():
            try:
                engine.preload(name, models.ensure_only)
                events.add("model_ready", model=name)
            except Exception as e:  # noqa: BLE001
                events.add("model_failed", model=name, error=str(e))

        threading.Thread(target=preload, daemon=True).start()
        return {"model": name, "loading": True}

    def validator_keys(names: list[str] | None) -> list[str]:
        keys = []
        for name in names or list(VALIDATORS):
            v = resolve(name)
            if v is None:
                raise HTTPException(400, f"unknown validator `{name}`; choose from {', '.join(VALIDATORS)}")
            keys.append(v.key)
        return keys

    def queue(review: Review) -> int:
        try:
            return engine.submit(review)
        except Stopped:
            raise HTTPException(409, STOPPED) from None

    @app.post("/reviews/pr", dependencies=auth)
    def submit_pr(body: PrBody):
        """Review one specific GitHub PR: Vader fetches it, so front-ends never touch GitHub."""
        if poller is None or not config.GITHUB_TOKEN:
            raise HTTPException(400, "GitHub isn't configured on Vader (GITHUB_TOKEN in .env.vader)")
        keys = validator_keys(body.validators)
        if not state.enabled:  # checked before fetching, so a stopped Vader doesn't call GitHub
            raise HTTPException(409, STOPPED)
        try:
            repo, number = parse_pr_ref(body.ref, config.GITHUB_REPOS[0] if config.GITHUB_REPOS else None)
            review = poller.review_for_pr(repo, number)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except httpx.HTTPError as e:
            raise HTTPException(502, f"couldn't fetch the PR from GitHub: {e}") from None
        review.source, review.validators, review.reply_to = "slack", keys, body.reply_to
        position = queue(review)
        return {"id": review.id, "position": position, "title": review.title, "meta": review.meta}

    @app.post("/reviews", dependencies=auth)
    def submit(body: ReviewBody):
        fields = parse_sections(body.text) if body.text else {}
        keys = validator_keys(body.validators)
        review = Review(source="slack", validators=keys, reply_to=body.reply_to,
                        title=body.title or fields.get("title", ""),
                        description=body.description or fields.get("description", ""),
                        ticket=body.ticket or fields.get("ticket", ""),
                        diff=body.diff or fields.get("diff", ""))
        if not review.diff:
            raise HTTPException(400, "no diff to review (give a DIFF: section or paste a diff)")
        return {"id": review.id, "position": queue(review)}

    @app.get("/reviews/{review_id}", dependencies=auth)
    def get_review(review_id: str):
        review = engine.get(review_id)
        if review is None:
            raise HTTPException(404, "no such review (reviews are kept in memory until Vader restarts)")
        return review.summary()

    @app.post("/reviews/{review_id}/reply-to", dependencies=auth)
    def set_reply_to(review_id: str, body: ReplyToBody):
        review = engine.get(review_id)
        if review is None:
            raise HTTPException(404, "no such review")
        review.reply_to = body.reply_to
        return {"ok": True}

    @app.get("/events", dependencies=auth)
    def get_events(since: int = 0):
        return {"boot_id": events.boot_id, "latest": events.latest(), "events": events.since(since)}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per model server/GitHub call is noise
    if not config.API_KEY:
        raise SystemExit("Set VADER_API_KEY in .env.vader (the Slack bot needs the same value).")
    if config.MODEL_RUNTIME not in ("generic", "ollama"):
        raise SystemExit(f"MODEL_RUNTIME must be generic or ollama, not {config.MODEL_RUNTIME!r}.")
    problems = check_skills()
    if problems:
        raise SystemExit("Skills failed to load:\n  " + "\n  ".join(problems))

    default, _ = models.catalog()
    state = State(config.STATE_FILE, default_model=default)
    events = Events()
    engine = Engine(state, events)
    poller = GitHubPoller(state, events, engine)
    engine.before_review = poller.attach_ci  # PR reviews get their commit's CI results as evidence
    engine.on_done = poller.finish  # merge PRs that pass, then post the report as a PR comment

    log.info("Vader on http://%s:%d | model server %s (%s) | model %s | %s | automation %s | GitHub %s",
             config.HOST, config.PORT, config.LLM_BASE_URL, config.MODEL_RUNTIME, state.model,
             "running" if state.enabled else "STOPPED",
             "on" if state.automation else "off",
             ", ".join(config.GITHUB_REPOS) if poller.configured else "not configured")
    _warn_about_models(state.model)

    uvicorn.run(create_app(state, events, engine, poller, config.API_KEY),
                host=config.HOST, port=config.PORT, log_level="warning")


def _warn_about_models(active: str | None) -> None:
    """At startup, log which model each validator uses and warn about any that won't run."""
    assigned = models.assignments()
    in_use = {assigned.get(k, active) for k in VALIDATORS} - {None}
    log.info("validator models: %s", ", ".join(f"{k}={assigned.get(k, active)}" for k in VALIDATORS))
    for k in sorted(set(assigned) - set(VALIDATORS)):
        log.warning("models.yaml assigns a model to unknown validator %r; ignored", k)
    for m in sorted(in_use):
        problem = models.check_allowed(m)
        if problem:
            log.warning("%s; its validators will fail until models.yaml allows it", problem.replace("`", ""))
    have = models.available()
    if have is None:
        log.warning("can't reach the model server at %s; reviews wait until it's running", config.LLM_BASE_URL)
    else:
        for m in sorted(in_use - have):
            log.warning("model %s isn't on the model server; its validators will fail until it's pulled or "
                        "downloaded there", m)


if __name__ == "__main__":
    main()
