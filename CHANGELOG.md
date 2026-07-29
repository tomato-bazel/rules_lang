# Changelog

All notable changes to rules_lang. The format is loosely
[Keep a Changelog](https://keepachangelog.com/) — version headers mirror the
published bazel-registry entries.

## 0.5.0 — the Lean toolchain registration is dev-scoped (consumers stop paying for it)

`register_toolchains("@rules_lang_lake//:lean_toolchain_def")` was at module
scope, non-dev. Registration is EAGER: Bazel loads a registered toolchain's
package to read its `toolchain_type`, and loading that package runs the
`lake_workspace` repository rule. Non-dev, that propagates to every transitive
consumer — so a repo that takes rules_lang as a `bazel_dep` materialized
`@rules_lang_lake` (Lean toolchain download + extraction) during ANALYSIS of
targets with no Lean anywhere in their graph, including pure-Rust ones. There is
no consumer-side escape: a `--config=lean`-style opt-in cannot suppress a
registration made inside a dependency module.

It is now `dev_dependency = True`. Nothing else changes:

- **rules_lang's own Lean targets are unaffected.** Dev registrations still apply
  when the module is ROOT, which is the case for this repo's own builds and CI.
  `//smoke` (a `lean_library` against `//lean:atlas`) resolves the toolchain
  exactly as before.
- **No consumer relied on it.** Every downstream repo with Lean targets already
  registers its own lake workspace's toolchain — rules_texlive
  (`@cweb_lean_ws//:lean_toolchain_def`), agentic_ide_runtime and agora
  (`@lake_deps//:lean_toolchain_def`), aion (`--extra_toolchains=@lake_deps//:
  lean_toolchain_def` under `--config=lean`). None of them reach for
  `@rules_lang_lake`.

Minor rather than patch because this removes a toolchain from rules_lang's
transitive surface. If a consumer *was* silently resolving `@rules_lang_lake`'s
Lean toolchain for its own `lean_*` targets, it will now fail toolchain
resolution and must register its own — which is the correct outcome, since
rules_lang's toolchain is pinned to rules_lang's `lean-toolchain`, not the
consumer's.

## 0.4.0 — cross-repo emit boundary

- Adds the `polyglot.emit.v1` emit boundary: `//proto/{lir,lir_codec,emit}.proto`
  + `//tools/emit:manifest_packer` (the Python packer) + the
  `//tools/emit:emit_manifest.bzl` `polyglot_emit_manifest` rule. A producer
  (e.g. `aion/lean`) renders source Lean-side via the atlas
  (`//polyglot:typescript_aion_emit_toolchain`) and packs it into a
  `TranslationManifest` binpb; a consumer (`aion/lift`'s `aion_ts_package`,
  `aion/sql`) decodes + assembles it with no Lean toolchain and no second
  renderer. Adds `rules_proto` + `protobuf` deps.

## 0.3.0 — atlas-v0.3.1: Syntax precedence kernel

- `//lean:atlas` now bundles the generic `Syntax.Expr` AST + the `Syntax.Prec`
  operator-precedence engine, alongside the existing Core/Lir, Sql, Typescript,
  Java, Wasm, Yaml. Consumers (e.g. `rules_texlive`'s Pascal-H parser) reach it
  via `deps = ["@rules_lang//lean:atlas"]`. Points `//lean:atlas.bzl` at the
  `atlas-v0.3.1` release (both per-arch tarballs verified to contain
  `Syntax/{Expr,Prec}.olean`); `//smoke` now gates the Syntax load.

## 0.2.0 — //rules/c rule definitions

- Ports the `//rules/c` C/clang AST-dump + struct-diff + LLVM-IR rule defs
  (`c_ast_dump_single`, `c_ast_struct_diff_test_suite`, `rust_llvm_ir_single`)
  plus the `cli/` python helpers, so consumers (e.g. `rules_postgres` Gate 3)
  resolve the loads without private-engine access. The heavy Rust diff/IR tools
  (`//crates/pipeline`) stay private in `aion/polyglot` — only the rule defs live
  here, as lazy default-attr labels. Adds `rules_cc` + `rules_python` deps and
  makes `rules_shell` non-dev.

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
