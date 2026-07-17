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
-- DELIBERATELY NO `require`s — do not add one back. This workspace exists
-- only to register a toolchain: nothing depends on `@rules_lang_lake//:<pkg>`
-- and no Lean source here imports Batteries. rules_lean handles a dep-free
-- workspace natively (emits just the toolchain and returns).
--
-- It previously carried a nominal `require batteries from git`, on the belief
-- that rules_lean needed >=1 package and that batteries cost "one cached
-- download". Both were false. rules_lean's dep-free path predates that
-- comment, and `from git` makes batteries a NON-Reservoir dep, so Lake's
-- cache skips it ("batteries: skipping non-Reservoir dependency") and
-- `allow_source_build` then compiled all ~200 Batteries modules from source
-- on every cold fetch, per output base. That was the multi-hour Lean lane in
-- consumers' CI — paid by any repo depending on rules_lang, even for targets
-- with no Lean in them at all.

package «rules-lang-lean» where
