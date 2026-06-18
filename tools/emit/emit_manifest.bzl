"""polyglot_emit_manifest — pack Lean-rendered sources into a TranslationManifest binpb.

The producer side of the cross-repo emit boundary. Each `render` is a
Lean-rendered source file (the output of a `lir_codec_emit` / `aion_emit` target
against the prebuilt atlas) tagged with its atlas `language` and the
consumer-relative `out_path`. The rule packs them — plus package identity and
topological deps — into `<name>.binpb`, a `polyglot.emit.v1.TranslationManifest`
that a consumer (aion/lift's `aion_ts_package`, aion/sql's `aion_sql_schema`)
decodes and assembles. Rendering already happened Lean-side, so no consumer needs
the Lean toolchain and there is no second renderer to drift.
"""

def _polyglot_emit_manifest_impl(ctx):
    n = len(ctx.attr.render_srcs)
    if len(ctx.attr.render_languages) != n or len(ctx.attr.render_out_paths) != n:
        fail("render_srcs, render_languages and render_out_paths must be the same length")

    out = ctx.actions.declare_file(ctx.label.name + ".binpb")

    args = ctx.actions.args()
    args.add("--package-name", ctx.attr.package_name)
    args.add("--manifest-version", ctx.attr.manifest_version)
    args.add("--source-revision", ctx.attr.source_revision)
    for dep in ctx.attr.aion_deps:
        args.add("--aion-dep", dep)

    inputs = []
    for i, target in enumerate(ctx.attr.render_srcs):
        files = target.files.to_list()
        if len(files) != 1:
            fail("each render src must produce exactly one file; %s produced %d" % (target.label, len(files)))
        src = files[0]
        inputs.append(src)
        args.add("--render", "%s:%s:%s" % (ctx.attr.render_languages[i], ctx.attr.render_out_paths[i], src.path))

    args.add("--out", out.path)

    ctx.actions.run(
        executable = ctx.executable._packer,
        arguments = [args],
        inputs = inputs,
        outputs = [out],
        mnemonic = "EmitManifest",
        progress_message = "Packing emit manifest %s" % ctx.label.name,
    )
    return [DefaultInfo(files = depset([out]))]

_polyglot_emit_manifest = rule(
    implementation = _polyglot_emit_manifest_impl,
    attrs = {
        "package_name": attr.string(mandatory = True, doc = "Consumer package name, e.g. \"@aion/logger\"."),
        "manifest_version": attr.string(default = "0.0.0", doc = "Provenance only; not the published version."),
        "source_revision": attr.string(default = "", doc = "Git revision of the producing repo."),
        "aion_deps": attr.string_list(default = [], doc = "Topological consumer deps (package names)."),
        "render_srcs": attr.label_list(allow_files = True, mandatory = True, doc = "Rendered-source targets (1 file each)."),
        "render_languages": attr.string_list(mandatory = True, doc = "Atlas language per render (TYPESCRIPT, SQL, …)."),
        "render_out_paths": attr.string_list(mandatory = True, doc = "Consumer-relative out_path per render."),
        "_packer": attr.label(
            default = "//tools/emit:manifest_packer",
            executable = True,
            cfg = "exec",
        ),
    },
)

def polyglot_emit_manifest(name, package_name, renders, **kwargs):
    """Pack rendered sources into a TranslationManifest binpb.

    Args:
      name: target name; produces `<name>.binpb`.
      package_name: consumer package name (e.g. "@aion/logger").
      renders: list of dicts {"src": <rendered-source label>, "language": "TYPESCRIPT",
        "out_path": "index.ts"}.
      **kwargs: manifest_version, source_revision, aion_deps, visibility, …
    """
    _polyglot_emit_manifest(
        name = name,
        package_name = package_name,
        render_srcs = [r["src"] for r in renders],
        render_languages = [r["language"] for r in renders],
        render_out_paths = [r["out_path"] for r in renders],
        **kwargs
    )
