#!/usr/bin/env python3
"""C-side AST diff over the rules_lang tensor.

Given a tensor directory and two C function names (a canonical and a
sibling cluster member), walks both functions' AST subtrees in
lockstep and extracts the per-instance differences:

  - **Ident bindings.** Where the two trees have the same `kind` at the
    same DFS position but a different `ident_idx`, emit a binding
    `canonical_ident → sibling_ident`. This is how `pg_sha256_init` vs
    `pg_sha384_init` reveals `sha256_initial_hash_value` →
    `sha384_initial_hash_value`, etc.

  - **Type bindings.** Same but for `type_idx` — surfaces e.g.
    `pg_sha256_ctx *` → `pg_sha384_ctx *`.

  - **Structural holes.** If at some DFS position the two trees have
    different `kind`s, or a node has a different child count, the
    subtrees have structurally diverged. We emit a `[[structural_hole]]`
    block summarizing both subtrees rather than trying to align inside
    them. The cluster discipline should keep structural-hole counts low.

Output is TOML. The C-side names are still in C convention
(snake_case); the consumer (translator binding-writer) applies a
C→Rust renaming policy on top — separate concern, kept out of this
tool to keep the cluster-discovery step pure.

Usage:
    python3 c_cluster_diff.py \\
        --tensor-dir ml/embed/data/postgres_full_tensor \\
        --canonical pg_sha256_init \\
        --sibling pg_sha384_init \\
        > sha256_to_sha384.c-bindings.toml

Stdlib-only (no numpy). The arrays are read via `array.array.frombytes`
from the same `<u2`/`<i4`/`<u4` files described in ast_to_tensor.py.
"""

from __future__ import annotations

import argparse
import array
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


# Map manifest dtype string → (array.array typecode, byte-width).
_DTYPE_DECODE = {
    "<u1": ("B", 1),
    "<u2": ("H", 2),
    "<u4": ("I", 4),
    "<i4": ("i", 4),
}


@dataclass
class Tensor:
    """In-memory view of the tensor arrays we care about for diffing.

    We deliberately only load what we need (kind, ident_idx, type_idx,
    first_child, next_sibling). 6M nodes × 5 arrays × ~4 bytes ≈ 120MB,
    which is fine for ad-hoc analysis.
    """

    kind: array.array          # u16
    ident_idx: array.array     # u32
    type_idx: array.array      # u32
    first_child: array.array   # i32
    next_sibling: array.array  # i32

    kind_vocab: list[str]
    ident_vocab: list[str]
    type_vocab: list[str]

    # Convenience lookups built from vocabs.
    function_decl_kind: int = field(init=False)
    ident_to_idx: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        try:
            self.function_decl_kind = self.kind_vocab.index("FunctionDecl")
        except ValueError as e:
            raise RuntimeError(
                f"kind_vocab missing 'FunctionDecl': {self.kind_vocab[:10]}..."
            ) from e
        # Reverse index lets us answer "what node-id has this ident name"
        # in O(1) per lookup. Building it once amortizes the scan cost.
        self.ident_to_idx = {name: i for i, name in enumerate(self.ident_vocab)}


def load_tensor(tensor_dir: Path) -> Tensor:
    manifest = json.loads((tensor_dir / "manifest.json").read_text())
    arrays_meta = manifest["arrays"]

    def load_array(name: str) -> array.array:
        meta = arrays_meta[name]
        typecode, _ = _DTYPE_DECODE[meta["dtype"]]
        arr = array.array(typecode)
        data = (tensor_dir / meta["file"]).read_bytes()
        arr.frombytes(data)
        if len(arr) != meta["length"]:
            raise RuntimeError(
                f"{name}: length mismatch {len(arr)} vs manifest {meta['length']}"
            )
        return arr

    return Tensor(
        kind=load_array("kind"),
        ident_idx=load_array("ident_idx"),
        type_idx=load_array("type_idx"),
        first_child=load_array("first_child"),
        next_sibling=load_array("next_sibling"),
        kind_vocab=json.loads((tensor_dir / "kind_vocab.json").read_text()),
        ident_vocab=json.loads((tensor_dir / "ident_vocab.json").read_text()),
        type_vocab=json.loads((tensor_dir / "type_vocab.json").read_text()),
    )


def find_function(t: Tensor, name: str) -> int:
    """Find the first FunctionDecl node with the given ident. Linear
    scan — at ~6M nodes this is ~50ms in pure Python, acceptable for an
    interactive tool. Could be sped up via numpy or a precomputed index
    if it becomes a bottleneck.
    """
    if name not in t.ident_to_idx:
        raise SystemExit(f"identifier {name!r} not in ident_vocab")
    target_ident = t.ident_to_idx[name]
    for node in range(len(t.kind)):
        if t.kind[node] == t.function_decl_kind and t.ident_idx[node] == target_ident:
            return node
    raise SystemExit(f"no FunctionDecl named {name!r}")


def children(t: Tensor, node: int) -> list[int]:
    out: list[int] = []
    c = t.first_child[node]
    while c != -1:
        out.append(c)
        c = t.next_sibling[c]
    return out


# ─── Diff walker ────────────────────────────────────────────────────────────


@dataclass
class IdentBinding:
    canonical: str
    sibling: str


@dataclass
class TypeBinding:
    canonical: str
    sibling: str


@dataclass
class StructuralHole:
    """Two subtrees that don't align at the same DFS position. We
    summarize them rather than try to recurse, since by definition the
    cluster isn't tight enough at this point.
    """

    canonical_kind: str
    sibling_kind: str
    canonical_node: int
    sibling_node: int
    canonical_subtree_summary: str
    sibling_subtree_summary: str


@dataclass
class DiffReport:
    canonical_fn: str
    sibling_fn: str
    ident_bindings: list[IdentBinding] = field(default_factory=list)
    type_bindings: list[TypeBinding] = field(default_factory=list)
    structural_holes: list[StructuralHole] = field(default_factory=list)
    # Idents/types found *inside* structural-hole subtrees, where the
    # paired walk can't establish a 1:1 correspondence. The downstream
    # naming policy applies its renames to each side independently;
    # the user's [ident_overrides] / [type_overrides] supply the
    # cross-side pairings. Without this section, anything inside a
    # structural hole was invisible to the bindings pipeline (see
    # agent A's "leverage ceiling" finding, 2026-05-24).
    structural_canonical_idents: list[str] = field(default_factory=list)
    structural_sibling_idents: list[str] = field(default_factory=list)
    structural_canonical_types: list[str] = field(default_factory=list)
    structural_sibling_types: list[str] = field(default_factory=list)


def collect_subtree_leaves(t: Tensor, root: int) -> tuple[list[str], list[str]]:
    """Walk a subtree, return (idents, qualtypes) seen — deduped, DFS
    pre-order. Idents only include non-empty entries (skips synthetic
    nodes like ImplicitCastExpr that have no ident). Same for types.
    """
    idents: list[str] = []
    types: list[str] = []
    seen_ident: set[str] = set()
    seen_type: set[str] = set()
    stack = [root]
    while stack:
        n = stack.pop()
        ident = t.ident_vocab[t.ident_idx[n]]
        if ident and ident not in seen_ident:
            seen_ident.add(ident)
            idents.append(ident)
        qt = t.type_vocab[t.type_idx[n]]
        if qt and qt not in seen_type:
            seen_type.add(qt)
            types.append(qt)
        for child in reversed(children(t, n)):
            stack.append(child)
    return idents, types


def _merge_unique(dest: list[str], src: list[str]) -> None:
    """Append items from `src` to `dest`, preserving order, dedup-aware."""
    seen = set(dest)
    for item in src:
        if item not in seen:
            seen.add(item)
            dest.append(item)


def _record_structural_hole(
    t: Tensor,
    canonical_node: int,
    sibling_node: int,
    hole: StructuralHole,
    report: DiffReport,
) -> None:
    """Append a structural hole + collect its loose idents/types into
    the report's structural_* aggregates."""
    report.structural_holes.append(hole)
    c_idents, c_types = collect_subtree_leaves(t, canonical_node)
    s_idents, s_types = collect_subtree_leaves(t, sibling_node)
    _merge_unique(report.structural_canonical_idents, c_idents)
    _merge_unique(report.structural_sibling_idents, s_idents)
    _merge_unique(report.structural_canonical_types, c_types)
    _merge_unique(report.structural_sibling_types, s_types)


def subtree_summary(t: Tensor, root: int, max_nodes: int = 16) -> str:
    """Compact one-line summary of a subtree — used for structural-hole
    reporting where we want to surface 'this is what diverged' without
    dumping the whole subtree.
    """
    parts: list[str] = []
    stack = [root]
    seen = 0
    while stack and seen < max_nodes:
        n = stack.pop()
        kind = t.kind_vocab[t.kind[n]]
        ident = t.ident_vocab[t.ident_idx[n]]
        if ident:
            parts.append(f"{kind}({ident})")
        else:
            parts.append(kind)
        seen += 1
        # Push children in reverse so we visit in source order.
        for child in reversed(children(t, n)):
            stack.append(child)
    if stack:
        parts.append("...")
    return " > ".join(parts)


def diff_subtrees(
    t: Tensor,
    canonical_node: int,
    sibling_node: int,
    report: DiffReport,
) -> None:
    """Parallel pre-order walk of two subtrees. At each step:
      - kinds equal + idents differ → ident binding
      - kinds equal + types differ → type binding
      - kinds equal + children counts equal → recurse pairwise
      - kinds differ OR child counts differ → structural hole, no recurse
    """
    ck = t.kind[canonical_node]
    sk = t.kind[sibling_node]
    if ck != sk:
        _record_structural_hole(
            t,
            canonical_node,
            sibling_node,
            StructuralHole(
                canonical_kind=t.kind_vocab[ck],
                sibling_kind=t.kind_vocab[sk],
                canonical_node=canonical_node,
                sibling_node=sibling_node,
                canonical_subtree_summary=subtree_summary(t, canonical_node),
                sibling_subtree_summary=subtree_summary(t, sibling_node),
            ),
            report,
        )
        return

    # Same kind — record ident/type diffs at this node.
    c_ident = t.ident_vocab[t.ident_idx[canonical_node]]
    s_ident = t.ident_vocab[t.ident_idx[sibling_node]]
    if c_ident and s_ident and c_ident != s_ident:
        report.ident_bindings.append(IdentBinding(canonical=c_ident, sibling=s_ident))

    c_type = t.type_vocab[t.type_idx[canonical_node]]
    s_type = t.type_vocab[t.type_idx[sibling_node]]
    if c_type and s_type and c_type != s_type:
        report.type_bindings.append(TypeBinding(canonical=c_type, sibling=s_type))

    # Recurse into children pairwise. Mismatched lengths → structural hole.
    cc = children(t, canonical_node)
    sc = children(t, sibling_node)
    if len(cc) != len(sc):
        _record_structural_hole(
            t,
            canonical_node,
            sibling_node,
            StructuralHole(
                canonical_kind=t.kind_vocab[ck],
                sibling_kind=t.kind_vocab[sk],
                canonical_node=canonical_node,
                sibling_node=sibling_node,
                canonical_subtree_summary=(
                    f"{t.kind_vocab[ck]} with {len(cc)} children: "
                    + subtree_summary(t, canonical_node)
                ),
                sibling_subtree_summary=(
                    f"{t.kind_vocab[sk]} with {len(sc)} children: "
                    + subtree_summary(t, sibling_node)
                ),
            ),
            report,
        )
        return

    for c_child, s_child in zip(cc, sc):
        diff_subtrees(t, c_child, s_child, report)


# ─── TOML emission ──────────────────────────────────────────────────────────


def deduplicate_idents(bindings: list[IdentBinding]) -> list[IdentBinding]:
    """Merge identical ident bindings — a function body that references
    the same constant N times produces N copies of the same binding."""
    seen: dict[tuple[str, str], IdentBinding] = {}
    for b in bindings:
        key = (b.canonical, b.sibling)
        if key not in seen:
            seen[key] = b
    return list(seen.values())


def deduplicate_types(bindings: list[TypeBinding]) -> list[TypeBinding]:
    seen: dict[tuple[str, str], TypeBinding] = {}
    for b in bindings:
        key = (b.canonical, b.sibling)
        if key not in seen:
            seen[key] = b
    return list(seen.values())


def render_toml(report: DiffReport) -> str:
    out: list[str] = []
    out.append("# C-side bindings extracted from the AST tensor.")
    out.append(f'canonical_fn = "{report.canonical_fn}"')
    out.append(f'sibling_fn = "{report.sibling_fn}"')
    out.append("")

    out.append("[c_idents]")
    out.append(
        "# canonical-side C identifier → sibling-side C identifier."
    )
    out.append(
        "# Apply C→Rust naming policy at the next stage to get translator-bindings."
    )
    for b in deduplicate_idents(report.ident_bindings):
        out.append(f'"{b.canonical}" = "{b.sibling}"')
    out.append("")

    out.append("[c_types]")
    out.append("# canonical-side qualType → sibling-side qualType.")
    for b in deduplicate_types(report.type_bindings):
        out.append(f'"{b.canonical}" = "{b.sibling}"')
    out.append("")

    if report.structural_holes:
        out.append("# Structural divergences — subtrees that don't align.")
        out.append("# Resolve by hand (or split the cluster).")
        for h in report.structural_holes:
            out.append("[[structural_hole]]")
            out.append(f'canonical_kind = "{h.canonical_kind}"')
            out.append(f'sibling_kind = "{h.sibling_kind}"')
            out.append(f"canonical_node = {h.canonical_node}")
            out.append(f"sibling_node = {h.sibling_node}")
            out.append(f'canonical_subtree = "{h.canonical_subtree_summary}"')
            out.append(f'sibling_subtree = "{h.sibling_subtree_summary}"')
            out.append("")

        # Loose idents/types found *inside* the structural-hole subtrees.
        # No 1:1 alignment — the downstream policy applies its renames
        # independently to each side; `[ident_overrides]` /
        # `[type_overrides]` in the per-cluster overrides TOML supply
        # the cross-side pairing. Without this section, anything inside
        # a structural hole was opaque to the bindings pipeline.
        out.append("[c_structural_idents]")
        out.append('canonical = [')
        for n in report.structural_canonical_idents:
            out.append(f'  "{n}",')
        out.append(']')
        out.append('sibling = [')
        for n in report.structural_sibling_idents:
            out.append(f'  "{n}",')
        out.append(']')
        out.append("")

        out.append("[c_structural_types]")
        out.append('canonical = [')
        for n in report.structural_canonical_types:
            out.append(f'  "{n}",')
        out.append(']')
        out.append('sibling = [')
        for n in report.structural_sibling_types:
            out.append(f'  "{n}",')
        out.append(']')
        out.append("")

    return "\n".join(out)


# ─── CLI ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tensor-dir", type=Path, required=True)
    ap.add_argument("--canonical", required=True, help="C function name (canonical)")
    ap.add_argument("--sibling", required=True, help="C function name (sibling)")
    args = ap.parse_args()

    t = load_tensor(args.tensor_dir)
    c_node = find_function(t, args.canonical)
    s_node = find_function(t, args.sibling)

    report = DiffReport(canonical_fn=args.canonical, sibling_fn=args.sibling)
    diff_subtrees(t, c_node, s_node, report)

    sys.stdout.write(render_toml(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
