# Agent Bench — Slack Socket Mode + local agents

## 1. Create the Slack app
1. api.slack.com/apps -> Create New App -> From a manifest -> pick your workspace -> paste `manifest.yaml`.
2. Basic Information -> App-Level Tokens -> Generate, scope `connections:write`. That's `SLACK_APP_TOKEN` (xapp-).
3. Install App -> Install to Workspace. Copy the Bot User OAuth Token. That's `SLACK_BOT_TOKEN` (xoxb-).
4. In the channel you'll use: `/invite @agent-bench`.

If Brian's bots already use `/agent`, rename the commands in the manifest to avoid a clash.

## 2. Pull models
    ollama pull llama3.2:3b
    ollama pull qwen2.5-coder:7b
    ollama pull llama3.1:8b

## 3. Run
    python -m venv .venv
    .venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
    pip install -r requirements.txt
    copy .env.example .env          # then fill in the tokens  (macOS/Linux: cp)
    python app.py

## 4. Try it
    /agents
    /agent echo say hi
    @agent-bench security <paste a small snippet>

Replies land in a thread under the "working" message.

## 5. Reviewer skills
Each reviewer is a skill folder under `skills/` (SKILL.md + references/).
`agents.py` maps role -> (skill folder, model). The loader inlines the references
and strips "When to Activate", since a plain Ollama call can't open files.

    /agent security <paste a diff>
    /agent logic <paste a diff>        # alias for correctness
    @agent-bench qa <paste a diff>

Reviewers reply in JSON (Finding schema); the bot renders it. Output that fails to
parse is reported as a FAILED review, never as clean.

Before posting to a real channel: diffs you paste go into Slack, which is outside the
firm. Use sample diffs in a test workspace.
