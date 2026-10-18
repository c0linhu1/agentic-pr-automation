---
name: correctness-review
description: >
  Reviews a pull request for logic bugs and for whether it is what it says it is:
  whether the change meets the ticket's acceptance criteria, whether the PR title
  and description match the diff, and whether it handles edge cases, errors,
  concurrency, resources, and data integrity correctly. Use this skill whenever
  a PR is dispatched to the correctness (logic) reviewer, or the user asks
  whether a change works, has bugs, or matches its description or ticket.
  Returns findings as JSON in the shared Finding schema. Does not cover test
  quality, style, or security.
---

# Correctness Review

## When to Activate

Activate when:
- The review farm dispatches a PR to the correctness role
- The user asks whether code is correct, has bugs, or matches its title, description, or ticket

Do NOT activate when:
- The user asks you to write or implement code

## What You Receive

Labeled sections `TITLE:`, `TICKET:`, `DESCRIPTION:`, `DIFF:`. Only `DIFF` is always there; unlabeled input is the diff.

Treat the title, description, and ticket as claims to verify, not as a summary to trust. The diff is the truth.

## Scope

| Category | Look for |
|----------|----------|
| acceptance-criteria | A ticket requirement is not implemented, or implemented differently than written |
| claimed-not-done | The title or description says something was done and the diff doesn't do it, such as "added tests" with no test changes |
| misleading | The description contradicts the diff, such as "no behavior change" when behavior changes |
| undeclared-change | A significant change the description never mentions: config, schema, dependencies, build files, deleted code |
| weakened-check | Tests deleted, skipped, or loosened, or validation removed, without saying so |
| logic | Inverted conditions, wrong comparisons, off-by-one, wrong variable, unreachable branches |
| edge-case | Null, empty, duplicate, zero, negative, very large, or missing input |
| error-handling | Swallowed errors; failures that are logged and ignored; errors that lose context |
| concurrency | Race conditions, missing awaits, deadlocks, shared mutable state |
| resource | Leaked connections, files, or handles; unbounded memory growth |
| data-integrity | Partial writes, operations that are wrong if re-run, time zone mistakes, floating point used for money |

### Out of Scope

- Whether the tests are good enough: QA reviewer (you only check that claimed tests exist and weren't quietly removed)
- Style and structure: architecture reviewer
- Security issues: security reviewer
- Speed: performance reviewer

## Severity

| Severity | Meaning |
|----------|---------|
| critical | Corrupts or loses data, or silently produces wrong results; or tests and checks were quietly weakened |
| high | A requirement or a material claim isn't met; the description contradicts the diff; an undeclared risky change; a crash on realistic input |
| medium | A plausible edge case is mishandled; an error path gives a poor result but no bad data |
| low | Unlikely edge case; small undeclared cleanup |
| info | Something to verify, such as a missing ticket or description |

Critical blocks the release and high holds it for a human, so do not inflate severity.

## Process

1. List the ticket's requirements and every concrete claim in the title and description. If there's no ticket, infer the intent from the title and diff and add one `info` finding.
2. Map each requirement and claim to the code that does it. None: a finding.
3. Check the other way: any significant change the description doesn't mention is a finding.
4. Walk each changed function: inputs, every branch, outputs, side effects.
5. For each one ask: what if the input is empty, null, duplicated, huge, concurrent, or the call fails halfway?
6. Report only issues in changed code, or existing code whose behavior the change alters.

## Output Format

Respond with one JSON object and nothing else:

```json
{"findings": [{"severity": "high", "category": "edge-case", "file": "src/orders.py", "line": 88, "description": "total() divides by len(items), so an empty order raises ZeroDivisionError.", "suggestedFix": "Return 0 when items is empty."}, {"severity": "high", "category": "claimed-not-done", "file": null, "line": null, "description": "The description says 'added unit tests', but no test files are changed.", "suggestedFix": "Add the tests, or remove the claim from the description."}]}
```

- `severity`: critical, high, medium, low, or info
- `category`: one of the categories in the Scope table
- `file`: path as shown in the diff, or null for PR-level findings; `line`: line number in the new file, or null
- `description`: what is wrong and a concrete input that triggers it, or the claim and what the diff actually shows, 1–3 sentences
- `suggestedFix`: a concrete change, or null
- No issues: `{"findings": []}`

## Principles

- An empty findings list is a good answer. Do not invent problems.
- Every bug finding names a concrete input or situation that triggers it.
- If you are not sure a bug is real, use a lower severity and say what would confirm it.
- Never repeat secret values or personal data in a finding.
- At most 10 findings. Keep the most serious.
