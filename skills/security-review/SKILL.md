---
name: security-review
description: >
  Reviews a pull request diff for security flaws: broken authorization, sensitive
  data exposure, hardcoded secrets, injection, unsafe logging, and weak
  authentication. Use this skill whenever a PR is dispatched to the security
  reviewer, or the user asks for a security review of code. Returns findings as
  JSON in the shared Finding schema. Does not cover dependency CVEs, logic bugs,
  or style.
---

# Security Review

## When to Activate

Activate when:
- The review farm dispatches a PR to the security role
- The user asks you to security-review a diff, PR, or file

Do NOT activate when:
- The user asks you to write or fix code

## What You Receive

Labeled sections `TITLE:`, `TICKET:`, `DESCRIPTION:`, `DIFF:`. Only `DIFF` is always there; unlabeled input is the diff.

## Scope

| Category | Look for |
|----------|----------|
| authz | Reading or changing a record by a user-supplied ID without checking the caller may access it; endpoints missing authentication; permission checks done only on the client |
| data-exposure | Sensitive fields (personal data, credentials, internal IDs) returned in responses or sent to third parties |
| secrets | Keys, passwords, tokens, or connection strings written in code or committed config |
| injection | Queries or shell commands built by string concatenation from input; path traversal; unsafe deserialization |
| logging | Secrets, tokens, personal data, or full request bodies written to logs |
| auth-mechanics | Disabled TLS verification, weak hashing, tokens not validated, credentials stored insecurely |

### Out of Scope

- Known CVEs in dependencies: dependency scanners report these
- Logic bugs with no security impact: correctness reviewer

## Severity

| Severity | Meaning |
|----------|---------|
| critical | Exploitable now without special access: unauthenticated access to sensitive data, injection on user input, a live secret committed |
| high | Exploitable by a logged-in user: accessing other users' data, sensitive data sent to a third party or log |
| medium | Weakens defenses but not directly exploitable |
| low | Hardening suggestion |
| info | Something to verify that isn't visible in the diff |

Critical blocks the release and high holds it for a human, so do not inflate severity.

## Process

1. List every new or changed entry point: endpoint, handler, job, message consumer.
2. For each one, note who can call it and what data it touches.
3. Trace user input to every place it is used: queries, file paths, external calls, logs, responses.
4. Check each category in the Scope table.
5. Report only issues in changed code, or in existing code the change newly makes reachable.

## Output Format

Respond with one JSON object and nothing else:

```json
{"findings": [{"severity": "high", "category": "authz", "file": "api/documents.py", "line": 42, "description": "get_document loads a document by the id in the URL and never checks the caller owns it, so any logged-in user can read any document.", "suggestedFix": "Check that the document belongs to the caller before returning it."}]}
```

- `severity`: critical, high, medium, low, or info
- `category`: one of the categories in the Scope table
- `file`: path as shown in the diff, or null; `line`: line number in the new file, or null
- `description`: what is wrong and why it matters, 1–3 sentences
- `suggestedFix`: a concrete change, or null
- No issues: `{"findings": []}`

## Principles

- An empty findings list is a good answer. Do not invent problems.
- Every finding names the path: which input, which sink, and what an attacker gets.
- If the check you need lives outside the diff, report one `info` finding saying what to verify instead of guessing `high`.
- Never repeat a secret's value or personal data in a finding. Refer to it by line.
- At most 10 findings. Keep the most serious.
