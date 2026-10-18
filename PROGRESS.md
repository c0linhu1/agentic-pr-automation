# Agent Bench — Progress & To-Do

Prototype of the review stage of Brean's Agentic Development Platform. Reviewer agents run on
local Ollama models, and Slack Socket Mode is how you talk to them. The design docs are in
`br/`: `agentic-validation-project-outline.pdf` (project) and `brean-work-and-project-outline.pdf`
(work log plus the same project as Part II).

_Last updated: 2026-10-14_

---

## Where things stand

| Piece | Status |
|---|---|
| Slack test workspace + app (Socket Mode, `/agent`, `/agents`, @-mentions) | ✅ Set up, tokens in `.env` |
| Bot (`app.py`) connects to Slack and Ollama | ✅ Confirmed: "Bolt app is running!" |
| `/agents`, `/agent echo`, `/agent security …` round-trip in Slack | ⬜ Not confirmed yet |
| Six reviewer skills load into prompts | ✅ All six load; name matches folder |
| PR title/description/ticket vs diff checks, folded into `correctness-review` (still 6 agents; `integrity` is an alias) | ✅ Written and loads; not yet tried on a model |
| Models pulled: `llama3.2:3b`, `llama3.1:8b`, `qwen2.5-coder:7b` | ✅ |
| Models missing: `gemma3:4b` (qa), `mistral:7b` (architecture), `phi4-mini` (compliance) | ⬜ |
| Each agent shows as its own bot in Slack | ⬜ Not built |
| Automatic PR reviews | ⬜ Not built |
| Several reviewers per PR at once + combined report | ⬜ Not built |
| Release decision (Pass / Auto-fix / Hold / Block) | ⬜ Not built |

---

## What's been done

### 1. Reviewed the design docs (`br/`)
Both PDFs read in full. Issues found, still to fix in the docs:

**Contradictions**
- "Every reviewer runs a different model", but Security and Performance both use `qwen2.5-coder:7b`.
- One doc calls BreanGPT "already finished", the other "a prototype pending sign-off".
- "LiteLLM routes externally" is stated as fact *and* listed as an open question.
- The review-farm sandbox uses a hosted model, while the rule is that nothing goes to hosted
  models. Note that it only ever saw sample diffs.
- Part II of the work log duplicates the project outline and has started to drift. Make the
  project outline the one source of truth.

**Gaps in the decision rule**
- No branch for CI that is missing or errored (should be Hold). "Tests red" and "CI couldn't run" are different cases.
- Auto-fix budget used up → should go to Hold.
- "Hard policy violation" isn't defined, and nobody is named as the one who detects it.
- Low-severity findings send PRs to Auto-fix, which creates churn. Consider letting them Pass with notes.
- One made-up critical blocks a release. Needs a human override, or a second reviewer/CI to agree.
- Apply the 10-finding cap after sorting by severity, so a critical never gets dropped.
- When dedupe merges two findings, keep the higher severity.

**Other gaps**
- **Silent context truncation (biggest risk).** If a prompt overflows Ollama's context window,
  the start gets cut off and the model still returns valid JSON, which looks like a clean review.
  *Handled in `llm.py`:* prompts that are too long are refused and reported as a failed review.
- JSON mode isn't the same across runtimes (Ollama `json_object` vs LM Studio/llama.cpp schemas),
  so swapping runtimes isn't truly one line.
- Finding schema: `file` and `line` should be optional (e.g. "missing tests" has no line).
- The eval plan only measures catch rate. Also run reviewers on **clean** diffs to measure
  how often they invent problems.
- `phi4-mini` (compliance) and `gemma3:4b` (QA) are small for judgment-heavy roles.
- Findings (`description`, `suggestedFix`) can quote code, so data still leaves through Slack.
- Slash commands have no event ID to deduplicate on; use the envelope ID instead.
- Decide the production language (Python vs .NET) before extracting the C# `Brean.Llm` library.
- The platform diagram has a "‹" carousel button over the Job Queues / Dynamic Agent Team area. Re-export it.
- Work log: "Other tickets" is empty; Legal Docs, Constant Contact and World-Check items are stale.
  Remove internal hostnames and the compliance incident before sharing it outside Brean.

### 2. Organized the skill files
The six uploaded `SKILL*.md` files were moved to `skills/<skill-name>/SKILL.md`. The file must
stay named `SKILL.md` because `skill_loader.py` looks for that name; the folder carries the skill name.

| Was | Now |
|---|---|
| `SKILL (5).md` | `skills/security-review/SKILL.md` |
| `SKILL (4).md` | `skills/correctness-review/SKILL.md` |
| `SKILL (3).md` | `skills/qa-review/SKILL.md` |
| `SKILL (2).md` | `skills/architecture-review/SKILL.md` |
| `SKILL (1).md` | `skills/compliance-review/SKILL.md` |
| `SKILL.md` | `skills/performance-review/SKILL.md` |

**Skills reset to generic (2026-10-14):** all six were rewritten with no Brean, C#/.NET or finance
content, no team conventions and no `references/` folders, as a clean baseline for experimenting
with how the models behave. They keep the same structure, categories and JSON output. All six use
the same labeled input (`TITLE:` / `TICKET:` / `DESCRIPTION:` / `DIFF:`, only DIFF required).
Correctness also checks the title, description and ticket against the diff.

### 3. Slack app
- Free test workspace (not Brean's), because pasted diffs end up in Slack.
- App created from `manifest.yaml`. The original error was most likely YAML pasted into the
  JSON tab, so `manifest.json` was added as a JSON copy.
- **App-level token** (`xapp-`, scope `connections:write`): opens the Socket Mode connection. This is how the bot *receives* commands.
- **Bot token** (`xoxb-`, from Install App): calls Slack's web API. This is how the bot *posts*.
- Not needed: the user token (`xoxp-`), signing secret, and client ID/secret.

**How Socket Mode works:** `app.py` opens a WebSocket out to Slack using the `xapp` token, so no
public URL is needed. Slack sends commands and mentions down that socket. The bot acknowledges
each within about 3 seconds, runs the model on a worker thread, and posts replies over HTTPS with
the `xoxb` token. Run only one process per app, because Slack spreads deliveries across every open connection.

### 4. Code written
| File | What it does |
|---|---|
| `app.py` | Socket Mode dispatcher: `/agents`, `/agent <name> <prompt>`, @-mentions. Acks right away, runs model calls on a pool (`MAX_WORKERS=2`), replies in a thread, skips repeated mention events, and gives a "/invite me" hint when it isn't in the channel. Warns at startup about models that aren't pulled. |
| `llm.py` | One model call through the OpenAI-compatible API (`LLM_BASE_URL`). JSON mode for reviewers. Refuses prompts that don't fit the context window, plus timeout, server-down and missing-model errors. All of these count as a **failed** review. |
| `requirements.txt` | `slack_bolt`, `openai`, `python-dotenv`, `PyYAML` |
| `.env.example` | Template for every setting |
| `.venv/` | Virtual environment with the requirements installed |
| `.gitignore` | Also ignores `.venv/` and `__pycache__/` |

Already present before this work: `agents.py` (role → skill + model), `skill_loader.py`,
`findings.py` (checks reviewer JSON; anything that doesn't parse is a failed review), `README.md`, `manifest.yaml`.

### 5. Context window
- llama3.2 supports **131,072** tokens, but Ollama gives it **8,192**, because
  `OLLAMA_CONTEXT_LENGTH=8192` is set in Windows. That only applies if Ollama was restarted after
  it was set; check with `ollama ps` while a model is loaded.
- `llm.py` defaults to **4,096** (`LLM_CONTEXT_TOKENS` isn't in `.env`), so it's stricter than it needs to be.
- 8 GB of GPU memory: 8k context is comfortable for 7–8B models at Q4. 16k or more risks
  pushing part of the model onto the CPU, which slows reviews a lot.

---

## How to run it
```powershell
cd C:\Users\colin\Downloads\slack_agents
.venv\Scripts\activate
python app.py
```
In Slack: `/invite @agent-bench`, `/agents`, `/agent echo say hi`, then
`/agent security public IActionResult Get(int id) => Ok(_db.Documents.FirstOrDefault(d => d.Id == id));`

| Symptom | Fix |
|---|---|
| "dispatch_failed" / app not responding | `app.py` isn't running, or the `xapp-` token is wrong |
| "I'm not in this channel" | `/invite @agent-bench` |
| "can't reach the model server" | Start the Ollama app |
| "prompt is ~N tokens" | Smaller diff, or raise `LLM_CONTEXT_TOKENS` |

---

## To do

### Now
- [ ] Confirm in Slack: `/agents`, `/agent echo say hi`, `/agent security …` each reply in a thread.
- [ ] Add `LLM_CONTEXT_TOKENS=8192` to `.env` on a **new line** (the file has no line break at the end), then restart `app.py`.
- [ ] Check the real context size: run an agent, then `ollama ps`.
- [ ] Pull the missing models: `ollama pull gemma3:4b`, `ollama pull mistral:7b`, `ollama pull phi4-mini`.

- [ ] Try `/agent correctness` (or `/agent integrity`) with a PR whose description claims something
      the diff doesn't do. Input sections: `TITLE:`, `TICKET:`, `DESCRIPTION:`, `DIFF:` (all optional except DIFF).
      The correctness skill is now about 2,100 tokens on its own, so set `LLM_CONTEXT_TOKENS=8192` first.
- [ ] Watch whether the bigger correctness rubric hurts its logic-bug catch rate on `llama3.1:8b`.
      If it does, move `weakened-check` to QA and `undeclared-change` to architecture.

### Next to build
- [ ] **Each agent as its own bot (Option A):** add the `chat:write.customize` scope and reinstall,
      give each agent a `display_name` and `icon` in `agents.py`, and post with `username=` / `icon_emoji=`.
      Keeps one app and one socket. (Option B, six separate Slack apps, works but is messy. Not recommended.)
- [ ] **`/review` command:** run all reviewers on one diff at once, each reply in the thread under its
      own name, plus a combined summary (Phase 3).
- [ ] **Release decision as a pure function with a unit test per branch** (Pass / Auto-fix / Hold / Block),
      including the gaps listed above (CI missing → Hold, budget used up → Hold, a failed reviewer → Hold).
- [ ] **Evals:** plant one bug in a sample diff and run each reviewer about 5×; also run on a clean
      diff to measure false positives. Use the results to tighten skills and resize models.
- [ ] **Add `references/` checklists** to each skill (Brean-specific rules).

### Automatic PR reviews (Phase 4)
Nothing exists yet. Plan for the prototype: **polling** (outbound only, fits Socket Mode, no public URL).
- [ ] Make a **private test repo** with a few sample PRs that contain planted bugs.
- [ ] Create a **fine-grained GitHub token** for that repo only: *Pull requests: Read*, *Contents: Read*.
      Add it as `GITHUB_TOKEN=` in `.env`.
- [ ] Pick a Slack channel for reports (e.g. `#pr-reviews`).
- [ ] Build `github_poller.py`: every 60 s list open PRs and track each PR's latest commit, so a
      new push means a new review. Fetch the diff, run the reviewers, post a "PR #N: title"
      message with findings in its thread. Save reviewed commits to JSON so a restart doesn't
      re-review everything. Run it on a background thread inside `app.py`.
- [ ] Large diffs: report "too large" as a failed review for now; split by file later.
- [ ] Later: post findings as PR comments (needs *Pull requests: Write*).

Options for production, to decide with Brian:
| Approach | Verdict |
|---|---|
| GitHub Actions, GitHub-hosted runner | ❌ Can't reach on-prem models |
| GitHub Actions, **self-hosted runner** | ✅ Good for production. Connects outbound and sits next to CI. **Private repos only.** |
| Webhooks | ⚠️ Needs a public URL |
| Polling | ✅ Simplest for the prototype |

Rule: the review job **reads the diff only**. It never builds or runs PR code; that belongs in CI's isolated environment.

### Questions for Brian
- [ ] Project boundary: boxes 6–10, or a narrower slice?
- [ ] Approved open-weight models, and the hardware (now vs. the Mac minis).
- [ ] Production language: Python, or .NET to share the BreanGPT core and his C# orchestrator?
- [ ] Extend his orchestrator, or run as a separate service?
- [ ] Slack policy: is posting findings only OK? Is the firm workspace allowed?
- [ ] CI red with no findings: Auto-fix or Hold?
- [ ] Where do Brean's repos live, and how does CI run? (Decides polling vs a self-hosted runner vs webhooks.)
- [ ] Which ticket fields are the acceptance criteria, and how should status be written back?
- [ ] Does LiteLLM route outside the firm? (BreanGPT only.)

### Later phases (from the PDFs)
- [ ] Phase 0: extract the C# LLM core into `Brean.Llm` (after the language decision); finish BreanGPT testing; rotate the gateway key.
- [ ] Phase 1 hardening: a backend queue, job state, and dedupe keyed on the envelope ID.
- [ ] Phase 5: Approve / Block / Hold / Auto-fix buttons in Slack; auto-fix loop back to Brian's agents; status written back to the ticket.
- [ ] Phase 6: retrieval from Obsidian/Onyx (ADRs, policy) into reviewer prompts.
- [ ] Phase 7: UAT → canary → production with rollback; telemetry to Datadog.
- [ ] Phase 8: budgets, kill switches, audit, a model registry, Mac-mini hosting (benchmark MLX), MCP adapter.
- [ ] Fix the doc issues listed under "What's been done → 1".
