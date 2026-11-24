#!/usr/bin/env python3
"""Merge a Dotrip pull request only after its current PR CI gate succeeds."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote

REPOSITORY = "ruccess/dotrip"
DEFAULT_BRANCH = "docs/ideation"
PR_GATE_JOB = "PR Gate"
WORKFLOW_PATH = ".github/workflows/ci.yml"


class SafetyError(RuntimeError):
    """Raised when a pull request cannot be safely merged locally."""


class GitHubClient:
    def get_json(self, endpoint: str) -> dict[str, Any]:
        result = self._run("api", "--method", "GET", endpoint)
        if result.returncode != 0:
            raise SafetyError(result.stderr.strip() or "GitHub API request failed.")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SafetyError("GitHub API returned invalid JSON.") from error
        if not isinstance(response, dict):
            raise SafetyError("GitHub API returned an unexpected response.")
        return response

    def merge_pull_request(self, pull_request_number: int, head_sha: str) -> str:
        result = self._run(
            "api",
            "--method",
            "PUT",
            f"/repos/{REPOSITORY}/pulls/{pull_request_number}/merge",
            "-F",
            f"sha={head_sha}",
            "-F",
            "merge_method=merge",
        )
        if result.returncode != 0:
            raise SafetyError(
                result.stderr.strip()
                or "GitHub refused the native pull request merge; no ref update fallback is allowed."
            )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SafetyError(
                "GitHub returned invalid JSON for the pull request merge."
            ) from error
        if not isinstance(response, dict) or response.get("merged") is not True:
            raise SafetyError(
                "GitHub did not merge the pull request; no ref update fallback is allowed."
            )
        return value_as_string(response, "sha")

    @staticmethod
    def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
        authentication = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com", "--user", "ruccess"],
            check=False,
            capture_output=True,
            text=True,
        )
        token = authentication.stdout.strip()
        if authentication.returncode != 0 or not token:
            raise SafetyError(
                "Could not obtain ruccess authentication; refusing to use another gh account."
            )
        environment = os.environ.copy()
        environment["GH_TOKEN"] = token
        return subprocess.run(
            ["gh", *arguments],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )


def verify_pull_request(
    client: GitHubClient, pull_request_number: int, reviewed_gate: str | None = None
) -> str:
    pull_request = client.get_json(f"/repos/{REPOSITORY}/pulls/{pull_request_number}")
    head = value_as_dict(pull_request, "head")
    head_sha = value_as_string(head, "sha")
    ensure_supported_pull_request(pull_request, head)
    repository = client.get_json(f"/repos/{REPOSITORY}")
    default_branch = repository.get("default_branch")
    if default_branch != DEFAULT_BRANCH:
        raise SafetyError(
            f"Repository default branch is {default_branch!r}, expected {DEFAULT_BRANCH!r}."
        )

    base = value_as_dict(pull_request, "base")
    if base.get("ref") != default_branch:
        raise SafetyError("Pull request does not target the repository default branch.")
    if (
        pull_request.get("mergeable") is not True
        or pull_request.get("mergeable_state") != "clean"
    ):
        raise SafetyError(
            "Pull request is not currently mergeable and clean; retry after GitHub finishes checks."
        )

    branch_ref = quote(DEFAULT_BRANCH, safe="")
    branch = client.get_json(f"/repos/{REPOSITORY}/branches/{branch_ref}")
    current_base_sha = value_as_string(value_as_dict(branch, "commit"), "sha")
    if base.get("sha") != current_base_sha:
        raise SafetyError(
            "Pull request is behind the latest default branch; update its base and rerun CI."
        )
    comparison = client.get_json(
        f"/repos/{REPOSITORY}/compare/{current_base_sha}...{head_sha}"
    )
    merge_base_sha = value_as_string(
        value_as_dict(comparison, "merge_base_commit"), "sha"
    )
    if merge_base_sha != current_base_sha:
        raise SafetyError(
            "Pull request head does not contain the current default base; update it and rerun CI."
        )
    ensure_gate_changes_reviewed(comparison, head_sha, reviewed_gate)

    run = latest_matching_run(client, pull_request_number, head_sha)
    ensure_current_successful_run(run, value_as_string(pull_request, "updated_at"))
    run_id = value_as_integer(run, "id")
    jobs = client.get_json(
        f"/repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
    ).get("jobs")
    if not isinstance(jobs, list) or not any(
        is_successful_pr_gate(job) for job in jobs
    ):
        raise SafetyError("The current ci.yml run has no successful PR Gate job.")
    return head_sha


def ensure_supported_pull_request(
    pull_request: dict[str, Any], head: dict[str, Any]
) -> None:
    if pull_request.get("state") != "open" or pull_request.get("draft") is not False:
        raise SafetyError(
            "Only an open, non-draft pull request can update the default branch."
        )
    head_repository = value_as_dict(head, "repo")
    if head_repository.get("full_name") != REPOSITORY:
        raise SafetyError(
            "Only a pull request whose head is in ruccess/dotrip is supported."
        )


def ensure_gate_changes_reviewed(
    comparison: dict[str, Any], head_sha: str, reviewed_gate: str | None
) -> None:
    files = comparison.get("files")
    if not isinstance(files, list) or len(files) >= 300:
        raise SafetyError(
            "Compare files may be incomplete at GitHub's 300-file limit; refusing to merge."
        )
    changed_gate_paths = {
        path
        for changed_file in files
        if isinstance(changed_file, dict)
        for path in (
            changed_file.get("filename"),
            changed_file.get("previous_filename"),
        )
        if isinstance(path, str) and is_gate_path(path)
    }
    if changed_gate_paths and (
        reviewed_gate != head_sha or not is_full_sha(reviewed_gate)
    ):
        raise SafetyError(
            "This PR changes CI or gate files; rerun with --reviewed-gate <full-SHA> matching the tested head."
        )


def is_gate_path(path: str) -> bool:
    return path.startswith((".github/workflows/", ".githooks/", "scripts/", "tests/"))


def is_full_sha(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def latest_matching_run(
    client: GitHubClient, pull_request_number: int, head_sha: str
) -> dict[str, Any]:
    response = client.get_json(
        f"/repos/{REPOSITORY}/actions/runs?event=pull_request&head_sha={head_sha}&per_page=100"
    )
    runs = response.get("workflow_runs")
    if not isinstance(runs, list):
        raise SafetyError("GitHub returned no workflow runs for the pull request head.")
    matching_runs = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("event") == "pull_request"
        and run.get("head_sha") == head_sha
        and workflow_path(run) == WORKFLOW_PATH
        and pull_request_number in pull_request_numbers(run)
    ]
    if not matching_runs:
        raise SafetyError(
            "No ci.yml pull_request run is linked to this pull request head."
        )
    return max(matching_runs, key=run_order_key)


def ensure_current_successful_run(
    run: dict[str, Any], pull_request_updated_at: str
) -> None:
    if value_as_string(run, "created_at") < pull_request_updated_at:
        raise SafetyError(
            "The latest ci.yml run was not created after the latest PR edit."
        )
    status = value_as_string(run, "status")
    if status != "completed":
        raise SafetyError(
            f"The latest ci.yml run is still {status}; wait for it to finish."
        )
    if run.get("conclusion") != "success":
        raise SafetyError(
            "The latest ci.yml run did not succeed; rerun or fix it before merging."
        )


def pull_request_numbers(run: dict[str, Any]) -> set[int]:
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list):
        return set()
    return {
        pull_request["number"]
        for pull_request in pull_requests
        if isinstance(pull_request, dict)
        and isinstance(pull_request.get("number"), int)
    }


def workflow_path(run: dict[str, Any]) -> str | None:
    path = run.get("path")
    if not isinstance(path, str):
        return None
    return path.partition("@")[0]


def run_order_key(run: dict[str, Any]) -> tuple[str, int, int]:
    return (
        value_as_string(run, "created_at"),
        optional_integer(run.get("run_attempt")),
        optional_integer(run.get("id")),
    )


def is_successful_pr_gate(job: object) -> bool:
    return (
        isinstance(job, dict)
        and job.get("name") == PR_GATE_JOB
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
    )


def value_as_dict(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise SafetyError(f"GitHub response is missing {key}.")
    return result


def value_as_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise SafetyError(f"GitHub response is missing {key}.")
    return result


def value_as_integer(value: dict[str, Any], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int):
        raise SafetyError(f"GitHub response is missing {key}.")
    return result


def optional_integer(value: object) -> int:
    return value if isinstance(value, int) else 0


def canonical_pull_request_number(argument: str) -> int:
    if not argument.isdecimal() or argument.startswith("0"):
        raise SafetyError("PR number must be a positive canonical decimal integer.")
    pull_request_number = int(argument)
    if pull_request_number < 1:
        raise SafetyError("PR number must be a positive canonical decimal integer.")
    return pull_request_number


def main(arguments: list[str]) -> int:
    try:
        pull_request_number, reviewed_gate = parse_arguments(arguments)
        client = GitHubClient()
        head_sha = verify_pull_request(client, pull_request_number, reviewed_gate)
        merge_sha = client.merge_pull_request(pull_request_number, head_sha)
    except SafetyError as error:
        print(f"Not merged: {error}", file=sys.stderr)
        return 1
    print(
        f"Merged PR #{pull_request_number} into {DEFAULT_BRANCH} at {merge_sha} through GitHub's native pull request merge."
    )
    return 0


def parse_arguments(arguments: list[str]) -> tuple[int, str | None]:
    if len(arguments) == 1:
        return canonical_pull_request_number(arguments[0]), None
    if len(arguments) == 3 and arguments[1] == "--reviewed-gate":
        reviewed_gate = arguments[2]
        if not is_full_sha(reviewed_gate):
            raise SafetyError(
                "--reviewed-gate must be a full lowercase 40-character SHA."
            )
        return canonical_pull_request_number(arguments[0]), reviewed_gate
    raise SafetyError(
        "Usage: python3 scripts/merge_checked.py <PR-number> [--reviewed-gate <full-SHA>]"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
