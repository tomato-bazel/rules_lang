"""Bazel rules for ingesting C source into JSON AST artifacts and
turning those into RDF graphs conforming to ontology/c.ttl.

Currently exposes:

  c_ast_dump_single  -- dump one .c file with explicit -I / -D flags
                        and copts. Output: <name>.ast.json (raw
                        clang -ast-dump=json stdout).

  c_ast_to_rdf       -- consume a c_ast_dump_single output and emit
                        a Turtle (.ttl) graph at mid-depth schema.
                        Output: <name>.ttl. Pairs with rules_jena /
                        rules_rdf consumers (rdf_dataset, jena_model,
                        sparql_query_test).

Future:

  c_ast_dump_from_compdb  -- ingest compile_commands.json (produced
                              by bear / meson / CMake) and dump
                              matching TUs. The real-world ingestion
                              path; see INTERNAL.md.

Trust boundary: the clang used here is whatever
@bazel_tools//tools/cpp:current_cc_toolchain resolves to. With
toolchains_llvm registered in MODULE.bazel that's a pinned LLVM
release; on dev machines without it, the system clang. The Lean
side never parses C — it only consumes the JSON these rules emit.
"""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@rules_cc//cc:action_names.bzl", "C_COMPILE_ACTION_NAME")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

def _c_ast_dump_single_impl(ctx):
    cc_toolchain = find_cc_toolchain(ctx)
    feature_configuration = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    clang = cc_common.get_tool_for_action(
        feature_configuration = feature_configuration,
        action_name = C_COMPILE_ACTION_NAME,
    )

    src = ctx.file.src
    output = ctx.actions.declare_file(ctx.attr.name + ".ast.json")

    flags = ["-Xclang", "-ast-dump=json", "-fsyntax-only"]
    flags += ["-I" + i for i in ctx.attr.includes]

    # Pick up include dirs + headers from cc_library deps. The
    # CcInfo.compilation_context exposes the include depsets that
    # Bazel uses for its own cc_library compile actions; the path
    # strings already account for sandbox-relative repo prefixes
    # (e.g. `external/+pg+postgres_src/src/include`).
    dep_inputs = []
    for dep in ctx.attr.deps:
        if CcInfo in dep:
            cc_info = dep[CcInfo]
            cc_ctx = cc_info.compilation_context
            dep_inputs.append(cc_ctx.headers)
            for inc in cc_ctx.includes.to_list():
                flags.append("-I" + inc)
            for inc in cc_ctx.quote_includes.to_list():
                flags.append("-iquote" + inc)
            for inc in cc_ctx.system_includes.to_list():
                flags.append("-isystem" + inc)

    flags += ["-D" + d for d in ctx.attr.defines]
    flags += list(ctx.attr.copts)

    cmd_parts = [shell.quote(clang)]
    cmd_parts += [shell.quote(f) for f in flags]
    cmd_parts.append(shell.quote(src.path))
    cmd_parts += [">", shell.quote(output.path)]
    command = " ".join(cmd_parts)

    ctx.actions.run_shell(
        outputs = [output],
        inputs = depset(
            [src],
            transitive = [cc_toolchain.all_files] + dep_inputs,
        ),
        command = command,
        mnemonic = "CAstDump",
        progress_message = "Dumping C AST for %s" % src.short_path,
    )

    return [DefaultInfo(files = depset([output]))]

c_ast_dump_single = rule(
    implementation = _c_ast_dump_single_impl,
    attrs = {
        "src": attr.label(
            allow_single_file = [".c"],
            mandatory = True,
            doc = "A single .c translation unit to parse.",
        ),
        "deps": attr.label_list(
            providers = [CcInfo],
            doc = "cc_library deps whose include dirs + headers are made available to the AST dump. Preferred over the string-only `includes` attr for header trees that live in external repos.",
        ),
        "includes": attr.string_list(
            default = [],
            doc = "Raw include directories passed as `-I<dir>`. Use cc_library `deps` for headers from external repos when possible.",
        ),
        "defines": attr.string_list(
            default = [],
            doc = "Preprocessor defines passed as `-D<name>` or `-D<name>=<value>`.",
        ),
        "copts": attr.string_list(
            default = [],
            doc = "Extra compiler flags passed through verbatim.",
        ),
    },
    toolchains = use_cc_toolchain(),
    fragments = ["cpp"],
    doc = "Dump a single .c file as JSON AST using clang -ast-dump=json.",
)

# =============================================================================
# c_llvm_ir_dump: clang -S -emit-llvm. Prerequisite for the C→Rust
# translation pipeline (see project_c_to_rust_translation memory) —
# producing LLVM IR from C is one half of the LLVM-IR-equivalence
# verification story; rustc --emit=llvm-ir is the other.
# =============================================================================

def _c_llvm_ir_dump_impl(ctx):
    cc_toolchain = find_cc_toolchain(ctx)
    feature_configuration = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    clang = cc_common.get_tool_for_action(
        feature_configuration = feature_configuration,
        action_name = C_COMPILE_ACTION_NAME,
    )

    src = ctx.file.src
    output = ctx.actions.declare_file(ctx.attr.name + ".ll")

    # -S -emit-llvm: produce textual LLVM IR (.ll) rather than .bc.
    # -O0 by default keeps the IR readable + structurally aligned with
    # the source — what we want for translation-equivalence work.
    # Optimization passes intentionally pushed to the consumer (via
    # `opt_level`) since equivalence checks at different opt levels
    # exercise different equivalence properties.
    flags = ["-S", "-emit-llvm"]
    flags.append("-O" + ctx.attr.opt_level)
    flags += ["-I" + i for i in ctx.attr.includes]
    flags += ["-D" + d for d in ctx.attr.defines]
    flags += list(ctx.attr.copts)

    cmd_parts = [shell.quote(clang)]
    cmd_parts += [shell.quote(f) for f in flags]
    cmd_parts.append(shell.quote(src.path))
    cmd_parts += ["-o", shell.quote(output.path)]
    command = " ".join(cmd_parts)

    ctx.actions.run_shell(
        outputs = [output],
        inputs = depset([src], transitive = [cc_toolchain.all_files]),
        command = command,
        mnemonic = "CLlvmIrDump",
        progress_message = "Emitting LLVM IR for %s" % src.short_path,
    )

    return [DefaultInfo(files = depset([output]))]

c_llvm_ir_dump = rule(
    implementation = _c_llvm_ir_dump_impl,
    attrs = {
        "src": attr.label(
            allow_single_file = [".c"],
            mandatory = True,
            doc = "A single .c translation unit to compile to LLVM IR.",
        ),
        "includes": attr.string_list(
            default = [],
            doc = "Include directories passed as `-I<dir>`.",
        ),
        "defines": attr.string_list(
            default = [],
            doc = "Preprocessor defines passed as `-D<name>` (or `-D<name>=<v>`).",
        ),
        "copts": attr.string_list(
            default = [],
            doc = "Extra compiler flags passed through verbatim.",
        ),
        "opt_level": attr.string(
            default = "0",
            values = ["0", "1", "2", "3", "s", "z"],
            doc = "clang -O level. Defaults to 0 (no opts) to keep IR " +
                  "structurally aligned with source for translation work.",
        ),
    },
    toolchains = use_cc_toolchain(),
    fragments = ["cpp"],
    doc = "Compile a .c file to textual LLVM IR (.ll) via clang -S -emit-llvm. " +
          "Used as the C-side input to llvm_ir_equiv_test (translation " +
          "equivalence verification; see project_c_to_rust_translation " +
          "memory).",
)

# =============================================================================
# c_ast_to_rdf: clang JSON AST → Turtle (mid-depth schema).
# =============================================================================

def _c_ast_to_rdf_impl(ctx):
    src = ctx.file.src
    output = ctx.actions.declare_file(ctx.attr.name + ".ttl")
    source_path = ctx.attr.source_path or src.short_path

    args = ctx.actions.args()
    args.add(src.path)
    args.add(output.path)
    args.add("--source-path", source_path)

    ctx.actions.run(
        outputs = [output],
        inputs = [src],
        executable = ctx.executable._converter,
        arguments = [args],
        mnemonic = "CAstToRdf",
        progress_message = "Converting %s to RDF" % src.short_path,
    )

    return [DefaultInfo(files = depset([output]))]

c_ast_to_rdf = rule(
    implementation = _c_ast_to_rdf_impl,
    attrs = {
        "src": attr.label(
            allow_single_file = [".json"],
            mandatory = True,
            doc = "Output of a c_ast_dump_single target (a .ast.json file).",
        ),
        "source_path": attr.string(
            default = "",
            doc = "Logical source path used to construct stable IRIs " +
                  "(urn:c:fn:<source_path>#<name>). Defaults to the JSON " +
                  "artifact's short path; pass the original .c repo-relative " +
                  "path for cross-TU IRI alignment.",
        ),
        "_converter": attr.label(
            default = "//rules/c/cli:json_to_ttl",
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Convert a clang JSON AST dump to Turtle (ontology/c.ttl, mid-depth).",
)

# =============================================================================
# c_ast_dump_from_compdb: real-world ingestion via compile_commands.json.
#
# This is the rule to point at non-Bazel-built codebases (Postgres,
# Linux, sqlite, ffmpeg, ...). The upstream build's compile_commands.json
# tells us what flags each TU needs; we re-run our pinned clang with
# those flags and dump the AST. See INTERNAL.md for the workflow.
# =============================================================================

def _c_ast_dump_from_compdb_impl(ctx):
    cc_toolchain = find_cc_toolchain(ctx)
    feature_configuration = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    clang = cc_common.get_tool_for_action(
        feature_configuration = feature_configuration,
        action_name = C_COMPILE_ACTION_NAME,
    )

    out_dir = ctx.actions.declare_directory(ctx.attr.name + ".dumps")
    manifest = ctx.actions.declare_file(ctx.attr.name + ".manifest.json")

    args = ctx.actions.args()
    args.add("--compile-commands", ctx.file.compile_commands.path)
    args.add("--clang", clang)
    args.add("--out-dir", out_dir.path)
    args.add("--source-root", ctx.attr.source_root)
    args.add("--manifest", manifest.path)
    args.add("--jobs", str(ctx.attr.jobs))
    for f in ctx.attr.filter:
        # `=` form so values starting with `-` (e.g. `-foo*`) aren't
        # mistaken by argparse for separate flags.
        args.add("--filter=" + f)
    for c in ctx.attr.extra_copts:
        args.add("--extra-copt=" + c)

    extra_inputs = []
    if ctx.attr.headers_dir:
        # headers_dir is a TreeArtifact from rules_meson's meson_configure.
        # We pass the directory's path to the driver, which substitutes
        # the ${MESON_HEADERS_TREE} sentinel in compdb -I flags.
        headers_files = ctx.attr.headers_dir[DefaultInfo].files.to_list()
        if len(headers_files) != 1:
            fail("c_ast_dump_from_compdb(%s): headers_dir must resolve " % ctx.label +
                 "to exactly one TreeArtifact, got %d files" % len(headers_files))
        headers_tree = headers_files[0]
        args.add("--headers-tree", headers_tree.path)
        extra_inputs.append(headers_tree)

    inputs = depset(
        [ctx.file.compile_commands] + ctx.files.srcs + extra_inputs,
        transitive = [cc_toolchain.all_files],
    )

    # execution_requirements local=1: the Rust driver shells to cargo,
    # which needs to write its lock file inside the source workspace.
    # Bazel's sandbox blocks that. Same workaround as
    # `rust_llvm_ir_single` (local=True on its genrule). Remove once
    # the pipeline crate is built via rules_rust against a pinned
    # toolchain.
    ctx.actions.run(
        outputs = [out_dir, manifest],
        inputs = inputs,
        executable = ctx.executable._driver,
        arguments = [args],
        mnemonic = "CAstDumpCompdb",
        progress_message = "Dumping ASTs from %s" % ctx.file.compile_commands.short_path,
        execution_requirements = {"local": "1"},
    )

    return [DefaultInfo(files = depset([out_dir, manifest]))]

# =============================================================================
# c_ast_to_tensor: corpus of AST JSON → compact parallel-array binary form.
# =============================================================================

def _c_ast_to_tensor_impl(ctx):
    out_dir = ctx.actions.declare_directory(ctx.attr.name + ".tensor")

    # The `dumps` label resolves to the c_ast_dump_from_compdb rule's
    # DefaultInfo, which produces TWO files: a directory of .ast.json
    # files and a manifest. We need both; the manifest tells the
    # converter which dumps to walk.
    all_files = ctx.attr.dumps[DefaultInfo].files.to_list()
    manifest = None
    for f in all_files:
        if f.path.endswith(".manifest.json"):
            manifest = f
            break
    if manifest == None:
        fail("c_ast_to_tensor(%s): dumps label %s didn't produce " % (
            ctx.label,
            ctx.attr.dumps.label,
        ) + "a *.manifest.json output")

    args = ctx.actions.args()
    args.add("--dumps-manifest", manifest.path)
    args.add("--out-dir", out_dir.path)
    if ctx.attr.include_implicit:
        args.add("--include-implicit")

    # Same local=1 workaround as CAstDumpCompdb — see comment there.
    ctx.actions.run(
        executable = ctx.executable._converter,
        outputs = [out_dir],
        inputs = depset(all_files),
        arguments = [args],
        mnemonic = "CAstToTensor",
        progress_message = "Packing %s into tensor corpus" % manifest.short_path,
        execution_requirements = {"local": "1"},
    )

    return [DefaultInfo(files = depset([out_dir]))]

c_ast_to_tensor = rule(
    implementation = _c_ast_to_tensor_impl,
    attrs = {
        "dumps": attr.label(
            mandatory = True,
            doc = "A c_ast_dump_from_compdb (or compatible) target. Must " +
                  "produce a .manifest.json file in its outputs (listing " +
                  "the per-TU .ast.json paths) plus the dump directory.",
        ),
        "include_implicit": attr.bool(
            default = False,
            doc = "Include clang's implicit / system-header decls in the " +
                  "tensor. Default False — strips them to keep the corpus " +
                  "dense with USER code (saves ~30k nodes per TU).",
        ),
        "_converter": attr.label(
            default = "//crates/pipeline:ast_to_tensor",
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Pack a corpus of clang JSON AST dumps into GPU-friendly " +
          "parallel-array binary files. Output: a directory with " +
          "nodes/*.<dtype> + vocabulary JSONs + manifest.json describing " +
          "how to load them via numpy.fromfile(path, dtype).",
)

def _c_llvm_ir_from_compdb_impl(ctx):
    """Emit LLVM IR for every TU matching a compile_commands.json filter.

    Variant of c_ast_dump_from_compdb that emits LLVM IR (.ll) rather
    than AST JSON. Shares the same driver (dump_from_compdb.py) via its
    --mode llvm code path."""
    cc_toolchain = find_cc_toolchain(ctx)
    feature_configuration = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    clang = cc_common.get_tool_for_action(
        feature_configuration = feature_configuration,
        action_name = C_COMPILE_ACTION_NAME,
    )

    out_dir = ctx.actions.declare_directory(ctx.attr.name + ".llvm")
    manifest = ctx.actions.declare_file(ctx.attr.name + ".manifest.json")

    args = ctx.actions.args()
    args.add("--compile-commands", ctx.file.compile_commands.path)
    args.add("--clang", clang)
    args.add("--out-dir", out_dir.path)
    args.add("--source-root", ctx.attr.source_root)
    args.add("--manifest", manifest.path)
    args.add("--jobs", str(ctx.attr.jobs))
    args.add("--mode", "llvm")
    args.add("--opt-level", ctx.attr.opt_level)
    for f in ctx.attr.filter:
        args.add("--filter=" + f)
    for c in ctx.attr.extra_copts:
        args.add("--extra-copt=" + c)

    extra_inputs = []
    if ctx.attr.headers_dir:
        headers_files = ctx.attr.headers_dir[DefaultInfo].files.to_list()
        if len(headers_files) != 1:
            fail("c_llvm_ir_from_compdb(%s): headers_dir must resolve " % ctx.label +
                 "to exactly one TreeArtifact, got %d files" % len(headers_files))
        headers_tree = headers_files[0]
        args.add("--headers-tree", headers_tree.path)
        extra_inputs.append(headers_tree)

    inputs = depset(
        [ctx.file.compile_commands] + ctx.files.srcs + extra_inputs,
        transitive = [cc_toolchain.all_files],
    )

    # Same local=1 workaround as CAstDumpCompdb — see comment there.
    ctx.actions.run(
        outputs = [out_dir, manifest],
        inputs = inputs,
        executable = ctx.executable._driver,
        arguments = [args],
        mnemonic = "CLlvmIrFromCompdb",
        progress_message = "Emitting LLVM IR from %s" % ctx.file.compile_commands.short_path,
        execution_requirements = {"local": "1"},
    )

    return [DefaultInfo(files = depset([out_dir, manifest]))]

c_llvm_ir_from_compdb = rule(
    implementation = _c_llvm_ir_from_compdb_impl,
    attrs = {
        "compile_commands": attr.label(
            allow_single_file = [".json"],
            mandatory = True,
            doc = "compile_commands.json (typically from rules_meson's " +
                  "meson_configure rule).",
        ),
        "srcs": attr.label_list(
            allow_files = True,
            doc = "All C source + header files referenced from " +
                  "compile_commands.json.",
        ),
        "source_root": attr.string(default = "."),
        "filter": attr.string_list(
            default = [],
            doc = "fnmatch globs on entry['file']. Empty = all TUs.",
        ),
        "extra_copts": attr.string_list(
            default = ["-Wno-everything"],
            doc = "Flags appended to every clang invocation.",
        ),
        "headers_dir": attr.label(
            allow_files = True,
            doc = "Optional. TreeArtifact of meson-generated headers; " +
                  "${MESON_HEADERS_TREE} sentinel resolution.",
        ),
        "opt_level": attr.string(
            default = "0",
            values = ["0", "1", "2", "3", "s", "z"],
            doc = "clang -O level. Default 0 keeps IR structurally " +
                  "aligned with source for translation-equivalence work.",
        ),
        "jobs": attr.int(default = 0, doc = "0 = os.cpu_count()."),
        "_driver": attr.label(
            default = "//crates/pipeline:dump_from_compdb",
            executable = True,
            cfg = "exec",
        ),
    },
    toolchains = use_cc_toolchain(),
    fragments = ["cpp"],
    doc = "LLVM IR variant of c_ast_dump_from_compdb. Output: a " +
          "directory of .ll files (one per matching TU) + a JSON " +
          "manifest. The C-side input to llvm_ir_equiv_test (the " +
          "Rust-side comes via rules_rust's rustc + the same target " +
          "machine-emitted IR; see project_c_to_rust_translation memory).",
)

c_ast_dump_from_compdb = rule(
    implementation = _c_ast_dump_from_compdb_impl,
    attrs = {
        "compile_commands": attr.label(
            allow_single_file = [".json"],
            mandatory = True,
            doc = "The compile_commands.json file produced by bear / " +
                  "meson / cmake on the upstream codebase.",
        ),
        "srcs": attr.label_list(
            allow_files = True,
            doc = "All C source + header files referenced from " +
                  "compile_commands.json (typically `glob([...])`). " +
                  "Must include every header the TUs transitively #include.",
        ),
        "source_root": attr.string(
            default = ".",
            doc = "Path used to compute output relpaths in out_dir. " +
                  "Typically the workspace root.",
        ),
        "filter": attr.string_list(
            default = [],
            doc = "fnmatch-style globs on entry['file']. Only matching TUs " +
                  "are dumped. Empty list = dump everything.",
        ),
        "extra_copts": attr.string_list(
            default = ["-Wno-everything"],
            doc = "Flags appended to every clang invocation. Default " +
                  "suppresses diagnostics (we only care about the AST).",
        ),
        "headers_dir": attr.label(
            allow_files = True,
            doc = "Optional. A label that resolves to a single TreeArtifact " +
                  "directory containing meson-generated headers (typically " +
                  "produced by rules_meson's meson_configure rule, accessed " +
                  "via a `filegroup(srcs=[...], output_group=\"headers\")`). " +
                  "When set, the driver substitutes the ${MESON_HEADERS_TREE} " +
                  "sentinel in compile_commands.json -I flags with this " +
                  "directory's actual path. Required for codebases (like " +
                  "Postgres) whose TUs #include meson-generated headers.",
        ),
        "jobs": attr.int(
            default = 0,
            doc = "Parallelism. 0 = os.cpu_count().",
        ),
        "_driver": attr.label(
            default = "//crates/pipeline:dump_from_compdb",
            executable = True,
            cfg = "exec",
        ),
    },
    toolchains = use_cc_toolchain(),
    fragments = ["cpp"],
    doc = "Dump AST JSON for every TU in a compile_commands.json. " +
          "Output: a directory of .ast.json files + a JSON manifest.",
)

# =============================================================================
# rust_llvm_ir_single: emit LLVM IR for a single-file Rust source.
#
# Non-hermetic spike: invokes the host `rustc` via a `genrule`. The
# proper implementation should pin rustc via rules_rust or
# toolchains_llvm-aligned cargo. For the C→Rust translation gate today
# (sha2.c spike), using host rustc is adequate — the gate compares
# *structure* between the C and Rust IRs, not bit-exact bytes.
# =============================================================================

def rust_llvm_ir_single(
        name,
        src,
        opt_level = "1",
        crate_type = "lib",
        edition = "2021",
        extra_args = None,
        **kwargs):
    """Compile a single .rs file to LLVM IR.

    Args:
      name: rule label; produces `<name>.ll`.
      src: Label of a single .rs source. Should be self-contained (no
        external crate deps beyond core/alloc/std).
      opt_level: `-C opt-level=N` value. Default "1" to match the
        instruction-count envelope expected by `llvm_ir_equiv_test`;
        "0" is noisier (extra alloca/load/store).
      crate_type: `--crate-type=` value. Default "lib" (rlib).
      edition: Rust edition. Default "2021".
      extra_args: list of extra rustc flags.
      **kwargs: forwarded to the underlying `native.genrule` (visibility,
        tags, etc.).
    """
    extra = " ".join(extra_args or [])

    # `manual` by default. This is a non-hermetic spike: it shells out to a HOST
    # rustc, which a clean machine or a CI image need not have. Untagged, it gets
    # swept into `bazel build //...` and fails the whole wildcard for everyone
    # (this is exactly what has been breaking rules_postgres' //... build).
    # `manual` only removes it from WILDCARD expansion — an explicit dep, e.g.
    # llvm_ir_equiv_test naming it, still builds it. Callers can override by
    # passing their own tags.
    tags = kwargs.pop("tags", [])
    if "manual" not in tags:
        tags = list(tags) + ["manual"]

    # Prepend the typical rustup install dir to PATH so the genrule
    # finds host-installed `rustc` even under Bazel's minimal action
    # env. local=True also skips sandboxing so any other host PATH
    # entries are usable. Non-hermetic spike pattern; the proper fix
    # is rules_rust + a pinned toolchain.
    #
    # ${HOME:-} — NOT $HOME. Bazel does not put HOME in an action's env (not even
    # for local=True, which skips the sandbox but still scrubs the env), and
    # genrule commands run under `set -u`. A bare $HOME therefore aborts with
    # "HOME: unbound variable" BEFORE rustc is ever consulted — the failure looks
    # like a toolchain problem but is just an unset variable. The default keeps
    # the rustup path when HOME exists and degrades to the other PATH entries
    # when it doesn't.
    #
    # The braces are DOUBLED because this string is .format()ed below: `{HOME:-}`
    # would otherwise be parsed as a format field ("Missing argument 'HOME:-'")
    # — `{{`/`}}` are how .format() escapes a literal brace. `$$` is separately
    # how a genrule escapes a literal `$` for the shell. So `$${{HOME:-}}` here
    # renders as `${HOME:-}` in the action.
    native.genrule(
        name = name,
        srcs = [src],
        outs = [name + ".ll"],
        cmd = ("export PATH=\"$${{HOME:-}}/.cargo/bin:/usr/local/bin:$$PATH\"; " +
               "rustc --edition={edition} --crate-type={crate_type} " +
               "--emit=llvm-ir -C opt-level={opt_level} {extra} " +
               "-o $@ $(location {src})").format(
            edition = edition,
            crate_type = crate_type,
            opt_level = opt_level,
            extra = extra,
            src = src,
        ),
        message = "rustc --emit=llvm-ir " + name,
        local = True,
        tags = tags,
        **kwargs
    )

# =============================================================================
# llvm_ir_equiv_test: structural-equivalence gate between a C IR and a
# Rust IR.
#
# Wraps `c/cli/llvm_ir_equiv.py` as a py_test. The script checks each
# named public symbol exists in both IRs with matching signature and
# falls within a configurable basic-block / instruction-count envelope.
# Complements (does not replace) behavioral equivalence: pair this gate
# with FIPS-vector / unit-test runs that prove byte-identical outputs.
# =============================================================================

def llvm_ir_equiv_test(
        name,
        c_ir,
        rust_ir,
        symbols,
        instruction_tolerance = 8.0,
        bb_tolerance = 5.0,
        renames = None,
        **kwargs):
    """Run a structural LLVM IR equivalence check.

    Args:
      name: rule label.
      c_ir: Label producing a single .ll file (use `rust_llvm_ir_single`
        on the Rust side, or factor your C IR target so it produces a
        single .ll — e.g., via a `filegroup` that picks one file out of
        `c_llvm_ir_from_compdb`'s tree).
      rust_ir: Label producing a single .ll file (typically
        `rust_llvm_ir_single`).
      symbols: list of public symbol names to verify.
      instruction_tolerance: max C/Rust instruction-count ratio per
        symbol. Default 8.0 — generous to absorb Rust panic-on-overflow
        and bounds-check branches without masking missing-loop drift.
      bb_tolerance: max C/Rust basic-block count ratio. Default 5.0.
      renames: optional dict {rust_name: c_name} for symbols whose
        names differ between sides.
      **kwargs: forwarded to the underlying `sh_test` rule (tags,
        visibility, size, timeout, etc.).
    """
    args = [
        "llvm_ir_equiv",  # consumed by run.sh as the binary name
        "--c-ir",
        "$(location {})".format(c_ir),
        "--rust-ir",
        "$(location {})".format(rust_ir),
        "--instruction-tolerance",
        str(instruction_tolerance),
        "--bb-tolerance",
        str(bb_tolerance),
        "--symbols",
    ] + symbols
    if renames:
        args.append("--rename")
        args.extend(["{}={}".format(r, c) for r, c in renames.items()])

    # The Rust port of the equivalence checker lives in
    # //crates/pipeline:llvm_ir_equiv (a sh_binary that runs run.sh
    # to lazily cargo-build the Rust binary). We use run.sh directly as
    # the sh_test's srcs, with the binary name embedded in args, so the
    # test target doesn't need a separate wrapper script.
    #
    # tags=["local"]: bypass Bazel's sandbox so cargo can write its
    # `target/release/.cargo-lock` to the source workspace. Non-hermetic
    # spike pattern; same reason `rust_llvm_ir_single`'s genrule uses
    # local=True.
    tags = kwargs.pop("tags", []) + ["local"]
    sh_test(
        name = name,
        srcs = ["//crates/pipeline:run.sh"],
        data = [c_ir, rust_ir],
        args = args,
        tags = tags,
        **kwargs
    )

# =============================================================================
# c_ast_struct_diff_test: assert two clang AST JSON dumps are structurally
# equivalent for a single function. Wraps the hermetic rust_binary
# `//crates/pipeline:c_ast_struct_diff` as a Bazel test action — no
# host cargo / shell invocation needed at test time.
# =============================================================================

def _c_ast_struct_diff_test_impl(ctx):
    left = ctx.file.left
    right = ctx.file.right
    fn_name = ctx.attr.fn_name
    diff_tool = ctx.executable._diff_tool

    runner = ctx.actions.declare_file(ctx.label.name + ".sh")
    workspace = ctx.workspace_name or "_main"
    ctx.actions.write(
        output = runner,
        content = (
            "#!/usr/bin/env bash\n" +
            "set -euo pipefail\n" +
            'RUNFILES="${{RUNFILES_DIR:-${{TEST_SRCDIR:-}}}}"\n' +
            'if [ -z "$RUNFILES" ]; then RUNFILES="${{BASH_SOURCE[0]}}.runfiles"; fi\n' +
            'exec "$RUNFILES/{ws}/{tool}" --left "$RUNFILES/{ws}/{left}" --right "$RUNFILES/{ws}/{right}" --fn-name "{fn}"\n'
        ).format(
            ws = workspace,
            tool = diff_tool.short_path,
            left = left.short_path,
            right = right.short_path,
            fn = fn_name,
        ),
        is_executable = True,
    )

    runfiles = ctx.runfiles(files = [left, right, diff_tool])
    return [DefaultInfo(
        executable = runner,
        runfiles = runfiles,
    )]

c_ast_struct_diff_test = rule(
    implementation = _c_ast_struct_diff_test_impl,
    test = True,
    attrs = {
        "left": attr.label(
            allow_single_file = [".json"],
            mandatory = True,
            doc = "AST JSON for the reference side (typically real Postgres source).",
        ),
        "right": attr.label(
            allow_single_file = [".json"],
            mandatory = True,
            doc = "AST JSON for the under-test side (typically a Lean-emitted C body).",
        ),
        "fn_name": attr.string(
            mandatory = True,
            doc = "Name of the function whose AST subtree to structurally diff.",
        ),
        "_diff_tool": attr.label(
            default = "//crates/pipeline:c_ast_struct_diff",
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Pass iff the two AST JSONs are structurally equivalent for fn_name.",
)

def c_ast_struct_diff_test_suite(name, left, right, fn_names, **kwargs):
    """Generate a `c_ast_struct_diff_test` per function name in `fn_names`.

    Args:
      name: stem for generated targets — each test is
        `<name>_<fn_name>` and a `test_suite(name)` aggregates them.
      left: AST JSON label for the reference side.
      right: AST JSON label for the under-test side.
      fn_names: list of C function names whose AST subtrees to diff.
      **kwargs: forwarded to each generated test (tags, visibility,
        size, timeout, etc.).
    """
    tests = []
    for fn in fn_names:
        test_name = "{}_{}".format(name, fn)
        c_ast_struct_diff_test(
            name = test_name,
            left = left,
            right = right,
            fn_name = fn,
            **kwargs
        )
        tests.append(":" + test_name)

    native.test_suite(
        name = name,
        tests = tests,
    )
