#!/usr/bin/env python3
"""GitHub branches, commits and workspaces exposed through Linux FUSE.

GitHub HTTP stays isolated in github_api.py. Mutable workspace data is local
until an explicit mkdir under _commit; published history is immutable.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import errno
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import quote, unquote

import pyfuse3
import trio

from github_api import GitHubAPI, GitHubAPIError
from workspace_store import Draft, WorkspaceStore

PAGE_SIZE = 30
MAX_BRANCHES = 500
MAX_HISTORY_PAGE = 1000
MAX_TREE_ENTRIES = 4000
TTL_BRANCHES = 30
TTL_HEAD = 8


def encode_branch(name: str) -> str:
    """Reversible one-component encoding: feature/login -> feature%2Flogin."""
    return quote(name, safe="-._~")


def decode_branch(name: str) -> str:
    decoded = unquote(name, errors="strict")
    if encode_branch(decoded) != name:
        raise ValueError("Branch must be in canonical percent encoding")
    return decoded


def validate_branch(name: str) -> bool:
    # Common `git check-ref-format --branch` constraints. GitHub is final judge.
    if not name or name.startswith("-") or name.endswith(("/", ".", ".lock")):
        return False
    if name.startswith("/") or "//" in name or ".." in name or "@{" in name:
        return False
    if any(s in name for s in ("\\", "?", "*", "[", " ", "~", "^", ":")):
        return False
    if any(ord(c) <= 32 or ord(c) == 127 for c in name):
        return False
    if any(part in ("", ".", "..") or part.startswith(".") or part.endswith(".lock")
           for part in name.split("/")):
        return False
    return True


@dataclass
class Node:
    inode: int
    parent: int
    name: bytes
    kind: str
    is_dir: bool
    repo: str = ""
    branch: str = ""
    sha: str = ""
    path: str = ""
    data: bytes | None = None
    size: int = 0
    blob_sha: str = ""
    file_mode: str = "100644"
    loaded: bool = False
    meta: dict = field(default_factory=dict)
    stamp_ns: int = field(default_factory=time.time_ns)


@dataclass
class BranchContext:
    draft: Draft
    lock: trio.Lock = field(default_factory=trio.Lock)
    loaded: bool = False
    base_files: dict[str, dict] = field(default_factory=dict)


class GitHubFS(pyfuse3.Operations):
    # Bypasses the kernel writeback cache, so each write is persisted to overlay.
    enable_writeback_cache = False

    def __init__(self, client: GitHubAPI, repositories: list[dict], store: WorkspaceStore):
        super().__init__()
        self.client = client
        self.store = store
        self.nodes: dict[int, Node] = {}
        self.children: dict[int, dict[bytes, int]] = {}
        self.next_inode = pyfuse3.ROOT_INODE + 1
        self.handles: dict[int, tuple[int, int]] = {}
        self.next_handle = 100
        self.branch_contexts: dict[tuple[str, str], BranchContext] = {}
        self.head_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self.repo_details: dict[str, dict] = {}
        self.branch_list_time: dict[str, float] = {}

        root = Node(pyfuse3.ROOT_INODE, pyfuse3.ROOT_INODE, b"", "root", True)
        self.nodes[root.inode] = root
        self.children[root.inode] = {}
        help_text = (
            f"FUSE GitHub v2 for @{client.username}\n"
            "Tree: <repo>/branches/<encoded-branch>/{HEAD.txt,commits,workspace,_commit}.\n"
            "Branch with /: feature/login becomes feature%2Flogin.\n"
            "mkdir branches/<new-name> creates a branch at default HEAD.\n"
            "Edit workspace/ locally; mkdir _commit/<unique-message> publishes one commit.\n"
            "No automatic merge/rebase, no force push, no history edits.\n"
            "Each commit's changed/ includes the new versions of changed files only.\n"
            "More history: commits/pages/2, commits/pages/3, ... (30 per page).\n"
            "GITHUB_TOKEN with Contents:write is needed for all write operations.\n"
            "Drafts persist outside mount. To refresh stale workspace, remount after resolving\n"
            "draft changes manually; never delete state before backing it up.\n"
        ).encode()
        self._add(root.inode, "HELP.txt", "static", False, data=help_text)
        for repo in repositories:
            name = repo.get("name")
            if not isinstance(name, str) or not name or "/" in name or "\0" in name:
                continue
            self.repo_details[name] = repo
            r = self._add(root.inode, name, "repository", True, repo=name)
            info = (
                f"Repository: {name}\n"
                f"Description: {repo.get('description') or '(none)'}\n"
                f"Language: {repo.get('language') or '(unknown)'}\n"
                f"Stars: {repo.get('stargazers_count', 0)}\n"
                f"Forks: {repo.get('forks_count', 0)}\n"
                f"Default branch: {repo.get('default_branch') or '(unknown)'}\n"
                f"Updated: {repo.get('updated_at') or '(unknown)'}\n"
                f"URL: {repo.get('html_url') or '(unknown)'}\n"
            ).encode("utf-8")
            self._add(r, "info.txt", "static", False, repo=name, data=info)
            self._add(r, "README.md", "readme", False, repo=name)
            self._add(r, "branches", "branches", True, repo=name)

    def _add(self, parent: int, name: str, kind: str, is_dir: bool, **kw) -> int:
        bname = name.encode("utf-8")
        if bname in self.children[parent]:
            return self.children[parent][bname]
        inode = self.next_inode
        self.next_inode += 1
        node = Node(inode, parent, bname, kind, is_dir, **kw)
        if node.data is not None:
            node.size = len(node.data)
        self.nodes[inode] = node
        self.children[parent][bname] = inode
        if is_dir:
            self.children[inode] = {}
        return inode

    def _remove(self, parent: int, name: bytes):
        # Preserve orphan inode for open handles and kernel lookup references.
        self.children[parent].pop(name, None)

    def _node(self, inode: int) -> Node:
        node = self.nodes.get(inode)
        if node is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return node

    @staticmethod
    def _err(exc: Exception):
        if isinstance(exc, pyfuse3.FUSEError):
            raise exc
        if isinstance(exc, GitHubAPIError):
            print(f"GitHub API: {exc}", file=sys.stderr, flush=True)
            codes = {401: errno.EACCES, 403: errno.EACCES,
                     404: errno.ENOENT, 409: errno.EBUSY,
                     413: errno.EFBIG, 422: errno.EINVAL,
                     429: errno.EAGAIN}
            raise pyfuse3.FUSEError(codes.get(exc.status, errno.EIO)) from exc
        print(f"FUSE internal error: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        raise pyfuse3.FUSEError(errno.EIO) from exc

    async def _api(self, callback, *args, **kwargs):
        try:
            # to_thread accepts positional only; use closure to pass kwargs.
            return await trio.to_thread.run_sync(lambda: callback(*args, **kwargs))
        except GitHubAPIError as exc:
            self._err(exc)

    def _attrs(self, node: Node):
        attr = pyfuse3.EntryAttributes()
        attr.st_ino = node.inode
        attr.generation = 0
        mutable = node.kind.startswith("ws_") or node.kind in (
            "workspace", "branch", "branches", "commits", "commit_trigger", "head_file")
        attr.entry_timeout = 0.5 if mutable else 30.0
        attr.attr_timeout = 0.5 if mutable else 30.0
        if node.is_dir:
            writable = node.kind in ("branches", "workspace", "ws_dir", "commit_trigger")
            attr.st_mode = stat.S_IFDIR | (0o755 if writable else 0o555)
            attr.st_nlink = 2 + sum(
                self.nodes[child].is_dir for child in self.children[node.inode].values())
            attr.st_size = 0
        else:
            writable = node.kind == "ws_file"
            perm = 0o644 if writable else 0o444
            if writable and node.file_mode == "100755":
                perm = 0o755
            attr.st_mode = stat.S_IFREG | perm
            attr.st_nlink = 1
            attr.st_size = node.size if node.data is None else len(node.data)
        attr.st_uid = os.getuid()
        attr.st_gid = os.getgid()
        attr.st_rdev = 0
        attr.st_blksize = 4096
        attr.st_blocks = (attr.st_size + 511) // 512
        attr.st_atime_ns = node.stamp_ns
        attr.st_mtime_ns = node.stamp_ns
        attr.st_ctime_ns = node.stamp_ns
        return attr

    async def getattr(self, inode, ctx=None):
        return self._attrs(self._node(inode))

    @staticmethod
    def _name(name: bytes) -> str:
        try:
            decoded = name.decode("utf-8", "strict")
        except UnicodeError:
            raise pyfuse3.FUSEError(errno.EINVAL)
        if decoded in ("", ".", "..") or "/" in decoded or "\0" in decoded:
            raise pyfuse3.FUSEError(errno.EINVAL)
        if len(name) > 255:
            raise pyfuse3.FUSEError(errno.ENAMETOOLONG)
        return decoded

    def _ctx_key(self, node: Node) -> tuple[str, str]:
        return node.repo, node.branch

    async def _head(self, repo: str, branch: str, fresh: bool = False) -> str:
        key = (repo, branch)
        cached = self.head_cache.get(key)
        if not fresh and cached and time.monotonic() - cached[1] < TTL_HEAD:
            return cached[0]
        sha = await self._api(self.client.head, repo, branch)
        self.head_cache[key] = (sha, time.monotonic())
        return sha

    async def _ensure_branches(self, node: Node):
        now = time.monotonic()
        if node.loaded and now - self.branch_list_time.get(node.repo, 0) < TTL_BRANCHES:
            return
        for page in range(1, (MAX_BRANCHES // 100) + 1):
            batch = await self._api(self.client.branches, node.repo, page, 100)
            for branch in batch:
                if isinstance(branch.get("name"), str):
                    self._add(node.inode, encode_branch(branch["name"]), "branch", True,
                              repo=node.repo, branch=branch["name"])
            if len(batch) < 100:
                break
        else:
            self._add(node.inode, "BRANCHES_LIMIT.txt", "static", False,
                      data=b"Only the first 500 branches are shown in this lab.\n")
        node.loaded = True
        self.branch_list_time[node.repo] = now

    async def _ensure_branch(self, node: Node):
        if node.loaded:
            return
        sha = await self._head(node.repo, node.branch)
        self._add(node.inode, "HEAD.txt", "head_file", False,
                  repo=node.repo, branch=node.branch, data=(sha + "\n").encode())
        self._add(node.inode, "commits", "commits", True, repo=node.repo, branch=node.branch)
        self._add(node.inode, "workspace", "workspace", True, repo=node.repo, branch=node.branch)
        self._add(node.inode, "_commit", "commit_trigger", True,
                  repo=node.repo, branch=node.branch)
        node.loaded = True

    async def _load_page(self, node: Node, page: int, parent: int):
        batch = await self._api(self.client.commits, node.repo, node.branch, page, PAGE_SIZE)
        for commit in batch:
            sha = commit.get("sha", "")
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                self._add(parent, sha, "commit", True,
                          repo=node.repo, branch=node.branch, sha=sha)
        return len(batch) == PAGE_SIZE

    async def _ensure_commits(self, node: Node):
        if node.loaded:
            return
        pages = self._add(node.inode, "pages", "page_index", True,
                          repo=node.repo, branch=node.branch)
        has_more = await self._load_page(node, 1, node.inode)
        # Share one stable snapshot of page 1 with the flat commits/ view.
        # Previously a second GitHub request could return a different list.
        first_shas = [name.decode("ascii") for name, inode in self.children[node.inode].items()
                      if self.nodes[inode].kind == "commit"]
        first_page = self._add(pages, "1", "page", True,
                               repo=node.repo, branch=node.branch, path="1")
        self.nodes[first_page].meta["cached_shas"] = first_shas
        if has_more:
            self._add(pages, "2", "page", True, repo=node.repo, branch=node.branch, path="2")
        node.loaded = True

    async def _ensure_page(self, node: Node):
        if node.loaded:
            return
        page = int(node.path)
        if page == 1 and "cached_shas" in node.meta:
            for sha in node.meta["cached_shas"]:
                self._add(node.inode, sha, "commit", True,
                          repo=node.repo, branch=node.branch, sha=sha)
            node.loaded = True
            return
        has_more = await self._load_page(node, page, node.inode)
        if has_more:
            self._add(node.parent, str(page + 1), "page", True,
                      repo=node.repo, branch=node.branch, path=str(page + 1))
        node.loaded = True

    async def _ensure_commit(self, node: Node):
        if node.loaded:
            return
        detail = await self._api(self.client.commit_details, node.repo, node.sha)
        parents = [p.get("sha", "") for p in detail.get("parents", [])]
        metadata = detail.get("commit") or {}
        author = metadata.get("author") or {}
        info = (
            f"SHA: {node.sha}\n"
            f"Author: {author.get('name', '?')} <{author.get('email', '?')}>\n"
            f"Date: {author.get('date', '?')}\n"
            f"Message: {metadata.get('message', '')}\n"
            f"Parents: {', '.join(parents) if parents else '(root commit)'}\n"
            "Comparison: first parent for merges; GitHub API commit file list.\n"
        ).encode("utf-8")
        files = detail.get("files", [])
        changes = [
            f"{f.get('status', '?').upper()} {f.get('filename', '?')}"
            + (f" (from {f.get('previous_filename')})" if f.get("previous_filename") else "")
            for f in files
        ]
        if detail.get("files_incomplete"):
            changes.append("WARNING: GitHub API file list truncated at 3000 files")
        self._add(node.inode, "INFO.txt", "static", False, data=info)
        self._add(node.inode, "CHANGES.txt", "static", False,
                  data=(("\n".join(changes) or "(no changed files)") + "\n").encode("utf-8"))
        self._add(node.inode, "diff.patch", "diff_file", False,
                  repo=node.repo, sha=node.sha)
        changed = self._add(node.inode, "changed", "changed", True,
                            repo=node.repo, branch=node.branch, sha=node.sha,
                            meta={"tree_sha": (metadata.get("tree") or {}).get("sha", "")})
        for f in files:
            if f.get("status") == "removed":
                continue
            if f.get("status") not in ("added", "modified", "renamed", "copied", "changed"):
                continue
            path = f.get("filename")
            if isinstance(path, str) and path:
                self._insert_file(changed, path, "changed_file", node.repo, node.branch,
                                  node.sha, f.get("sha") or "", f.get("size", 0))
        node.loaded = True

    async def _ensure_changed_sizes(self, node: Node):
        """Populate file sizes in bulk, without downloading every blob.

        GitHub's commit `files` records do not have a `size` field. A recursive
        Git tree contains blob sizes, at the cost of one additional API call
        per accessed changed/ directory. A tree lookup failure must not make
        historical file names or contents inaccessible.
        """
        if node.loaded:
            return
        node.loaded = True
        tree_sha = node.meta.get("tree_sha", "")
        if not tree_sha:
            return
        try:
            tree = await self._api(self.client.tree, node.repo, tree_sha, True)
        except pyfuse3.FUSEError as exc:
            print(f"WARNING: sizes unavailable for {node.repo}/{node.sha}: "
                  f"Git tree lookup failed ({exc}); displaying unknown sizes as 0",
                  file=sys.stderr, flush=True)
            return
        if not isinstance(tree, dict):
            print(f"WARNING: invalid Git tree for {node.repo}/{node.sha}",
                  file=sys.stderr, flush=True)
            return
        if tree.get("truncated"):
            print(f"WARNING: Git tree truncated for {node.repo}/{node.sha}; "
                  "some historical file sizes may remain unknown",
                  file=sys.stderr, flush=True)
        sizes = {item.get("path"): item.get("size") for item in tree.get("tree", [])
                 if item.get("type") == "blob" and isinstance(item.get("size"), int)}
        pending = [node.inode]
        while pending:
            for child_inode in self.children.get(pending.pop(), {}).values():
                child = self.nodes[child_inode]
                if child.is_dir:
                    pending.append(child_inode)
                elif child.kind == "changed_file" and child.path in sizes:
                    child.size = sizes[child.path]

    def _insert_file(self, root_inode: int, path: str, kind: str, repo: str,
                     branch: str, sha: str, blob_sha: str, size: int,
                     file_mode: str = "100644") -> int:
        components = path.split("/")
        cur = root_inode
        for i, part in enumerate(components[:-1]):
            cur = self._add(cur, part, "ws_dir" if kind.startswith("ws_") else "changed_dir",
                            True, repo=repo, branch=branch,
                            path="/".join(components[:i+1]))
        name = components[-1]
        return self._add(cur, name, kind, False, repo=repo, branch=branch,
                         sha=sha, path=path, blob_sha=blob_sha, size=size,
                         file_mode=file_mode)

    async def _branch_context(self, node: Node) -> BranchContext:
        key = self._ctx_key(node)
        if key not in self.branch_contexts:
            head = await self._head(node.repo, node.branch, fresh=True)
            try:
                draft = self.store.load(self.client.username, node.repo, node.branch, head)
            except (OSError, ValueError, KeyError) as exc:
                self._err(exc)
            self.branch_contexts[key] = BranchContext(draft)
        return self.branch_contexts[key]

    def _save(self, node: Node, ctx: BranchContext):
        try:
            self.store.save(self.client.username, node.repo, node.branch, ctx.draft)
        except OSError as exc:
            self._err(exc)

    async def _ensure_workspace(self, node: Node):
        ctx = await self._branch_context(node)
        if ctx.loaded:
            return
        git_commit = await self._api(self.client.git_commit, node.repo, ctx.draft.base_sha)
        tree_sha = git_commit["tree"]["sha"]
        tree = await self._api(self.client.tree, node.repo, tree_sha, True)
        if tree.get("truncated") or len(tree.get("tree", [])) > MAX_TREE_ENTRIES:
            raise pyfuse3.FUSEError(errno.EFBIG)
        for item in tree.get("tree", []):
            path = item.get("path", "")
            if not path or any(p in ("", ".", "..") for p in path.split("/")):
                continue
            if item.get("type") == "tree":
                self._insert_dir(node.inode, path, node.repo, node.branch)
            elif item.get("type") == "blob" and item.get("mode") in ("100644", "100755"):
                ctx.base_files[path] = item
                self._insert_file(node.inode, path, "ws_file", node.repo, node.branch,
                                  "", item.get("sha", ""), item.get("size") or 0,
                                  item.get("mode", "100644"))
            else:
                # Do not silently represent symlinks or gitlinks as ordinary editable files.
                info = f"Unsupported Git object: {path}; mode={item.get('mode')}, type={item.get('type')}\n".encode()
                inode = self._insert_file(node.inode, path, "unsupported", node.repo,
                                          node.branch, "", "", len(info))
                self.nodes[inode].data = info
        for path in sorted(ctx.draft.dirs, key=lambda s: (s.count("/"), s)):
            self._insert_dir(node.inode, path, node.repo, node.branch)
        for path, data in ctx.draft.edits.items():
            if data is None:
                self._remove_by_path(node.inode, path)
            else:
                inode = self._insert_file(node.inode, path, "ws_file", node.repo,
                                          node.branch, "", "", len(data),
                                          ctx.draft.modes.get(path, "100644"))
                obj = self.nodes[inode]
                obj.kind, obj.data, obj.size = "ws_file", data, len(data)
                obj.blob_sha = ""
        ctx.loaded = True
        node.loaded = True

    def _insert_dir(self, root: int, path: str, repo: str, branch: str) -> int:
        cur = root
        components = path.split("/")
        for idx, part in enumerate(components):
            cur = self._add(cur, part, "ws_dir", True, repo=repo, branch=branch,
                            path="/".join(components[:idx+1]))
        return cur

    def _remove_by_path(self, root: int, path: str):
        cur = root
        for part in path.split("/")[:-1]:
            cur = self.children.get(cur, {}).get(part.encode("utf-8"))
            if cur is None:
                return
        self._remove(cur, path.split("/")[-1].encode("utf-8"))

    async def _ensure_receipts(self, node: Node):
        ctx = await self._branch_context(node)
        for label, sha in ctx.draft.receipts.items():
            self._make_receipt(node.inode, label, node.repo, node.branch, sha)

    def _make_receipt(self, parent: int, label: str, repo: str, branch: str, sha: str):
        receipt = self._add(parent, label, "receipt", True, repo=repo, branch=branch, sha=sha)
        self._add(receipt, "SHA.txt", "static", False, data=(sha + "\n").encode())
        return receipt

    async def _ensure(self, node: Node):
        if node.kind == "branches":
            await self._ensure_branches(node)
        elif node.kind == "branch":
            await self._ensure_branch(node)
        elif node.kind == "commits":
            await self._ensure_commits(node)
        elif node.kind == "page":
            await self._ensure_page(node)
        elif node.kind == "commit":
            try:
                await self._ensure_commit(node)
            except Exception as exc:
                # Unexpected errors used to surface as an unexplained EIO.
                self._err(exc)
        elif node.kind == "changed":
            await self._ensure_changed_sizes(node)
        elif node.kind == "workspace":
            await self._ensure_workspace(node)
        elif node.kind == "commit_trigger":
            await self._ensure_receipts(node)

    async def lookup(self, parent_inode, name, ctx=None):
        parent = self._node(parent_inode)
        if not parent.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if name == b".":
            return self._attrs(parent)
        if name == b"..":
            return self._attrs(self._node(parent.parent))
        await self._ensure(parent)
        inode = self.children[parent_inode].get(name)
        # A nonexistent history page must be ENOENT, not an empty directory.
        # Next page numbers are added only after the previous page is loaded.
        if inode is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attrs(self._node(inode))

    async def opendir(self, inode, ctx=None):
        node = self._node(inode)
        if not node.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        await self._ensure(node)
        return inode

    async def readdir(self, fh, start_id, token):
        parent = self._node(fh)
        await self._ensure(parent)
        entries = list(self.children[fh].items())
        for pos, (name, inode) in enumerate(entries, 1):
            if pos <= start_id:
                continue
            if not pyfuse3.readdir_reply(token, name, self._attrs(self.nodes[inode]), pos):
                break

    async def releasedir(self, fh):
        return None

    async def fsyncdir(self, fh, datasync):
        self._node(fh)

    async def access(self, inode, mode, ctx=None):
        node = self._node(inode)
        if mode & os.W_OK:
            if not self.client.token:
                return False
            if node.kind not in ("branches", "workspace", "ws_dir",
                                 "ws_file", "commit_trigger"):
                return False
        return True

    async def _data(self, node: Node, refresh: bool = False) -> bytes:
        if node.data is not None and not refresh:
            return node.data
        if node.kind == "readme":
            try:
                data = await trio.to_thread.run_sync(lambda: self.client.get_readme(node.repo))
            except GitHubAPIError as exc:
                print(f"README temporarily unavailable ({node.repo}): {exc}",
                      file=sys.stderr, flush=True)
                data = f"README temporarily unavailable for {node.repo}: {exc}\n".encode()
        elif node.kind == "head_file":
            data = ((await self._head(node.repo, node.branch)) + "\n").encode()
        elif node.kind in ("changed_file", "ws_file") and node.blob_sha:
            data = await self._api(self.client.blob, node.repo, node.blob_sha)
        elif node.kind == "diff_file":
            try:
                data = await trio.to_thread.run_sync(lambda: self.client.commit_diff(node.repo, node.sha))
            except GitHubAPIError as exc:
                # Diff is informational: preserve other immutable commit data.
                data = (f"Diff unavailable from GitHub API: {exc}\n"
                        "Binary/large diffs may not be available.\n").encode()
        else:
            data = node.data or b""
        node.data = data
        node.size = len(data)
        return data

    def _new_handle(self, node: Node, flags: int) -> pyfuse3.FileInfo:
        fh = self.next_handle
        self.next_handle += 1
        self.handles[fh] = (node.inode, flags)
        return pyfuse3.FileInfo(fh=fh, direct_io=True, keep_cache=False)

    def _handle(self, fh: int) -> tuple[Node, int]:
        entry = self.handles.get(fh)
        if entry is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        return self._node(entry[0]), entry[1]

    def _allow_write(self):
        if not self.client.token:
            raise pyfuse3.FUSEError(errno.EACCES)

    async def open(self, inode, flags, ctx=None):
        node = self._node(inode)
        if node.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)
        access = flags & os.O_ACCMODE
        if access != os.O_RDONLY and node.kind != "ws_file":
            raise pyfuse3.FUSEError(errno.EROFS)
        if node.kind in ("unsupported",) and access != os.O_RDONLY:
            raise pyfuse3.FUSEError(errno.EROFS)
        if access != os.O_RDONLY:
            self._allow_write()
        if flags & os.O_TRUNC:
            if node.kind != "ws_file":
                raise pyfuse3.FUSEError(errno.EROFS)
            if access == os.O_RDONLY:
                raise pyfuse3.FUSEError(errno.EACCES)
            self._allow_write()
            await self._replace(node, b"")
        elif node.kind == "ws_file" and node.data is None:
            await self._data(node)
        return self._new_handle(node, flags)

    async def read(self, fh, off, size):
        node, flags = self._handle(fh)
        if flags & os.O_ACCMODE == os.O_WRONLY:
            raise pyfuse3.FUSEError(errno.EBADF)
        data = await self._data(node, refresh=node.kind == "head_file")
        return data[off:off + size]

    def _stage_unlocked(self, node: Node, branch_ctx: BranchContext, data: bytes):
        if len(data) > self.client.MAX_BLOB:
            raise pyfuse3.FUSEError(errno.EFBIG)
        draft = branch_ctx.draft
        old_edits = draft.edits.get(node.path, ...)
        old_modes = draft.modes.get(node.path, ...)
        draft.edits[node.path] = data
        draft.modes[node.path] = node.file_mode
        try:
            self._save(node, branch_ctx)
        except pyfuse3.FUSEError:
            if old_edits is ...:
                draft.edits.pop(node.path, None)
            else:
                draft.edits[node.path] = old_edits
            if old_modes is ...:
                draft.modes.pop(node.path, None)
            else:
                draft.modes[node.path] = old_modes
            raise
        node.data = data
        node.size = len(data)
        node.blob_sha = ""
        node.stamp_ns = time.time_ns()

    async def _replace(self, node: Node, data: bytes):
        branch_ctx = await self._branch_context(node)
        async with branch_ctx.lock:
            self._stage_unlocked(node, branch_ctx, data)

    async def write(self, fh, off, buf):
        node, flags = self._handle(fh)
        if node.kind != "ws_file" or flags & os.O_ACCMODE == os.O_RDONLY:
            raise pyfuse3.FUSEError(errno.EROFS)
        self._allow_write()
        branch_ctx = await self._branch_context(node)
        async with branch_ctx.lock:
            previous = await self._data(node)
            if flags & os.O_APPEND:
                off = len(previous)
            if off < 0 or off + len(buf) > self.client.MAX_BLOB:
                raise pyfuse3.FUSEError(errno.EFBIG)
            value = previous[:off].ljust(off, b"\0") + buf
            if off + len(buf) < len(previous):
                value += previous[off + len(buf):]
            self._stage_unlocked(node, branch_ctx, value)
        return len(buf)

    async def create(self, parent_inode, name, mode, flags, ctx=None):
        parent = self._node(parent_inode)
        self._allow_write()
        if parent.kind not in ("workspace", "ws_dir"):
            raise pyfuse3.FUSEError(errno.EROFS)
        filename = self._name(name)
        await self._ensure(parent)
        if name in self.children[parent_inode]:
            raise pyfuse3.FUSEError(errno.EEXIST)
        path = "/".join(part for part in (parent.path, filename) if part)
        branch_ctx = await self._branch_context(parent)
        async with branch_ctx.lock:
            branch_ctx.draft.edits[path] = b""
            file_mode = "100755" if mode & 0o111 else "100644"
            branch_ctx.draft.modes[path] = file_mode
            self._save(parent, branch_ctx)
            inode = self._add(parent_inode, filename, "ws_file", False,
                              repo=parent.repo, branch=parent.branch, path=path,
                              data=b"", file_mode=file_mode)
        node = self._node(inode)
        return self._new_handle(node, flags), self._attrs(node)

    async def setattr(self, inode, attr, fields, fh=None, ctx=None):
        node = self._node(inode)
        if node.kind != "ws_file":
            if fields.update_size or fields.update_mode:
                raise pyfuse3.FUSEError(errno.EROFS)
            return self._attrs(node)
        self._allow_write()
        if fields.update_size:
            size = attr.st_size
            if size < 0 or size > self.client.MAX_BLOB:
                raise pyfuse3.FUSEError(errno.EFBIG)
            current = await self._data(node)
            await self._replace(node, current[:size].ljust(size, b"\0"))
        if fields.update_mode:
            value = "100755" if attr.st_mode & 0o111 else "100644"
            node.file_mode = value
            ctx_branch = await self._branch_context(node)
            async with ctx_branch.lock:
                ctx_branch.draft.edits[node.path] = await self._data(node)
                ctx_branch.draft.modes[node.path] = value
                self._save(node, ctx_branch)
        return self._attrs(node)

    async def unlink(self, parent_inode, name, ctx=None):
        parent = self._node(parent_inode)
        self._allow_write()
        if parent.kind not in ("workspace", "ws_dir"):
            raise pyfuse3.FUSEError(errno.EROFS)
        await self._ensure(parent)
        inode = self.children[parent_inode].get(name)
        if inode is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        node = self._node(inode)
        if node.is_dir:
            raise pyfuse3.FUSEError(errno.EISDIR)
        if node.kind != "ws_file":
            raise pyfuse3.FUSEError(errno.EROFS)
        branch_ctx = await self._branch_context(parent)
        async with branch_ctx.lock:
            if node.path in branch_ctx.base_files:
                branch_ctx.draft.edits[node.path] = None
            else:
                branch_ctx.draft.edits.pop(node.path, None)
            branch_ctx.draft.modes.pop(node.path, None)
            self._save(parent, branch_ctx)
            self._remove(parent_inode, name)

    async def rmdir(self, parent_inode, name, ctx=None):
        parent = self._node(parent_inode)
        self._allow_write()
        if parent.kind not in ("workspace", "ws_dir"):
            raise pyfuse3.FUSEError(errno.EROFS)
        await self._ensure(parent)
        inode = self.children[parent_inode].get(name)
        if inode is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        node = self._node(inode)
        if not node.is_dir:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if self.children[inode]:
            raise pyfuse3.FUSEError(errno.ENOTEMPTY)
        branch_ctx = await self._branch_context(parent)
        async with branch_ctx.lock:
            branch_ctx.draft.dirs.discard(node.path)
            self._save(parent, branch_ctx)
            self._remove(parent_inode, name)

    async def _publish(self, parent: Node, label: str) -> str:
        # This function executes under the per-branch Trio lock.
        branch_ctx = await self._branch_context(parent)
        ws_inode = self.children[self._node(parent.parent).inode][b"workspace"]
        await self._ensure_workspace(self._node(ws_inode))
        draft = branch_ctx.draft
        head = await self._head(parent.repo, parent.branch, fresh=True)
        if head != draft.base_sha:
            print("Commit refused: remote HEAD changed. Draft preserved. "
                  "Resolve changes manually and remount safely.", file=sys.stderr)
            raise pyfuse3.FUSEError(errno.ESTALE)
        old_commit = await self._api(self.client.git_commit, parent.repo, head)
        base_tree = old_commit["tree"]["sha"]
        operations = []
        for path, data in sorted(draft.edits.items()):
            if data is None:
                if path not in branch_ctx.base_files:
                    continue
                operations.append({"path": path, "mode": branch_ctx.base_files[path]["mode"],
                                   "type": "blob", "sha": None})
            else:
                blob_sha = await self._api(self.client.create_blob, parent.repo, data)
                operations.append({"path": path, "mode": draft.modes.get(
                    path, branch_ctx.base_files.get(path, {}).get("mode", "100644")),
                    "type": "blob", "sha": blob_sha})
        tree = (await self._api(self.client.create_tree, parent.repo, base_tree, operations)
                if operations else base_tree)
        sha = await self._api(self.client.create_commit, parent.repo, tree, head, label)
        # Recheck before moving ref; force=false also prevents a race with a new remote commit.
        if await self._head(parent.repo, parent.branch, fresh=True) != head:
            raise pyfuse3.FUSEError(errno.ESTALE)
        try:
            await self._api(self.client.update_branch, parent.repo, parent.branch, sha)
        except pyfuse3.FUSEError as exc:
            if exc.errno in (errno.EINVAL, errno.EBUSY):
                print("Commit not published: reference update rejected (possibly "
                      "stale HEAD or GitHub ruleset). Draft preserved.", file=sys.stderr)
                raise pyfuse3.FUSEError(errno.ESTALE) from exc
            raise
        self.head_cache[(parent.repo, parent.branch)] = (sha, time.monotonic())
        # Remote ref update SUCCEEDED; errors saving local metadata must NOT masquerade
        # as an unpublished commit. Record success in memory and warn if disk fails.
        for path, data in draft.edits.items():
            if data is None:
                branch_ctx.base_files.pop(path, None)
            else:
                branch_ctx.base_files[path] = {"path": path, "mode": draft.modes.get(path, "100644"),
                                                "type": "blob", "size": len(data)}
        draft.base_sha = sha
        draft.edits.clear()
        draft.modes.clear()
        draft.receipts[label] = sha
        try:
            self.store.save(self.client.username, parent.repo, parent.branch, draft)
        except OSError as exc:
            print(f"WARNING: remote commit {sha} published, but local receipt save failed: {exc}",
                  file=sys.stderr, flush=True)
        branch_inode = parent.parent
        head_inode = self.children[branch_inode].get(b"HEAD.txt")
        if head_inode is not None:
            self.nodes[head_inode].data = (sha + "\n").encode()
            self.nodes[head_inode].size = len(sha) + 1
        commits_inode = self.children[branch_inode].get(b"commits")
        if commits_inode is not None:
            self._add(commits_inode, sha, "commit", True,
                      repo=parent.repo, branch=parent.branch, sha=sha)
        return sha

    async def mkdir(self, parent_inode, name, mode, ctx=None):
        parent = self._node(parent_inode)
        self._allow_write()
        component = self._name(name)
        await self._ensure(parent)
        if name in self.children[parent_inode]:
            raise pyfuse3.FUSEError(errno.EEXIST)
        if parent.kind == "branches":
            try:
                branch = decode_branch(component)
            except ValueError:
                raise pyfuse3.FUSEError(errno.EINVAL)
            if not validate_branch(branch):
                raise pyfuse3.FUSEError(errno.EINVAL)
            default_branch = self.repo_details[parent.repo].get("default_branch")
            if not default_branch:
                raise pyfuse3.FUSEError(errno.EINVAL)
            base = await self._head(parent.repo, default_branch, fresh=True)
            await self._api(self.client.create_branch, parent.repo, branch, base)
            self.head_cache[(parent.repo, branch)] = (base, time.monotonic())
            inode = self._add(parent_inode, component, "branch", True,
                              repo=parent.repo, branch=branch)
        elif parent.kind in ("workspace", "ws_dir"):
            path = "/".join(part for part in (parent.path, component) if part)
            branch_ctx = await self._branch_context(parent)
            async with branch_ctx.lock:
                branch_ctx.draft.dirs.add(path)
                self._save(parent, branch_ctx)
                inode = self._add(parent_inode, component, "ws_dir", True,
                                  repo=parent.repo, branch=parent.branch, path=path)
        elif parent.kind == "commit_trigger":
            if not 1 <= len(component) <= 80 or component.startswith("."):
                raise pyfuse3.FUSEError(errno.EINVAL)
            branch_ctx = await self._branch_context(parent)
            async with branch_ctx.lock:
                if component in branch_ctx.draft.receipts:
                    raise pyfuse3.FUSEError(errno.EEXIST)
                sha = await self._publish(parent, component)
                inode = self._make_receipt(parent_inode, component, parent.repo, parent.branch, sha)
        else:
            raise pyfuse3.FUSEError(errno.EROFS)
        return self._attrs(self._node(inode))

    async def flush(self, fh):
        # All modifications are fsync+atomic-rename persisted on every write.
        self._handle(fh)

    async def fsync(self, fh, datasync):
        self._handle(fh)

    async def release(self, fh):
        self.handles.pop(fh, None)


def main():
    parser = argparse.ArgumentParser(description="Writable GitHub FUSE v2 lab")
    parser.add_argument("--username", default="tisak142", help="GitHub username")
    parser.add_argument("--limit", type=int, default=10, help="Public repos (1..30)")
    parser.add_argument("--mountpoint", default=None)
    parser.add_argument("--state-dir", default=None, help="Drafts outside the mountpoint")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    mountpoint = Path(args.mountpoint).resolve() if args.mountpoint else root / "mnt"
    state_dir = Path(args.state_dir).resolve() if args.state_dir else root / ".fuse_github_state"
    mountpoint.mkdir(exist_ok=True, parents=True)
    if os.path.ismount(mountpoint):
        parser.error(f"Already mounted: {mountpoint}")
    if mountpoint == state_dir or mountpoint in state_dir.parents or state_dir in mountpoint.parents:
        parser.error("State directory must be outside mountpoint")
    try:
        client = GitHubAPI(args.username)
        repos = client.list_repositories(args.limit)
        store = WorkspaceStore(state_dir)
    except (ValueError, GitHubAPIError, OSError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    fs = GitHubFS(client, repos, store)
    options = set(pyfuse3.default_options)
    options.add("fsname=fuse_github_v2")
    # NOT read-only: only selected paths allow writes.
    pyfuse3.init(fs, str(mountpoint), options)
    print(f"Mounted @{client.username} ({len(repos)} repos) at {mountpoint}", flush=True)
    print(f"Drafts outside mount: {state_dir}", flush=True)
    print("Unmount with: fusermount3 -u '" + str(mountpoint) + "'", flush=True)
    try:
        trio.run(pyfuse3.main)
    except KeyboardInterrupt:
        pass
    finally:
        pyfuse3.close(unmount=True)


if __name__ == "__main__":
    main()
