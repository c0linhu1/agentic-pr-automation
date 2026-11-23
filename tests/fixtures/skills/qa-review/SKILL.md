---
name: qa-review
description: A minimal QA skill for the tests.
---
## When to Activate

Only in tests; this section is left out of the instructions.

## Scope

| Category | Look for |
|---|---|
| missing-tests | A change with no test |
| weak-assertion | A test that can't fail |
| flaky | A test that depends on time or order |

## Output Format

Return findings as JSON in the shared Finding schema.
