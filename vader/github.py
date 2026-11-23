"""GitHub access (read PRs, diffs, linked issues, file contents; write the review comment, merge PRs that
pass, and close the issues of closed PRs). Polls GITHUB_REPOS: with automation on, queues each PR commit
not yet reviewed; with CLOSE_LINKED_ISSUES, closes the issues linked from newly closed PRs."""
import logging
import re
import threading
import time
from collections import Counter
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import quote

import httpx

from . import config, models, report
from .agents import VALIDATORS
from .diffs import FileDiff, split_files
from .engine import Engine, Events, Review, Stopped
from .skips import filter_files, generated_source_changed, has_generated_banner, needs_person, unsafe_skips
from .state import State

log = logging.getLogger("vader.github")

CONTEXT_MAX_FILES = 30      # changed files whose full contents are fetched for context
CONTEXT_MAX_CHARS = 60_000  # skip bigger files
RETRY_UNAVAILABLE_SECONDS = 300  # pause before retrying a review that couldn't reach the model server
MERGEABLE_CHECKS = 3        # times to ask GitHub whether a PR can merge before trying anyway
CHECK_RUN_PAGES = 10        # most pages of check runs read per commit (100 each); more counts as unknown CI
BANNER_CHECKS = 20          # files named like generated code whose banner is checked before a merge
BANNER_LINES = 10           # how far into a file a generator banner is looked for
PR_FILE_PAGES = 30          # GitHub lists at most 3000 of a PR's files, 100 a page

CLOSED_PR_PAGES = 10        # most pages of closed PRs read per repo (100 each); only the first check needs more than one
LINKED_ISSUE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)")
PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_REF = re.compile(r"^(?:([\w.-]+/[\w.-]+))?#?(\d+)$")


def parse_pr_ref(ref: str, default_repo: str | None) -> tuple[str, int]:
    """'11', '#11', 'owner/repo#11', or a PR URL -> (repo, number)."""
    ref = ref.strip().strip("<>")
    m = PR_URL.search(ref)
    if m:
        return m.group(1), int(m.group(2))
    m = PR_REF.match(ref)
    if not m:
        raise ValueError(f"`{ref}` isn't a PR: use a number like 11, owner/repo#11, or the PR's URL")
    repo = m.group(1) or default_repo
    if not repo:
        raise ValueError("no repo given and GITHUB_REPOS is empty: use owner/repo#11")
    return repo, int(m.group(2))


# Code whose tests are usually written in the same language. Variants count as one kind (.tsx as .ts), and
# kinds in one family cover each other (a .ts change in a repo tested in .js has tests).
CODE_KIND = {".py": ".py", ".ts": ".ts", ".tsx": ".ts", ".js": ".js", ".jsx": ".js", ".mjs": ".js", ".cjs": ".js",
             ".cs": ".cs", ".fs": ".fs", ".vb": ".vb", ".java": ".java", ".kt": ".kt", ".scala": ".scala",
             ".go": ".go", ".rb": ".rb", ".rs": ".rs", ".php": ".php", ".swift": ".swift", ".dart": ".dart",
             ".c": ".c", ".h": ".c", ".cc": ".cpp", ".cpp": ".cpp", ".hpp": ".cpp", ".ex": ".ex", ".exs": ".ex"}
KIND_FAMILY = {".ts": "js", ".js": "js", ".c": "c", ".cpp": "c"}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}
TEST_DIR_SUFFIXES = (".tests", ".test", "-tests", "_tests", ".specs")
TEST_NAME = re.compile(r"^test_|_(test|spec)$|[a-z0-9]Tests?$|\.(test|spec)$")  # on the file name minus its extension


def _test_root(path: PurePosixPath) -> str | None:
    """Where a test file lives (its last test-like folder, or its folder), or None if it isn't a test."""
    test_dirs = [i for i, d in enumerate(path.parts[:-1])
                 if d.lower() in TEST_DIRS or d.lower().endswith(TEST_DIR_SUFFIXES)]
    if test_dirs:
        return "/".join(path.parts[:test_dirs[-1] + 1])
    return str(path.parent) if TEST_NAME.search(path.stem) else None


def tests_summary(paths: list[str], changed: list[str], truncated: bool = False) -> str:
    """The TESTS line: the repo's test files by kind and folder, and which kinds of code the PR changes that
    have no tests at all, so a missing test can be told apart from a layer with no test setup."""
    roots: dict[str, Counter] = {}
    for path in map(PurePosixPath, paths):
        kind, root = CODE_KIND.get(path.suffix.lower()), _test_root(path)
        if kind and root:
            roots.setdefault(kind, Counter())[root] += 1
    partial = " The repo is too large to list completely, so this may be incomplete." if truncated else ""
    if not roots:
        return "TESTS: the repo has no test files." + partial

    def describe(kind: str) -> str:
        n, top = sum(roots[kind].values()), sorted(roots[kind].items(), key=lambda kv: (-kv[1], kv[0]))
        where = ", ".join(r for r, _ in top[:3]) + (f", and {len(top) - 3} more" if len(top) > 3 else "")
        return f"{n} {kind} file{'s' if n != 1 else ''} ({where})"

    kinds = sorted(roots, key=lambda k: (-sum(roots[k].values()), k))
    tested = {KIND_FAMILY.get(k, k) for k in roots}
    untested = sorted({k for k in (CODE_KIND.get(PurePosixPath(p).suffix.lower()) for p in changed)
                       if k and KIND_FAMILY.get(k, k) not in tested})
    line = "TESTS: the repo's test files are " + " and ".join(describe(k) for k in kinds) + "."
    if untested:
        line += f" There are no test files for {', '.join(untested)}, which this PR changes."
    return line + partial


def linked_issues(body: str | None) -> list[int]:
    """Issues a PR says it closes ("Closes #12", "fixes: #3"), in order, without repeats."""
    return list(dict.fromkeys(int(n) for n in LINKED_ISSUE.findall(body or "")))


class GitHubPoller:
    def __init__(self, state: State, events: Events, engine: Engine):
        self.state, self.events, self.engine = state, events, engine
        self._last_error: dict[str, str] = {}  # job -> its last error, so each new problem is reported once
        self._failed: dict[str, str] = {}  # pr_key -> head sha that couldn't be fetched (reported once)
        self._retry_at: dict[tuple[str, str], float] = {}  # (pr_key, sha) -> when to retry a review that saw nothing
        self._thread = threading.Thread(target=self._loop, name="vader-github", daemon=True)

    @property
    def configured(self) -> bool:
        return bool(config.GITHUB_TOKEN and config.GITHUB_REPOS)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        while True:
            self.tick()
            time.sleep(config.POLL_SECONDS)

    def tick(self) -> None:
        """One round of background work. Stop pauses all of it; automation only turns on reviews."""
        if not (self.configured and self.state.enabled):
            return
        if self.state.automation:
            self._guard("Automatic PR reviews", self.poll_once)
        if config.CLOSE_LINKED_ISSUES:
            self._guard("Closing linked issues", self.sync_closed_prs)

    def _guard(self, job: str, run: Callable[[], object]) -> None:
        try:
            run()
            self._last_error.pop(job, None)
        except Stopped:
            pass
        except Exception as e:  # noqa: BLE001 - keep going; retried next round
            message = f"{job} failed: {e}"
            log.warning(message)
            if message != self._last_error.get(job):
                self.events.add("poller_error", message=message)
            self._last_error[job] = message

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=config.GITHUB_API, timeout=30, headers={
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def poll_once(self) -> int:
        """Queue reviews for new PR commits. Returns how many were queued."""
        if models.available() is None:  # reviews would see nothing; wait (reported once) instead of queuing
            raise models.ModelServerDown(f"can't reach the model server at {config.LLM_BASE_URL}, so new PR "
                                         "commits wait until it's back")
        self._retry_at = {k: t for k, t in self._retry_at.items() if t > time.time()}
        queued = 0
        with self._client() as gh:
            for repo in config.GITHUB_REPOS:
                prs = gh.get(f"/repos/{repo}/pulls", params={"state": "open", "per_page": 50})
                prs.raise_for_status()
                for pr in prs.json():
                    if pr.get("draft") and not config.REVIEW_DRAFTS:
                        continue
                    pr_key, sha = f"{repo}#{pr['number']}", pr["head"]["sha"]
                    if (self.state.reviewed_sha(pr_key) == sha or self._failed.get(pr_key) == sha
                            or (pr_key, sha) in self._retry_at or self.engine.is_pending(pr_key, sha)):
                        continue
                    try:
                        review = self._review_for(gh, repo, pr, pr_key, sha)
                    except httpx.HTTPStatusError as e:
                        # One PR GitHub won't serve (e.g. 406) mustn't stop the others; retried on its next push.
                        self._failed[pr_key] = sha
                        self.events.add("poller_error", message=f"couldn't fetch {pr_key} for review: "
                                        f"GitHub returned {e.response.status_code}")
                        continue
                    self.engine.submit(review)
                    queued += 1
        return queued

    def sync_closed_prs(self) -> int:
        """Close the issues linked from PRs closed since the last check, merged or not. Returns how many."""
        closed = 0
        with self._client() as gh:
            for repo in config.GITHUB_REPOS:
                since = done = self.state.issues_synced_through(repo)
                open_links = None  # issues an open PR still links, read once if a PR closed without merging
                try:
                    for pr in sorted(self._closed_since(gh, repo, since), key=lambda p: p["closed_at"]):
                        issues = linked_issues(pr.get("body"))
                        if issues and not pr.get("merged_at"):
                            # A PR replaced by another one (e.g. combined into it) leaves its issues to that PR.
                            if open_links is None:
                                open_links = self._open_pr_links(gh, repo)
                            issues = [n for n in issues if n not in open_links]
                        closed += sum(self._close_issue(gh, repo, n, pr) for n in issues)
                        done = pr["closed_at"]
                finally:  # one save per repo; after an error, up to the last PR fully handled
                    if done != since:
                        self.state.mark_issues_synced(repo, done)
        return closed

    @staticmethod
    def _closed_since(gh: httpx.Client, repo: str, since: str | None) -> list[dict]:
        """PRs closed after `since` (every recent one when None). Newest-updated first, so after the first
        check one page is enough: anything closed since has been updated since."""
        found = []
        for page in range(1, CLOSED_PR_PAGES + 1):
            r = gh.get(f"/repos/{repo}/pulls", params={"state": "closed", "sort": "updated", "direction": "desc",
                                                       "per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            found += [p for p in batch if p.get("closed_at") and (since is None or p["closed_at"] > since)]
            if len(batch) < 100 or (since and batch[-1]["updated_at"] <= since):
                break
        return found

    @staticmethod
    def _open_pr_links(gh: httpx.Client, repo: str) -> set[int]:
        """Issues linked ("Closes #12") from the repo's open PRs."""
        links = set()
        for page in range(1, CLOSED_PR_PAGES + 1):
            r = gh.get(f"/repos/{repo}/pulls", params={"state": "open", "per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            links.update(n for p in batch for n in linked_issues(p.get("body")))
            if len(batch) < 100:
                return links
        return links

    def _close_issue(self, gh: httpx.Client, repo: str, number: int, pr: dict) -> int:
        """Close one linked issue with a comment saying why; 1 if closed. A refused close raises, so the
        PR is retried next round; an issue GitHub won't close for other reasons is reported and skipped."""
        r = gh.get(f"/repos/{repo}/issues/{number}")
        if r.status_code in (404, 410):  # no such issue, or issues are off
            return 0
        r.raise_for_status()
        issue = r.json()
        if "pull_request" in issue or issue["state"] != "open":  # "#12" is a PR, or the issue is already closed
            return 0
        merged = bool(pr.get("merged_at"))
        closing = gh.patch(f"/repos/{repo}/issues/{number}",
                           json={"state": "closed", "state_reason": "completed" if merged else "not_planned"})
        if closing.status_code in (401, 403, 404):
            raise RuntimeError(f"GitHub refused to close {repo}#{number} ({closing.status_code}); the token needs "
                               "\"Issues: Read and write\" (or set CLOSE_LINKED_ISSUES=false)")
        if closing.status_code in (410, 422):
            self.events.add("issue_close_failed", message=f"GitHub won't close {repo}#{number} "
                                                          f"({closing.status_code}); close it by hand")
            return 0
        closing.raise_for_status()
        how = f"was merged into `{pr['base']['ref']}`" if merged else "was closed without being merged"
        # Closed first, then the comment: a failed comment must not leave the issue open or post twice.
        note = gh.post(f"/repos/{repo}/issues/{number}/comments", json={"body": f"Closed by Vader: #{pr['number']} {how}."})
        if note.status_code >= 400:
            log.warning("closed %s#%s but couldn't comment (%s)", repo, number, note.status_code)
        self.events.add("issue_closed", repo=repo, issue=number, title=issue["title"], url=issue["html_url"],
                        pr=pr["number"], merged=merged)
        return 1

    def comment_on_pr(self, review: Review) -> None:
        """Post a finished PR review's report as a PR comment (GITHUB_COMMENT; needs Pull requests: write)."""
        meta = review.meta
        if not (config.GITHUB_COMMENT and config.GITHUB_TOKEN and meta.get("repo") and meta.get("number")):
            return
        try:
            with self._client() as gh:
                r = gh.post(f"/repos/{meta['repo']}/issues/{meta['number']}/comments",
                            json={"body": report.markdown(review)})
        except httpx.HTTPError as e:
            self.events.add("pr_comment_failed", review=review.summary(), error=f"couldn't reach GitHub: {e}")
            return
        if r.status_code in (401, 403, 404):
            self.events.add("pr_comment_failed", review=review.summary(), error=(
                f"GitHub refused the comment ({r.status_code}); the token needs \"Pull requests: Read and write\" "
                "on this repo (or set GITHUB_COMMENT=false)"))
        elif r.status_code >= 400:
            self.events.add("pr_comment_failed", review=review.summary(),
                            error=f"GitHub returned {r.status_code}: {r.text[:200]}")
        else:
            self.events.add("pr_comment_posted", review=review.summary(), url=r.json().get("html_url"))

    def attach_ci(self, review: Review) -> None:
        """Add the PR commit's CI check runs just before the review runs; on any error, no CI."""
        meta = review.meta
        if not (config.GITHUB_TOKEN and meta.get("repo") and meta.get("sha")):
            return
        with self._client() as gh:
            review.ci = self._check_runs(gh, meta["repo"], meta["sha"])

    @staticmethod
    def _check_runs(gh: httpx.Client, repo: str, sha: str) -> list[dict] | None:
        """Every check run on the commit, or None if they couldn't all be read: an unread failure must
        never look like a pass."""
        runs: list[dict] = []
        try:
            for page in range(1, CHECK_RUN_PAGES + 1):
                r = gh.get(f"/repos/{repo}/commits/{sha}/check-runs", params={"per_page": 100, "page": page})
                if r.status_code != 200:
                    return None
                data = r.json()
                runs += [{"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion")}
                         for c in data.get("check_runs", [])]
                total = data.get("total_count", len(runs))
                if len(runs) >= total or not data.get("check_runs"):
                    break
            if len(runs) < total:
                log.warning("%s@%s has %d check runs; read only %d, so CI counts as unknown",
                            repo, sha[:7], total, len(runs))
                return None
            return runs
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("couldn't read CI for %s@%s: %s", repo, sha[:7], e)
        return None

    def finish(self, review: Review) -> None:
        """After a review: merge it if it passed, then post the report (which says what the merge did)."""
        if review.unavailable:  # it saw nothing: no PR comment, and the poller tries again after a pause
            if review.meta.get("pr_key"):
                self._retry_at[(review.meta["pr_key"], review.meta["sha"])] = time.time() + RETRY_UNAVAILABLE_SECONDS
            return
        self.merge_if_passed(review)
        self.comment_on_pr(review)

    def merge_if_passed(self, review: Review) -> None:
        """Merge a PR into AUTO_MERGE_BRANCH after a PASS; sets review.merge and tells front-ends."""
        meta, branch = review.meta, config.AUTO_MERGE_BRANCH
        if not (branch and config.GITHUB_TOKEN and meta.get("number") and review.verdict == "PASS"):
            return
        try:
            with self._client() as gh:
                status, message = self._merge(gh, review, branch)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            status, message = "failed", f"Couldn't merge into `{branch}`: the GitHub request failed ({e})."
        review.merge = {"status": status, "branch": branch, "message": message}
        log.info("merge %s into %s: %s", meta.get("pr_key"), branch, status)
        self.events.add("pr_merge", review=review.summary(), status=status, message=message)

    @staticmethod
    def _first_lines(gh: httpx.Client, repo: str, ref: str, path: str) -> str | None:
        """The file's first lines at this commit; None when it doesn't exist there, "" when unreadable."""
        try:
            r = gh.get(f"/repos/{repo}/contents/{quote(path)}", params={"ref": ref},
                       headers={"Accept": "application/vnd.github.raw+json"})
        except httpx.HTTPError:
            return ""
        if r.status_code == 404:
            return None
        return "\n".join(r.text.splitlines()[:BANNER_LINES]) if r.status_code == 200 else ""

    def _really_generated(self, gh: httpx.Client, repo: str, pr: dict, path: str, reviewed: set[str]) -> bool:
        """A file skipped for its generated-looking name is safe to merge unread when it starts with a
        generator's banner that it didn't just gain, and the source it's generated from changed in this PR
        and was reviewed. A banner is easy to copy and only shows the file was generated once, so a new file
        or an edit with nothing it was generated from needs a person."""
        head = self._first_lines(gh, repo, pr["head"]["sha"], path)
        if head is None:
            return True  # deleted in this PR: adds no code
        if not has_generated_banner(head):
            return False
        base = self._first_lines(gh, repo, pr["base"]["sha"], path)
        if base is not None and not has_generated_banner(base):
            return False  # the banner was added in this PR
        return generated_source_changed(path, reviewed)

    @staticmethod
    def _unread_why(reason: str) -> str:
        return ("named like generated code, but it doesn't start with a generator's banner, the banner is new, "
                "or it's a new file whose source isn't in this PR") if reason == "generated" else reason

    @staticmethod
    def _behind_by(gh: httpx.Client, repo: str, branch: str, sha: str) -> int | None:
        """Commits on `branch` that this commit doesn't have; None if GitHub couldn't say."""
        try:
            r = gh.get(f"/repos/{repo}/compare/{quote(branch, safe='')}...{sha}")
            return int(r.json()["behind_by"]) if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _changed_paths(gh: httpx.Client, repo: str, number: int) -> list[str] | None:
        """Every path the PR touches, a renamed file's old path too; None if GitHub couldn't list them all."""
        paths = []
        try:
            for page in range(1, PR_FILE_PAGES + 1):
                r = gh.get(f"/repos/{repo}/pulls/{number}/files", params={"per_page": 100, "page": page})
                if r.status_code != 200:
                    return None
                files = r.json()
                paths += [p for f in files for p in (f["filename"], f.get("previous_filename")) if p]
                if len(files) < 100:
                    return paths
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        return None  # more files than GitHub lists

    def _unread(self, review: Review, skip: str, generated_ok: Callable[[str], bool]) -> tuple[str, str]:
        unread = unsafe_skips(review.skipped_files, generated_ok)
        files = ", ".join(f"`{s['path']}` ({self._unread_why(s['reason'])})" for s in unread[:10])
        more = f" and {len(unread) - 10} more" if len(unread) > 10 else ""
        return "skipped", skip + (f"some files weren't reviewed, and why they were skipped doesn't rule out "
                                  f"hand-written or tampered code: {files}{more}. Check them and merge by hand.")

    def _merge(self, gh: httpx.Client, review: Review, branch: str) -> tuple[str, str]:
        repo, number, sha = review.meta["repo"], review.meta["number"], review.meta["sha"]
        skip = f"Not merged into `{branch}`: "
        if set(review.validators) != set(VALIDATORS):
            return "skipped", skip + "only a review by all six validators can merge a PR."
        if not self.state.enabled:
            return "skipped", skip + "Vader is stopped."
        if unsafe_skips(review.skipped_files, lambda path: True):  # can't pass whatever the generated files are
            return self._unread(review, skip, lambda path: False)
        pr = self._pr_for_merge(gh, repo, number)
        if pr.get("merged"):
            return "skipped", skip + "it's already merged."
        if pr["state"] != "open":
            return "skipped", skip + "the PR is closed."
        if pr["base"]["ref"] != branch:
            return "skipped", skip + f"it targets `{pr['base']['ref']}`, and Vader only merges into `{branch}`."
        if pr.get("draft"):
            return "skipped", skip + "it's a draft."
        moved = ("skipped", skip + "a newer commit was pushed after this review, and it needs its own review.")
        if pr["head"]["sha"] != sha:
            return moved
        if config.NEEDS_PERSON_PATHS:
            paths = self._changed_paths(gh, repo, number)
            if paths is None:
                return "skipped", skip + ("couldn't list every file the PR changes, to check them against "
                                          "`NEEDS_PERSON_PATHS`. A person should check it and merge it by hand.")
            if hits := needs_person(paths, config.NEEDS_PERSON_PATHS):
                more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
                return "skipped", skip + (f"it changes {', '.join(f'`{p}`' for p in hits[:3])}{more}, which a "
                                          "person always reviews (`NEEDS_PERSON_PATHS`). This isn't a problem the "
                                          "review found: a person should review it and merge it by hand.")
        named = [s["path"] for s in review.skipped_files if s["reason"] == "generated"][:BANNER_CHECKS]
        if named:
            reviewed = {f.path for f in split_files(review.diff)} - {s["path"] for s in review.skipped_files}
            ok = lambda path: path in named and self._really_generated(gh, repo, pr, path, reviewed)  # noqa: E731
            if unsafe_skips(review.skipped_files, ok):
                return self._unread(review, skip, ok)
        if held := self.state.held_reason(review.meta.get("pr_key", f"{repo}#{number}"), sha):
            return "skipped", skip + (f"an earlier review of this same commit held it ({held}), and this one "
                                      "didn't find that. A person should check the earlier findings and merge "
                                      "it by hand if they were wrong.")
        # CI and the review saw the PR as it was. If the target branch has moved on since, the merged result
        # was never built or tested, so the branch must be brought up to date (and reviewed again) first.
        behind = self._behind_by(gh, repo, branch, sha)
        if behind is None:
            return "skipped", skip + f"couldn't check whether `{pr['head']['ref']}` is up to date with `{branch}`."
        if behind:
            return "skipped", skip + (f"`{pr['head']['ref']}` is {behind} commit(s) behind `{branch}`, so CI and "
                                      f"this review never saw it together with them. Merge or rebase `{branch}` "
                                      f"into it and push; Vader reviews the new commit (automatically when "
                                      f"automation is on, or with `/vader pr {number}`).")
        review.ci = self._check_runs(gh, repo, sha)  # CI often finishes while the review runs
        if review.ci_status == "failed":
            return "skipped", skip + "CI failed."
        if review.ci == []:
            return "skipped", skip + (f"no CI checks ran on this commit, and Vader merges only after CI passes. "
                                      f"If the repo has no CI, add a workflow that builds and runs the tests on "
                                      f"`pull_request`. If CI is just slow to start, run `/vader pr {number}` "
                                      "again once it has passed.")
        if review.ci_status is None:
            return "skipped", skip + (f"CI hasn't passed yet (still running, or its results couldn't be read). "
                                      f"Run `/vader pr {number}` again once it has.")

        conflict = ("conflict", f"Not merged: `{pr['head']['ref']}` has merge conflicts with `{branch}`. Keep the "
                                f"PR's branch up to date with `{branch}`: merge or rebase `{branch}` into it, fix the "
                                f"conflicts, and push. Vader reviews the new commit (automatically when automation "
                                f"is on, or with `/vader pr {number}`).")
        if pr.get("mergeable") is False:
            return conflict
        # Pinned to the reviewed commit: GitHub refuses (409) if the branch moved since.
        r = gh.put(f"/repos/{repo}/pulls/{number}/merge", json={"sha": sha, "merge_method": "merge"})
        if r.status_code == 200:
            return "merged", f"Merged into `{branch}` ({r.json().get('sha', '')[:7]})."
        try:
            reason = r.json().get("message", "")
        except ValueError:
            reason = r.text[:200]
        if r.status_code == 405 and "not mergeable" in reason.lower():
            return conflict
        if r.status_code == 409:
            return moved
        if r.status_code in (401, 403, 404):
            return "failed", (f"Couldn't merge into `{branch}`: GitHub refused ({r.status_code}). The token needs "
                              "\"Contents: Read and write\" and \"Pull requests: Read and write\" on this repo "
                              "(or set AUTO_MERGE_BRANCH= to turn merging off).")
        return "failed", f"Couldn't merge into `{branch}`: GitHub said {reason or r.status_code}."

    @staticmethod
    def _pr_for_merge(gh: httpx.Client, repo: str, number: int) -> dict:
        """The PR as it is now. GitHub works out `mergeable` in the background (null until it's done)."""
        for attempt in range(MERGEABLE_CHECKS):
            r = gh.get(f"/repos/{repo}/pulls/{number}")
            r.raise_for_status()
            pr = r.json()
            if pr.get("mergeable") is not None or pr.get("state") != "open" or attempt == MERGEABLE_CHECKS - 1:
                return pr
            time.sleep(2)
        return pr

    def review_for_pr(self, repo: str, number: int) -> Review:
        """Build a review of one specific PR (asked for by a person, so it's reviewed even if
        this commit was reviewed before, and whether or not automation is on)."""
        with self._client() as gh:
            r = gh.get(f"/repos/{repo}/pulls/{number}")
            if r.status_code == 404:
                raise LookupError(f"{repo}#{number} wasn't found (or the token can't see it)")
            r.raise_for_status()
            pr = r.json()
            return self._review_for(gh, repo, pr, f"{repo}#{number}", pr["head"]["sha"])

    @staticmethod
    def _test_layout(gh: httpx.Client, repo: str, sha: str, changed: list[str]) -> str:
        """The TESTS line for the commit's file tree (one request). Optional: empty if GitHub can't list it."""
        try:
            r = gh.get(f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"})
            if r.status_code != 200:
                return ""
            data = r.json()
            paths = [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]
        except (httpx.HTTPError, ValueError, KeyError, AttributeError):
            return ""
        return tests_summary(paths, changed, truncated=bool(data.get("truncated")))

    @staticmethod
    def _file_contents(gh: httpx.Client, repo: str, number: int, sha: str) -> dict[str, str]:
        """Full contents of the PR's changed source files at its commit, for context. Deleted,
        generated, binary, and very large files, and any that fail, are left out."""
        try:
            r = gh.get(f"/repos/{repo}/pulls/{number}/files", params={"per_page": 100})
            r.raise_for_status()
            changed = [FileDiff(f["filename"], "") for f in r.json() if f.get("status") != "removed"]
        except (httpx.HTTPError, ValueError, KeyError):
            return {}
        kept, _ = filter_files(changed)
        contents = {}
        for f in kept[:CONTEXT_MAX_FILES]:
            try:
                c = gh.get(f"/repos/{repo}/contents/{quote(f.path)}", params={"ref": sha},
                           headers={"Accept": "application/vnd.github.raw+json"})
            except httpx.HTTPError:
                continue
            if c.status_code == 200 and len(c.text) <= CONTEXT_MAX_CHARS and "\x00" not in c.text:
                contents[f.path] = c.text
        return contents

    def _review_for(self, gh: httpx.Client, repo: str, pr: dict, pr_key: str, sha: str) -> Review:
        diff = gh.get(f"/repos/{repo}/pulls/{pr['number']}", headers={"Accept": "application/vnd.github.diff"})
        diff.raise_for_status()

        body = pr.get("body") or ""
        ticket = ""
        match = LINKED_ISSUE.search(body)
        if match:
            issue = gh.get(f"/repos/{repo}/issues/{match.group(1)}")
            if issue.status_code == 200:
                i = issue.json()
                ticket = f"#{i['number']} {i['title']}\n{i.get('body') or ''}".strip()

        return Review(
            source="github", validators=list(VALIDATORS),
            title=pr["title"], description=body, ticket=ticket, diff=diff.text,
            context=self._file_contents(gh, repo, pr["number"], sha),
            test_layout=self._test_layout(gh, repo, sha, [f.path for f in split_files(diff.text)]),
            meta={"pr_key": pr_key, "sha": sha, "repo": repo, "number": pr["number"], "url": pr["html_url"],
                  "author": pr["user"]["login"], "linked_issue": int(match.group(1)) if match else None},
        )
