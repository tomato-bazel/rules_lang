"""Module extension: fetch the per-arch polyglot atlas olean from this repo's
GitHub release assets.

The private mattmarshall/polyglot GitHub Actions release workflow builds `polyglot_atlas-<os>_<arch>.tar.gz` and attaches it to the matching
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

# atlas-v0.3.4 publishes native artifacts from mattmarshall/polyglot with
# source-commit, compiler, platform, and checksum provenance in its manifest.
#
# atlas-v0.3.3 adds Polyglot.RoundTrip to the bundle, alongside the generic
# Syntax precedence kernel (Syntax.Expr + Syntax.Prec, added in v0.3.1) and
# Core/Lir, Sql, Typescript, Java, Wasm, Yaml. Both per-arch tarballs contain
# Syntax/{Expr,Prec}.olean and Polyglot/RoundTrip.olean (33 entries each).
#
# RoundTrip is here so consumers can state theorems against
# Polyglot.RoundTrip.Strict WITHOUT depending on the private polyglot source
# module — agentic_ide_runtime's L0 acceptance gate is the first consumer.
#
# The URL names tomato-bazel explicitly. It used to say fastverk/rules_lang,
# which still REDIRECTS to here, but relying on that is fragile: the moment
# anyone creates a new repo at fastverk/rules_lang the redirect disappears and
# this would silently point at a different repository.
_BASE = "https://github.com/tomato-bazel/rules_lang/releases/download/atlas-v0.3.4"

def _atlas_ext_impl(_ctx):
    http_archive(
        name = "polyglot_atlas_linux_x86_64",
        url = _BASE + "/polyglot_atlas-linux_x86_64.tar.gz",
        sha256 = "4774e635fc27a010dd9a8b24c745b84c56da60bd01016d97ed05567675fa0b2b",
        build_file_content = _ATLAS_BUILD,
    )
    http_archive(
        name = "polyglot_atlas_darwin_arm64",
        url = _BASE + "/polyglot_atlas-darwin_arm64.tar.gz",
        sha256 = "d7e59584bfbed99eeae7a2254c3cc137cfaf835785a9a2d5c14d61cdb02ad801",
        build_file_content = _ATLAS_BUILD,
    )

atlas_ext = module_extension(implementation = _atlas_ext_impl)
