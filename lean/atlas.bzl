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

# The native archives contain compiled Polyglot and Syntax modules, including
# the Rust renderer and checked-emission APIs. The public provenance manifest
# records the private source revision, Lean compiler, platforms, and checksums.
# Keep explicit tomato-bazel URLs and immutable release bytes.
_BASE = "https://github.com/tomato-bazel/rules_lang/releases/download/atlas-v0.3.5"

def _atlas_ext_impl(_ctx):
    http_archive(
        name = "polyglot_atlas_linux_x86_64",
        url = _BASE + "/polyglot_atlas-linux_x86_64.tar.gz",
        sha256 = "9cb48a7ff003b4f90c3dbdc104f8a95745c10f4e345d1ee22a7cc71cfbc5aaa0",
        build_file_content = _ATLAS_BUILD,
    )
    http_archive(
        name = "polyglot_atlas_darwin_arm64",
        url = _BASE + "/polyglot_atlas-darwin_arm64.tar.gz",
        sha256 = "901bb84966f5d7fe65ac7c0f35c3c3bc1555f3b26edebbb8c2715ab26ae36112",
        build_file_content = _ATLAS_BUILD,
    )

atlas_ext = module_extension(implementation = _atlas_ext_impl)
