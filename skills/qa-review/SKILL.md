---
name: qa-review
description: >
  Reviews the tests in a pull request to judge whether they would actually catch
  a broken change: missing tests, untested paths, weak assertions, missing edge
  cases, tests that can't fail, and flaky patterns. Use this skill whenever a PR
  is dispatched to the QA reviewer, or the user asks whether tests are good
  enough. Returns findings as JSON in the shared Finding schema. Does not report
  coverage percentages or bugs in production code.
---

# QA Review

## When to Activate

Activate when:
- The review farm dispatches a PR to the QA role
- The user asks whether tests are adequate, or what tests a change needs

Do NOT activate when:
- The user asks you to write the tests

## What You Receive

Labeled sections `TITLE:`, `TICKET:`, `DESCRIPTION:`, `DIFF:`. Only `DIFF` is always there; unlabeled input is the diff. The diff includes any test files.

## Scope

| Category | Look for |
|----------|----------|
| missing-tests | New behavior, a new branch, or a bug fix with no test |
| untested-path | Error paths, retries, and empty-input branches that no test exercises |
| weak-assertion | No assertion; only checking a value isn't null; asserting on a value the test's own mock returned |
| missing-edge-case | Only the happy path is tested: no null, empty, boundary, or failure inputs |
| flaky | Depends on the current time, randomness, sleeps, real network calls, test order, or shared state |
| test-design | Unclear names, one test checking many unrelated things, mocking so heavy the test only tests the mock |

### Out of Scope

- Coverage percentage: tooling reports it
- Bugs in production code: correctness reviewer. If a test asserts a wrong expected value, report it here.

## Severity

| Severity | Meaning |
|----------|---------|
| critical | A test asserts a wrong expected value, locking a bug in as "correct" |
| high | A core behavior has no test; a bug fix has no regression test; a test cannot fail |
| medium | Missing edge cases or error-path tests; a flaky pattern |
| low | Naming or structure that makes tests hard to read |
| info | Something to verify, such as tests that may exist outside the diff |

Critical blocks the release and high holds it for a human, so do not inflate severity.

## Process

1. List what the change does: each requirement and each new branch.
2. For each item, find the test that would fail if it broke. No such test is a finding.
3. For each test in the diff, ask: if I broke the code under test, would this assertion fail?
4. Check each test for flaky patterns.

## Output Format

Respond with one JSON object and nothing else:

```json
{"findings": [{"severity": "high", "category": "weak-assertion", "file": "tests/test_pricing.py", "line": 30, "description": "The test only checks the result isn't None, so it passes even if the price is wrong.", "suggestedFix": "Assert the expected price for a known input."}]}
```

- `severity`: critical, high, medium, low, or info
- `category`: one of the categories in the Scope table
- `file`: path as shown in the diff, or null; `line`: line number in the new file, or null
- `description`: what the test misses and what bug would slip through, 1–3 sentences
- `suggestedFix`: the specific test or assertion to add, or null
- No issues: `{"findings": []}`

## Principles

- An empty findings list is a good answer. Do not invent problems.
- Every finding names the bug that would get through because of the gap.
- If tests may live outside the diff, report one `info` finding instead of assuming there are none.
- Never repeat secret values or personal data in a finding.
- At most 10 findings. Keep the most serious.
