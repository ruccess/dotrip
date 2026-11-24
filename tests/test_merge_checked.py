from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
PRE_PUSH_HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-push"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import merge_checked


class MergeCheckedTest(unittest.TestCase):
    def test_requires_open_non_draft_same_repository_pull_request(self) -> None:
        client = Mock()
        payload = pull_request()
        payload["state"] = "open"
        payload["draft"] = True
        payload["head"] = {"sha": "head-sha", "repo": {"full_name": "ruccess/dotrip"}}
        client.get_json.return_value = payload

        with self.assertRaisesRegex(merge_checked.SafetyError, "open, non-draft"):
            merge_checked.verify_pull_request(client, 42)

    def test_rejects_a_pull_request_head_from_another_repository(self) -> None:
        client = Mock()
        payload = pull_request()
        payload["head"] = {"sha": "head-sha", "repo": {"full_name": "fork/dotrip"}}
        client.get_json.return_value = payload

        with self.assertRaisesRegex(
            merge_checked.SafetyError, "head is in ruccess/dotrip"
        ):
            merge_checked.verify_pull_request(client, 42)

    def test_parses_reviewed_gate_only_with_a_full_sha(self) -> None:
        head_sha = "a" * 40

        self.assertEqual(
            merge_checked.parse_arguments(["42", "--reviewed-gate", head_sha]),
            (42, head_sha),
        )
        with self.assertRaisesRegex(merge_checked.SafetyError, "full lowercase"):
            merge_checked.parse_arguments(["42", "--reviewed-gate", "short-sha"])

    def test_requires_an_exact_reviewed_gate_sha_for_gate_file_changes(self) -> None:
        comparison = {"files": [{"filename": "scripts/check_repo.py"}]}

        with self.assertRaisesRegex(merge_checked.SafetyError, "--reviewed-gate"):
            merge_checked.ensure_gate_changes_reviewed(comparison, "a" * 40, None)

        merge_checked.ensure_gate_changes_reviewed(comparison, "a" * 40, "a" * 40)

    def test_rejects_a_possibly_truncated_compare_file_list(self) -> None:
        comparison = {
            "files": [{"filename": f"src/{index}.py"} for index in range(300)]
        }

        with self.assertRaisesRegex(merge_checked.SafetyError, "300-file limit"):
            merge_checked.ensure_gate_changes_reviewed(comparison, "a" * 40, None)

    def test_requires_review_when_a_gate_file_was_renamed(self) -> None:
        comparison = {
            "files": [
                {
                    "filename": "docs/pre-push-notes.txt",
                    "previous_filename": ".githooks/pre-push",
                }
            ]
        }

        with self.assertRaisesRegex(merge_checked.SafetyError, "--reviewed-gate"):
            merge_checked.ensure_gate_changes_reviewed(comparison, "a" * 40, None)

    def test_updates_default_ref_with_a_non_force_fast_forward(self) -> None:
        authentication = merge_checked.subprocess.CompletedProcess(
            args=["gh", "auth", "token"], returncode=0, stdout="token\n", stderr=""
        )
        updated_reference = merge_checked.subprocess.CompletedProcess(
            args=["gh", "api"], returncode=0, stdout="{}", stderr=""
        )
        with patch.object(
            merge_checked.subprocess,
            "run",
            side_effect=[authentication, updated_reference],
        ) as run:
            merge_checked.GitHubClient().update_default_reference("a" * 40)

        update_arguments = run.call_args_list[1].args[0]
        self.assertEqual(update_arguments[:4], ["gh", "api", "--method", "PATCH"])
        self.assertIn(
            "/repos/ruccess/dotrip/git/refs/heads/docs%2Fideation", update_arguments
        )
        self.assertIn("force=false", update_arguments)
        self.assertNotIn("--force", update_arguments)

    def test_pre_push_blocks_default_branch_and_allows_feature_branch(self) -> None:
        self.assertTrue(PRE_PUSH_HOOK.is_file(), "pre-push hook must exist")
        self.assertTrue(
            PRE_PUSH_HOOK.stat().st_mode & 0o111, "pre-push hook must be executable"
        )
        cases = {
            "default branch update": (
                "refs/heads/docs/ideation old refs/heads/docs/ideation new\n",
                1,
            ),
            "default branch deletion": (
                "(delete) old refs/heads/docs/ideation new\n",
                1,
            ),
            "feature branch": ("refs/heads/feature old refs/heads/feature new\n", 0),
        }
        for name, (input_text, expected_return_code) in cases.items():
            with self.subTest(name=name):
                result = subprocess.run(
                    ["sh", str(PRE_PUSH_HOOK)],
                    input=input_text,
                    capture_output=True,
                    check=False,
                    text=True,
                )
                self.assertEqual(result.returncode, expected_return_code, result.stderr)

    def test_refuses_to_invoke_gh_when_ruccess_token_lookup_fails(self) -> None:
        failed_authentication = merge_checked.subprocess.CompletedProcess(
            args=["gh", "auth", "token"],
            returncode=1,
            stdout="",
            stderr="not logged in",
        )
        with (
            patch.object(
                merge_checked.subprocess, "run", return_value=failed_authentication
            ) as run,
            self.assertRaisesRegex(merge_checked.SafetyError, "ruccess authentication"),
        ):
            merge_checked.GitHubClient._run("api", "/user")

        run.assert_called_once()

    def test_rejects_a_previous_success_when_the_latest_run_is_in_progress(
        self,
    ) -> None:
        client = Mock()
        client.get_json.side_effect = [
            {
                "number": 42,
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"sha": "head-sha", "repo": {"full_name": "ruccess/dotrip"}},
                "base": {"ref": "docs/ideation", "sha": "base-sha"},
                "state": "open",
                "draft": False,
                "updated_at": "2026-11-24T10:00:00Z",
            },
            {"default_branch": "docs/ideation"},
            {"commit": {"sha": "base-sha"}},
            {"merge_base_commit": {"sha": "base-sha"}, "files": []},
            {
                "workflow_runs": [
                    successful_run(100, "2026-11-24T10:01:00Z"),
                    matching_run(101, "in_progress", None, "2026-11-24T10:02:00Z"),
                ]
            },
        ]

        with self.assertRaisesRegex(merge_checked.SafetyError, "still in_progress"):
            merge_checked.verify_pull_request(client, 42)

    def test_accepts_only_a_current_pr_gate_job_from_the_ci_workflow(self) -> None:
        client = Mock()
        client.get_json.side_effect = [
            {
                "number": 42,
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"sha": "head-sha", "repo": {"full_name": "ruccess/dotrip"}},
                "base": {"ref": "docs/ideation", "sha": "base-sha"},
                "state": "open",
                "draft": False,
                "updated_at": "2026-11-24T10:00:00Z",
            },
            {"default_branch": "docs/ideation"},
            {"commit": {"sha": "base-sha"}},
            {"merge_base_commit": {"sha": "base-sha"}, "files": []},
            {"workflow_runs": [successful_run(100, "2026-11-24T10:01:00Z")]},
            {
                "jobs": [
                    {"name": "PR Gate", "status": "completed", "conclusion": "success"}
                ]
            },
        ]

        verified = merge_checked.verify_pull_request(client, 42)

        self.assertEqual(verified, "head-sha")
        client.get_json.assert_any_call(
            "/repos/ruccess/dotrip/branches/docs%2Fideation"
        )
        client.get_json.assert_any_call(
            "/repos/ruccess/dotrip/compare/base-sha...head-sha"
        )
        client.get_json.assert_any_call(
            "/repos/ruccess/dotrip/actions/runs?event=pull_request&head_sha=head-sha&per_page=100"
        )
        client.get_json.assert_any_call(
            "/repos/ruccess/dotrip/actions/runs/100/jobs?filter=latest&per_page=100"
        )

    def test_rejects_a_successful_run_created_before_the_pr_was_edited(self) -> None:
        client = Mock()
        client.get_json.side_effect = [
            {
                "number": 42,
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"sha": "head-sha", "repo": {"full_name": "ruccess/dotrip"}},
                "base": {"ref": "docs/ideation", "sha": "base-sha"},
                "state": "open",
                "draft": False,
                "updated_at": "2026-11-24T10:00:00Z",
            },
            {"default_branch": "docs/ideation"},
            {"commit": {"sha": "base-sha"}},
            {"merge_base_commit": {"sha": "base-sha"}, "files": []},
            {"workflow_runs": [successful_run(100, "2026-11-24T09:59:59Z")]},
        ]

        with self.assertRaisesRegex(
            merge_checked.SafetyError, "after the latest PR edit"
        ):
            merge_checked.verify_pull_request(client, 42)

    def test_rejects_a_head_that_does_not_contain_the_current_default_base(
        self,
    ) -> None:
        client = Mock()
        client.get_json.side_effect = [
            pull_request(),
            {"default_branch": "docs/ideation"},
            {"commit": {"sha": "base-sha"}},
            {"merge_base_commit": {"sha": "older-base-sha"}, "files": []},
        ]

        with self.assertRaisesRegex(merge_checked.SafetyError, "does not contain"):
            merge_checked.verify_pull_request(client, 42)

    def test_rejects_a_successful_run_from_a_different_workflow(self) -> None:
        client = Mock()
        client.get_json.side_effect = [
            pull_request(),
            {"default_branch": "docs/ideation"},
            {"commit": {"sha": "base-sha"}},
            {"merge_base_commit": {"sha": "base-sha"}, "files": []},
            {
                "workflow_runs": [
                    matching_run(
                        100, "completed", "success", "2026-11-24T10:01:00Z", "other.yml"
                    )
                ]
            },
            {
                "jobs": [
                    {"name": "PR Gate", "status": "completed", "conclusion": "success"}
                ]
            },
        ]

        with self.assertRaisesRegex(merge_checked.SafetyError, "No ci.yml"):
            merge_checked.verify_pull_request(client, 42)


def matching_run(
    identifier: int,
    status: str,
    conclusion: str | None,
    created_at: str,
    workflow_file: str = "ci.yml",
) -> dict[str, object]:
    return {
        "id": identifier,
        "event": "pull_request",
        "head_sha": "head-sha",
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "run_attempt": 1,
        "pull_requests": [{"number": 42}],
        "path": f".github/workflows/{workflow_file}@refs/pull/42/merge",
    }


def successful_run(identifier: int, created_at: str) -> dict[str, object]:
    return matching_run(identifier, "completed", "success", created_at)


def pull_request() -> dict[str, object]:
    return {
        "number": 42,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "head-sha", "repo": {"full_name": "ruccess/dotrip"}},
        "base": {"ref": "docs/ideation", "sha": "base-sha"},
        "state": "open",
        "draft": False,
        "updated_at": "2026-11-24T10:00:00Z",
    }


if __name__ == "__main__":
    unittest.main()
