"""GitHub access (read PRs, diffs, linked issues, file contents; write only the review comment).
With automation on, polls GITHUB_REPOS and queues each PR commit not yet reviewed."""
import logging
import re
import threading
import time
from urllib.parse import quote

import httpx

from . import config, report
from .agents import VALIDATORS
from .diffs import FileDiff, filter_files
from .engine import Engine, Events, Review, Stopped
from .state import State

log = logging.getLogger("vader.github")

CONTEXT_MAX_FILES = 30      # changed files whose full contents are fetched for context
CONTEXT_MAX_CHARS = 60_000  # skip bigger files

LINKED_ISSUE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")
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


def _is_copilot(login: str) -> bool:
    # TBD per the ticket: confirm the exact account names on a test issue/PR.
    return "copilot" in login.lower()


class GitHubPoller:
    def __init__(self, state: State, events: Events, engine: Engine):
        self.state, self.events, self.engine = state, events, engine
        self._last_error: str | None = None
        self._failed: dict[str, str] = {}  # pr_key -> head sha that couldn't be fetched (reported once)
        self._thread = threading.Thread(target=self._loop, name="vader-github", daemon=True)

    @property
    def configured(self) -> bool:
        return bool(config.GITHUB_TOKEN and config.GITHUB_REPOS)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        while True:
            if self.configured and self.state.enabled and self.state.automation:
                try:
                    self.poll_once()
                    self._last_error = None
                except Stopped:
                    pass
                except Exception as e:  # noqa: BLE001 - keep polling; report each new problem once
                    message = f"GitHub polling failed: {e}"
                    log.warning(message)
                    if message != self._last_error:
                        self.events.add("poller_error", message=message)
                    self._last_error = message
            time.sleep(config.POLL_SECONDS)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=config.GITHUB_API, timeout=30, headers={
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def poll_once(self) -> int:
        """Queue reviews for new PR commits. Returns how many were queued."""
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
                            or self.engine.is_pending(pr_key, sha)):
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
        try:
            with self._client() as gh:
                r = gh.get(f"/repos/{meta['repo']}/commits/{meta['sha']}/check-runs", params={"per_page": 100})
            if r.status_code == 200:
                review.ci = [{"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion")}
                             for c in r.json().get("check_runs", [])]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("couldn't read CI for %s: %s", meta.get("pr_key"), e)

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
        ticket, issue_assignees = "", []
        match = LINKED_ISSUE.search(body)
        if match:
            issue = gh.get(f"/repos/{repo}/issues/{match.group(1)}")
            if issue.status_code == 200:
                i = issue.json()
                ticket = f"#{i['number']} {i['title']}\n{i.get('body') or ''}".strip()
                issue_assignees = [a["login"] for a in i.get("assignees") or []]

        author = pr["user"]["login"]
        return Review(
            source="github", validators=list(VALIDATORS),
            title=pr["title"], description=body, ticket=ticket, diff=diff.text,
            context=self._file_contents(gh, repo, pr["number"], sha),
            meta={"pr_key": pr_key, "sha": sha, "repo": repo, "number": pr["number"], "url": pr["html_url"],
                  "author": author, "linked_issue": int(match.group(1)) if match else None,
                  "copilot_author": _is_copilot(author),
                  "copilot_assigned": any(_is_copilot(a) for a in issue_assignees)},
        )
