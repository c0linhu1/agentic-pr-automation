"""The Slack bot's only way to reach Vader: its HTTP API on localhost.
The bot has no GitHub token and no Ollama address; everything goes through Vader."""
import httpx


class VaderUnavailable(Exception):
    """Vader isn't running or can't be reached."""


class VaderError(Exception):
    """Vader refused the request; the message is safe to show to the user."""


class VaderClient:
    def __init__(self, url: str, api_key: str):
        # 60s: /reviews/pr waits while Vader fetches the PR from GitHub.
        self._http = httpx.Client(base_url=url.rstrip("/"), headers={"X-Vader-Key": api_key}, timeout=60)

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            r = self._http.request(method, path, **kwargs)
        except httpx.TransportError as e:
            raise VaderUnavailable(str(e)) from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise VaderError(str(detail))
        return r.json()

    def status(self) -> dict:
        return self._call("GET", "/status")

    def control(self, action: str) -> dict:
        return self._call("POST", "/control", json={"action": action})

    def automation(self, enabled: bool) -> dict:
        return self._call("POST", "/automation", json={"enabled": enabled})

    def models(self) -> dict:
        return self._call("GET", "/models")

    def switch_model(self, name: str) -> dict:
        return self._call("POST", "/model", json={"name": name})

    def submit(self, text: str, validators: list[str] | None, reply_to: dict) -> dict:
        return self._call("POST", "/reviews", json={"text": text, "validators": validators, "reply_to": reply_to})

    def submit_pr(self, ref: str, validators: list[str] | None, reply_to: dict) -> dict:
        return self._call("POST", "/reviews/pr", json={"ref": ref, "validators": validators, "reply_to": reply_to})

    def set_reply_to(self, review_id: str, reply_to: dict) -> None:
        self._call("POST", f"/reviews/{review_id}/reply-to", json={"reply_to": reply_to})

    def events(self, since: int) -> dict:
        return self._call("GET", "/events", params={"since": since})
