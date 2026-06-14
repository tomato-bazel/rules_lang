"""Module extension: fetch the per-arch polyglot atlas olean from this repo's
GitHub release assets.

The private engine's release CI (gitlab aion/polyglot, `publish_atlas_olean`)
builds `polyglot_atlas-<os>_<arch>.tar.gz` and attaches it to the matching
`atlas-v<ver>` release here. `//lean:atlas` (a lean_imported_library) selects
the arch-matching archive — no engine source, no recompile.
"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

_ATLAS_BUILD = """exports_files([".lean_root"])

filegroup(
    name = "all",
    srcs = glob(["**"]),
    visibility = ["//visibility:public"],
)
"""

# atlas-v0.3.1 adds the generic Syntax precedence kernel (Syntax.Expr +
# Syntax.Prec) to the bundle, alongside Core/Lir, Sql, Typescript, Java, Wasm,
# Yaml. Both per-arch tarballs contain Syntax/{Expr,Prec}.olean.
_BASE = "https://github.com/fastverk/rules_lang/releases/download/atlas-v0.3.1"

def _atlas_ext_impl(_ctx):
    http_archive(
        name = "polyglot_atlas_linux_x86_64",
        url = _BASE + "/polyglot_atlas-linux_x86_64.tar.gz",
        sha256 = "3db0d0108edcfd52d74a007c635d605e46b20bcacbcebe5eb1847ea50ba4d3a6",
        build_file_content = _ATLAS_BUILD,
    )
    http_archive(
        name = "polyglot_atlas_darwin_arm64",
        url = _BASE + "/polyglot_atlas-darwin_arm64.tar.gz",
        sha256 = "bb53a9abe0cdb13fdb67c32d2fefdfd6a5043bda25c179b4c6df129710e1356e",
        build_file_content = _ATLAS_BUILD,
    )

atlas_ext = module_extension(implementation = _atlas_ext_impl)
