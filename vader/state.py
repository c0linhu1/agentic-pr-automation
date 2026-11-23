"""Vader's saved state, so a restart doesn't reset anything: on/off, automation, the active model,
which PR commits have already been reviewed (and which were held for findings), and how far closing
linked issues has got.
One JSON file, written atomically (temp file, then rename)."""
import json
import os
import threading
from pathlib import Path

# issues_synced: repo -> closed_at of the newest closed PR whose linked issues were handled
# held: pr_key -> {"sha", "reason"} for a commit a review held or blocked for its findings
DEFAULTS = {"enabled": True, "automation": False, "model": None, "reviewed": {}, "issues_synced": {}, "held": {}}


class State:
    def __init__(self, path: Path, default_model: str | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS, reviewed={}, issues_synced={}, held={})
        if path.exists():
            self._data.update(json.loads(path.read_text(encoding="utf-8")))
        if not self._data["model"]:
            self._data["model"] = default_model

    def __getattr__(self, key):
        if key.startswith("_") or key not in DEFAULTS:
            raise AttributeError(key)
        with self._lock:
            return self._data[key]

    def set(self, **changes) -> None:
        with self._lock:
            self._data.update(changes)
            self._save()

    def reviewed_sha(self, pr_key: str) -> str | None:
        with self._lock:
            return self._data["reviewed"].get(pr_key)

    def mark_reviewed(self, pr_key: str, sha: str) -> None:
        with self._lock:
            self._data["reviewed"][pr_key] = sha
            self._save()

    def held_reason(self, pr_key: str, sha: str) -> str | None:
        """Why an earlier review held this commit, or None if none did."""
        with self._lock:
            held = self._data["held"].get(pr_key) or {}
            return held.get("reason") if held.get("sha") == sha else None

    def mark_held(self, pr_key: str, sha: str, reason: str) -> None:
        with self._lock:
            self._data["held"][pr_key] = {"sha": sha, "reason": reason}
            self._save()

    def issues_synced_through(self, repo: str) -> str | None:
        with self._lock:
            return self._data["issues_synced"].get(repo)

    def mark_issues_synced(self, repo: str, closed_at: str) -> None:
        with self._lock:
            self._data["issues_synced"][repo] = closed_at
            self._save()

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
