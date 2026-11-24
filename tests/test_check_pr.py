import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import check_pr


class PullRequestContractTests(unittest.TestCase):
    def setUp(self):
        self.issue = {
            "number": 42,
            "state": "open",
            "body": "### 사용자 행동\n사용자가 지출을 저장한다.\n\n"
            "### 완료 조건\n저장한 지출을 다시 조회할 수 있다.",
        }

    def valid_body(self, evidence: str, ui_change: str, reason: str = "") -> str:
        return f"""\
## 연결 이슈
Closes #42

## 검증 결과
- 실행한 검사: python -m unittest
- 결과: 8개 테스트 통과

## 독립 리뷰
- 리뷰어: @reviewer
- 발견 및 처리: 저장 실패 사례를 추가하고 재검증함

## 화면 증거
- 화면 변경 여부: {ui_change}
- 증거 URL 또는 해당 없음: {evidence}
- 해당 없음 이유: {reason}
"""

    def test_closed_issue_with_required_sections_is_accepted(self):
        pull_request_body = """\
## 연결 이슈
Closes #42

## 검증 결과
- 실행한 검사: python -m unittest
- 결과: 7개 테스트 통과

## 독립 리뷰
- 리뷰어: @reviewer
- 발견 및 처리: 누락 사례를 확인했고 추가 문제는 발견하지 못함

## 화면 증거
- 화면 변경 여부: UI 변경 없음
- 증거 URL 또는 해당 없음: 해당 없음
- 해당 없음 이유: 문서와 검증 도구만 변경함
"""
        issue = {
            "number": 42,
            "state": "closed",
            "body": """\
### 사용자 행동
기여자가 PR에서 구현과 검증 근거를 한번에 확인한다.

### 완료 조건
연결된 이슈와 검증 결과가 모두 있어야 한다.
""",
        }

        errors = check_pr.validate_contract(
            pull_request_body,
            ["README.md", "scripts/check_pr.py"],
            lambda number: issue,
        )

        self.assertEqual([], errors)

    def test_ui_change_rejects_placeholder_link_and_accepts_real_link_shape(self):
        for placeholder_link in ("https://example.com/demo", "https://github.com"):
            with self.subTest(placeholder_link=placeholder_link):
                placeholder_errors = check_pr.validate_contract(
                    self.valid_body(placeholder_link, "UI 변경"),
                    ["src/screens/ExpenseScreen.tsx"],
                    lambda number: self.issue,
                )
                self.assertIn(
                    "UI 변경에는 실제 화면을 확인할 수 있는 http(s) 증거 URL이 필요합니다.",
                    placeholder_errors,
                )
        valid_errors = check_pr.validate_contract(
            self.valid_body(
                "https://github.com/dotrip/dotrip/actions/runs/123/artifacts/456",
                "UI 변경",
            ),
            ["src/screens/ExpenseScreen.tsx"],
            lambda number: self.issue,
        )

        self.assertEqual([], valid_errors)

    def test_unknown_application_path_needs_explicit_reason_when_evidence_is_not_applicable(
        self,
    ):
        errors = check_pr.validate_contract(
            self.valid_body("해당 없음", "UI 변경 없음"),
            ["src/domain/expense.py"],
            lambda number: self.issue,
        )

        self.assertIn(
            "앱 또는 분류되지 않은 경로를 변경하면서 화면 증거가 없다면 그 이유를 구체적으로 작성해주세요.",
            errors,
        )

    def test_docs_and_tools_only_change_may_mark_evidence_not_applicable(self):
        errors = check_pr.validate_contract(
            self.valid_body("해당 없음", "UI 변경 없음"),
            ["README.md", "scripts/check_pr.py", "tests/test_check_pr.py"],
            lambda number: self.issue,
        )

        self.assertEqual([], errors)

    def test_linked_pull_request_is_rejected_as_work_item(self):
        errors = check_pr.validate_contract(
            self.valid_body("해당 없음", "UI 변경 없음"),
            ["README.md"],
            lambda number: {
                "number": number,
                "pull_request": {"url": "https://api.github.test/pr/42"},
            },
        )

        self.assertIn("#42은(는) 작업 이슈가 아니라 다른 PR입니다.", errors)

    def test_placeholders_do_not_satisfy_required_pr_fields(self):
        body = self.valid_body("https://example.com/demo", "UI 변경")
        body = body.replace("python -m unittest", "<!-- 실행한 검사를 작성해주세요 -->")
        body = body.replace("@reviewer", "<!-- @reviewer -->")

        errors = check_pr.validate_contract(
            body,
            ["src/App.tsx"],
            lambda number: self.issue,
        )

        self.assertTrue(any("실행한 검사" in error for error in errors))
        self.assertTrue(any("리뷰어 식별자" in error for error in errors))
        self.assertTrue(any("증거 URL" in error for error in errors))

    def test_empty_field_does_not_consume_the_next_markdown_line(self):
        body = self.valid_body("해당 없음", "UI 변경 없음").replace(
            "- 실행한 검사: python -m unittest\n- 결과: 8개 테스트 통과",
            "- 실행한 검사:\n- 결과: 8개 테스트 통과",
        )

        errors = check_pr.validate_contract(
            body,
            ["README.md"],
            lambda number: self.issue,
        )

        self.assertTrue(any("실행한 검사" in error for error in errors))

    def test_local_agent_session_may_be_named_as_independent_reviewer(self):
        body = self.valid_body("해당 없음", "UI 변경 없음").replace(
            "@reviewer", "agent: ci-review-session"
        )

        errors = check_pr.validate_contract(
            body,
            ["README.md"],
            lambda number: self.issue,
        )

        self.assertEqual([], errors)

    def test_pull_request_author_cannot_name_themselves_as_github_reviewer(self):
        errors = check_pr.validate_contract(
            self.valid_body("해당 없음", "UI 변경 없음"),
            ["README.md"],
            lambda number: self.issue,
            pull_request_author="reviewer",
        )

        self.assertTrue(any("PR 작성자 본인" in error for error in errors))

    def test_explicit_ui_path_cannot_skip_evidence_with_a_reason(self):
        errors = check_pr.validate_contract(
            self.valid_body("해당 없음", "UI 변경 없음", "내부 변경이라고 판단함"),
            ["src/screens/ExpenseScreen.tsx"],
            lambda number: self.issue,
        )

        self.assertTrue(any("명백한 UI 경로" in error for error in errors))

    def test_unedited_pull_request_template_does_not_pass(self):
        template = Path(".github/pull_request_template.md").read_text(encoding="utf-8")

        errors = check_pr.validate_contract(
            template,
            ["README.md"],
            lambda number: self.fail(
                "placeholder issue must not trigger an API lookup"
            ),
        )

        self.assertTrue(any("Closes #번호" in error for error in errors))
        self.assertTrue(any("실행한 검사" in error for error in errors))
        self.assertTrue(any("리뷰어 식별자" in error for error in errors))

    def test_main_uses_event_repository_issue_and_base_head_diff(self):
        base_sha = "a" * 40
        head_sha = "b" * 40
        event = {
            "repository": {"full_name": "dotrip/dotrip"},
            "pull_request": {
                "body": self.valid_body("해당 없음", "UI 변경 없음"),
                "base": {"sha": base_sha},
                "head": {"sha": head_sha},
                "user": {"login": "author"},
            },
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False
        ) as event_file:
            json.dump(event, event_file)
            event_path = event_file.name

        environment = {
            "GITHUB_EVENT_PATH": event_path,
            "GITHUB_REPOSITORY": "dotrip/dotrip",
            "GITHUB_TOKEN": "token",
        }
        try:
            with (
                mock.patch.object(
                    check_pr,
                    "_get_changed_paths",
                    return_value=["scripts/check_pr.py"],
                ) as get_changed_paths,
                mock.patch.object(
                    check_pr, "_get_issue", return_value=self.issue
                ) as get_issue,
                mock.patch("builtins.print") as print_output,
            ):
                exit_code = check_pr.main(environment)
        finally:
            os.unlink(event_path)

        self.assertEqual(0, exit_code)
        get_changed_paths.assert_called_once_with(base_sha, head_sha)
        get_issue.assert_called_once_with("dotrip/dotrip", "token", 42)
        print_output.assert_any_call("PR contract check passed.")

    def test_untrusted_sha_is_rejected_before_git_is_started(self):
        with (
            mock.patch.object(check_pr.subprocess, "run") as run_git,
            self.assertRaises(check_pr.ContractCheckFailure),
        ):
            check_pr._get_changed_paths("--output=/tmp/untrusted", "b" * 40)

        run_git.assert_not_called()


if __name__ == "__main__":
    unittest.main()
