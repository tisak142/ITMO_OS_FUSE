"""Isolated GitHub REST API adapter for FUSE GitHub v2.

This is the only module that performs HTTP. No shell git commands are used.
Prepared with LLM assistance for the university FUSE laboratory.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class GitHubAPIError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHubAPI:
    BASE_URL = "https://api.github.com"
    MAX_BLOB = 2 * 1024 * 1024  # Intentionally small, suitable for the lab.

    def __init__(self, username: str, timeout: int = 20):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", username):
            raise ValueError("Invalid GitHub username")
        self.username = username
        self.timeout = timeout
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.readme_cache: dict[str, bytes] = {}

    @staticmethod
    def _q(value: str, slash: bool = False) -> str:
        return quote(value, safe="/" if slash else "")

    def require_token(self):
        if not self.token:
            raise GitHubAPIError("GITHUB_TOKEN is required for GitHub writes", 403)

    def _request(self, method: str, path: str, params=None, payload=None,
                 accept: str = "application/vnd.github+json"):
        url = self.BASE_URL + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Accept": accept,
            "User-Agent": "FUSE-GitHub-OS-Lab/2.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
                if accept != "application/vnd.github+json":
                    return raw
                return json.loads(raw) if raw else None
        except HTTPError as error:
            # Never log Authorization headers or response bodies (can contain secrets).
            reasons = {
                401: "bad or expired token", 403: "access denied, rate limit, or branch protection",
                404: "not found or insufficient access", 409: "conflict",
                422: "validation failed or non-fast-forward update", 429: "rate limit",
            }
            raise GitHubAPIError(
                f"GitHub HTTP {error.code}: {reasons.get(error.code, 'request failed')}",
                error.code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise GitHubAPIError(f"Network error: {error}") from error
        except (ValueError, UnicodeError) as error:
            raise GitHubAPIError("Invalid GitHub API response") from error

    def _get(self, path, params=None):
        return self._request("GET", path, params)

    def _post(self, path, payload):
        self.require_token()
        return self._request("POST", path, payload=payload)

    def _patch(self, path, payload):
        self.require_token()
        return self._request("PATCH", path, payload=payload)

    def _repo(self, repo: str) -> str:
        return f"/repos/{self._q(self.username)}/{self._q(repo)}"

    def list_repositories(self, limit: int = 10) -> list[dict]:
        if not 1 <= limit <= 30:
            raise ValueError("limit must be between 1 and 30")
        try:
            result = self._get(f"/users/{self._q(self.username)}/repos", {
                "type": "owner", "sort": "updated", "direction": "desc",
                "per_page": limit, "page": 1,
            })
        except GitHubAPIError as exc:
            if exc.status == 404:
                raise GitHubAPIError(f"GitHub user '{self.username}' not found", 404) from exc
            raise
        if not isinstance(result, list):
            raise GitHubAPIError("Invalid repository list format")
        return result

    def repository(self, repo: str) -> dict:
        return self._get(self._repo(repo))

    def get_readme(self, repository: str) -> bytes:
        if repository in self.readme_cache:
            return self.readme_cache[repository]
        try:
            data = self._get(self._repo(repository) + "/readme")
        except GitHubAPIError as exc:
            if exc.status == 404:
                result = f"Repository {repository} has no README.\n".encode()
                self.readme_cache[repository] = result
                return result
            raise
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise GitHubAPIError("Unsupported README response")
        try:
            result = base64.b64decode(data["content"])
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise GitHubAPIError("Invalid README Base64") from exc
        self.readme_cache[repository] = result
        return result

    def branches(self, repo: str, page: int = 1, per_page: int = 100) -> list[dict]:
        return self._get(self._repo(repo) + "/branches", {"page": page, "per_page": per_page})

    def head(self, repo: str, branch: str) -> str:
        result = self._get(self._repo(repo) + "/git/ref/heads/" + self._q(branch, slash=True))
        return result["object"]["sha"]

    def commits(self, repo: str, branch: str, page: int = 1,
                per_page: int = 30) -> list[dict]:
        return self._get(self._repo(repo) + "/commits", {
            "sha": branch, "page": page, "per_page": per_page,
        })

    def commit_details(self, repo: str, sha: str, max_files: int = 3000) -> dict:
        # GitHub splits large commit file lists across several pages.
        path = self._repo(repo) + "/commits/" + self._q(sha)
        page = 1
        files = []
        first = None
        while len(files) < max_files:
            current = self._get(path, {"per_page": 100, "page": page})
            if not isinstance(current, dict):
                raise GitHubAPIError("Invalid commit detail response")
            if first is None:
                first = current
            batch = current.get("files", [])
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        first["files"] = files[:max_files]
        first["files_incomplete"] = len(files) >= max_files
        return first

    def commit_diff(self, repo: str, sha: str) -> bytes:
        return self._request("GET", self._repo(repo) + "/commits/" + self._q(sha),
                             accept="application/vnd.github.diff")

    def git_commit(self, repo: str, sha: str) -> dict:
        return self._get(self._repo(repo) + "/git/commits/" + self._q(sha))

    def tree(self, repo: str, sha: str, recursive: bool = True) -> dict:
        params = {"recursive": 1} if recursive else None
        return self._get(self._repo(repo) + "/git/trees/" + self._q(sha), params)

    def blob(self, repo: str, sha: str) -> bytes:
        result = self._get(self._repo(repo) + "/git/blobs/" + self._q(sha))
        if result.get("size", 0) > self.MAX_BLOB:
            raise GitHubAPIError("Blob exceeds 2 MiB lab limit", 413)
        if result.get("encoding") != "base64":
            raise GitHubAPIError("Unsupported Git blob encoding")
        try:
            content = base64.b64decode(result["content"])
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise GitHubAPIError("Invalid Git blob Base64") from exc
        if len(content) > self.MAX_BLOB:
            raise GitHubAPIError("Blob exceeds 2 MiB lab limit", 413)
        return content

    def merge_branch(self, repo: str, base: str, head: str,
                     message: str | None = None):
        """Merge published `head` into `base`; 204 returns None."""
        payload = {"base": base, "head": head}
        if message:
            payload["commit_message"] = message
        return self._post(self._repo(repo) + "/merges", payload)

    def delete_branch(self, repo: str, branch: str):
        """Delete refs/heads/<branch> (GitHub forbids default branch deletion)."""
        self.require_token()
        return self._request("DELETE", self._repo(repo) +
                             "/git/refs/heads/" + self._q(branch, slash=True))

    def create_branch(self, repo: str, branch: str, sha: str):
        return self._post(self._repo(repo) + "/git/refs", {
            "ref": "refs/heads/" + branch, "sha": sha,
        })

    def create_blob(self, repo: str, content: bytes) -> str:
        if len(content) > self.MAX_BLOB:
            raise GitHubAPIError("Blob exceeds 2 MiB lab limit", 413)
        result = self._post(self._repo(repo) + "/git/blobs", {
            "content": base64.b64encode(content).decode("ascii"), "encoding": "base64",
        })
        return result["sha"]

    def create_tree(self, repo: str, base_tree: str, entries: list[dict]) -> str:
        result = self._post(self._repo(repo) + "/git/trees", {
            "base_tree": base_tree, "tree": entries,
        })
        return result["sha"]

    def create_commit(self, repo: str, tree: str, parent: str, message: str) -> str:
        result = self._post(self._repo(repo) + "/git/commits", {
            "message": message, "tree": tree, "parents": [parent],
        })
        return result["sha"]

    def update_branch(self, repo: str, branch: str, sha: str):
        # NEVER force-push: GitHub enforces fast-forward semantics.
        return self._patch(self._repo(repo) + "/git/refs/heads/" + self._q(branch, slash=True), {
            "sha": sha, "force": False,
        })
