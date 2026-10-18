"""Socket Mode dispatcher: one process holds the Slack socket and routes by agent name.

    /agents                     list agents
    /agent <name> <prompt>      run an agent; the reply lands in a thread
    @agent-bench <name> <prompt>

Listeners ack immediately (Slack wants a response within ~3 s); the model call
runs on a worker pool and posts back into the job's thread.

Prototype limits: in-memory state, no queue. Text you send goes to Slack, which
is outside the firm -- use sample diffs in a test workspace."""
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv()  
from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402

import llm  # noqa: E402
from agents import AGENTS, Agent, get_agent  # noqa: E402
from findings import parse_findings, to_slack  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent-bench")

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "2"))  # each model is loaded in memory; keep this low on a laptop
SLACK_TEXT_LIMIT = 39_000
LEADING_MENTIONS = re.compile(r"^(\s*<@[A-Z0-9]+>)+\s*")

app = App(token=os.environ["SLACK_BOT_TOKEN"])
pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
seen_events: set[str] = set()  # Socket Mode can redeliver; in-memory until there's a job store


def usage() -> str:
    names = ", ".join(f"`{n}`" for n in sorted(AGENTS))
    return f"Usage: `/agent <name> <prompt>`. Agents: {names}"


def user_message(agent: Agent, prompt: str) -> str:
    return f"Review this change:\n\n{prompt}" if agent.json_output else prompt


def run_job(client, channel: str, thread_ts: str, agent: Agent, prompt: str) -> None:
    try:
        try:
            raw = llm.complete(agent.model, agent.system_prompt, user_message(agent, prompt), agent.json_output)
        except llm.LlmError as e:
            text = f":warning: *{agent.name}* ({agent.model}): {e}."
            if agent.json_output:
                text += " This counts as missing evidence, not a clean review."
        else:
            if agent.json_output:
                findings, error = parse_findings(raw)
                text = to_slack(agent.name, agent.model, findings, error)
            else:
                text = raw.strip() or "_(empty reply)_"
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text[:SLACK_TEXT_LIMIT])
    except Exception:
        log.exception("job failed: agent=%s channel=%s", agent.name, channel)
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=f":x: *{agent.name}* crashed; see the bot's console log.")
        except SlackApiError:
            pass


def dispatch(client, channel: str, thread_ts: str | None, user: str, text: str) -> str | None:
    """Starts a job. Returns an error string for the user, or None if it started."""
    name, _, prompt = text.strip().partition(" ")
    agent = get_agent(name)
    if agent is None:
        return f"Unknown agent `{name}`. {usage()}" if name else usage()
    if not prompt.strip():
        return f"`{agent.name}` needs a prompt: `/agent {name} <prompt>`"

    working = client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":hourglass_flowing_sand: <@{user}> started *{agent.name}* on `{agent.model}`. Results will appear in this thread.",
    )
    log.info("job: agent=%s model=%s user=%s chars=%d", agent.name, agent.model, user, len(prompt))
    pool.submit(run_job, client, channel, thread_ts or working["ts"], agent, prompt.strip())
    return None


@app.command("/agents")
def list_agents(ack):
    lines = [f"`{a.name}` -> {a.model}" for n, a in sorted(AGENTS.items()) if n == a.name]
    lines += [f"`{n}` -> alias for `{a.name}`" for n, a in sorted(AGENTS.items()) if n != a.name]
    ack("\n".join(lines))


@app.command("/agent")
def agent_command(ack, command, client, respond):
    ack()
    try:
        error = dispatch(client, command["channel_id"], None, command["user_id"], command.get("text", ""))
    except SlackApiError as e:
        if e.response.get("error") in ("not_in_channel", "channel_not_found"):
            error = "I'm not in this channel yet. Run `/invite @agent-bench` here, then try again."
        else:
            raise
    if error:
        respond(error)  # only the person who ran the command sees this


@app.event("app_mention")
def on_mention(event, body, client):
    event_id = body.get("event_id")
    if event_id in seen_events:
        return
    seen_events.add(event_id)

    thread_ts = event.get("thread_ts") or event["ts"]
    text = LEADING_MENTIONS.sub("", event.get("text", ""))
    error = dispatch(client, event["channel"], thread_ts, event["user"], text)
    if error:
        client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts, text=error)


def check_models() -> None:
    have = llm.available_models()
    if have is None:
        log.warning("model server not reachable at %s -- agents will fail until it is", os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"))
        return
    for model in sorted({a.model for a in AGENTS.values()} - have):
        users = sorted({a.name for a in AGENTS.values() if a.model == model})
        log.warning("model %s not pulled (used by %s): ollama pull %s", model, ", ".join(users), model)


if __name__ == "__main__":
    check_models()
    log.info("agents: %s", ", ".join(sorted(AGENTS)))
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
