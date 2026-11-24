"""Validate the work item and evidence recorded in a pull request."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

ISSUE_REFERENCE = re.compile(r"(?im)^[ \t]*closes[ \t]+#([1-9][0-9]*)[ \t]*$")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
PLACEHOLDERS = {
    "",
    "todo",
    "tbd",
    "n/a",
    "none",
    "없음",
    "해당 없음",
    "작성해주세요",
    "입력해주세요",
    "_no response_",
}
TOOL_PREFIXES = (".github/", "docs/", "scripts/", "tests/")
ROOT_DOCUMENTS = {"AGENTS.md", "CONTEXT.md", "README.md", "LICENSE"}
DIRECT_UI_SUFFIXES = {
    ".css",
    ".html",
    ".less",
    ".sass",
    ".scss",
    ".storyboard",
    ".svelte",
    ".vue",
    ".xib",
}
UI_CODE_SUFFIXES = {".dart", ".js", ".jsx", ".kt", ".kts", ".swift", ".ts", ".tsx"}
UI_PATH_PARTS = {"components", "pages", "screen", "screens", "ui", "views"}


class ContractCheckFailure(RuntimeError):
    """Raised when the check cannot obtain trustworthy validation inputs."""


def _clean(value: str) -> str:
    return HTML_COMMENT.sub("", value).strip().strip("`").strip()


def _is_meaningful(value: str) -> bool:
    cleaned = _clean(value)
    return cleaned.casefold() not in PLACEHOLDERS and "..." not in cleaned


def _section(markdown: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^#{{2,3}}\s+{re.escape(heading)}\s*$\n(.*?)(?=^#{{1,3}}\s|\Z)",
        markdown,
    )
    return match.group(1).strip() if match else ""


def _field(section: str, label: str) -> str:
    match = re.search(
        rf"(?m)^[ \t]*[-*][ \t]*{re.escape(label)}[ \t]*:[ \t]*([^\r\n]*)$",
        section,
    )
    return _clean(match.group(1)) if match else ""


def validate_contract(
    pull_request_body: str,
    changed_paths: list[str],
    get_issue: Callable[[int], dict[str, Any]],
    pull_request_author: str | None = None,
) -> list[str]:
    """Return all user-actionable contract errors for a pull request."""
    errors: list[str] = []
    pull_request_body = HTML_COMMENT.sub("", pull_request_body).replace("\r\n", "\n")
    issue_numbers = {
        int(number)
        for number in ISSUE_REFERENCE.findall(_section(pull_request_body, "연결 이슈"))
    }
    if len(issue_numbers) != 1:
        errors.append(
            "PR 본문에 같은 저장소 작업 이슈를 'Closes #번호'로 하나만 연결해주세요."
        )
    else:
        issue_number = issue_numbers.pop()
        issue = get_issue(issue_number)
        if "pull_request" in issue:
            errors.append(f"#{issue_number}은(는) 작업 이슈가 아니라 다른 PR입니다.")
        else:
            issue_body = issue.get("body") or ""
            for heading in ("사용자 행동", "완료 조건"):
                if not _is_meaningful(_section(issue_body, heading)):
                    errors.append(
                        f"연결 이슈 #{issue_number}의 '{heading}'을 구체적으로 작성해주세요."
                    )

    verification = _section(pull_request_body, "검증 결과")
    for label in ("실행한 검사", "결과"):
        if not _is_meaningful(_field(verification, label)):
            errors.append(f"PR의 '검증 결과 > {label}'을 구체적으로 작성해주세요.")

    review = _section(pull_request_body, "독립 리뷰")
    reviewer = _field(review, "리뷰어")
    github_reviewer = re.fullmatch(
        r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", reviewer
    )
    agent_reviewer = re.fullmatch(
        r"agent:[ \t]*\S(?:[^\r\n]*\S)?", reviewer, re.IGNORECASE
    )
    if not _is_meaningful(reviewer) or not (github_reviewer or agent_reviewer):
        errors.append(
            "PR의 '독립 리뷰 > 리뷰어'에 @GitHub아이디 또는 'agent: 세션이름' "
            "형식의 리뷰어 식별자를 작성해주세요."
        )
    elif (
        github_reviewer
        and pull_request_author
        and reviewer[1:].casefold() == pull_request_author.casefold()
    ):
        errors.append("독립 리뷰어에 PR 작성자 본인의 GitHub 계정을 적을 수 없습니다.")
    if not _is_meaningful(_field(review, "발견 및 처리")):
        errors.append("PR의 '독립 리뷰 > 발견 및 처리'를 구체적으로 작성해주세요.")

    evidence = _section(pull_request_body, "화면 증거")
    ui_change = _field(evidence, "화면 변경 여부")
    evidence_value = _field(evidence, "증거 URL 또는 해당 없음")
    no_evidence_reason = _field(evidence, "해당 없음 이유")
    path_class = classify_paths(changed_paths)
    if ui_change not in {"UI 변경", "UI 변경 없음"}:
        errors.append(
            "PR의 '화면 변경 여부'를 'UI 변경' 또는 'UI 변경 없음'으로 작성해주세요."
        )
    elif ui_change == "UI 변경":
        if not _is_evidence_url(evidence_value):
            errors.append(
                "UI 변경에는 실제 화면을 확인할 수 있는 http(s) 증거 URL이 필요합니다."
            )
    elif path_class == "explicit_ui":
        errors.append(
            "명백한 UI 경로를 변경했으므로 'UI 변경'을 선택하고 화면 증거 URL을 작성해주세요."
        )
    elif evidence_value == "해당 없음":
        if path_class == "application_or_unknown" and not _is_meaningful(
            no_evidence_reason
        ):
            errors.append(
                "앱 또는 분류되지 않은 경로를 변경하면서 화면 증거가 없다면 "
                "그 이유를 구체적으로 작성해주세요."
            )
    elif not _is_evidence_url(evidence_value):
        errors.append("화면 증거에는 유효한 URL 또는 '해당 없음'을 작성해주세요.")

    if not changed_paths:
        errors.append("base...head에서 변경 경로를 찾지 못했습니다.")
    return errors


def _is_evidence_url(value: str) -> bool:
    cleaned = _clean(value)
    if not _is_meaningful(cleaned) or any(marker in cleaned for marker in "<>{}"):
        return False
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or parsed.path in {"", "/"}:
        return False
    return host not in {
        "example.com",
        "www.example.com",
        "localhost",
    } and not host.endswith(".example.com")


def classify_paths(changed_paths: list[str]) -> str:
    """Classify only clearly documented/tooling paths as non-application changes."""
    if changed_paths and all(
        path in ROOT_DOCUMENTS
        or path.endswith((".md", ".mdx"))
        or path.startswith(TOOL_PREFIXES)
        for path in changed_paths
    ):
        return "docs_tools_only"
    for path in changed_paths:
        lowered = path.casefold()
        suffix = Path(lowered).suffix
        path_parts = Path(lowered).parts
        android_layout = suffix == ".xml" and any(
            part == "res"
            and index + 1 < len(path_parts)
            and (
                path_parts[index + 1] == "layout"
                or path_parts[index + 1].startswith("layout-")
            )
            for index, part in enumerate(path_parts)
        )
        if (
            suffix in DIRECT_UI_SUFFIXES
            or android_layout
            or (
                suffix in UI_CODE_SUFFIXES
                and (
                    set(path_parts) & UI_PATH_PARTS
                    or Path(lowered).stem in {"app", "main"}
                )
            )
        ):
            return "explicit_ui"
    return "application_or_unknown"


def _get_changed_paths(base_sha: str, head_sha: str) -> list[str]:
    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", sha) for sha in (base_sha, head_sha)):
        raise ContractCheckFailure(
            "PR 이벤트의 base/head SHA 형식이 올바르지 않습니다."
        )
    result = subprocess.run(
        ["git", "diff", "--name-only", "-z", f"{base_sha}...{head_sha}", "--"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractCheckFailure(
            "base...head 변경 경로를 계산하지 못했습니다. checkout의 fetch-depth를 0으로 설정해주세요."
            + (f" ({detail})" if detail else "")
        )
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def _get_issue(repository: str, token: str, issue_number: int) -> dict[str, Any]:
    repository_path = quote(repository, safe="/")
    request = Request(
        f"https://api.github.com/repos/{repository_path}/issues/{issue_number}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "dotrip-pr-contract-check",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise ContractCheckFailure(
            f"GitHub API에서 이슈 #{issue_number}을(를) 조회하지 못했습니다 (HTTP {error.code})."
        ) from error
    except (URLError, TimeoutError) as error:
        detail = getattr(error, "reason", str(error))
        raise ContractCheckFailure(
            f"GitHub API 이슈 조회에 실패했습니다: {detail}"
        ) from error
    if not isinstance(payload, dict):
        raise ContractCheckFailure(
            "GitHub API가 예상하지 못한 이슈 응답을 반환했습니다."
        )
    return payload


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ContractCheckFailure(f"필수 환경변수 {name}이(가) 없습니다.")
    return value


def main(environment: Mapping[str, str] | None = None) -> int:
    environment = os.environ if environment is None else environment
    try:
        event_path = _required_environment(environment, "GITHUB_EVENT_PATH")
        repository = _required_environment(environment, "GITHUB_REPOSITORY")
        token = _required_environment(environment, "GITHUB_TOKEN")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ContractCheckFailure(
                "GITHUB_REPOSITORY는 owner/repository 형식이어야 합니다."
            )

        with Path(event_path).open(encoding="utf-8") as event_file:
            event = json.load(event_file)
        pull_request = event.get("pull_request")
        if not isinstance(pull_request, dict):
            raise ContractCheckFailure("pull_request 이벤트 JSON이 필요합니다.")
        event_repository = (event.get("repository") or {}).get("full_name")
        if event_repository and event_repository.casefold() != repository.casefold():
            raise ContractCheckFailure(
                "이벤트 저장소와 GITHUB_REPOSITORY가 일치하지 않습니다."
            )

        base_sha = pull_request.get("base", {}).get("sha", "")
        head_sha = pull_request.get("head", {}).get("sha", "")
        if not base_sha or not head_sha:
            raise ContractCheckFailure("PR 이벤트에 base/head SHA가 없습니다.")
        changed_paths = _get_changed_paths(base_sha, head_sha)
        errors = validate_contract(
            pull_request.get("body") or "",
            changed_paths,
            lambda number: _get_issue(repository, token, number),
            pull_request_author=(pull_request.get("user") or {}).get("login"),
        )
    except (ContractCheckFailure, json.JSONDecodeError, OSError) as failure:
        errors = [str(failure)]

    if errors:
        for message in errors:
            print(f"::error::{message.replace(chr(10), ' ')}")
        return 1
    print("PR contract check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
