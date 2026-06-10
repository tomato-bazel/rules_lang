"""Polyglot.Aion — spec → multi-language source emit pipeline (public API).

Architecture (upgraded from the initial macro to a real provider /
aspect / toolchain stack — see INTERNAL.md):

  * `aion_spec` rule — wraps a `Polyglot.Core.Lir.Module`-producing
    Lean module in an `AionSpecInfo` provider.
  * `aion_emit_toolchain` rule — defines a per-target-language
    projection (import_module, render_fn, src_files) as an
    `AionEmitToolchainInfo` provider. Repos register their own
    toolchains; no rules_lang edits needed for a new language.
  * `aion_emit` macro — composes the internal `_aion_emit_main_gen`
    rule (which reads both providers and writes the lean Main) with
    `lean_emit` (which compiles).
  * `aion_spec_aspect` — propagates `AionSpecInfo` through dep
    graphs for additional consumers (doc-gen, fixture-validation,
    metrics).

Default usage (point at a registered toolchain by label):

    load("@rules_lang//rules:aion.bzl", "aion_spec", "aion_emit")

    aion_spec(
        name   = "logger_spec",
        srcs   = [
            "LoggerSpec.lean",                    # builds loggerModule
            "LoggerEmit/LoggerEmit.lean",         # catalog
        ],
        module = "Aion.V0.Logger.LoggerSpec",
        symbol = "loggerModule",
    )

    aion_emit(
        name   = "logger_ts",
        spec   = ":logger_spec",
        target = "@rules_lang//polyglot:typescript_aion_emit_toolchain",
        out    = "logger.ts",
        deps   = LAKE_PACKAGES,
    )

Shortcut: `target` accepts the bare language name `"typescript"` /
`"sql"` / `"python"` / `"rust"` and resolves to the corresponding
`@rules_lang//polyglot:<lang>_aion_emit_toolchain` label.
Custom toolchains use an explicit label.

Adding a new target language (in any repo):

    aion_emit_toolchain(
        name = "haskell_aion_emit_toolchain",
        language = "haskell",
        import_module = "Polyglot.Haskell",
        render_fn = "Polyglot.Haskell.OfLir.render",
        src_files = [
          "@polyglot_ast//:Polyglot/Core/Lir.lean",
          "@polyglot_ast//:Polyglot/Core.lean",
          "@polyglot_ast//:Polyglot/Haskell/OfLir.lean",
        ],
    )

Then use `target = ":haskell_aion_emit_toolchain"` (or its full
label from another package).
"""

load("@rules_lean//lean:lean.bzl", "lean_emit")
load(":aion_aspects.bzl", _aion_spec_aspect = "aion_spec_aspect")
load(
    ":aion_providers.bzl",
    _AionEmitToolchainInfo = "AionEmitToolchainInfo",
    _AionSpecInfo = "AionSpecInfo",
)
load(
    ":aion_rules.bzl",
    _aion_emit_main_gen = "aion_emit_main_gen",
    _aion_emit_toolchain = "aion_emit_toolchain",
    _aion_spec = "aion_spec",
)

# Re-export the rule, aspect, and provider names so consumers can
# load them all from this one .bzl file.
aion_spec = _aion_spec
aion_emit_toolchain = _aion_emit_toolchain
aion_spec_aspect = _aion_spec_aspect
AionSpecInfo = _AionSpecInfo
AionEmitToolchainInfo = _AionEmitToolchainInfo

# Bare-language-name shortcut → registered toolchain label. Update
# this dict when a new toolchain instance lands at
# @rules_lang//polyglot:<lang>_aion_emit_toolchain. Custom
# toolchains skip this dict and pass an explicit label.
_BARE_LANGUAGE_SHORTCUTS = {
    "typescript": "@rules_lang//polyglot:typescript_aion_emit_toolchain",
    "sql": "@rules_lang//polyglot:sql_aion_emit_toolchain",
    "python": "@rules_lang//polyglot:python_aion_emit_toolchain",
    "rust": "@rules_lang//polyglot:rust_aion_emit_toolchain",
}

def aion_emit(name, spec, target, out, deps = None, visibility = None):
    """Project a Lir-spec target to source via the toolchain's lowering.

    Args:
      name: target name. The lean compilation runs as
        `<name>` (a `lean_emit` underneath); the Main-generation
        step is `<name>_main_gen` (private intermediate).
      spec: label of an `aion_spec` target (provides `AionSpecInfo`).
      target: either a bare language name (`"typescript"`, `"sql"`,
        `"python"`, `"rust"`) — resolved to the registered toolchain
        at `@rules_lang//polyglot:<lang>_aion_emit_toolchain` — or
        an explicit label of an `aion_emit_toolchain` target.
      out: emitted source filename (e.g. `"logger.ts"`).
      deps: lean_emit deps (typically LAKE_PACKAGES).
      visibility: forwarded to the emitted lean_emit target.
    """
    toolchain_label = _BARE_LANGUAGE_SHORTCUTS.get(target, target)
    main_gen_name = name + "_main_gen"

    _aion_emit_main_gen(
        name = main_gen_name,
        spec = spec,
        toolchain = toolchain_label,
        visibility = ["//visibility:private"],
    )

    # main_gen's DefaultInfo carries the Main.lean file plus the
    # spec + toolchain srcs in dependency order. lean_emit consumes
    # them as srcs. The entry path matches the Main file's
    # package-relative path.
    lean_emit(
        name = name,
        srcs = [":" + main_gen_name],
        entry = main_gen_name + ".lean",
        out = out,
        # The toolchain forwards the imported atlas as LeanInfo — deps on it so
        # the lowering oleans (Polyglot.<Lang>.OfLir / Render / Core.Lir) are on
        # the LEAN_PATH without recompiling engine source.
        deps = (deps or []) + [toolchain_label],
        visibility = visibility,
    )
