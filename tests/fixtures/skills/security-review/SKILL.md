---
name: security-review
description: A minimal security skill for the tests.
---
## When to Activate

Only in tests; this section is left out of the instructions.

## Scope

| Category | Look for |
|---|---|
| injection | Input joined into a query or command |
| authz | A missing permission check |
| secrets | A key or password in the code |

## Output Format

Return findings as JSON in the shared Finding schema.
