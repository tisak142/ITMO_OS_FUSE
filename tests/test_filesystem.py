"""Offline contract tests for pyfuse3 callbacks and Git publication semantics.

Stubs FUSE/Trio so tests can run on machines lacking /dev/fuse. Real mount
must still be tested separately on Ubuntu.
"""
import asyncio
import errno
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Light shim mirrors the parts of pyfuse3 and Trio exercised by our filesystem.
pyfuse3 = types.ModuleType("pyfuse3")
pyfuse3.ROOT_INODE = 1
pyfuse3.Operations = type("Operations", (), {})
class FUSEError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.errno = code
pyfuse3.FUSEError = FUSEError
pyfuse3.EntryAttributes = type("EntryAttributes", (), {})
class FileInfo:
    def __init__(self, fh, direct_io=False, keep_cache=False):
        self.fh = fh
        self.direct_io = direct_io
        self.keep_cache = keep_cache
pyfuse3.FileInfo = FileInfo
pyfuse3.readdir_reply = lambda token, name, attrs, pos: token.append((name, attrs.st_ino)) or True
sys.modules["pyfuse3"] = pyfuse3
trio = types.ModuleType("trio")
trio.Lock = asyncio.Lock
class Thread:
    @staticmethod
    async def run_sync(callback):
        return callback()
trio.to_thread = Thread
sys.modules["trio"] = trio

from fuse_github import GitHubFS, encode_branch, decode_branch, validate_branch
from workspace_store import WorkspaceStore

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40


class FakeAPI:
    username = "student"
    MAX_BLOB = 2 * 1024 * 1024
    def __init__(self, token="fake-token"):
        self.token = token
        self.br = {"main": A}
        self.blobs = {"sha-readme": b"initial", "sha-existing": b"x=1\n", "sha-old": b"delete me"}
        self.trees = {"tree-a": {
            "README.md": {"path": "README.md", "type": "blob", "mode": "100644", "sha": "sha-readme", "size": 7},
            "src": {"path": "src", "type": "tree", "mode": "040000", "sha": "tree-src"},
            "src/existing.py": {"path": "src/existing.py", "type": "blob", "mode": "100644", "sha": "sha-existing", "size": 4},
            "old.txt": {"path": "old.txt", "type": "blob", "mode": "100644", "sha": "sha-old", "size": 9},
        }}
        self.commits_data = {A: {"tree": "tree-a", "parents": []}}
        self._new_count = 0
        self.created = []
        self.published = []

    def branches(self, repo, page, per_page):
        return [{"name": key, "commit": {"sha": sha}} for key, sha in self.br.items()] if page == 1 else []
    def head(self, repo, branch):
        return self.br[branch]
    def create_branch(self, repo, branch, sha):
        if branch in self.br:
            raise ValueError("Already exists")
        self.br[branch] = sha
    def commits(self, repo, branch, page, per_page):
        if page != 1:
            return []
        sha = self.br[branch]
        out = []
        while sha:
            out.append({"sha": sha})
            sha = (self.commits_data[sha]["parents"] or [""])[0]
        return out
    def commit_details(self, repo, sha):
        return {"sha": sha, "parents": [{"sha": p} for p in self.commits_data[sha]["parents"]],
                "commit": {"message": self.commits_data[sha].get("message", "init"),
                           "author": {"name": "Tester", "email": "x@test", "date": "today"}},
                "files": self.commits_data[sha].get("files", [])}
    def commit_diff(self, repo, sha):
        return b"diff --git a/test b/test\n"
    def git_commit(self, repo, sha):
        return {"tree": {"sha": self.commits_data[sha]["tree"]}}
    def tree(self, repo, sha, recursive):
        return {"tree": list(self.trees[sha].values()), "truncated": False}
    def blob(self, repo, sha):
        return self.blobs[sha]
    def create_blob(self, repo, data):
        result = f"newblob-{len(self.blobs)}"
        self.blobs[result] = data
        return result
    def create_tree(self, repo, base_tree, entries):
        new_tree = dict(self.trees[base_tree])
        for ent in entries:
            if ent["sha"] is None:
                del new_tree[ent["path"]]
            else:
                new_tree[ent["path"]] = {**ent, "size": len(self.blobs[ent["sha"]])}
        new_sha = f"tree-new-{len(self.trees)}"
        self.trees[new_sha] = new_tree
        self.created.append((base_tree, entries))
        return new_sha
    def create_commit(self, repo, tree, parent, message):
        sha = [B, C, D][self._new_count]
        self._new_count += 1
        base_tree = self.commits_data[parent]["tree"]
        base = self.trees[base_tree]
        current = self.trees[tree]
        paths = set(base) | set(current)
        files = []
        for path in sorted(paths):
            if path not in base:
                status = "added"
            elif path not in current:
                status = "removed"
            elif base[path].get("sha") != current[path].get("sha"):
                status = "modified"
            else:
                continue
            files.append({"status": status, "filename": path,
                          "sha": current.get(path, {}).get("sha"),
                          "size": current.get(path, {}).get("size", 0)})
        self.commits_data[sha] = {"tree": tree, "parents": [parent],
                                  "message": message, "files": files}
        return sha
    def update_branch(self, repo, branch, sha):
        parent = self.commits_data[sha]["parents"][0]
        if self.br[branch] != parent:
            raise RuntimeError("non-fast-forward")
        self.br[branch] = sha
        self.published.append((branch, sha))
    def get_readme(self, repo):
        return b"initial"


REPOS = [{"name": "demo", "default_branch": "main", "html_url": "https://github.com/student/demo"}]


class FuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name) / "state")
        self.api = FakeAPI()
        self.fs = GitHubFS(self.api, REPOS, self.store)
    def tearDown(self):
        self.temp.cleanup()
    def run_async(self, awaitable):
        return asyncio.run(awaitable)
    def inode(self, path):
        async def walk():
            cur = 1
            for part in path.split("/"):
                if part:
                    cur = (await self.fs.lookup(cur, part.encode())).st_ino
            return cur
        return self.run_async(walk())

    def test_branch_name_roundtrip(self):
        self.assertEqual(encode_branch("feature/login"), "feature%2Flogin")
        self.assertEqual(decode_branch("feature%2Flogin"), "feature/login")
        self.assertTrue(validate_branch("feature/login"))
        self.assertFalse(validate_branch("feature..bad"))

    def test_initial_history_and_immutable_commit(self):
        sha = self.inode(f"demo/branches/main/commits/{A}/INFO.txt")
        async def check():
            fh = (await self.fs.open(sha, 0)).fh
            self.assertIn(b"SHA: " + A.encode(), await self.fs.read(fh, 0, 1000))
            with self.assertRaises(FUSEError) as failed:
                await self.fs.open(sha, 1)
            self.assertEqual(failed.exception.errno, errno.EROFS)
            await self.fs.release(fh)
        self.run_async(check())

    def test_create_branch_and_publish_multiple_edits(self):
        branches = self.inode("demo/branches")
        async def create_branch():
            return (await self.fs.mkdir(branches, b"feature%2Flogin", 0o755)).st_ino
        self.run_async(create_branch())
        self.assertEqual(self.api.br["feature/login"], A)
        ws = self.inode("demo/branches/feature%2Flogin/workspace")
        file_old = self.inode("demo/branches/feature%2Flogin/workspace/old.txt")
        file_readme = self.inode("demo/branches/feature%2Flogin/workspace/README.md")
        async def edits():
            fi, _ = await self.fs.create(ws, b"hello.py", 0o644, 1)
            await self.fs.write(fi.fh, 0, b"hello!\n")
            await self.fs.release(fi.fh)
            fi = await self.fs.open(file_readme, 2 | 0x200)  # O_RDWR | O_TRUNC
            await self.fs.write(fi.fh, 0, b"updated")
            await self.fs.release(fi.fh)
            await self.fs.unlink(ws, b"old.txt")
        self.run_async(edits())
        self.assertEqual(self.api.br["feature/login"], A) # no remote mutation yet
        trigger = self.inode("demo/branches/feature%2Flogin/_commit")
        self.run_async(self.fs.mkdir(trigger, b"001-add-and-edit", 0o755))
        self.assertEqual(self.api.br["feature/login"], B)
        self.assertEqual(self.api.commits_data[B]["parents"], [A])
        tree = self.api.trees[self.api.commits_data[B]["tree"]]
        self.assertIn("src/existing.py", tree) # unrelated file survives
        self.assertNotIn("old.txt", tree)
        self.assertIn("hello.py", tree)
        self.assertEqual(self.api.blobs[tree["README.md"]["sha"]], b"updated")
        receipt = self.inode("demo/branches/feature%2Flogin/_commit/001-add-and-edit/SHA.txt")
        async def read_receipt():
            fh = (await self.fs.open(receipt, 0)).fh
            result = await self.fs.read(fh, 0, 200)
            await self.fs.release(fh)
            return result
        self.assertEqual(self.run_async(read_receipt()), B.encode() + b"\n")
        self.assertIn("old.txt", [x["filename"] for x in self.api.commits_data[B]["files"]])

    def test_empty_commit(self):
        trigger = self.inode("demo/branches/main/_commit")
        self.run_async(self.fs.mkdir(trigger, b"empty", 0o755))
        self.assertEqual(self.api.br["main"], B)
        self.assertEqual(self.api.commits_data[B]["tree"], "tree-a")
        self.assertFalse(self.api.commits_data[B]["files"])

    def test_stale_head_preserves_persisted_draft(self):
        ws = self.inode("demo/branches/main/workspace")
        async def write():
            fh, _ = await self.fs.create(ws, b"local.txt", 0o644, 1)
            await self.fs.write(fh.fh, 0, b"my local draft")
            await self.fs.release(fh.fh)
        self.run_async(write())
        self.api.br["main"] = C
        trigger = self.inode("demo/branches/main/_commit")
        async def check():
            with self.assertRaises(FUSEError) as failed:
                await self.fs.mkdir(trigger, b"should-fail", 0o755)
            self.assertEqual(failed.exception.errno, errno.ESTALE)
        self.run_async(check())
        self.assertFalse(self.api.published)
        restored = self.store.load("student", "demo", "main", C)
        self.assertEqual(restored.base_sha, A)
        self.assertEqual(restored.edits["local.txt"], b"my local draft")

    def test_persistence_across_mounts(self):
        ws = self.inode("demo/branches/main/workspace")
        async def write():
            fh, _ = await self.fs.create(ws, b"persist.txt", 0o644, 1)
            await self.fs.write(fh.fh, 0, b"binary\x00data")
            await self.fs.release(fh.fh)
        self.run_async(write())
        self.fs = GitHubFS(self.api, REPOS, self.store)
        inode = self.inode("demo/branches/main/workspace/persist.txt")
        async def read():
            fh = (await self.fs.open(inode, 0)).fh
            result = await self.fs.read(fh, 0, 20)
            await self.fs.release(fh)
            return result
        self.assertEqual(self.run_async(read()), b"binary\x00data")

    def test_nested_creation_and_changed_files(self):
        ws = self.inode("demo/branches/main/workspace")
        async def stage():
            directory = (await self.fs.mkdir(ws, b"nested", 0o755)).st_ino
            inner = (await self.fs.mkdir(directory, b"more", 0o755)).st_ino
            fh, _ = await self.fs.create(inner, b"demo.txt", 0o644, 1)
            await self.fs.write(fh.fh, 0, b"nested content\n")
            await self.fs.release(fh.fh)
        self.run_async(stage())
        trigger = self.inode("demo/branches/main/_commit")
        self.run_async(self.fs.mkdir(trigger, b"nested-commit", 0o755))
        changed = self.inode(f"demo/branches/main/commits/{B}/changed/nested/more/demo.txt")
        async def read_changed():
            fh = (await self.fs.open(changed, 0)).fh
            data = await self.fs.read(fh, 0, 100)
            await self.fs.release(fh)
            return data
        self.assertEqual(self.run_async(read_changed()), b"nested content\n")

    def test_branch_workspace_isolation(self):
        branches = self.inode("demo/branches")
        self.run_async(self.fs.mkdir(branches, b"feature", 0o755))
        ws_feature = self.inode("demo/branches/feature/workspace")
        ws_main = self.inode("demo/branches/main/workspace")
        async def stage():
            fh, _ = await self.fs.create(ws_feature, b"only-feature.txt", 0o644, 1)
            await self.fs.write(fh.fh, 0, b"feature")
            await self.fs.release(fh.fh)
            with self.assertRaises(FUSEError) as missing:
                await self.fs.lookup(ws_main, b"only-feature.txt")
            self.assertEqual(missing.exception.errno, errno.ENOENT)
        self.run_async(stage())
        self.assertNotIn("only-feature.txt", self.store.load(
            "student", "demo", "main", A).edits)
        self.assertIn("only-feature.txt", self.store.load(
            "student", "demo", "feature", A).edits)

    def test_no_token_write_rejected(self):
        self.api.token = ""
        ws = self.inode("demo/branches/main/workspace")
        async def check():
            with self.assertRaises(FUSEError) as failed:
                await self.fs.create(ws, b"no.txt", 0o644, 1)
            self.assertEqual(failed.exception.errno, errno.EACCES)
        self.run_async(check())

if __name__ == "__main__":
    unittest.main()
