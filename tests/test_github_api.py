"""HTTP-level tests with fake GitHub responses (no network/token required)."""
import base64
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from github_api import GitHubAPI, GitHubAPIError

class Response:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.data

class APITests(unittest.TestCase):
    def test_username_and_limit(self):
        with self.assertRaises(ValueError):
            GitHubAPI("not a username")
        with patch("github_api.urlopen"):
            with self.assertRaises(ValueError):
                GitHubAPI("demo").list_repositories(31)

    def test_list_repositories(self):
        client = GitHubAPI("demo")
        with patch("github_api.urlopen", return_value=Response([{"name": "one"}])) as call:
            self.assertEqual(client.list_repositories(3)[0]["name"], "one")
            url = call.call_args.args[0].full_url
            self.assertIn("per_page=3", url)
            self.assertIn("/users/demo/repos", url)

    def test_missing_readme(self):
        error = HTTPError("url", 404, "not found", {}, io.BytesIO())
        with patch("github_api.urlopen", side_effect=error) as call:
            client = GitHubAPI("demo")
            self.assertIn(b"no README", client.get_readme("repo"))
            self.assertIn(b"no README", client.get_readme("repo"))
            self.assertEqual(call.call_count, 1)

    def test_blob_binary_roundtrip(self):
        client = GitHubAPI("demo")
        content = b"hello\x00\xff\x80"
        with patch("github_api.urlopen", return_value=Response({
            "encoding": "base64", "size": len(content),
            "content": base64.b64encode(content).decode(),
        })):
            self.assertEqual(client.blob("repo", "1" * 40), content)

    def test_no_token_writes(self):
        client = GitHubAPI("demo")
        client.token = ""
        with self.assertRaises(GitHubAPIError) as err:
            client.create_branch("repo", "new", "0" * 40)
        self.assertEqual(err.exception.status, 403)

    def test_create_tree_keeps_base_and_no_force(self):
        client = GitHubAPI("demo")
        client.token = "example-test-token"
        recorded = []
        def request(req, timeout):
            recorded.append(req)
            if req.get_method() == "PATCH":
                return Response({"object": {"sha": "1" * 40}})
            return Response({"sha": "2" * 40})
        with patch("github_api.urlopen", side_effect=request):
            client.create_tree("repo", "0" * 40, [
                {"path": "sub/file.txt", "mode": "100644", "type": "blob", "sha": None}
            ])
            client.update_branch("repo", "feature/login", "2" * 40)
        tree_payload = json.loads(recorded[0].data)
        ref_payload = json.loads(recorded[1].data)
        self.assertEqual(tree_payload["base_tree"], "0" * 40)
        self.assertIsNone(tree_payload["tree"][0]["sha"])
        self.assertEqual(ref_payload, {"sha": "2" * 40, "force": False})
        self.assertIn("/feature/login", recorded[1].full_url)

    def test_http_422_raised(self):
        client = GitHubAPI("demo")
        error = HTTPError("url", 422, "validation", {}, io.BytesIO(b"sensitive"))
        with patch("github_api.urlopen", side_effect=error):
            with self.assertRaises(GitHubAPIError) as err:
                client.head("repo", "main")
        self.assertEqual(err.exception.status, 422)
        self.assertNotIn("sensitive", str(err.exception))

    def test_commit_file_pagination(self):
        client = GitHubAPI("demo")
        calls = []
        def request(req, timeout):
            calls.append(req.full_url)
            return Response({"commit": {}, "files": ([{"filename": "x"}] * 100 if len(calls) == 1
                                                        else [{"filename": "last"}])})
        with patch("github_api.urlopen", side_effect=request):
            d = client.commit_details("repo", "a" * 40)
        self.assertEqual(len(d["files"]), 101)
        self.assertEqual(len(calls), 2)

if __name__ == "__main__":
    unittest.main()
