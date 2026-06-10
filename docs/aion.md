<!-- Generated with Stardoc: http://skydoc.bazel.build -->

Polyglot.Aion — spec → multi-language source emit pipeline (public API).

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

<a id="aion_emit_toolchain"></a>

## aion_emit_toolchain

<pre>
load("@rules_lang//polyglot:aion.bzl", "aion_emit_toolchain")

aion_emit_toolchain(<a href="#aion_emit_toolchain-name">name</a>, <a href="#aion_emit_toolchain-atlas">atlas</a>, <a href="#aion_emit_toolchain-import_module">import_module</a>, <a href="#aion_emit_toolchain-language">language</a>, <a href="#aion_emit_toolchain-render_fn">render_fn</a>)
</pre>

Defines a per-target-language projection for `aion_emit`. Each
language registers one of these; `aion_emit(target = ":<tc>", ...)`
consumes it via the provider. Adding a new target language is a
new `aion_emit_toolchain` declaration — no rules_lang edits needed.

**ATTRIBUTES**


| Name  | Description | Type | Mandatory | Default |
| :------------- | :------------- | :------------- | :------------- | :------------- |
| <a id="aion_emit_toolchain-name"></a>name |  A unique name for this target.   | <a href="https://bazel.build/concepts/labels#target-names">Name</a> | required |  |
| <a id="aion_emit_toolchain-atlas"></a>atlas |  The imported polyglot atlas (a lean_imported_library over the prebuilt olean): provides Core.Lir + the language's Render/OfLir as compiled oleans, no source recompile. Forwarded as LeanInfo so aion_emit's lean_emit deps on it.   | <a href="https://bazel.build/concepts/labels">Label</a> | required |  |
| <a id="aion_emit_toolchain-import_module"></a>import_module |  Lean module the generated Main imports to reach the lowering (e.g. `Polyglot.Typescript`).   | String | required |  |
| <a id="aion_emit_toolchain-language"></a>language |  Human-readable language identifier (e.g. `typescript`, `sql`).   | String | required |  |
| <a id="aion_emit_toolchain-render_fn"></a>render_fn |  Fully-qualified Lean function name `Lir.Module → String` (e.g. `Polyglot.Typescript.OfLir.render`).   | String | required |  |


<a id="aion_spec"></a>

## aion_spec

<pre>
load("@rules_lang//polyglot:aion.bzl", "aion_spec")

aion_spec(<a href="#aion_spec-name">name</a>, <a href="#aion_spec-srcs">srcs</a>, <a href="#aion_spec-module">module</a>, <a href="#aion_spec-symbol">symbol</a>)
</pre>

Wraps a set of Lean files producing a `Polyglot.Core.Lir.Module` into
an `AionSpecInfo`-bearing target. Consumers like `aion_emit`,
doc-gen, fixture-validation, etc. read the provider.

**ATTRIBUTES**


| Name  | Description | Type | Mandatory | Default |
| :------------- | :------------- | :------------- | :------------- | :------------- |
| <a id="aion_spec-name"></a>name |  A unique name for this target.   | <a href="https://bazel.build/concepts/labels#target-names">Name</a> | required |  |
| <a id="aion_spec-srcs"></a>srcs |  Lean files. Must include the module defining the spec_symbol Lir.Module value and any catalog modules it imports.   | <a href="https://bazel.build/concepts/labels">List of labels</a> | required |  |
| <a id="aion_spec-module"></a>module |  Fully-qualified Lean module path housing the Lir.Module value (e.g. `Aion.V0.Logger.LoggerSpec`).   | String | required |  |
| <a id="aion_spec-symbol"></a>symbol |  Name of the Lir.Module value within that module (e.g. `loggerModule`).   | String | required |  |


<a id="AionEmitToolchainInfo"></a>

## AionEmitToolchainInfo

<pre>
load("@rules_lang//polyglot:aion.bzl", "AionEmitToolchainInfo")

AionEmitToolchainInfo(<a href="#AionEmitToolchainInfo-import_module">import_module</a>, <a href="#AionEmitToolchainInfo-render_fn">render_fn</a>, <a href="#AionEmitToolchainInfo-language">language</a>)
</pre>

Toolchain config for projecting a Lir.Module to one specific target language.

**FIELDS**

| Name  | Description |
| :------------- | :------------- |
| <a id="AionEmitToolchainInfo-import_module"></a>import_module |  Lean module the generated Main imports to reach the lowering (e.g. `Polyglot.Typescript`).    |
| <a id="AionEmitToolchainInfo-render_fn"></a>render_fn |  fully-qualified Lean function name `Lir.Module → String` (e.g. `Polyglot.Typescript.OfLir.render`).    |
| <a id="AionEmitToolchainInfo-language"></a>language |  human-readable language identifier (e.g. `typescript`, `sql`) — surfaced in progress messages.    |


<a id="AionSpecInfo"></a>

## AionSpecInfo

<pre>
load("@rules_lang//polyglot:aion.bzl", "AionSpecInfo")

AionSpecInfo(<a href="#AionSpecInfo-srcs">srcs</a>, <a href="#AionSpecInfo-module">module</a>, <a href="#AionSpecInfo-symbol">symbol</a>)
</pre>

Information about an aion_spec target — a Lean module producing a Polyglot.Core.Lir.Module.

**FIELDS**

| Name  | Description |
| :------------- | :------------- |
| <a id="AionSpecInfo-srcs"></a>srcs |  depset of .lean files (the spec module + any catalog modules it imports).    |
| <a id="AionSpecInfo-module"></a>module |  fully-qualified Lean module path housing the Lir.Module value (e.g. `Aion.V0.Logger.LoggerSpec`).    |
| <a id="AionSpecInfo-symbol"></a>symbol |  name of the Lir.Module value within that module (e.g. `loggerModule`).    |


<a id="aion_emit"></a>

## aion_emit

<pre>
load("@rules_lang//polyglot:aion.bzl", "aion_emit")

aion_emit(<a href="#aion_emit-name">name</a>, <a href="#aion_emit-spec">spec</a>, <a href="#aion_emit-target">target</a>, <a href="#aion_emit-out">out</a>, <a href="#aion_emit-deps">deps</a>, <a href="#aion_emit-visibility">visibility</a>)
</pre>

Project a Lir-spec target to source via the toolchain's lowering.

**PARAMETERS**


| Name  | Description | Default Value |
| :------------- | :------------- | :------------- |
| <a id="aion_emit-name"></a>name |  target name. The lean compilation runs as `<name>` (a `lean_emit` underneath); the Main-generation step is `<name>_main_gen` (private intermediate).   |  none |
| <a id="aion_emit-spec"></a>spec |  label of an `aion_spec` target (provides `AionSpecInfo`).   |  none |
| <a id="aion_emit-target"></a>target |  either a bare language name (`"typescript"`, `"sql"`, `"python"`, `"rust"`) — resolved to the registered toolchain at `@rules_lang//polyglot:<lang>_aion_emit_toolchain` — or an explicit label of an `aion_emit_toolchain` target.   |  none |
| <a id="aion_emit-out"></a>out |  emitted source filename (e.g. `"logger.ts"`).   |  none |
| <a id="aion_emit-deps"></a>deps |  lean_emit deps (typically LAKE_PACKAGES).   |  `None` |
| <a id="aion_emit-visibility"></a>visibility |  forwarded to the emitted lean_emit target.   |  `None` |


<a id="aion_spec_aspect"></a>

## aion_spec_aspect

<pre>
load("@rules_lang//polyglot:aion.bzl", "aion_spec_aspect")

aion_spec_aspect()
</pre>

Walks a target's deps / spec / specs attrs collecting `AionSpecInfo`
records. Adds an `AionSpecCollection` provider with a depset of all
specs reachable through that subgraph.

Apply to rule attrs via `attr.label(aspects = [aion_spec_aspect])`
in a consumer rule's definition. Then inside that rule's impl,
read `ctx.attr.<edge>[AionSpecCollection].specs` to iterate the
specs.

**ASPECT ATTRIBUTES**


| Name | Type |
| :------------- | :------------- |
| deps| String |
| spec| String |
| specs| String |


**ATTRIBUTES**



