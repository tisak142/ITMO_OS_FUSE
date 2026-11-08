"""Offline tests of the GitHub REST endpoints for merge and branch deletion."""
import unittest
from unittest.mock import Mock
from github_api import GitHubAPI, GitHubAPIError


class MergeDeleteAPITests(unittest.TestCase):
    def setUp(self):
        self.api = GitHubAPI("demo")
        self.api.token = "testing-token"  # fake token; no network is used
        self.api._request = Mock(return_value={"sha": "a" * 40})

    def test_merge_endpoint(self):
        result = self.api.merge_branch("project", "main", "feature/login")
        self.assertEqual(result["sha"], "a" * 40)
        self.api._request.assert_called_once_with(
            "POST", "/repos/demo/project/merges",
            payload={"base": "main", "head": "feature/login"}
        )

    def test_merge_message(self):
        self.api.merge_branch("project", "main", "feature", "Merge feature")
        self.assertEqual(self.api._request.call_args.kwargs["payload"]["commit_message"], "Merge feature")

    def test_merge_already_integrated(self):
        self.api._request.return_value = None  # HTTP 204 from GitHub
        self.assertIsNone(self.api.merge_branch("project", "main", "feature"))

    def test_delete_reference(self):
        self.api._request.return_value = None  # HTTP 204 from GitHub
        self.api.delete_branch("project", "feature/login")
        self.api._request.assert_called_once_with(
            "DELETE", "/repos/demo/project/git/refs/heads/feature/login"
        )

    def test_delete_requires_token(self):
        self.api.token = ""
        with self.assertRaises(GitHubAPIError):
            self.api.delete_branch("project", "feature")
        self.api._request.assert_not_called()

    def test_merge_requires_token(self):
        self.api.token = ""
        with self.assertRaises(GitHubAPIError):
            self.api.merge_branch("project", "main", "feature")
        self.api._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
