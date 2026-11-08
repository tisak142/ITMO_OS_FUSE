"""Crash-resistant, per-(user/repository/branch) local staging state.

Atomic JSON files are stored OUTSIDE the FUSE mount. Payload bytes use Base64.
No credentials are written to disk. A single mounted process owns the state.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path


@dataclass
class Draft:
    base_sha: str
    edits: dict[str, bytes | None] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    receipts: dict[str, str] = field(default_factory=dict)

    def dirty(self) -> bool:
        return bool(self.edits)


class WorkspaceStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def _path(self, owner: str, repo: str, branch: str) -> Path:
        identity = json.dumps([owner, repo, branch], ensure_ascii=False).encode("utf-8")
        return self.directory / (hashlib.sha256(identity).hexdigest() + ".json")

    def load(self, owner: str, repo: str, branch: str, head: str) -> Draft:
        path = self._path(owner, repo, branch)
        if not path.exists():
            return Draft(base_sha=head)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or data.get("identity") != [owner, repo, branch]:
            raise ValueError("Incompatible or mismatched saved workspace state")
        edits = {
            key: (None if val is None else base64.b64decode(val, validate=True))
            for key, val in data["edits"].items()
        }
        draft = Draft(
            base_sha=data["base_sha"], edits=edits,
            modes=data.get("modes", {}), dirs=set(data.get("dirs", [])),
            receipts=data.get("receipts", {}),
        )
        if not draft.dirty() and draft.base_sha != head:
            draft.base_sha = head
        return draft

    def save(self, owner: str, repo: str, branch: str, draft: Draft) -> None:
        path = self._path(owner, repo, branch)
        data = {
            "version": 1, "identity": [owner, repo, branch],
            "base_sha": draft.base_sha,
            "edits": {key: None if value is None else base64.b64encode(value).decode("ascii")
                      for key, value in draft.edits.items()},
            "modes": draft.modes, "dirs": sorted(draft.dirs),
            "receipts": draft.receipts,
        }
        temp = path.with_name(path.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as f:
                os.chmod(temp, 0o600)
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
            # Durable rename on filesystems which support directory fsync.
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)
