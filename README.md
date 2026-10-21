# Vader — validator agents behind Slack

Vader reviews code changes with six validator agents running on local Ollama models (one loaded at
a time), and reports a verdict (Pass / Auto-fix / Hold / Block) in Slack.

```
 Slack ──► slack_bot (the face) ──HTTP, localhost──► vader (the brain) ──► Ollama
                                                          └──────────► GitHub (reads PRs; posts the review comment)
```

- **Two programs.** `vader` is the governance layer: the only program that talks to Ollama and
  GitHub. `slack_bot` holds only the Slack tokens and Vader's local address. Neither has the
  other's secrets, and Vader never talks to Slack.
- **Models: one loaded at a time.** Validators use the active model (`/vader model <name>`), or
  their own model if one is set under `validators:` in `models.yaml`. Validators that share a
  model run together, and each review starts with the model already loaded, so swaps are kept to
  a minimum. Only models listed and enabled in `models.yaml` are allowed. Thinking models need
  room to reason (they reason inside `LLM_OUTPUT_TOKENS`, now 12,288), and `reasoning_effort:` can be set per model.
  Model test results and hardware plans: `MODEL_FINDINGS.md`.
- **Reports everywhere.** Each validator posts its findings in the review's Slack thread, and the
  verdict comes with a report (one line per validator plus the most serious findings), also shown
  in the channel. PR reviews are also posted as a comment on the PR (`GITHUB_COMMENT=true`, needs
  *Pull requests: Read and write* on the token).
- **Replies always come back in the right format.** Vader sends the Finding format as a JSON
  schema (structured outputs, a standard OpenAI-API feature), so the server only lets the model
  produce `{"findings": [...]}`. As a safety net, an unusable reply is asked again once
  (`PARSE_RETRIES`) and saved to `failed_replies/` (gitignored).
- **Portable model calls.** Vader uses the standard OpenAI-compatible API, so LM Studio, vLLM, or
  llama.cpp work by changing `LLM_BASE_URL`. Loading/unloading models and `bench.py` are still
  Ollama-specific.
- **A queue, not parallel work.** Reviews wait in line and run one at a time. Inside a review the
  six validators run one after another, and every validator finishes even if an earlier one
  found something serious.
- **Each validator shows up in Slack** under its own name and icon in the review's thread
  (the bot posts on their behalf). The validators never talk to each other.
- **Any PR size.** Lockfiles, generated and compiled code, minified bundles, build output, and
  binaries are dropped before review (listed in Slack as "Not reviewed"). If what's left doesn't
  fit the context window, it's split into parts at file and hunk boundaries; each validator
  reviews every part, with the PR's title, description, ticket, and full file list, and the
  findings are merged.
- **Shared prompt start.** Every validator's prompt begins with the same system message and PR, with its
  own instructions last, and large PRs run part by part, so the model server reuses its work
  reading the PR instead of re-reading it for each validator.
- **Validators see the whole changed files.** For PR reviews, Vader also fetches the full contents of
  each changed source file (not generated, binary, deleted, or over 60k characters) and gives them
  as `CONTEXT`, so validators can see what changed code calls and the patterns already in the file.
  Context is only included when it doesn't add parts (model calls); the largest files drop out first.
- **CI as evidence.** When a PR review starts, Vader reads the commit's CI check runs and puts them in
  the prompt (`CI: build-test success…`); the verdict also uses them (CI failed and nothing worse → AUTO-FIX).
  Private repos need *Checks: Read* on the token.
- **Second look before posting.** Each high or critical finding (the ones that decide the verdict) is
  re-checked once by the same model against the same code (`VERIFY_SEVERITIES`, empty = off). Only an
  explicit rejection drops it, and dropped findings are still listed (in Slack and the PR comment) with
  the reason.
- **Time limit for slow hardware.** `REVIEW_TIME_LIMIT_MINUTES` (30 on the laptop). After the
  first model call Vader posts an estimate; work still left at the limit is skipped and the
  verdict is HOLD. Set it to `0` on hardware that doesn't need it (Mac mini, RTX PRO 6000).

## The six validators

| Validator | Skill | Looks for |
|---|---|---|
| :shield: Security Validator | `skills/security-review` | authorization, data exposure, secrets, injection, logging, auth |
| :dart: Correctness Validator | `skills/correctness-review` | logic bugs, edge cases, and whether the title/description/ticket match the diff |
| :test_tube: QA Validator | `skills/qa-review` | missing or weak tests, flaky tests |
| :building_construction: Architecture Validator | `skills/architecture-review` | layering, coupling, duplication, hardcoded config |
| :scales: Compliance Validator | `skills/compliance-review` | data sent to third parties, personal data, audit trail, licenses |
| :zap: Performance Validator | `skills/performance-review` | queries in loops, memory, I/O patterns |

Skills are re-read for every review, so editing a `SKILL.md` takes effect without a restart.

## Slack commands

```
/vader help
/vader status                       on/off, automation, model, what's running, the queue
/vader stop | start                 kill switch (a running review finishes; queued ones wait)
/vader auto on | off                automatic PR reviews from GitHub
/vader models                       allowed models, and which are pulled
/vader model <name>                 switch the active model (not while a review runs)
/vader pr <number or PR link>       one GitHub PR, all six validators (Vader fetches it;
                                    works with automation off; add a validator name for just one)
/vader review <input>               all six validators on pasted input
/vader run <validator> <input>      one validator (aliases: logic, integrity, tests, perf)
@Vader <any of the above>           same, replies in a thread
```

Input is a bare diff, or labeled sections (Shift+Enter for new lines):
```
TITLE: Fix crash when customer has no address
TICKET: #42 Customers without an address crash checkout
DESCRIPTION:
Adds a None check and unit tests.
DIFF:
--- a/src/shipping.py
+++ b/src/shipping.py
@@ -20,3 +20,5 @@
+    if customer.address is None:
+        return None
```

## Verdict

| Verdict | When |
|---|---|
| BLOCK | any critical finding |
| HOLD | any high finding, any validator that failed (timeout, unparseable output), or CI missing when `REQUIRE_CI=true` |
| AUTO-FIX | only medium/low findings, or CI failed with nothing worse |
| PASS | nothing above info, and every validator succeeded |

Vader reports the verdict; it doesn't merge or send fixes back yet.

## Setup

### 1. Slack app
Update the existing app at api.slack.com/apps → your app → **App Manifest**: paste
`slack_bot/manifest.yaml` (on the YAML tab) and save. That renames it to
**Vader**, adds `/vader`, and adds the `chat:write.customize` scope. Then **Install App →
Reinstall** and check that the `xoxb-` token in `.env.slack` still matches. In each channel
you'll use: `/invite @Vader`.

### 2. Environment files
Each program has its own file (both are gitignored; see `.env.example`):

| File | Holds |
|---|---|
| `.env.vader` | `VADER_API_KEY`, Ollama URL, context limits, GitHub token and repos |
| `.env.slack` | Slack tokens, `VADER_URL`, the same `VADER_API_KEY`, `SLACK_NOTIFY_CHANNEL` |

`SLACK_NOTIFY_CHANNEL` is the channel **ID** where automatic PR reviews and alerts are posted
(channel name → View channel details → bottom of the About tab).

### 3. Ollama
Start the Ollama app (or `ollama serve`), then pull every model `models.yaml` assigns to a validator.
Vader's startup log lists any that are missing:
```powershell
ollama pull qwen3.6:35b-a3b
ollama pull devstral:24b
ollama pull gemma3:12b
```
On the Vader machine, set these Ollama environment variables (Windows: System → Environment
Variables), then restart Ollama:

| Variable | Laptop (8 GB GPU) | Why |
|---|---|---|
| `OLLAMA_CONTEXT_LENGTH` | `32768` | Context window; match `LLM_CONTEXT_TOKENS` in `.env.vader` |
| `OLLAMA_FLASH_ATTENTION` | `1` | Needed for the cache setting below |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Halves the context's memory with little quality loss |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | A second guard for the one-model rule |
| `OLLAMA_KEEP_ALIVE` | `1m` (optional) | Frees memory soon after a review |

Models bigger than the GPU spill into system RAM, which is slower but works; mixture-of-experts
models (`qwen3.6:35b-a3b`) handle that best. Check with `ollama ps` while a model is loaded. If
Ollama loads a model with less context than `LLM_CONTEXT_TOKENS`, Vader refuses it (the review is
HOLD with a message) rather than let prompts be cut off silently. Keep `OLLAMA_HOST` at its default
(`127.0.0.1`): Ollama has no login, so `0.0.0.0` lets anyone on the network use it. With part of the model on the CPU, give each
validator time: `LLM_TIMEOUT_SECONDS=600`.

### 4. Run
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
.\start.ps1                 # opens two windows: Vader, then the Slack bot
```
Or by hand, in two terminals: `python -m vader.service` and `python -m slack_bot.bot`.

### 5. Automatic PR reviews (optional)
1. Create a **fine-grained** GitHub token for the test repo only: *Contents: Read*, *Issues: Read*,
   *Pull requests: Read and write* (write is only for the review comment; set `GITHUB_COMMENT=false`
   to stay read-only). Put it in `.env.vader` as `GITHUB_TOKEN`, and set
   `GITHUB_REPOS=owner/repo` (comma-separated for more than one).
2. Set `SLACK_NOTIFY_CHANNEL` in `.env.slack`.
3. Restart both, then `/vader auto on`.

Every `POLL_SECONDS` (default 60), Vader checks the open PRs. A PR is reviewed when it has a
commit Vader hasn't reviewed yet. Drafts are skipped unless `REVIEW_DRAFTS=true` (Copilot
opens draft PRs while it's still working). A PR body with "Fixes #12" pulls issue #12 in as
the ticket, and the header says whether Copilot opened the PR or is assigned to the issue.

## Benchmarking models

```powershell
python bench.py                                  # every enabled, pulled model in models.yaml
python bench.py --models qwen3:14b,devstral:24b --runs 5
```
For each model: load time, memory, generation and prompt speed (tokens/s), time per
validator and per full review, bugs caught across the planted cases in `bench_cases.yaml`,
false alarms on a clean change, and JSON failures. Results go to `bench_results/`.
Run it on the Mac mini for real speed numbers; catch rates carry over between machines.

## Tests

```powershell
python -m unittest discover -s tests -t .
```
They use a fake model, so they run without Ollama or Slack.

## Layout

```
vader/       service.py (API)  engine.py (queue)  agents.py (validators)  models.py (one-model rule)
             decision.py (verdict)  github.py (poller)  report.py (PR comment)  state.py  findings.py
             llm.py  skill_loader.py  config.py
slack_bot/   bot.py  vader_client.py  fmt.py  manifest.yaml (gitignored)
skills/      the six SKILL.md files (generic; gitignored)
models.yaml  bench.py  bench_cases.yaml  start.ps1  tests/
```

Data note: anything posted to Slack leaves your machine. Use sample diffs in a test
workspace until it's cleared for real code.
