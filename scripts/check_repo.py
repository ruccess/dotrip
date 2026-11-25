#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import subprocess
from itertools import chain
from pathlib import Path
from urllib.parse import unquote, urlsplit

REQUIRED_DOCUMENTS = {"AGENTS.md", "CONTEXT.md", "README.md"}
OTTER_ARTWORK_HASHES = {
    "assets/otter/icons/coffee.png": "9c3fadea13bebe9a2be49f4921295052807d5a470a4076126bfdef315171d7e7",
    "assets/otter/icons/exchange.png": "eb736d1ede1c62f519ee17701930c37f2b5aa19691fd226eb21b84ee880abba6",
    "assets/otter/icons/place-pin.png": "5b07e4764e00449216c1bd27b32f0cfc5d526017e99bcd5dc8877b6bb1d28aab",
    "assets/otter/icons/receipt.png": "6618b50ffda12ea319b9603ecb7e749d9a5ad6e1945a9f836bc359e106baf212",
    "assets/otter/icons/saved.png": "e1029c842900e804d27593ef1df6378a5b42b01aa4eb9b0f5748af36db5e4a3e",
    "assets/otter/icons/wallet.png": "42268243c3570d670c495d24a5d61b608078c4123b61c94d0c3f9bd7ce46fc06",
    "assets/otter/otter-first-step-clean-cheeks.png": "9393103bdf30b3fcfa58fab5ed2822b3ea98af5935d5eafdd90c5c5578e9754f",
    "assets/otter/poses/coffee.png": "8202ba940c0028809635906b7d6202fcda94adc65e14154ccb67f421fc70e7ac",
    "assets/otter/poses/float.png": "c8ccbd0d3bac3482e30ee649cefe55ce6a51aa0ebc88d4102783105ddd2010a0",
    "assets/otter/poses/read-map.png": "b32b98e17afc9fca1563d14851f787f0f63c57ea0bebc4974e4933226f6eeb45",
    "assets/otter/poses/stamp-receipt.png": "54923645345c5da61293e7d48750220f9b6da1606b8ffc58c148cd91e7a7147e",
    "assets/otter/poses/walk.png": "982b9ce1c70b30d01bf5755060ee5be6a7b7b3d1b67296d73284ba5dbe888b74",
    "assets/otter/poses/write-expense.png": "fda27f5571297be6438274a5ad774fdfbe4b772accd1283bbe325d6d47658d0a",
    "assets/otter/sudali-travel-preview.png": "875c99c5e0fb3434dbd6760c9b68862e5ac4f57d9628f1c2f12ab07a22286c2d",
}
EXACT_SUPPORTED_FILES = REQUIRED_DOCUMENTS | {
    *OTTER_ARTWORK_HASHES,
    ".github/ISSUE_TEMPLATE/work-item.yml",
    ".github/pull_request_template.md",
    ".github/workflows/ci.yml",
    ".githooks/pre-push",
    ".gitignore",
}
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?P<target><[^>]+>|[^\s)]+)")
REFERENCE_LINK = re.compile(
    r"^\s{0,3}\[[^\]]+\]:\s*(?P<target><[^>\n]+>|\S+)", re.MULTILINE
)
FENCE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(?P<ticks>`+).*?(?P=ticks)", re.DOTALL)


def is_supported_path(path: str) -> bool:
    if path in EXACT_SUPPORTED_FILES or path.endswith(".md"):
        return True
    if path.startswith("scripts/") and path.endswith((".py", ".sh")):
        return True
    return path.startswith("tests/") and path.endswith(".py")


def without_fenced_code(contents: str) -> str:
    visible_lines: list[str] = []
    open_fence: str | None = None
    for line in contents.splitlines(keepends=True):
        match = FENCE.match(line)
        if open_fence is None and match:
            open_fence = match.group("fence")
            visible_lines.append("\n")
        elif open_fence is not None:
            stripped = line.lstrip()
            if stripped.startswith(open_fence[0] * len(open_fence)):
                open_fence = None
            visible_lines.append("\n")
        else:
            visible_lines.append(line)
    return "".join(visible_lines)


def without_inline_code(contents: str) -> str:
    return INLINE_CODE.sub(lambda match: "\n" * match.group().count("\n"), contents)


def missing_local_links(
    repository: Path, markdown_path: str, tracked_files: set[str]
) -> list[str]:
    try:
        contents = (repository / markdown_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ["파일을 UTF-8 Markdown으로 읽을 수 없습니다"]
    contents = without_inline_code(without_fenced_code(contents))

    tracked_directories = {
        directory
        for path in tracked_files
        for directory in Path(path).parents
        if str(directory) != "."
    }
    missing: list[str] = []
    for match in chain(
        INLINE_LINK.finditer(contents), REFERENCE_LINK.finditer(contents)
    ):
        raw_target = match.group("target").strip("<>")
        parsed = urlsplit(raw_target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        decoded_path = unquote(parsed.path)
        if decoded_path.startswith("/"):
            candidate = posixpath.normpath(decoded_path.lstrip("/"))
        else:
            candidate = posixpath.normpath(
                posixpath.join(posixpath.dirname(markdown_path), decoded_path)
            )
        if (
            candidate not in tracked_files
            and Path(candidate) not in tracked_directories
        ):
            missing.append(raw_target)
    return missing


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        print("저장소의 추적 파일을 읽지 못했습니다.")
        return 1

    tracked_files = {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}
    missing_documents = sorted(REQUIRED_DOCUMENTS - tracked_files)
    if missing_documents:
        for path in missing_documents:
            print(f"필수 프로젝트 문서가 없습니다: {path}")
        return 1

    unsupported_files = sorted(
        path for path in tracked_files if not is_supported_path(path)
    )
    if unsupported_files:
        for path in unsupported_files:
            print(f"지원되지 않는 추적 파일입니다: {path}")
        print(
            "새 앱·설정·도구 파일을 추가하려면 이 검사에 해당 경로와 "
            "실제 검증 방법을 명시적으로 등록하세요."
        )
        return 1

    link_errors: list[tuple[str, str]] = []
    repository = Path.cwd()
    for name, expected_hash in OTTER_ARTWORK_HASHES.items():
        artwork = repository / name
        if name not in tracked_files or not artwork.is_file():
            print(f"수달이 이미지 파일이 없습니다: {name}")
            return 1
        if hashlib.sha256(artwork.read_bytes()).hexdigest() != expected_hash:
            print(f"승인한 수달이 이미지와 다릅니다: {name}")
            return 1
    for markdown_path in sorted(path for path in tracked_files if path.endswith(".md")):
        link_errors.extend(
            (markdown_path, target)
            for target in missing_local_links(repository, markdown_path, tracked_files)
        )
    if link_errors:
        for markdown_path, target in link_errors:
            print(f"잘못된 Markdown 로컬 파일 링크: {markdown_path} -> {target}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
