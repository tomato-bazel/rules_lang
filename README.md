# rules_lang

Public Bazel rules for the **polyglot** universal-IR codec (`@rules_lang`).

The rule packages (`polyglot/`, `rules/`) are public; the **engine + AST source**
stay private in GitLab `aion/polyglot`. The Lean **atlas** — the compiled
`Polyglot.*` olean the rules project through — is consumed as a **prebuilt,
per-arch release asset** (`//lean:atlas`), so consumers (aion, rules_postgres)
resolve `rules_lang` anonymously: no private-source access, no per-consumer token.

## Layout
- `polyglot/aion.bzl` — `aion_spec` / `aion_emit` (a Lir spec → target source via
  the imported atlas's `OfLir.render`) + `aion_emit_toolchain`.
- `polyglot/sql.bzl` — SQL parse rules + `//polyglot/sql:postgres_toolchain_type`
  (the libpg_query impl is the consumer's, via `rules_postgres`).
- `rules/aion.bzl` — compat shim re-exporting `//polyglot:aion.bzl`.
- `lean/` — `lean_imported_library(atlas)` over the per-arch release olean +
  the pinned Lean toolchain hook.

## The atlas
Built by the private engine's release CI and attached to `atlas-v<ver>` releases
here as `polyglot_atlas-<os>_<arch>.tar.gz`. `//lean:atlas.bzl` http_archives the
arch-matching asset; `//lean:atlas` exposes it as `LeanInfo` with no recompile.

> Bootstrap status: olean-only core (the `aion`/`sql` rules + the atlas import).
> The tool-invoking rules (`rules/typescript`, `rules/c`) are not yet published.
