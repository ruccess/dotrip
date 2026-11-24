#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

base=${1:-HEAD}
if [[ "$base" == 0000000000000000000000000000000000000000 ]]; then
  base=$(git hash-object -w -t tree /dev/null)
fi
git diff --check "$base" --
python3 scripts/check_repo.py

ruff check scripts tests
ruff format --check scripts tests
mypy --check-untyped-defs scripts
actionlint
while IFS= read -r -d '' script; do
  bash -n "$script"
  shellcheck "$script"
done < <(git ls-files -z -- 'scripts/*.sh' .githooks/pre-push)

ruby -ryaml -e '
  form = YAML.safe_load_file(ARGV.fetch(0))
  %w[name description body].each { |key| form.fetch(key) }
  fields = form.fetch("body").select { |field| field["type"] == "textarea" && field.dig("validations", "required") == true }
  labels = fields.map { |f| f.fetch("attributes").fetch("label") }
  missing = ["사용자 행동", "완료 조건"] - labels
  abort "Missing required issue fields: #{missing.join(", ")}" unless missing.empty?
' .github/ISSUE_TEMPLATE/work-item.yml

python3 -c '
import sys, unittest
loader = unittest.defaultTestLoader
for name in ("check_repo", "check_pr", "merge_checked"):
    if not loader.discover("tests", pattern=f"test_{name}.py").countTestCases():
        sys.exit(f"No regression tests discovered for {name}")
suite = loader.discover("tests")
sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
'
