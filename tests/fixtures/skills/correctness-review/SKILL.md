---
name: correctness-review
description: A minimal correctness skill for the tests.
---
## When to Activate

Only in tests; this section is left out of the instructions.

## Scope

| Category | Look for |
|---|---|
| logic | Code that does the wrong thing |
| edge-case | An input it mishandles |
| data-integrity | Data saved wrongly or twice |
| concurrency | A race |

## Output Format

Return findings as JSON in the shared Finding schema.
