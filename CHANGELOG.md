# Changelog

All notable changes to rules_lang. The format is loosely
[Keep a Changelog](https://keepachangelog.com/) — version headers mirror the
published bazel-registry entries.

## 0.1.0 — public split: rules layer + imported atlas olean

- Initial public release. The `@rules_lang` rule layer (`polyglot/`, `rules/`) is
  public; the engine + AST source stay private in GitLab `aion/polyglot`.
- `lean_imported_library(//lean:atlas)` consumes the compiled `Polyglot.*` atlas
  olean as a **prebuilt, per-arch GitHub release asset** (`atlas-v<ver>`) — no
  engine source, no recompile; consumers resolve anonymously.
- `polyglot/aion.bzl` — `aion_spec` / `aion_emit` (a Lir spec → target source via
  the imported atlas's `OfLir.render`) + `aion_emit_toolchain`.
- `polyglot/sql.bzl` — SQL parse rules + `//polyglot/sql:postgres_toolchain_type`
  (the libpg_query impl is the consumer's, via `rules_postgres`).
- Stardoc reference docs for `aion.bzl` + `sql.bzl`, gated by `//docs` diff_tests.
