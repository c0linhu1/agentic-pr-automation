# Vader — validator agents behind Slack

Vader reviews code changes with six validator agents running on local open-weight models (one loaded
at a time) on any OpenAI-compatible model server (Ollama, LM Studio, vLLM, llama.cpp), and reports a
verdict (Pass / Minor / Hold / Block) in Slack.

```
 Slack ──► slack_bot (the face) ──HTTP, localhost──► vader (the brain) ──► model server
                                                          └──────────► GitHub (reads PRs; posts the review comment)
```

- **Two programs.** `vader` is the governance layer: the only program that talks to the model server and
  GitHub. `slack_bot` holds only the Slack tokens and Vader's local address. Neither has the
  other's secrets, and Vader never talks to Slack.
- **Models: one loaded at a time.** Validators use the active model (`/vader model <name>`), or
  their own model if one is set under `validators:` in `models.yaml`. Validators that share a
  model run together, and each review starts with the model already loaded, so swaps are kept to
  a minimum. Only models listed and enabled in `models.yaml` are allowed. Thinking models need
  room to reason (they reason inside `LLM_OUTPUT_TOKENS`, now 12,288), and `reasoning_effort:` and
  `temperature:` (default 0.1) can be set per model. Use the temperature the model's makers recommend
  for thinking: near 0 a thinking model can reason in circles until it runs out of reply space. If one
  still runs out, Vader asks once more with thinking off and says so in the report ("answered without
  thinking"), because a shallower answer is evidence and a failure isn't.
- **Reports everywhere.** Each validator posts its findings in the review's Slack thread, and the
  verdict comes with a report (one line per validator plus the most serious findings), also shown
  in the channel. PR reviews are also posted as a comment on the PR (`GITHUB_COMMENT=true`, needs
  *Pull requests: Read and write* on the token).
- **PRs that pass can be merged.** With `AUTO_MERGE_BRANCH` set (e.g. `staging`), a PR into that
  branch that gets PASS from all six validators, with CI passed on the reviewed commit, is merged
  (see [Auto-merge](#auto-merge)). A PR with merge conflicts isn't merged: Vader says to update the
  PR's branch with the target branch, in Slack and on the PR.
- **Replies always come back in the right format.** Vader sends the Finding format as a JSON
  schema (structured outputs, a standard OpenAI-API feature), so the server only lets the model
  produce `{"findings": [...]}`. As a safety net, an unusable reply is asked again once
  (`PARSE_RETRIES`) and saved to `failed_replies/` (gitignored).
- **No silently cut-off prompts.** Prompts are sized at 2.4 characters per token (code runs about 2.5,
  prose about 4), and after every call Vader checks the server's own token counts. A prompt that came back much
  smaller than it was sent (the server cut it) or a prompt plus reply that overflowed the context fails
  that validator (HOLD) instead of counting as a clean review.
- **Any model server.** Reviews, the model list, and `bench.py` use the standard OpenAI-compatible
  API, so switching between Ollama, LM Studio, vLLM, or llama.cpp's server is a change of `LLM_BASE_URL`.
  With `MODEL_RUNTIME=ollama`, Vader also loads one model at a time itself and checks the context it
  was loaded with; with `MODEL_RUNTIME=generic` (the default), the server loads models on demand.
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
- **Shared prompt start.** Every validator's prompt begins with the same system message (which explains
  the message's sections and the rules all validators share) and PR, with its own instructions last, and large PRs run part by part, so the model server reuses its work
  reading the PR instead of re-reading it for each validator.
- **Validators see the whole changed files.** For PR reviews, Vader also fetches the full contents of
  each changed source file (not generated, binary, deleted, or over 60k characters) and gives them
  as `CONTEXT`, so validators can see what changed code calls and the patterns already in the file.
  Context is only included when it doesn't add parts (model calls); the largest files drop out first.
- **CI as evidence.** When a PR review starts, Vader reads the commit's CI check runs and puts them in
  the prompt (`CI: build-test success…`); the verdict also uses them (CI failed and nothing worse → MINOR).
  Private repos need *Checks: Read* on the token.
- **Test layout as evidence.** For PR reviews, Vader reads the commit's file tree (one GitHub call) and
  tells the validators which kinds of code the repo has tests for (`TESTS: … There are no test files for
  .ts, which this PR changes.`), so a missing test can be told apart from a layer with no test setup yet.
- **Second look before posting.** Each high or critical finding (the ones that decide the verdict) is
  re-checked once by the same model against the same code (`VERIFY_SEVERITIES`, empty = off). Only an
  explicit rejection drops it, and dropped findings are still listed (in Slack and the PR comment) with
  the reason.
- **Asking more than once.** A model can catch a bug in one run and miss it in the next.
  `VALIDATOR_ASKS=correctness=3` asks that validator three times per review (each extra ask is one
  more model call). Every critical or high finding from any ask is kept (the verify pass checks it).
  Below high, a finding most asks didn't report (same file, within 3 lines) may be a one-off false
  alarm, and would still stop an auto-merge, so it goes to the verify pass too rather than being
  dropped: a real bug is sometimes caught by one ask only. Off by default: measure it first with
  `bench.py --asks`.
- **Time limit for slow hardware.** `REVIEW_TIME_LIMIT_MINUTES` (`0` = no limit). After the first
  model call Vader posts an estimate; work still left at the limit is skipped and the verdict is HOLD.

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
[REVIEW_CHECKS.md](REVIEW_CHECKS.md) lists every category each validator looks for.

## Slack commands

```
/vader help
/vader status                       on/off, automation, auto-merge, model, what's running, the queue
/vader stop | start                 kill switch (a running review finishes; queued ones wait)
/vader auto on | off                automatic PR reviews from GitHub
/vader models                       allowed models, and which the model server has
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
| MINOR | only medium/low findings, or CI failed with nothing worse |
| PASS | nothing above info, and every validator succeeded; the PR is then merged (see below) |

MINOR, HOLD, and BLOCK are never merged automatically; they're left for a person.

## Auto-merge

Set `AUTO_MERGE_BRANCH` in `.env.vader` to the branch PRs should be merged into, e.g. `staging`
(empty = off). After a PR review, Vader merges the PR only when all of these hold, and otherwise says
why it didn't (in the review's Slack thread and in the PR comment):

- the verdict is PASS from all six validators (`/vader pr 18 security` never merges);
- the PR is open, not a draft, and targets `AUTO_MERGE_BRANCH` (PRs into any other branch are never merged);
- CI passed on the reviewed commit (re-read at merge time, since CI often finishes during the review);
- nothing was pushed after the review: the merge is pinned to the reviewed commit, and a newer commit
  gets its own review first;
- the PR's branch is up to date with `AUTO_MERGE_BRANCH`. If that branch has moved on since, CI and the
  review never saw the PR together with the new commits, so it has to be brought up to date (and is
  reviewed again) first;
- the PR changes none of the paths in `NEEDS_PERSON_PATHS` (comma-separated globs, e.g.
  `src/Pricing/**,appsettings*.json`), counting a renamed file's old path too. Code where a missed bug
  costs money or data, such as pricing, booking, settings and the schema, then always gets a person,
  however clean the review. The message says the hold comes from this rule, not from a finding;
- Vader isn't stopped (`/vader stop` also stops merges);
- no earlier review of the same commit held or blocked it for its findings. A clean rerun of a commit
  that was held doesn't merge on its own: a person checks the earlier findings and merges it by hand if
  they were wrong. A hold only because a validator didn't finish doesn't count;
- every file left out of the review was left out for a reason that can't hide hand-written or
  tampered code: an image, font or document, a source map, a file named like generated code that
  really starts with a generator's banner (checked at merge time, and not just added) and whose
  source changed in the PR too (`X.Designer.cs` next to `X.cs`, `user_pb2.py` next to `user.proto`),
  since a banner is easy to copy and only shows the file was generated once; or a lockfile whose manifest
  changed too and that only downloads from package registries (add private ones with
  `TRUSTED_PACKAGE_HOSTS`). GitHub doesn't count as a registry, since anyone can host a fork there,
  so git dependencies, SwiftPM, and Composer (whose lockfile can't be checked line by line) need a
  person. Anything else stops the merge too and the message names the file: a folder skip
  (`vendor/`, `dist/`), a banner seen only in the diff, a binary lockfile, a lockfile changed on its
  own, a minified file (it could hold anything), compiled `.js` (it can differ from the reviewed `.ts`), a binary that can run or unpack (`.dll`, `.jar`,
  `.zip`), a key or certificate, or a binary of a type Vader doesn't know.

If the PR has **merge conflicts** with that branch, it isn't merged, and Vader asks for the PR's branch to
be kept up to date with it (merge or rebase the target branch into it, fix the conflicts, push); the new
commit is then reviewed again. Merges and conflicts are also posted to the channel. Merging needs
*Contents: Read and write* on the token.

## Closing linked issues

GitHub only closes an issue from "Closes #12" when the PR merges into the repo's default branch. With
`CLOSE_LINKED_ISSUES=true`, Vader closes the issues a PR links whenever the PR is closed, into any branch
and **whether or not it was merged**:

- merged: the issue is closed as *completed*; closed without merging: as *not planned*;
- each issue gets a comment saying which PR closed it, and Slack gets a notice;
- the first check catches up on PRs closed before; after that only newly closed PRs count, so an issue
  a person reopens stays open (unless another PR that links it closes);
- `#12` that is a PR, an issue that's already closed, or a number with no issue is left alone;
- a refused close (e.g. the token lacks *Issues: Read and write*) is reported once and retried every
  poll, so it catches up once the token is fixed.

It runs every `POLL_SECONDS` while Vader is running, with automation on or off; `/vader stop` pauses it.

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
| `.env.vader` | `VADER_API_KEY`, model server URL and runtime, context limits, GitHub token and repos |
| `.env.slack` | Slack tokens, `VADER_URL`, the same `VADER_API_KEY`, `SLACK_NOTIFY_CHANNEL` |

`SLACK_NOTIFY_CHANNEL` is the channel **ID** where automatic PR reviews and alerts are posted
(channel name → View channel details → bottom of the About tab).

### 3. Model server
Any OpenAI-compatible server works. Set `LLM_BASE_URL` in `.env.vader` and make every model
`models.yaml` assigns to a validator available on it (use the names the server lists at
`<LLM_BASE_URL>/models`); Vader's startup log lists any that are missing. Set `LLM_CONTEXT_TOKENS` to
the context the server gives each model. Whatever the server, Vader checks each reply's token counts and
fails the validator (HOLD) if the prompt was cut off.

**LM Studio, vLLM, llama.cpp** (`MODEL_RUNTIME=generic`): the server loads models itself. To keep to
one model at a time, set it to keep only the last model it loaded (LM Studio: just-in-time loading with
only the last loaded model kept; vLLM and llama.cpp serve one model per process). LM Studio's address is
`http://localhost:1234/v1`.

**Ollama** (`MODEL_RUNTIME=ollama`, `LLM_BASE_URL=http://localhost:11434/v1`): Vader loads one model at
a time and refuses one loaded with less context than `LLM_CONTEXT_TOKENS`. Start Ollama (`ollama serve`),
then pull the models:
```powershell
ollama pull qwen3.6:35b-a3b
ollama pull devstral:24b
ollama pull gemma3:12b
```
On the Vader machine, set these Ollama environment variables (Windows: System → Environment
Variables), then restart Ollama (LM Studio has the same settings in its model load options):

| Variable | Suggested | Why |
|---|---|---|
| `OLLAMA_CONTEXT_LENGTH` | `32768` | Context window; match `LLM_CONTEXT_TOKENS` in `.env.vader` |
| `OLLAMA_FLASH_ATTENTION` | `1` | Needed for the cache setting below |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Halves the context's memory with little quality loss |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | A second guard for the one-model rule |
| `OLLAMA_KEEP_ALIVE` | `1m` (optional) | Frees memory soon after a review |

Models bigger than the GPU spill into system RAM, which is slower but works; mixture-of-experts
models (`qwen3.6:35b-a3b`) handle that best. Check with `ollama ps` while a model is loaded. Keep
`OLLAMA_HOST` at its default (`127.0.0.1`): Ollama has no login, so `0.0.0.0` lets anyone on the network
use it. With part of the model on the CPU, give each validator time: `LLM_TIMEOUT_SECONDS=600`.

### 4. Run
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
.\start.ps1                 # opens two windows: Vader, then the Slack bot
```
Or by hand, in two terminals: `python -m vader.service` and `python -m slack_bot.bot`.

### 5. Automatic PR reviews (optional)
1. Create a **fine-grained** GitHub token for the test repo only: *Contents: Read and write* (write is
   only for auto-merge; *Read* is enough with `AUTO_MERGE_BRANCH=` empty), *Issues: Read and write* (write
   is only for `CLOSE_LINKED_ISSUES`), *Checks: Read*,
   *Pull requests: Read and write* (write is for the review comment and merging; set `GITHUB_COMMENT=false`
   and leave auto-merge off to stay read-only). Put it in `.env.vader` as `GITHUB_TOKEN`, and set
   `GITHUB_REPOS=owner/repo` (comma-separated for more than one).
2. Set `SLACK_NOTIFY_CHANNEL` in `.env.slack`.
3. Restart both, then `/vader auto on`.

Every `POLL_SECONDS` (default 60), Vader checks the open PRs. A PR is reviewed when it has a
commit Vader hasn't reviewed yet. Drafts are skipped unless `REVIEW_DRAFTS=true` (Copilot
opens draft PRs while it's still working). A PR body with "Fixes #12" pulls issue #12 in as
the ticket.

While the model server can't be reached, nothing is queued (Slack gets one warning). A review that
couldn't reach it at all saw nothing, so it isn't recorded as reviewed, gets no PR comment, and is tried
again after 5 minutes. Every other failure (a model that isn't allowed or on the server, timeouts, unusable
replies) is recorded with its HOLD, so nothing retries in a loop.

## Benchmarking models

```powershell
python bench.py                                  # every enabled model in models.yaml the server has
python bench.py --models gpt-oss:20b,devstral:24b --runs 5   # default 3
python bench.py --asks correctness=3 --only correctness     # one ask vs three combined, on the same calls
python bench.py --cases-file bench_cases_blind.yaml         # a held-out set, kept apart from bench_cases.yaml
```
With `--asks`, each case reports each ask on its own, the asks combined, and the asks combined without
the minor findings Vader sends to the verify pass (which the benchmark doesn't run, so the truth is
between those two).
For each model: load time, memory, generation and prompt speed (tokens/s), time per
validator and per full review, bugs caught across the planted cases in `bench_cases.yaml`,
false alarms on clean changes, and JSON failures. Results go to `bench_results/`. Progress is saved after
each case, so if a run is stopped (a crash, or the machine running out of memory), `--resume` with the
same `--runs` carries on from the last finished case.
Run it on the machine Vader will use: speed depends on the hardware, catch rates carry over.

## Tests

```powershell
python -m unittest discover -s tests -t .
```
They use a fake model, clock, and GitHub, so they run without a model server, Slack, or network.
`tests/helpers.py` lists which file covers which area.

## How the models' output is checked

Three layers, each answering a different question.

**1. The unit tests: can Vader misread what a model says?** The fake model returns replies a test
chooses, so these tests can't tell whether a finding is true. They prove that whatever comes back,
Vader handles it the same way every time:
- a malformed reply, a failed or timed-out call, or a prompt the server cut off counts as missing
  evidence (HOLD), never as a clean review;
- every verdict rule (`test_decision.py`);
- the re-check drops a finding only on an explicit "rejected", and keeps it on any error;
- a PR is merged only after PASS from all six validators with CI passed, at the commit that was reviewed,
  and never after an earlier review of that commit held it for its findings.

**2. Guards in every review: is this finding grounded in the code?**
- The reply format is fixed by a JSON schema, so there is no free text to misparse. Its fields are in
  the order the model writes them: the file, the quoted code, and the description come before the
  severity, so the model rates a problem after it has described it. On the second look it writes its
  reason before its verdict. Each validator's `category` is limited to the names in its skill's Scope
  table, so it can't invent one (a skill without that table allows any).
- **Quoted code is checked.** Each finding quotes the code it's about (`evidence`). Vader checks the quote
  against the diff and full files the validator was shown, ignoring spacing and case (no model involved);
  a quote rewrapped onto one line or shortened with "..." counts if its words appear in that order, and a
  claim about the PR may quote its title, ticket, or description.
  A finding whose quote isn't there is dropped and listed with the reason, before the second look, so it
  costs no model call. When the quote is found, Vader takes the file path and line number from where it is,
  instead of trusting the model's count. A finding with nothing to quote (a missing test, a claim about the
  PR) is kept as it is.
- **Stated weekdays are checked.** A finding that says a date is a certain weekday ("2028-04-13 is a
  Wednesday", in any common date format) is checked in Python and dropped with the reason if it's wrong,
  whatever its severity. Models often get these wrong.
- Validators get evidence, not just the diff: the changed files in full (`CONTEXT`), the CI results,
  and the repo's test layout (`TESTS`), so a validator can check a claim (a "test is wrong" claim on a
  test CI passed) instead of guessing.
- The system message and skills say: report only what the diff changes; an empty list is a good
  answer; work out dates and totals step by step or leave them out.
- High and critical findings, the ones that decide the verdict, get a second look against the
  same code, with the quote. Only an explicit rejection drops one, and dropped findings are still listed
  with the reason.
- Only PASS acts on its own (auto-merge). Every other verdict goes to a person, with each finding's
  file and line so it can be checked.

**3. The benchmark: how good is this model?** `bench.py` runs the validators on planted bugs with
known answers (`bench_cases.yaml`), in Python, TypeScript and C#. A catch needs the right category, and
with `expect_words`, a description that names the actual bug. Some cases are held out: no skill
describes their bug, so they test reasoning rather than whether the model repeats a hint. It also
counts false alarms on clean changes, some built to look suspicious (date maths, float asserts), and replies
that failed, plus findings dropped because their quote wasn't in the code. The same quote check runs
there as in a review. Models vary from run to run, so compare with `--runs 3` or more.

**Limits.** The quote check catches invented code and wrong locations, not a wrong conclusion about real
code; that is what the second look and the benchmark are for. A finding that quotes only unchanged code
is kept, because a change can make existing code newly reachable. Vader doesn't use embeddings or
cosine similarity: similarity says whether two texts are about the same thing, not whether a claim is
true ("doesn't check for zero" and "checks for zero" score as almost identical).

## Layout

```
vader/       service.py (API)  engine.py (queue)  agents.py (validators)  models.py (one-model rule)
             decision.py (verdict)  github.py (poller, auto-merge)  report.py (PR comment)  state.py  findings.py
             llm.py  skill_loader.py  config.py
slack_bot/   bot.py  vader_client.py  fmt.py  manifest.yaml (gitignored)
skills/      the six SKILL.md files (generic; gitignored)
models.yaml  bench.py  bench_cases.yaml  start.ps1  tests/
```

Data note: anything posted to Slack leaves your machine. Use sample diffs in a test
workspace until it's cleared for real code.
