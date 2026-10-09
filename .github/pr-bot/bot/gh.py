"""Minimal GitHub REST client (stdlib only) used by the PR review bot."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, url: str):
        super().__init__(f"GitHub {status} on {url}: {message}")
        self.status = status


class GitHub:
    def __init__(self, token: str | None = None, repo: str | None = None, dry_run: bool = False):
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "shospodarets/awesome-platform-engineering")
        self.dry_run = dry_run
        self.writes: list[dict] = []  # record of mutating calls (also used in dry-run)

    # -- transport -----------------------------------------------------------------
    def _request(self, method: str, path: str, body=None, accept: str = "application/vnd.github+json",
                 raw: bool = False, retries: int = 3, api_version: str = "2022-11-28"):
        url = path if path.startswith("http") else f"{API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": api_version,
            "User-Agent": "awesome-platform-engineering-pr-bot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = resp.read()
                    if raw:
                        return payload.decode("utf-8", "replace"), dict(resp.headers)
                    return (json.loads(payload) if payload else None), dict(resp.headers)
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:500]
                limited = e.code in (403, 429) and "rate limit" in msg.lower()
                if (e.code >= 500 or limited) and attempt < retries:
                    wait = min(60, 5 * (attempt + 1))
                    if limited:
                        reset = e.headers.get("x-ratelimit-reset")
                        after = e.headers.get("retry-after")
                        if after and after.isdigit():
                            wait = int(after)
                        elif reset and reset.isdigit():
                            wait = max(1, int(reset) - int(time.time()) + 1)
                        if wait > 600:
                            raise GitHubError(e.code, msg, url) from None
                    time.sleep(wait)
                    continue
                raise GitHubError(e.code, msg, url) from None
            except urllib.error.URLError:
                if attempt < retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        raise AssertionError("unreachable")

    def get(self, path: str, **params):
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        return self._request("GET", path)[0]

    def get_raw(self, path: str, accept: str) -> str:
        return self._request("GET", path, accept=accept, raw=True)[0]

    def paginate(self, path: str, max_pages: int = 10, accept: str = "application/vnd.github+json", **params):
        params.setdefault("per_page", 100)
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        out = []
        for _ in range(max_pages):
            data, headers = self._request("GET", url, accept=accept)
            out.extend(data if isinstance(data, list) else data.get("items", []))
            nxt = _next_link(headers.get("Link") or headers.get("link"))
            if not nxt:
                break
            url = nxt
        return out

    def count(self, path: str, **params) -> int:
        """Count items of a list endpoint with one request (per_page=1 + last page number)."""
        params["per_page"] = 1
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        try:
            data, headers = self._request("GET", url)
        except GitHubError as e:
            if e.status in (204, 404, 409):  # empty repo / no commits
                return 0
            raise
        last = _last_page(headers.get("Link") or headers.get("link"))
        if last:
            return last
        return len(data) if isinstance(data, list) else 0

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        data = self._request("POST", "/graphql", body={"query": query, "variables": variables or {}})[0] or {}
        return data.get("data") or {}

    def write(self, method: str, path: str, body=None):
        self.writes.append({"method": method, "path": path, "body": body})
        if self.dry_run:
            return None
        return self._request(method, path, body=body)[0]

    # -- repo helpers ----------------------------------------------------------------
    def r(self, suffix: str) -> str:
        return f"/repos/{self.repo}{suffix}"


def _next_link(link: str | None) -> str | None:
    if not link:
        return None
    for part in link.split(","):
        if 'rel="next"' in part:
            return part[part.find("<") + 1:part.find(">")]
    return None


def _last_page(link: str | None) -> int | None:
    if not link:
        return None
    for part in link.split(","):
        if 'rel="last"' in part:
            url = part[part.find("<") + 1:part.find(">")]
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "page" in q:
                return int(q["page"][0])
    return None
