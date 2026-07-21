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
_BASE = "https://github.com/tomato-bazel/rules_lang/releases/download/atlas-v0.3.3"

def _atlas_ext_impl(_ctx):
    http_archive(
        name = "polyglot_atlas_linux_x86_64",
        url = _BASE + "/polyglot_atlas-linux_x86_64.tar.gz",
        sha256 = "32c793f93ffdf913593b959170e688774a288a40fa743aa460f9c7498969995e",
        build_file_content = _ATLAS_BUILD,
    )
    http_archive(
        name = "polyglot_atlas_darwin_arm64",
        url = _BASE + "/polyglot_atlas-darwin_arm64.tar.gz",
        sha256 = "f3fa16a8a1a9c95259aeb28a1eec22c906d428f9b70345a88c09d7e99cbbe14c",
        build_file_content = _ATLAS_BUILD,
    )

atlas_ext = module_extension(implementation = _atlas_ext_impl)
