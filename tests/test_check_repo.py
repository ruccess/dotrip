from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.check_repo import OTTER_ARTWORK_HASHES

CHECK_REPO = Path(__file__).resolve().parents[1] / "scripts" / "check_repo.py"


class CheckRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        subprocess.run(
            ["git", "init", "--quiet", str(self.repository)],
            check=True,
            capture_output=True,
            text=True,
        )
        for relative_path in OTTER_ARTWORK_HASHES:
            target = self.repository / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(CHECK_REPO.parents[1] / relative_path, target)
        subprocess.run(
            ["git", "add", "--", "assets/otter"], cwd=self.repository, check=True
        )

    def write_tracked(self, relative_path: str, contents: str = "") -> None:
        path = self.repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        subprocess.run(
            ["git", "add", "--", relative_path],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def add_required_documents(self) -> None:
        for relative_path in ("AGENTS.md", "CONTEXT.md", "README.md"):
            self.write_tracked(relative_path, f"# {relative_path}\n")

    def run_checker(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECK_REPO)],
            cwd=self.repository,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_allows_current_documents_and_artwork(self) -> None:
        self.add_required_documents()

        result = self.run_checker()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_repository_missing_a_required_document(self) -> None:
        self.write_tracked("AGENTS.md", "# Instructions\n")
        self.write_tracked("README.md", "# Project\n")

        result = self.run_checker()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONTEXT.md", result.stdout)

    def test_rejects_unregistered_application_stack_file(self) -> None:
        self.add_required_documents()
        self.write_tracked("package.json", '{"scripts": {"test": "echo unchecked"}}\n')

        result = self.run_checker()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("package.json", result.stdout)
        self.assertIn("실제 검증", result.stdout)

    def test_rejects_broken_local_markdown_link(self) -> None:
        self.add_required_documents()
        self.write_tracked("docs/guide.md", "[설정 안내](missing.md)\n")

        result = self.run_checker()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("docs/guide.md", result.stdout)
        self.assertIn("missing.md", result.stdout)

    def test_rejects_replaced_otter_artwork(self) -> None:
        self.add_required_documents()
        self.write_tracked("assets/otter/icons/coffee.png", "changed")

        result = self.run_checker()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("승인한 수달이 이미지와 다릅니다", result.stdout)

    def test_rejects_incomplete_otter_artwork_set(self) -> None:
        self.add_required_documents()
        (self.repository / "assets/otter/poses/walk.png").unlink()

        result = self.run_checker()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("수달이 이미지 파일이 없습니다", result.stdout)

    def test_rejects_untracked_artwork_even_if_files_remain_on_disk(self) -> None:
        self.add_required_documents()
        subprocess.run(
            ["git", "rm", "--quiet", "--cached", "-r", "--", "assets/otter"],
            cwd=self.repository,
            check=True,
        )

        result = self.run_checker()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("수달이 이미지 파일이 없습니다", result.stdout)

    def test_rejects_unapproved_otter_image(self) -> None:
        self.add_required_documents()
        self.write_tracked("assets/otter/poses/draft.png")

        result = self.run_checker()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("지원되지 않는 추적 파일", result.stdout)

    def test_rejects_broken_reference_style_link_destination(self) -> None:
        self.add_required_documents()
        self.write_tracked(
            "docs/guide.md",
            "자세한 내용은 [설정 안내][setup]를 참고하세요.\n\n[setup]: ./missing.md\n",
        )

        result = self.run_checker()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.md", result.stdout)

    def test_ignores_link_syntax_inside_fenced_code_block(self) -> None:
        self.add_required_documents()
        self.write_tracked(
            "docs/markdown-example.md",
            "```markdown\n[예시](not-a-real-link.md)\n```\n",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_ignores_link_syntax_inside_inline_code(self) -> None:
        self.add_required_documents()
        self.write_tracked(
            "docs/markdown-example.md",
            "로컬 링크는 `[설명](path/to/file.md)`처럼 작성합니다.\n",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_allows_only_the_registered_infrastructure_and_document_paths(self) -> None:
        self.add_required_documents()
        self.write_tracked("docs/guide with spaces.md", "# Guide\n")
        self.write_tracked("scripts/helper.py", "print('ok')\n")
        self.write_tracked("scripts/verify.sh", "#!/bin/sh\n")
        self.write_tracked("tests/test_example.py", "# test placeholder\n")
        self.write_tracked(".github/workflows/ci.yml", "name: CI\n")
        self.write_tracked(".github/ISSUE_TEMPLATE/work-item.yml", "name: Work item\n")
        self.write_tracked(".github/pull_request_template.md", "# Checklist\n")
        self.write_tracked(".githooks/pre-push", "#!/bin/sh\n")
        self.write_tracked(".gitignore", "__pycache__/\n")
        self.write_tracked(
            "README.md",
            "[안내](docs/guide%20with%20spaces.md) [문서 폴더](docs/) "
            "[현재 절](#section) [외부](https://example.com)\n",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_ignores_untracked_working_tree_files(self) -> None:
        self.add_required_documents()
        (self.repository / "package.json").write_text("{}\n", encoding="utf-8")

        result = self.run_checker()

        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
