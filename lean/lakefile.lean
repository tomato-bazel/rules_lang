import Lake
open Lake DSL

-- Minimal Lake workspace for rules_lean's `lake.workspace` extension
-- to register a hermetic Lean toolchain Bazel can use for
-- `lean_test` / `lean_emit` actions.
--
-- The actual Polyglot library lives in `//lean` with its own
-- lakefile.lean (used for local development via `lake build` from
-- that directory). This file is purely a Bazel-side hook.
--
-- One nominal `require` (batteries) because rules_lean's
-- `lake_workspace` extension fails analysis with zero packages —
-- the rule assumes downstream targets need something importable.
-- Batteries is small + Reservoir-backed; the cost is one cached
-- download.

package «rules-lang-lean» where

require batteries from git
  "https://github.com/leanprover-community/batteries.git" @ "v4.30.0-rc2"
