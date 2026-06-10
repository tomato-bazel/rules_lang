"""Polyglot.Aion — provider definitions.

Two providers anchor the aion_emit pipeline:

  * `AionSpecInfo` — produced by the `aion_spec` rule. Carries the
    Lean files that define a `Polyglot.Core.Lir.Module` (the spec
    srcs) plus the fully-qualified module path + symbol name of
    the Lir.Module value. Any consumer that wants to project a
    spec to source consumes this provider.

  * `AionEmitToolchainInfo` — produced by the `aion_emit_toolchain`
    rule. Carries everything an `aion_emit` invocation needs to
    project a Lir.Module into one specific target language: the
    `Polyglot.<Lang>.OfLir` module to import, the render function
    name (`Polyglot.<Lang>.OfLir.render`), and the polyglot_ast
    Lean files the projection depends on.

The provider-based shape replaces the macro's hardcoded
`_TARGET_REGISTRY` dict: new languages register an
`aion_emit_toolchain` target (in their own repo, no rules_lang
edit needed), and `aion_emit` consumes it via a label attr.

Future aspect-based consumers (doc-gen, fixture-validation,
metrics) read `AionSpecInfo` from `aion_spec` targets via the
`aion_spec_aspect`, without re-instantiating the spec.
"""

AionSpecInfo = provider(
    doc = "Information about an aion_spec target — a Lean module producing a Polyglot.Core.Lir.Module.",
    fields = {
        "srcs": "depset of .lean files (the spec module + any catalog modules it imports).",
        "module": "fully-qualified Lean module path housing the Lir.Module value (e.g. `Aion.V0.Logger.LoggerSpec`).",
        "symbol": "name of the Lir.Module value within that module (e.g. `loggerModule`).",
    },
)

AionEmitToolchainInfo = provider(
    doc = "Toolchain config for projecting a Lir.Module to one specific target language.",
    fields = {
        "import_module": "Lean module the generated Main imports to reach the lowering (e.g. `Polyglot.Typescript`).",
        "render_fn": "fully-qualified Lean function name `Lir.Module → String` (e.g. `Polyglot.Typescript.OfLir.render`).",
        "language": "human-readable language identifier (e.g. `typescript`, `sql`) — surfaced in progress messages.",
    },
)
