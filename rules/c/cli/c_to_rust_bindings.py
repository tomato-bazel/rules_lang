#!/usr/bin/env python3
"""Apply a C→Rust naming policy to a C-bindings TOML.

Reads the output of `c_cluster_diff.py` (C-side ident + type pairs
extracted from the AST tensor) and emits a Rust-bindings TOML keyed by
the canonical-Rust value at each hole position. The translator's CLI
then resolves these content-keyed bindings to its positional hole IDs
(`_H0`, `_H1`, …) by running its own `diff(canonical_rust, sibling_rust)`.

This script is intentionally simple and **not** a general C→Rust naming
framework. It encodes the conventions used in the SHA family of
canonicals in `rules_lang/translated/sha2/`:

  - **Function names** (`pg_<lower>_<lower>`): identity. Rust uses
    snake_case for fns; the canonical Rust source uses the same name
    as the C function (kept intentionally for ABI-compat via
    `#[no_mangle] extern "C"`).

  - **Constants referenced via DeclRefExpr** (`<lower>_<lower>` not
    starting with `pg_`, like `sha256_initial_hash_value`): convert to
    SCREAMING_SNAKE_CASE. Rust convention for `const` items.

  - **Macros / lengths** (`PG_<UPPER>_<UPPER>`, like
    `PG_SHA256_BLOCK_LENGTH`): identity. Rust convention for `const`
    items matches the C macro casing exactly.

  - **Struct types** (extracted from qualTypes like `pg_sha256_ctx *`):
    strip `pg_` prefix, drop `_ctx`/`_state` suffix, Pascal-case the
    rest → `Sha256Ctx`. Override table can collapse aliases (e.g.,
    `pg_sha224_ctx → Sha256Ctx` because Rust reuses one struct).

  - **Primitive type renames** (`uint32 → u32`, `uint64 → u64`,
    `uint8 → u8`): hand-mapped.

  - **Structural holes**: not handled here — the C-side diff captures
    them as `[[structural_hole]]` blocks for human review.

Per-cluster overrides live in a TOML file passed via `--overrides`.

Input:  C-bindings TOML on stdin (or via --input)
Output: Rust-bindings TOML on stdout

  [bindings]
  "pg_sha256_init" = "pg_sha384_init"
  "Sha256Ctx" = "Sha512Ctx"
  "SHA256_INITIAL_HASH_VALUE" = "SHA384_INITIAL_HASH_VALUE"
  "PG_SHA256_BLOCK_LENGTH" = "PG_SHA384_BLOCK_LENGTH"
  "uint32" = "uint64"   # collapses the C qualType differences

  # Optionally a supplementary manual block — bindings that the tensor
  # diff couldn't supply (e.g., structural holes).
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field


# ─── Type qualType parsing ──────────────────────────────────────────────────

# Strip pointer/array/const decoration from a qualType to get the base
# type identifier. The tensor reports things like:
#   "pg_sha256_ctx *"
#   "const uint32 *"
#   "uint32[8]"
#   "const uint32[8]"
# We want just the type-name component so the policy can rename it.
_QUALTYPE_DECORATION_RE = re.compile(r"\s*(?:\*|const\b|volatile\b|\[\d*\])\s*")


def base_type(qualtype: str) -> str:
    """Strip C type-qualifier / pointer / array decoration."""
    return _QUALTYPE_DECORATION_RE.sub(" ", qualtype).strip()


# ─── Naming transforms ──────────────────────────────────────────────────────


_PG_PREFIX_RE = re.compile(r"^pg_")
_PRIMITIVE_TYPE_RENAMES = {
    "uint8": "u8",
    "uint16": "u16",
    "uint32": "u32",
    "uint64": "u64",
    "int8": "i8",
    "int16": "i16",
    "int32": "i32",
    "int64": "i64",
    "size_t": "usize",
    "ssize_t": "isize",
    "char": "u8",
}


def struct_type_to_rust(c_type: str) -> str:
    """`pg_sha256_ctx` → `Sha256Ctx`. Strip `pg_`, Pascal-case the rest."""
    name = _PG_PREFIX_RE.sub("", c_type)
    return "".join(part.capitalize() for part in name.split("_") if part)


def ident_to_rust(c_ident: str) -> str:
    """Apply the per-pattern rule to a C identifier.

    Patterns recognized:
      - `pg_<word>_<word>`             → identity (Postgres function names)
      - `PG_<UPPER>_<UPPER>`           → identity (Postgres macros)
      - `<word>_<word>` (lowercase)    → SCREAMING_SNAKE
      - everything else                → identity (with warning to stderr)
    """
    if c_ident.startswith("PG_") and c_ident.isupper():
        return c_ident
    if c_ident.startswith("pg_") and c_ident.islower() and "_" in c_ident:
        return c_ident
    if c_ident.islower() and "_" in c_ident:
        return c_ident.upper()
    sys.stderr.write(
        f"warning: no policy for ident {c_ident!r}; emitting identity\n"
    )
    return c_ident


def type_to_rust(c_type: str, overrides: dict[str, str]) -> str:
    """Resolve a C type-name to its Rust counterpart.

    `c_type` is the *base* type (no `*`, no `[N]`, no `const`). The
    caller is expected to have stripped those via `base_type()` first.
    """
    base = base_type(c_type)
    if base in overrides:
        return overrides[base]
    if base in _PRIMITIVE_TYPE_RENAMES:
        return _PRIMITIVE_TYPE_RENAMES[base]
    if base.startswith("pg_"):
        return struct_type_to_rust(base)
    # Fall through: assume the C type name happens to be a valid Rust
    # type. Emit with a warning so the user can either accept it or add
    # an override.
    sys.stderr.write(f"warning: no policy for type {base!r}; emitting identity\n")
    return base


# ─── Pipeline ───────────────────────────────────────────────────────────────


@dataclass
class CBindings:
    canonical_fn: str
    sibling_fn: str
    idents: dict[str, str]        # c_canonical → c_sibling (paired)
    types: dict[str, str]         # c_canonical_qualtype → c_sibling_qualtype (paired)
    # Idents/types living *inside* structural-hole subtrees that the
    # paired walk couldn't align. The user supplies cross-side pairings
    # via `[structural_pairs]` (idents) and `[structural_type_pairs]`
    # (types) in the per-cluster overrides TOML.
    structural_canonical_idents: list[str] = field(default_factory=list)
    structural_sibling_idents: list[str] = field(default_factory=list)
    structural_canonical_types: list[str] = field(default_factory=list)
    structural_sibling_types: list[str] = field(default_factory=list)


def parse_c_bindings(text: str) -> CBindings:
    data = tomllib.loads(text)
    struct_idents = data.get("c_structural_idents") or {}
    struct_types = data.get("c_structural_types") or {}
    return CBindings(
        canonical_fn=data.get("canonical_fn", "?"),
        sibling_fn=data.get("sibling_fn", "?"),
        idents=data.get("c_idents") or {},
        types=data.get("c_types") or {},
        structural_canonical_idents=struct_idents.get("canonical") or [],
        structural_sibling_idents=struct_idents.get("sibling") or [],
        structural_canonical_types=struct_types.get("canonical") or [],
        structural_sibling_types=struct_types.get("sibling") or [],
    )


def derive_rust_bindings(
    c: CBindings,
    type_overrides: dict[str, str],
    ident_overrides: dict[str, str],
    extra: dict[str, str],
    structural_pairs: dict[str, str] | None = None,
    structural_type_pairs: dict[str, str] | None = None,
) -> dict[str, str]:
    """Apply policy to each C pair and emit `canonical_rust → target_rust`.

    The result is the content-keyed Bindings the translator's CLI
    expects. We dedupe — same canonical_rust appearing multiple times
    (e.g., the type appears via both `void (pg_sha256_ctx *)` and
    `pg_sha256_ctx *`) collapses to one entry. Conflicting values fail
    loudly so the user knows the policy is ambiguous.
    """
    out: dict[str, str] = {}

    def set_binding(k: str, v: str) -> None:
        if k in out and out[k] != v:
            raise SystemExit(
                f"policy conflict for canonical {k!r}: "
                f"want both {out[k]!r} and {v!r}"
            )
        out[k] = v

    # Idents: apply ident_overrides first, then policy.
    for c_canonical, c_sibling in c.idents.items():
        rust_canonical = ident_overrides.get(c_canonical) or ident_to_rust(c_canonical)
        rust_sibling = ident_overrides.get(c_sibling) or ident_to_rust(c_sibling)
        set_binding(rust_canonical, rust_sibling)

    # Types: strip decoration first, then rename. Skip function-pointer
    # types (e.g., `void (pg_sha256_ctx *)`) — they're the FunctionDecl's
    # own qualType and the inner struct type is already in the type
    # table as `pg_sha256_ctx *` separately. Including them would
    # produce a no-op binding with confusing whitespace.
    for c_canonical_qt, c_sibling_qt in c.types.items():
        if "(" in c_canonical_qt or "(" in c_sibling_qt:
            continue
        c_canonical_base = base_type(c_canonical_qt)
        c_sibling_base = base_type(c_sibling_qt)
        if not c_canonical_base or not c_sibling_base:
            continue
        rust_canonical = type_to_rust(c_canonical_base, type_overrides)
        rust_sibling = type_to_rust(c_sibling_base, type_overrides)
        set_binding(rust_canonical, rust_sibling)

    # Structural pairs: cross-side pairings the user supplies for idents
    # inside structural-hole subtrees that c_cluster_diff.py couldn't
    # align positionally. Verified against the c_structural_idents
    # listings — a typo'd pair fails loudly so it doesn't silently
    # become a no-op.
    if structural_pairs:
        struct_canon_set = set(c.structural_canonical_idents)
        struct_sib_set = set(c.structural_sibling_idents)
        for c_canonical, c_sibling in structural_pairs.items():
            if c_canonical not in struct_canon_set:
                sys.stderr.write(
                    f"warning: structural_pairs key {c_canonical!r} not in "
                    f"c_structural_idents.canonical — pair ignored. "
                    f"Did the cluster lose this ident?\n"
                )
                continue
            if c_sibling not in struct_sib_set:
                sys.stderr.write(
                    f"warning: structural_pairs value {c_sibling!r} not in "
                    f"c_structural_idents.sibling — pair ignored.\n"
                )
                continue
            rust_canonical = ident_overrides.get(c_canonical) or ident_to_rust(c_canonical)
            rust_sibling = ident_overrides.get(c_sibling) or ident_to_rust(c_sibling)
            set_binding(rust_canonical, rust_sibling)

    if structural_type_pairs:
        struct_canon_type_set = set(c.structural_canonical_types)
        struct_sib_type_set = set(c.structural_sibling_types)
        for c_canonical_qt, c_sibling_qt in structural_type_pairs.items():
            if c_canonical_qt not in struct_canon_type_set:
                sys.stderr.write(
                    f"warning: structural_type_pairs key {c_canonical_qt!r} not "
                    f"in c_structural_types.canonical — pair ignored.\n"
                )
                continue
            if c_sibling_qt not in struct_sib_type_set:
                sys.stderr.write(
                    f"warning: structural_type_pairs value {c_sibling_qt!r} "
                    f"not in c_structural_types.sibling — pair ignored.\n"
                )
                continue
            c_canonical_base = base_type(c_canonical_qt)
            c_sibling_base = base_type(c_sibling_qt)
            if not c_canonical_base or not c_sibling_base:
                continue
            rust_canonical = type_to_rust(c_canonical_base, type_overrides)
            rust_sibling = type_to_rust(c_sibling_base, type_overrides)
            set_binding(rust_canonical, rust_sibling)

    # Extra (manual supplement — true escape hatch for cases the
    # structural_pairs/extra-ident-discovery layers can't supply).
    for k, v in extra.items():
        set_binding(k, v)

    # Drop self-bindings — canonical == target means the policy mapped
    # both sides to the same Rust value (e.g., uint8 → u8 on both sides
    # of a `uint8[64]` ↔ `uint8[128]` qualType). These would be no-op
    # holes for the translator and just add noise.
    return {k: v for k, v in out.items() if k != v}


# ─── TOML emission ──────────────────────────────────────────────────────────


def _toml_escape(s: str) -> str:
    """Minimal TOML basic-string escaping for keys/values."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def render_rust_bindings(
    bindings: dict[str, str],
    canonical_fn: str,
    sibling_fn: str,
) -> str:
    out: list[str] = []
    out.append("# Rust-side bindings, derived from C-tensor diff + naming policy.")
    out.append(f'canonical_fn = "{canonical_fn}"')
    out.append(f'sibling_fn = "{sibling_fn}"')
    out.append("")
    out.append("[bindings]")
    out.append("# canonical Rust value → target Rust value.")
    out.append("# Translator CLI matches these against the holes its diff discovers.")
    for k in sorted(bindings):
        out.append(f'"{_toml_escape(k)}" = "{_toml_escape(bindings[k])}"')
    out.append("")
    return "\n".join(out)


# ─── CLI ────────────────────────────────────────────────────────────────────


def load_overrides_file(
    path: str | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """Returns (type_overrides, ident_overrides, extra_bindings,
    structural_pairs, structural_type_pairs)."""
    if path is None:
        return {}, {}, {}, {}, {}
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return (
        data.get("type_overrides") or {},
        data.get("ident_overrides") or {},
        data.get("extra_bindings") or {},
        data.get("structural_pairs") or {},
        data.get("structural_type_pairs") or {},
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", type=argparse.FileType("r"), default=sys.stdin,
                    help="C-bindings TOML (default: stdin)")
    ap.add_argument("--overrides", type=str, default=None,
                    help="per-cluster overrides TOML (type_overrides, ident_overrides, extra_bindings)")
    args = ap.parse_args()

    c = parse_c_bindings(args.input.read())
    (
        type_overrides,
        ident_overrides,
        extra,
        structural_pairs,
        structural_type_pairs,
    ) = load_overrides_file(args.overrides)
    rust = derive_rust_bindings(
        c,
        type_overrides,
        ident_overrides,
        extra,
        structural_pairs=structural_pairs,
        structural_type_pairs=structural_type_pairs,
    )
    sys.stdout.write(render_rust_bindings(rust, c.canonical_fn, c.sibling_fn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
