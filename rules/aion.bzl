"""Compatibility shim — re-exports `//polyglot:aion.bzl`'s public surface.

The Aion macros + rules + aspects + providers live in
`//polyglot:aion.bzl`. This module exists so consumers that load
them as `@rules_lang//rules:aion.bzl` keep working without a path
bump.
"""

load(
    "//polyglot:aion.bzl",
    _AionEmitToolchainInfo = "AionEmitToolchainInfo",
    _AionSpecInfo = "AionSpecInfo",
    _aion_emit = "aion_emit",
    _aion_emit_toolchain = "aion_emit_toolchain",
    _aion_spec = "aion_spec",
    _aion_spec_aspect = "aion_spec_aspect",
)

aion_spec = _aion_spec
aion_emit = _aion_emit
aion_emit_toolchain = _aion_emit_toolchain
aion_spec_aspect = _aion_spec_aspect
AionSpecInfo = _AionSpecInfo
AionEmitToolchainInfo = _AionEmitToolchainInfo
