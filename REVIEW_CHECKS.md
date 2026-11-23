# What each validator checks

A summary of `skills/*/SKILL.md`. The skill files are what the validators actually read, so when you
change a skill, update this file too.

Every validator returns findings with a `category` from its own table below and a `severity`:
`critical` blocks the release, `high` holds it for a human, `medium` and `low` mean MINOR, and
`info` is a note only.

## :shield: Security (`skills/security-review`)

| Category | Looks for |
|---|---|
| `authz` | A record read or changed by a user-supplied ID with no ownership check; endpoints with no authentication; permission checks only on the client |
| `data-exposure` | Personal data, credentials, or internal IDs returned in responses or sent to third parties |
| `secrets` | Keys, passwords, tokens, or connection strings in code or committed config |
| `injection` | SQL or shell commands built by string concatenation from input; path traversal; unsafe deserialization |
| `logging` | Secrets, tokens, personal data, or full request bodies written to logs |
| `auth-mechanics` | TLS verification disabled, weak hashing, tokens not validated, credentials stored insecurely |

- **Critical:** exploitable with no special access (unauthenticated access to sensitive data, injection on user input, a live secret committed).
- **High:** exploitable by a logged-in user (other users' data, sensitive data sent to a third party or a log).
- **Doesn't cover:** dependency CVEs (that's for scanners), and logic bugs with no security impact.

## :dart: Correctness (`skills/correctness-review`)

Checks that the PR matches its title, description, and ticket, and checks the logic itself.

| Category | Looks for |
|---|---|
| `acceptance-criteria` | A ticket requirement not implemented, or implemented differently than written |
| `claimed-not-done` | The description says something was done that the diff doesn't do (e.g. "added tests" with no test changes) |
| `misleading` | The description contradicts the diff (e.g. "no behavior change" when behavior does change) |
| `undeclared-change` | Config, schema, dependency, build, or deleted-code changes the description never mentions |
| `weakened-check` | Tests deleted, skipped, or loosened, or validation removed, without saying so |
| `logic` | Inverted conditions, wrong comparisons, off-by-one errors, the wrong variable, unreachable branches |
| `edge-case` | Null, empty, duplicate, zero, negative, very large, or missing input |
| `error-handling` | Swallowed errors; failures logged and then ignored; errors that lose context |
| `concurrency` | Races, missing awaits, deadlocks, shared mutable state; event order (`change`/`blur` fires before the submit); values read after an `await` |
| `resource` | Leaked connections, files, or handles; unbounded memory growth |
| `data-integrity` | Partial writes, operations that go wrong if re-run, time zone mistakes, floats used for money |

- **Critical:** corrupts or loses data, silently produces wrong results, or tests and checks were quietly weakened.
- **High:** a requirement or claim isn't met, the description contradicts the diff, an undeclared risky change, or a crash on realistic input.
- **No ticket:** adds one `info` finding.

## :test_tube: QA (`skills/qa-review`)

| Category | Looks for |
|---|---|
| `missing-tests` | New behavior, a new branch, or a bug fix with no test |
| `untested-path` | Error paths, retries, and empty-input branches that no test exercises |
| `weak-assertion` | No assertion; only `is not None`; asserting on a value the test's own mock returned |
| `missing-edge-case` | Only the happy path is tested |
| `flaky` | Depends on the current time, randomness, sleeps, real network calls, test order, or shared state |
| `test-design` | Unclear names, one test checking unrelated things, mocking so heavy the test only tests the mock |

- **Critical:** a test asserts a wrong expected value, which locks the bug in as correct.
- **High:** core behavior has no test, a bug fix has no regression test, or a test can't fail.
- **No tests for that kind of code at all** (per the `TESTS:` line): reported once, at `medium` at most, with the smallest next step suggested rather than new test infrastructure.

## :building_construction: Architecture (`skills/architecture-review`)

| Category | Looks for |
|---|---|
| `consistency` | Does something differently from the surrounding code |
| `layering` | Data access in UI or request handlers; business logic in views; a lower layer depending on a higher one |
| `coupling` | A hard dependency on a concrete class or service where an abstraction would keep parts independent |
| `duplication` | Rebuilds logic that already appears elsewhere in the diff |
| `config` | URLs, names, limits, or IDs hardcoded instead of read from configuration |
| `api-design` | Inconsistent endpoint or function shapes; internal models leaking out through a public interface; a public surface bigger than needed |

- **High:** a structural problem that will be costly to undo once other code depends on it.
- **Critical:** rare; only when the change will break other parts of the system.
- **Doesn't cover:** formatting (linters handle it), and hardcoded secrets (security handles them).

## :scales: Compliance (`skills/compliance-review`)

| Category | Looks for |
|---|---|
| `data-egress` | Data sent to a new third-party service, API, or hosted tool, and what that data contains |
| `personal-data` | New personal fields collected, stored, exposed, or copied to new places |
| `audit-trail` | Important records changed with no record of who changed what and when |
| `license` | Code copied in with a license header, or from a copyleft source |
| `retention` | Records deleted or overwritten that may need to be kept; bulk changes with no backup |

- **Critical:** personal or confidential data sent to a third party with no sign it's intended.
- **High:** no audit trail on important records, copyleft code copied in, or data deleted with no backup.
- **Laws:** doesn't cite specific laws unless the code or ticket names them.

## :zap: Performance (`skills/performance-review`)

| Category | Looks for |
|---|---|
| `n-plus-one` | A database query or API call inside a loop over records |
| `memory` | Loading a whole table or file before filtering; no batching on large data |
| `io-pattern` | Independent network or disk calls made one at a time; unlimited concurrency against a rate-limited service |
| `query` | Filtering or sorting in code instead of the database; unused columns or rows fetched; missing pagination |
| `caching` | The same expensive lookup repeated within one run |
| `algorithm` | Nested loops over large collections where a lookup table would work |

- **Volume:** every finding must be tied to a data volume; a loop over 5 items isn't a problem.
- **Critical:** a job or request will time out or fail at realistic volume.
- **High:** a query in a loop, or a full-table load, on a path with thousands of records or more.

## API calls

Checks on code that calls an external API are spread across validators:

| Concern | Validator / category |
|---|---|
| API call inside a loop | Performance `n-plus-one` |
| Calls made one at a time, or flooding a rate-limited API | Performance `io-pattern` |
| Missing pagination | Performance `query` |
| Hardcoded base URL or endpoint | Architecture `config` |
| Endpoint shape, internal models leaking | Architecture `api-design` |
| What data is sent and who receives it | Compliance `data-egress`, Security `data-exposure` |
| TLS verification off, tokens not validated | Security `auth-mechanics` |
| API key in code | Security `secrets` |
| Request or response bodies logged | Security `logging` |
| Failed call logged and ignored | Correctness `error-handling` |
| Retried call that isn't safe to repeat | Correctness `data-integrity` |
| Tests that hit the real network | QA `flaky` |

**Not covered by any skill:** missing timeouts and missing retry/backoff.

## Rules every validator follows

- An empty findings list is a good answer; at most 10 findings, keeping the most serious.
- Only issues in changed code (or existing code the change newly makes reachable or alters).
- `evidence` must quote the code exactly. A finding whose quote isn't in the diff, the full files, or the PR text is dropped.
- Never repeat secret values or personal data in a finding.
- Don't state worked-out facts (weekdays, totals, unit conversions) without working them out; a wrong weekday gets the finding dropped.

## What Vader does around the skills

- **Not reviewed:** lockfiles (npm, yarn, pnpm, bun, NuGet, Poetry, uv, Cargo, Go, Gradle, CocoaPods, SwiftPM, Dart, Mix, and more), generated files (.NET designer files and model snapshots, protobuf output in most languages, Dart codegen, and any file that starts with a generator's do-not-edit banner), minified files and source maps, build and package folders (`obj`, `bin/Debug`, `bin/Release`, `node_modules`, `.next`, `__pycache__`, `.venv` and others anywhere; `dist`, `out`, `target` and `coverage` only near the repo root (or a monorepo project's root, such as `packages/a/dist`), and only for files a build writes, so `src/coverage/policy.py` is still reviewed; `build/` is always reviewed, since a committed one usually holds hand-written scripts; a plain `bin/` holds hand-written scripts, so it's reviewed), vendored code (`vendor/`, `third_party/`, `Pods/` at the root or one folder down, and `wwwroot/lib/`), binaries, and `.js` files compiled from a `.ts` file of the same name. A `.js` file counts as compiled only with evidence (it's under `wwwroot`, or has a source-map link or TypeScript's module marker), so a hand-written one that shares a name is still reviewed (`vader/diffs.py`). A skip only lets a PR auto-merge when it can't hide hand-written or tampered code: images, fonts and documents, source maps, a generated-looking file name whose file really starts with a generator's banner (checked at merge time, and not added in this PR) and whose source changed in the PR too, for new files and edits alike (a banner is easy to copy and only shows the file was generated once, so a lone `Foo.g.cs` needs a person; protobuf output is matched to its `.proto` by name), and a lockfile whose manifest changed in the same PR and that only downloads from package registries (`TRUSTED_PACKAGE_HOSTS` adds private ones; GitHub isn't a registry, since anyone can host a fork there, except Cargo's crates.io index). Folder skips, banners seen only in the diff, lockfiles changed on their own, binary lockfiles (`bun.lockb`), Composer lockfiles (whose download lines can't be told from support and funding links), minified files (no one reads a new `evil.min.js`), compiled `.js` (it can differ from the reviewed `.ts`), binaries that can run or unpack (`.dll`, `.jar`, `.zip`, `.wasm`), keys and certificates, and binaries of an unknown type stop the auto-merge.
- **Re-check:** each `critical` and `high` finding is checked a second time against the code and dropped if the model rejects it (`VERIFY_SEVERITIES`).
- **Several asks** (`VALIDATOR_ASKS`): extra asks repeat the main ask. Critical and high findings from any ask are kept for the re-check. Below high, a finding most asks didn't report goes to the re-check rather than being dropped.
- **Merging:** duplicate findings (same file, line, and category) are merged, keeping the worst copy.
- **Verdict** (`vader/decision.py`):
  - Any critical → **BLOCK**.
  - Any high, a validator that didn't finish, or missing CI when `REQUIRE_CI` is set → **HOLD**.
  - Only medium or low findings, or failed CI → **MINOR**.
  - Otherwise → **PASS**.
- **Paths that always need a person:** a PASS still isn't auto-merged when the PR changes a path in `NEEDS_PERSON_PATHS`, including a renamed file's old path. The verdict stays what the review found; only the merge is held, and the message says so.

## Benchmark cases

`bench_cases.yaml` has one planted bug per validator, run with `bench.py`:

| Case | Validator | Expected category |
|---|---|---|
| `sql-injection` | Security | `injection` |
| `divide-by-zero` | Correctness | `edge-case`, `logic` |
| `claimed-tests-missing` | Correctness | `claimed-not-done`, `misleading` |
| `change-event-overwrites-input` | Correctness | event-order bug (blur/change before submit) |
| `weak-assertion` | QA | `weak-assertion` |
| `missing-test-where-the-layer-has-none` | QA | nothing above `medium` |
| `hardcoded-url` | Architecture | `config` |
| `pii-to-third-party` | Compliance | `data-egress`, `personal-data` |
| `query-in-loop` | Performance | `n-plus-one` |
| `clean-change` | All | no findings (false-alarm check) |
