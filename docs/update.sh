#!/usr/bin/env bash
# Regenerate docs/aion.md and docs/sql.md from stardoc output. Run after
# changing rule docstrings. Invoked via `bazel run //docs:update`.
set -euo pipefail

if [[ -z "${BUILD_WORKSPACE_DIRECTORY:-}" ]]; then
  echo "error: must be invoked via 'bazel run //docs:update'" >&2
  exit 1
fi

RUNFILES_DIR="${RUNFILES_DIR:-$0.runfiles}"
AION_GEN="$(find "$RUNFILES_DIR" -name aion.md.generated -print -quit)"
SQL_GEN="$(find "$RUNFILES_DIR" -name sql.md.generated -print -quit)"

cp "$AION_GEN" "$BUILD_WORKSPACE_DIRECTORY/docs/aion.md"
cp "$SQL_GEN" "$BUILD_WORKSPACE_DIRECTORY/docs/sql.md"

echo "docs/aion.md and docs/sql.md regenerated."
