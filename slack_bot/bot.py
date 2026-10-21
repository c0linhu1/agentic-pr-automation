"""Vader's Slack front-end (run: python -m slack_bot.bot). Holds only the Slack tokens and Vader's
address: turns /vader and @Vader into Vader requests, and Vader's events into Slack messages."""
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env.slack")

from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402

from slack_bot import fmt  # noqa: E402
from slack_bot.vader_client import VaderClient, VaderError, VaderUnavailable  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # the event poll every 2s would flood the log
log = logging.getLogger("slack_bot")

NOTIFY_CHANNEL = os.getenv("SLACK_NOTIFY_CHANNEL", "")  # channel ID for automatic PR reviews and alerts
LEADING_MENTIONS = re.compile(r"^(\s*<@[A-Z0-9]+>)+\s*")
NOT_RUNNING = ":electric_plug: Vader isn't running or can't be reached. Start it with `python -m vader.service`."

app = App(token=os.environ["SLACK_BOT_TOKEN"])
vader = VaderClient(os.getenv("VADER_URL", "http://127.0.0.1:8700"), os.environ["VADER_API_KEY"])

threads: dict[str, dict] = {}  # review id -> {"channel", "thread_ts"}
model_reply_to: dict | None = None  # where to announce that a model finished loading
seen_events: OrderedDict[str, None] = OrderedDict()  # Socket Mode can redeliver mentions; recent IDs only
SEEN_LIMIT = 1000


def post(reply_to: dict | None, text: str, **kwargs) -> dict | None:
    target = reply_to or ({"channel": NOTIFY_CHANNEL} if NOTIFY_CHANNEL else None)
    if not target:
        log.info("no channel for message: %s", text[:80])
        return None
    return app.client.chat_postMessage(channel=target["channel"], thread_ts=target.get("thread_ts"),
                                       text=fmt.cut(text), **kwargs)


def post_as(reply_to: dict | None, name: str, icon: str, text: str) -> None:
    """Post under a validator's name and icon; falls back to a name prefix without chat:write.customize."""
    try:
        post(reply_to, text, username=name, icon_emoji=icon)
    except SlackApiError as e:
        if e.response.get("error") != "missing_scope":
            raise
        post(reply_to, f"{icon} *{name}*\n{text}")


def queue_review(channel: str, thread_ts: str | None, head: str, submit, queued_text) -> None:
    """Post a placeholder, ask Vader to queue the review, then update the placeholder with the outcome.
    The placeholder's thread is where the validators' results will appear."""
    parent = app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=head)
    reply_to = {"channel": channel, "thread_ts": thread_ts or parent["ts"]}
    try:
        r = submit(reply_to)
    except VaderUnavailable:
        text = f"{head}\n{NOT_RUNNING}"
    except VaderError as e:
        text = f"{head}\n:x: {e}"
    else:
        threads[r["id"]] = reply_to
        text = queued_text(r)
    app.client.chat_update(channel=channel, ts=parent["ts"], text=text)


def start_pr_review(user: str, channel: str, thread_ts: str | None, ref: str, validators: list[str] | None) -> None:
    """Ask Vader to fetch one GitHub PR and review it. Vader does the fetching."""
    who = "all six validators" if validators is None else f"the {validators[0]} validator"
    queue_review(channel, thread_ts, f":hourglass_flowing_sand: <@{user}> asked Vader to review PR `{ref}` with {who}",
                 lambda reply_to: vader.submit_pr(ref, validators, reply_to),
                 lambda r: (f":mag: <@{user}> asked Vader to review <{r['meta']['url']}|{r['meta']['repo']}#"
                            f"{r['meta']['number']}> _{fmt.esc(r['title'])}_ by `{fmt.esc(r['meta']['author'])}` with {who}\n"
                            f"Queued (position {r['position']}). Results will appear in this thread."))


def start_review(user: str, channel: str, thread_ts: str | None, text: str, validators: list[str] | None,
                 private) -> None:
    if not text.strip():
        private("Give me something to review: a diff, or `TITLE:` / `DESCRIPTION:` / `DIFF:` sections.")
        return
    who = "all six validators" if validators is None else f"the {validators[0]} validator"
    head = f":hourglass_flowing_sand: <@{user}> asked Vader to review this with {who}"
    queue_review(channel, thread_ts, head, lambda reply_to: vader.submit(text, validators, reply_to),
                 lambda r: f"{head}\nQueued (position {r['position']}). Results will appear in this thread.")


def handle_command(text: str, user: str, channel: str, thread_ts: str | None, private, public) -> None:
    global model_reply_to
    sub, _, rest = fmt.unescape(text).strip().partition(" ")
    sub, rest = sub.lower(), rest.strip()
    try:
        if sub in ("", "help"):
            private(fmt.HELP)
        elif sub == "status":
            private(fmt.status(vader.status()))
        elif sub in ("stop", "start"):
            r = vader.control(sub)
            if sub == "stop":
                public(f":octagonal_sign: <@{user}> stopped Vader. Nothing new is accepted and queued reviews wait."
                       + (" The review in progress will finish all its validators." if r["running"] else ""))
            else:
                public(f":arrow_forward: <@{user}> started Vader.")
        elif sub == "auto":
            if rest.lower() not in ("on", "off"):
                private("Usage: `/vader auto on` or `/vader auto off`")
                return
            r = vader.automation(rest.lower() == "on")
            msg = f":robot_face: <@{user}> turned automatic PR reviews *{rest.lower()}*."
            if r["automation"] and not r["github_configured"]:
                msg += " (GitHub isn't configured on Vader yet, so nothing will be picked up.)"
            public(msg)
        elif sub == "models" or (sub == "model" and not rest):
            private(fmt.models(vader.models()))
        elif sub == "model":
            r = vader.switch_model(rest)
            model_reply_to = {"channel": channel, "thread_ts": thread_ts}
            public(f":arrows_counterclockwise: <@{user}> switched Vader to `{r['model']}`. Loading it now…")
        elif sub == "review":
            start_review(user, channel, thread_ts, rest, None, private)
        elif sub == "run":
            name, _, body = rest.partition(" ")
            if not name:
                private("Usage: `/vader run <validator> <input>`")
                return
            start_review(user, channel, thread_ts, body, [name], private)
        elif sub == "pr":
            ref, _, only = rest.partition(" ")
            if not ref:
                private("Usage: `/vader pr <number or PR link> [validator]`, e.g. `/vader pr 11`")
                return
            start_pr_review(user, channel, thread_ts, ref, [only.strip()] if only.strip() else None)
        else:
            private(f"Unknown command `{sub}`.\n\n{fmt.HELP}")
    except VaderUnavailable:
        private(NOT_RUNNING)
    except VaderError as e:
        private(f":x: {e}")
    except SlackApiError as e:
        if e.response.get("error") in ("not_in_channel", "channel_not_found"):
            private("I'm not in this channel yet. Run `/invite @Vader` here, then try again.")
        else:
            raise


@app.command("/vader")
def vader_command(ack, command, respond):
    ack()
    channel = command["channel_id"]
    handle_command(command.get("text", ""), command["user_id"], channel, None,
                   private=respond, public=lambda t: app.client.chat_postMessage(channel=channel, text=t))


@app.event("app_mention")
def on_mention(event, body):
    if body.get("event_id") in seen_events:
        return
    seen_events[body.get("event_id")] = None
    if len(seen_events) > SEEN_LIMIT:
        seen_events.popitem(last=False)
    channel, thread_ts = event["channel"], event.get("thread_ts") or event["ts"]
    reply = lambda t: app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=t)  # noqa: E731
    handle_command(LEADING_MENTIONS.sub("", event.get("text", "")), event["user"], channel, thread_ts,
                   private=reply, public=reply)


def handle_event(e: dict) -> None:
    kind = e["type"]
    review = e.get("review") or {}
    reply_to = threads.get(review.get("id")) or review.get("reply_to")

    if kind == "review_queued" and review["source"] == "github" and not reply_to:
        msg = post(None, fmt.pr_header(review, e["position"]))
        if msg:
            reply_to = {"channel": msg["channel"], "thread_ts": msg["ts"]}
            threads[review["id"]] = reply_to
            vader.set_reply_to(review["id"], reply_to)
    elif kind == "pr_comment_posted":
        post(reply_to, f":speech_balloon: Posted the report on the PR: <{e['url']}|view comment>")
    elif kind == "pr_comment_failed":
        post(reply_to, f":warning: Couldn't comment on the PR: {fmt.esc(e['error'])}")
    elif kind == "review_started":
        post(reply_to, fmt.started(review))
    elif kind == "review_estimate":
        post(reply_to, fmt.estimate(e["estimate_s"], e["limit_s"], e["calls"]))
    elif kind == "validator_done":
        r = e["result"]
        post_as(reply_to, r["display_name"], r["icon"], fmt.validator_result(r))
    elif kind == "review_done":
        post(reply_to, fmt.verdict(review), reply_broadcast=bool(reply_to and reply_to.get("thread_ts")))
        threads.pop(review["id"], None)
    elif kind == "model_ready":
        post(model_reply_to, f":white_check_mark: Model `{e['model']}` is loaded and ready.")
    elif kind == "model_failed":
        post(model_reply_to, f":x: Model `{e['model']}` failed to load: {fmt.esc(e['error'])}")
    elif kind == "poller_error":
        post(None, f":warning: {fmt.esc(e['message'])}")


def event_loop() -> None:
    since, boot, warned = 0, None, False
    while True:
        try:
            data = vader.events(since)
            if data["boot_id"] != boot:
                first = boot is None
                boot, since = data["boot_id"], data["latest"] if first else 0
                if not first:
                    log.info("Vader restarted; following its new events")
                continue
            for e in data["events"]:
                try:
                    handle_event(e)
                except Exception:  # noqa: BLE001 - one bad message mustn't stop the feed
                    log.exception("couldn't post event %s", e.get("type"))
                since = e["seq"]
            warned = False
        except VaderUnavailable:
            if not warned:
                log.warning("Vader isn't reachable; will keep trying")
            warned = True
        except Exception:  # noqa: BLE001
            log.exception("event loop error")
        time.sleep(2)


def main() -> None:
    try:
        s = vader.status()
        log.info("connected to Vader: %s, model %s", "running" if s["enabled"] else "stopped", s["model"])
    except (VaderUnavailable, VaderError) as e:
        log.warning("Vader not reachable yet (%s); commands will say so until it is", e)
    if not NOTIFY_CHANNEL:
        log.warning("SLACK_NOTIFY_CHANNEL isn't set; automatic PR reviews and alerts won't be posted")
    threading.Thread(target=event_loop, name="vader-events", daemon=True).start()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
