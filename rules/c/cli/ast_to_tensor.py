#!/usr/bin/env python3
"""Convert a directory of clang `.ast.json` dumps into a compact
tensor-shaped corpus that loads cleanly into GPU memory.

We use Python's stdlib `array` module on the producer side and emit
each AST array as a raw little-endian binary file. The consumer
(PyTorch / cuDF / whatever) reads them via `numpy.fromfile(path, dtype)`
or `torch.frombuffer`. This keeps the producer dependency footprint to
zero (stdlib only); the GPU side gets numpy/torch as a normal training-
script dependency.

Output layout (per-corpus, one directory):

  <out_dir>/
    manifest.json      # describes shape + dtype of each array file
    nodes/
      kind.u16         # uint16, length = num_nodes
      parent.i32       # int32
      first_child.i32
      next_sibling.i32
      depth.u8
      start_line.u32
      start_col.u16
      end_line.u32
      end_col.u16
      ident_idx.u32
      type_idx.u32
      tu_idx.u16
    kind_vocab.json    # list of clang AST kind names
    ident_vocab.json   # interned identifier strings (idx 0 = "")
    type_vocab.json    # interned qualType strings (idx 0 = "")
    tu_vocab.json      # source paths (idx 0 = "<root>")

PyTorch usage:

    import json, numpy as np, torch
    manifest = json.load(open("out_dir/manifest.json"))
    arrs = {
        name: np.fromfile(
            f"out_dir/nodes/{info['file']}", dtype=info['dtype']
        )
        for name, info in manifest['arrays'].items()
    }
    kinds = torch.from_numpy(arrs["kind"]).cuda()
    parents = torch.from_numpy(arrs["parent"]).cuda()
    # ...

Vocabularies reserve index 0 as a sentinel ("" for ident/type; "<root>"
for tu_vocab; "<unknown>" for kind_vocab). Real entries start at 1.
"""

from __future__ import annotations

import argparse
import array
import json
import os
import sys
from typing import Any

# `array` typecodes + the (stable) numpy dtype strings consumers expect.
# Stdlib's `array` has a single typecode per width; we annotate dtype
# separately because for `int32` etc. we need to disambiguate signed vs
# unsigned (typecode 'i' is signed int, 'I' is unsigned int).
_DTYPE_SPEC = {
    "kind": ("H", "<u2"),           # uint16
    "parent": ("i", "<i4"),         # int32
    "first_child": ("i", "<i4"),
    "next_sibling": ("i", "<i4"),
    "depth": ("B", "<u1"),          # uint8
    "start_line": ("I", "<u4"),     # uint32
    "start_col": ("H", "<u2"),
    "end_line": ("I", "<u4"),
    "end_col": ("H", "<u2"),
    "ident_idx": ("I", "<u4"),
    "type_idx": ("I", "<u4"),
    "tu_idx": ("H", "<u2"),
}


def _is_in_main_file(node: dict[str, Any]) -> bool:
    """Strip clang implicit + included-header decls from the tensor.

    Same heuristic as `c/cli/json_to_ttl.py:_is_in_main_file`. Without
    this, a typical TU contributes ~30k clang-prelude nodes (implicit
    int128 typedefs, NSString stuff on darwin, system-header inlines)
    that swamp the actual user code in the tensor."""
    loc = node.get("loc") or {}
    rng = node.get("range") or {}
    begin = rng.get("begin") or {}
    if loc.get("includedFrom") or begin.get("includedFrom"):
        return False
    if node.get("isImplicit"):
        return False
    return True


class _Builder:
    """Accumulates per-node arrays + vocabularies across many TUs."""

    def __init__(self, include_implicit: bool) -> None:
        self.include_implicit = include_implicit

        # Vocabularies. Index 0 reserved as sentinel.
        self._kind_vocab: list[str] = ["<unknown>"]
        self._kind_to_idx: dict[str, int] = {"<unknown>": 0}
        self._ident_vocab: list[str] = [""]
        self._ident_to_idx: dict[str, int] = {"": 0}
        self._type_vocab: list[str] = [""]
        self._type_to_idx: dict[str, int] = {"": 0}
        self._tu_vocab: list[str] = ["<root>"]
        self._tu_to_idx: dict[str, int] = {"<root>": 0}

        # Per-node arrays. Use `array.array` (homogeneous typed buffer
        # backed by C array; ~5x memory savings vs `list[int]` and
        # writes-to-file in one syscall via `.tofile()`).
        def _arr(name: str) -> array.array:
            return array.array(_DTYPE_SPEC[name][0])

        self.arrs: dict[str, array.array] = {
            name: _arr(name) for name in _DTYPE_SPEC
        }

    def _intern(self, vocab: list[str], lookup: dict[str, int], s: str) -> int:
        if s in lookup:
            return lookup[s]
        idx = len(vocab)
        vocab.append(s)
        lookup[s] = idx
        return idx

    def _kind_id(self, k: str) -> int:
        return self._intern(self._kind_vocab, self._kind_to_idx, k)

    def _ident_id(self, s: str | None) -> int:
        if not s:
            return 0
        return self._intern(self._ident_vocab, self._ident_to_idx, s)

    def _type_id(self, t: str | None) -> int:
        if not t:
            return 0
        return self._intern(self._type_vocab, self._type_to_idx, t)

    def _tu_id(self, path: str) -> int:
        return self._intern(self._tu_vocab, self._tu_to_idx, path)

    def add_translation_unit(self, payload: dict[str, Any],
                             source_path: str) -> int:
        """Walk one TU's AST tree. Returns the number of nodes added."""
        tu_idx = self._tu_id(source_path)
        start_count = len(self.arrs["kind"])

        def visit(node: dict[str, Any], parent_idx: int, depth: int) -> int:
            """Emit node + recurse children. Returns this node's index,
            or -1 if it was skipped (implicit / included)."""
            if not (self.include_implicit or _is_in_main_file(node)):
                return -1

            kind = node.get("kind", "<unknown>")
            # Prefer the node's own `name` (set on declarations:
            # FunctionDecl, VarDecl, ParmVarDecl, MemberExpr, etc.).
            # Fall back to `referencedDecl.name` so reference nodes
            # (DeclRefExpr) get the symbol they point at — without this,
            # `memcpy(state, sha256_initial_hash_value, ...)` records
            # the DeclRefExpr with an empty ident, and downstream tools
            # can't tell two cluster instances apart by their referenced
            # constants. Same for `ownedTagDecl.name` on tag-referencing
            # nodes (struct foo declared inline as a type).
            name = node.get("name")
            if not name:
                ref = node.get("referencedDecl")
                if isinstance(ref, dict):
                    name = ref.get("name")
            qt = (node.get("type") or {}).get("qualType")
            rng = node.get("range") or {}
            begin = rng.get("begin") or {}
            end = rng.get("end") or {}

            idx = len(self.arrs["kind"])
            self.arrs["kind"].append(self._kind_id(kind))
            self.arrs["parent"].append(parent_idx)
            self.arrs["first_child"].append(-1)
            self.arrs["next_sibling"].append(-1)
            self.arrs["depth"].append(min(depth, 255))
            self.arrs["start_line"].append(int(begin.get("line") or 0))
            self.arrs["start_col"].append(min(int(begin.get("col") or 0), 65535))
            self.arrs["end_line"].append(int(end.get("line") or begin.get("line") or 0))
            self.arrs["end_col"].append(min(int(end.get("col") or 0), 65535))
            self.arrs["ident_idx"].append(self._ident_id(name))
            self.arrs["type_idx"].append(self._type_id(qt))
            self.arrs["tu_idx"].append(tu_idx)

            # Recurse over children, building the sibling chain.
            prev_sibling_idx = -1
            for child in (node.get("inner") or []):
                if not isinstance(child, dict):
                    continue
                child_idx = visit(child, idx, depth + 1)
                if child_idx < 0:
                    continue
                if self.arrs["first_child"][idx] == -1:
                    self.arrs["first_child"][idx] = child_idx
                if prev_sibling_idx != -1:
                    self.arrs["next_sibling"][prev_sibling_idx] = child_idx
                prev_sibling_idx = child_idx

            return idx

        visit(payload, parent_idx=-1, depth=0)
        return len(self.arrs["kind"]) - start_count

    def finalize(self, out_dir: str) -> dict[str, Any]:
        os.makedirs(out_dir, exist_ok=True)
        nodes_dir = os.path.join(out_dir, "nodes")
        os.makedirs(nodes_dir, exist_ok=True)

        # Write each array as a raw little-endian binary file.
        # array.array.tofile writes in NATIVE byte order; we explicitly
        # byteswap if the host is big-endian (rare in practice — macOS
        # arm64 and linux x86_64 / arm64 are all little-endian).
        arr_records = {}
        for name, arr in self.arrs.items():
            typecode, dtype_str = _DTYPE_SPEC[name]
            file_name = "%s.%s" % (name, dtype_str[1:])  # e.g. kind.u2
            path = os.path.join(nodes_dir, file_name)
            if sys.byteorder == "big":
                arr.byteswap()
            with open(path, "wb") as fh:
                arr.tofile(fh)
            if sys.byteorder == "big":
                arr.byteswap()  # restore in-memory order
            arr_records[name] = {
                "file": "nodes/" + file_name,
                "dtype": dtype_str,
                "length": len(arr),
                "bytes": arr.itemsize * len(arr),
            }

        with open(os.path.join(out_dir, "kind_vocab.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(self._kind_vocab, fh)
        with open(os.path.join(out_dir, "ident_vocab.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(self._ident_vocab, fh)
        with open(os.path.join(out_dir, "type_vocab.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(self._type_vocab, fh)
        with open(os.path.join(out_dir, "tu_vocab.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(self._tu_vocab, fh)

        n_nodes = len(self.arrs["kind"])
        total_bytes = sum(r["bytes"] for r in arr_records.values())
        manifest = {
            "version": 1,
            "num_tus": len(self._tu_vocab) - 1,  # excl. sentinel
            "num_nodes": n_nodes,
            "kind_vocab_size": len(self._kind_vocab),
            "ident_vocab_size": len(self._ident_vocab),
            "type_vocab_size": len(self._type_vocab),
            "total_node_bytes": total_bytes,
            "bytes_per_node": total_bytes // max(n_nodes, 1),
            "include_implicit": self.include_implicit,
            "arrays": arr_records,
            "vocab_files": {
                "kind": "kind_vocab.json",
                "ident": "ident_vocab.json",
                "type": "type_vocab.json",
                "tu": "tu_vocab.json",
            },
        }
        with open(os.path.join(out_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        return manifest


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dumps-manifest",
        required=True,
        help="Path to the c_ast_dump_from_compdb manifest.json. The 'ok' "
             "list is the input — one .ast.json per TU.",
    )
    p.add_argument("--out-dir", required=True,
                   help="Output directory (will be created).")
    p.add_argument(
        "--include-implicit",
        action="store_true",
        help="Include clang's implicit + included-header decls in the "
             "tensor. By default these are stripped (TranslationUnit "
             "still emitted, but its implicit children — ~30k per TU "
             "from system headers — are skipped).",
    )
    args = p.parse_args()

    with open(args.dumps_manifest, "r", encoding="utf-8") as fh:
        dumps_manifest = json.load(fh)

    builder = _Builder(include_implicit=args.include_implicit)

    ok_entries = dumps_manifest.get("ok", [])
    print(
        "[ast_to_tensor] processing %d TU dumps from %s"
        % (len(ok_entries), args.dumps_manifest),
        file=sys.stderr,
    )

    total_nodes_added = 0
    for i, entry in enumerate(ok_entries):
        ast_path = entry["ast_json"]
        source = entry["source"]
        try:
            with open(ast_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print("  skip %s: %s" % (source, e), file=sys.stderr)
            continue
        added = builder.add_translation_unit(payload, source)
        total_nodes_added += added
        if (i + 1) % 50 == 0 or i + 1 == len(ok_entries):
            print(
                "  [%d/%d] %s (+%d nodes; cumulative %d)"
                % (i + 1, len(ok_entries), source, added, total_nodes_added),
                file=sys.stderr,
            )

    manifest = builder.finalize(args.out_dir)
    print(
        "[ast_to_tensor] done. %d nodes across %d TUs. "
        "kind_vocab=%d, ident_vocab=%d, type_vocab=%d. "
        "~%.1f MB across node arrays."
        % (
            manifest["num_nodes"],
            manifest["num_tus"],
            manifest["kind_vocab_size"],
            manifest["ident_vocab_size"],
            manifest["type_vocab_size"],
            manifest["total_node_bytes"] / 1e6,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
