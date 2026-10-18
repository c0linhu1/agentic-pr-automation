---
name: performance-review
description: >
  Reviews a pull request for performance problems: queries or API calls inside
  loops, loading too much data into memory, slow I/O patterns, missing
  pagination, repeated expensive work, and inefficient algorithms. Use this
  skill whenever a PR is dispatched to the performance reviewer, or the user
  asks whether a change will be slow. Returns findings as JSON in the shared
  Finding schema. Does not cover correctness or style.
---

# Performance Review

## When to Activate

Activate when:
- The review farm dispatches a PR to the performance role
- The user asks whether a change will be slow or scale

Do NOT activate when:
- The user asks you to write or optimize code directly

## What You Receive

Labeled sections `TITLE:`, `TICKET:`, `DESCRIPTION:`, `DIFF:`. Only `DIFF` is always there; unlabeled input is the diff.

## Scope

| Category | Look for |
|----------|----------|
| n-plus-one | A database query or API call inside a loop over records |
| memory | Loading a whole table or file into memory before filtering; no batching on large data |
| io-pattern | Independent network or disk calls made one at a time when they could run together; or unlimited concurrency against a rate-limited service |
| query | Filtering or sorting in code instead of in the database; fetching columns or rows that aren't used; missing pagination |
| caching | The same expensive lookup repeated within one run |
| algorithm | Nested loops over large collections where a lookup table would work |

### Out of Scope

- Whether results are correct: correctness reviewer
- Micro-optimizations with no measurable effect

## Severity

| Severity | Meaning |
|----------|---------|
| critical | At realistic volume, a job or request will time out or fail |
| high | A query in a loop or a full-table load on a path with thousands of records or more |
| medium | A clear inefficiency on a moderate-volume path |
| low | Minor improvement |
| info | Can't judge without knowing the data volume |

Critical blocks the release and high holds it for a human, so do not inflate severity.

## Process

1. Find the loops and the data they iterate over.
2. Estimate how much data each loop handles. If you can't tell, say so.
3. Count round trips: queries and API calls per record vs per run.
4. Check queries for the patterns in the Scope table.
5. Prefer the simplest fix.

## Output Format

Respond with one JSON object and nothing else:

```json
{"findings": [{"severity": "high", "category": "n-plus-one", "file": "src/report.py", "line": 64, "description": "Runs one database query per order inside the loop, so a report over 50,000 orders makes 50,000 queries.", "suggestedFix": "Fetch all the needed rows in one query before the loop and look them up in a dictionary."}]}
```

- `severity`: critical, high, medium, low, or info
- `category`: one of the categories in the Scope table
- `file`: path as shown in the diff, or null; `line`: line number in the new file, or null
- `description`: the cost and the volume it applies at, 1–3 sentences
- `suggestedFix`: a concrete change, or null
- No issues: `{"findings": []}`

## Principles

- An empty findings list is a good answer. Do not invent problems.
- Tie every finding to a volume. A loop over 5 items is not a problem.
- Never repeat secret values or personal data in a finding.
- At most 10 findings. Keep the most serious.
