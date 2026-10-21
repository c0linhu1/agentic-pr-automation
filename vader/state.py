"""Vader's saved state, so a restart doesn't reset anything: on/off, automation,
the active model, and which PR commits have already been reviewed.
One JSON file, written atomically (temp file, then rename)."""
import json
import os
import threading
from pathlib import Path

DEFAULTS = {"enabled": True, "automation": False, "model": None, "reviewed": {}}


class State:
    def __init__(self, path: Path, default_model: str | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS, reviewed={})
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

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
