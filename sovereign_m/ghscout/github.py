"""Read-only GitHub client. GET only; no star, fork, issue, PR, comment or login. A token, if present in
an environment variable, is sent as a header and never printed, logged or put in an error."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from sovereign_m.ghscout.config import Settings

API = "https://api.github.com"
TOKEN_ENVS = ("M_GH_TOKEN", "GITHUB_TOKEN")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubError(RuntimeError):
    pass


def token_from_env() -> str | None:
    for k in TOKEN_ENVS:
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return None


class GitHubClient:
    """The only HTTP verb this class can issue is GET."""

    def __init__(self, token: str | None = None, min_interval: float | None = None, timeout: float = 30.0, sleep=time.sleep):
        self._token = token if token is not None else token_from_env()
        self.min_interval = (1.0 if self._token else 6.5) if min_interval is None else min_interval
        self.timeout = timeout
        self._sleep = sleep
        self._last = 0.0
        self.requests: list[str] = []     # URLs asked for (never headers)

    def __repr__(self) -> str:
        return f"GitHubClient(authenticated={bool(self._token)})"

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "[REDACTED]") if self._token else text

    def get(self, path: str, params: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0 and self._last:
            self._sleep(wait)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "m-gh-scout (read-only)", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(url, headers=headers, method="GET")
        self.requests.append(url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            hint = " (rate limited: unauthenticated search allows ~10 per minute)" if exc.code in (403, 429) else ""
            raise GitHubError(self._redact(f"GitHub HTTP {exc.code}{hint}")) from None
        except Exception as exc:  # noqa: BLE001
            raise GitHubError(self._redact(f"GitHub request failed: {type(exc).__name__}")) from None
        finally:
            self._last = time.monotonic()
        return data


def search_query(topic: str, s: Settings, now: datetime | None = None) -> str:
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=s.active_days)).strftime("%Y-%m-%d")
    return f"topic:{topic} stars:{s.min_stars}..{s.max_stars} archived:false fork:false pushed:>{since}"


def search_topic(client, topic: str, s: Settings, per_page: int = 15, now: datetime | None = None) -> list[dict]:
    data = client.get("/search/repositories", {"q": search_query(topic, s, now), "sort": "updated", "order": "desc", "per_page": per_page})
    return list(data.get("items") or [])


def recent_commit_count(client, full_name: str, s: Settings, now: datetime | None = None) -> int | None:
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=s.active_days)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        items = client.get(f"/repos/{full_name}/commits", {"since": since, "per_page": 30})
    except GitHubError:
        return None
    return len(items) if isinstance(items, list) else None


def days_since(iso: str | None, now: datetime | None = None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ((now or datetime.now(timezone.utc)) - t).total_seconds() / 86400


def screen(repo: dict, s: Settings, now: datetime | None = None) -> str | None:
    """Return a rejection reason, or None if the repo may be scouted. Re-checks everything the query asked for."""
    name = str(repo.get("full_name", ""))
    owner, _, rname = name.partition("/")
    if not (NAME.match(owner) and NAME.match(rname)):
        return "unsafe repo name"
    if repo.get("archived"):
        return "archived"
    if repo.get("fork"):
        return "fork"
    if repo.get("private") or repo.get("disabled"):
        return "not public"
    stars = repo.get("stargazers_count")
    if not isinstance(stars, int):
        return "stars UNVERIFIED"
    if stars > s.max_stars:
        return f"{stars} stars, over the {s.max_stars} limit"
    if stars < s.min_stars:
        return f"{stars} stars, under the {s.min_stars} floor"
    d = days_since(repo.get("pushed_at"), now)
    if d is None:
        return "last push UNVERIFIED"
    if d > s.active_days:
        return f"no push in {int(d)} days"
    if int(repo.get("size") or 0) > s.max_repo_kb:
        return f"repo is {int(repo['size']) // 1024} MB, too large to clone"
    return None


def rank_key(repo: dict, now: datetime | None = None) -> float:
    """Hidden gems first: recent pushes and few stars rank higher."""
    d = days_since(repo.get("pushed_at"), now)
    d = 90.0 if d is None else d
    return -((1 / (1 + d / 30.0)) * (1 / (1 + int(repo.get("stargazers_count") or 0) / 200.0)))
