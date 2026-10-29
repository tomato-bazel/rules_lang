# rules_lang

Public Bazel rules for Polyglot language tooling. The canonical engine and AST
source remain private in `mattmarshall/polyglot`. Consumers load compiled Lean
atlas artifacts from this public repository; they need no private source token.

## Use the published module

Add `bazel_dep(name = "rules_lang", version = "0.5.0")` to `MODULE.bazel` and use
these registries in the workspace `.bazelrc`:

```text
common --registry=https://registry.tbzl.dev/
common --registry=https://bcr.bazel.build/
```

The wrapper pins atlas `0.3.3` and its Lean version. Supported atlas platforms are
Linux x86_64 and macOS arm64. Linux arm64 is explicitly unsupported. Changing the
Lean toolchain independently can make the imported `.olean` files unloadable.

## Packages

- `polyglot/aion.bzl`: `aion_spec`, `aion_emit`, and emit toolchains for
  specification-to-source generation through the compiled atlas.
- `polyglot/sql.bzl`: SQL parse rules and the PostgreSQL toolchain type; consumers
  provide their parser implementation through `rules_postgres`.
- `rules/aion.bzl`: compatibility entry point for existing users.
- `rules/c/`: C AST, LLVM-IR, and comparison rule definitions. Instantiating tool
  rules also requires the corresponding producer-provided executables; the public
  definitions do not disclose or package every private Rust tool.
- `proto/` and `tools/emit/`: protobuf interchange and emit-manifest packaging.
- `lean/`: imported atlas library and matching Lean toolchain configuration.

These are engineering interfaces. A syntactic round trip or successful emission
is not by itself a proof of equivalent program behavior.

## Build and check

```sh
bazel build //smoke/...
bazel test //docs/... --test_output=errors
```

The smoke target is a Lean library: compilation verifies that the released atlas
can be imported. The docs tests check generated reference-document drift. Use
`bazel run //docs:update` after changing documented rule interfaces. A cold root
build downloads the Lean toolchain, even for documentation; consumer modules
avoid this eager root-development registration cost.

## Release boundary

The private source's `.github/workflows/atlas-release.yml` builds and validates
both native archives, tests them through this wrapper, and uploads the complete
pair plus protobuf provenance here. Verification tags remain drafts. Public
version tags require successful source CI and main-branch ancestry.

An atlas upload does not update this module. Update `lean/atlas.bzl` URLs and both
SHA256 checksums together, keep its Lean toolchain compatible, and run native
smoke builds before publishing a new wrapper version through tbzl. Never replace
published archive bytes or rewrite an existing registry version. The source
repository, atlas tag, wrapper version, and registry entry are separate identities.
