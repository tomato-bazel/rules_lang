#!/usr/bin/env python3
"""Convert a clang `-ast-dump=json` artifact to a Turtle (.ttl) graph
conforming to ontology/c.ttl (mid-depth schema).

Mid-depth contract:
- Top-level Decls (FunctionDecl, VarDecl, TypedefDecl, RecordDecl,
  EnumDecl) get semantic IRIs and a full property set.
- Statements with control-flow significance (CompoundStmt, IfStmt,
  ReturnStmt, For/While/Do, Switch/Case/Default, Break/Continue,
  Goto/Label, Decl/Expr/NullStmt) get span-based IRIs and parent
  c:hasChild edges to the enclosing FunctionDecl / Stmt.
- Expressions are collapsed: they don't get their own IRIs. Instead,
  the enclosing Stmt accumulates c:calls / c:refs edges extracted from
  its expression subtree, plus an optional c:spelling literal.

IRI scheme:
  Named decls:        urn:c:fn:<file>#<spelling>      (FunctionDecl)
                      urn:c:var:<file>#<spelling>     (VarDecl at file scope)
                      urn:c:typedef:<file>#<spelling>
                      urn:c:struct:<file>#<spelling>
                      urn:c:union:<file>#<spelling>
                      urn:c:enum:<file>#<spelling>
  Translation unit:   urn:c:tu:<file>
  Inner statements:   urn:c:stmt:<file>#L<l1>C<c1>-L<l2>C<c2>:<kind>

Cross-TU reference edges fall back to a name-only IRI (urn:c:fn:<name>)
when the referenced decl isn't in this TU's JSON. Downstream warehouses
can join those edges across TUs by querying for the bare-name IRI.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

# -----------------------------------------------------------------------------
# Turtle emitter (hand-rolled — keeps the dependency footprint zero).

C_PREFIX = "urn:rules-lang:c:"


def _esc_literal(s: str) -> str:
    """Escape a string for inclusion as a Turtle string literal."""
    return (
        s.replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _iri(s: str) -> str:
    """Wrap a raw IRI for Turtle output. We only emit URNs internally so
    no IRI-escaping logic is needed beyond a sanity guard."""
    if ">" in s or "<" in s:
        raise ValueError(f"refusing to emit IRI with angle brackets: {s!r}")
    return f"<{s}>"


class TurtleWriter:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._prefixes_emitted = False

    def prefixes(self) -> None:
        if self._prefixes_emitted:
            return
        self.lines.append("@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .")
        self.lines.append("@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .")
        self.lines.append("@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .")
        self.lines.append(f"@prefix c:    <{C_PREFIX}> .")
        self.lines.append("")
        self._prefixes_emitted = True

    def triple(self, s: str, p: str, o: str) -> None:
        self.lines.append(f"{s} {p} {o} .")

    def cls(self, subj_iri: str, cls_local: str) -> None:
        self.triple(_iri(subj_iri), "a", f"c:{cls_local}")

    def obj_prop(self, subj_iri: str, prop_local: str, obj_iri: str) -> None:
        self.triple(_iri(subj_iri), f"c:{prop_local}", _iri(obj_iri))

    def str_prop(self, subj_iri: str, prop_local: str, val: str) -> None:
        self.triple(_iri(subj_iri), f"c:{prop_local}", f'"{_esc_literal(val)}"')

    def int_prop(self, subj_iri: str, prop_local: str, val: int) -> None:
        self.triple(_iri(subj_iri), f"c:{prop_local}", f'"{val}"^^xsd:integer')

    def bool_prop(self, subj_iri: str, prop_local: str, val: bool) -> None:
        lit = "true" if val else "false"
        self.triple(_iri(subj_iri), f"c:{prop_local}", f'"{lit}"^^xsd:boolean')

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


# -----------------------------------------------------------------------------
# Clang JSON walker.

# Stmt kinds we treat as first-class (mid-depth schema). Everything else
# inside a function body either gets flattened into c:calls/c:refs/c:spelling
# on its enclosing first-class Stmt or is silently dropped.
STMT_KINDS = {
    "CompoundStmt",
    "ReturnStmt",
    "IfStmt",
    "ForStmt",
    "WhileStmt",
    "DoStmt",
    "SwitchStmt",
    "CaseStmt",
    "DefaultStmt",
    "BreakStmt",
    "ContinueStmt",
    "GotoStmt",
    "LabelStmt",
    "NullStmt",
    "DeclStmt",
}

# Decl kinds at the top level we model semantically. Everything else
# (e.g. clang's implicit __int128_t typedefs) becomes a generic c:Decl
# without a semantic IRI.
NAMED_DECL_KINDS = {
    "FunctionDecl": ("fn", "FunctionDecl"),
    "VarDecl": ("var", "VarDecl"),
    "TypedefDecl": ("typedef", "TypedefDecl"),
    "EnumDecl": ("enum", "EnumDecl"),
}
RECORD_TAG_CLASS = {
    None: "StructDecl",      # tagless "struct { ... }"
    "struct": "StructDecl",
    "union": "UnionDecl",
}


@dataclass
class Ctx:
    """Conversion state for a single translation unit."""
    source_path: str
    tu_iri: str
    writer: TurtleWriter
    # Map from clang pointer id → resolved entity IRI for decls we've
    # already emitted. Lets us turn DeclRefExpr.referencedDecl.id into a
    # c:refs/c:calls edge target.
    decl_iri_by_clang_id: dict[str, str]
    # Counter for inner-Stmt fallback IRIs when a span is missing.
    fallback_counter: int = 0


def _is_in_main_file(node: dict[str, Any]) -> bool:
    """Reject nodes that came from included headers or were synthesized
    by clang (the implicit `__int128_t`, `__NSConstantString_tag`, etc.
    that prelude every TU).

    Clang's `-ast-dump=json` uses delta encoding for source locations:
    the first user decl in the main file carries a full `file=...` in
    its `loc`; subsequent decls in the same file omit it. So we cannot
    use `file` presence as a rejection signal — `includedFrom` and
    `isImplicit` are the only reliable filters."""
    loc = node.get("loc") or {}
    rng = node.get("range") or {}
    begin = rng.get("begin") or {}
    if loc.get("includedFrom") or begin.get("includedFrom"):
        return False
    if node.get("isImplicit"):
        return False
    return True


def _span_iri(ctx: Ctx, kind: str, node: dict[str, Any]) -> str:
    rng = node.get("range") or {}
    begin = rng.get("begin") or {}
    end = rng.get("end") or {}
    bl, bc = begin.get("line"), begin.get("col")
    el, ec = end.get("line", bl), end.get("col")
    if bl is None or bc is None:
        ctx.fallback_counter += 1
        return f"urn:c:stmt:{ctx.source_path}#node-{ctx.fallback_counter}:{kind}"
    return f"urn:c:stmt:{ctx.source_path}#L{bl}C{bc}-L{el}C{ec}:{kind}"


def _emit_span_data(ctx: Ctx, iri: str, node: dict[str, Any]) -> None:
    rng = node.get("range") or {}
    begin = rng.get("begin") or {}
    end = rng.get("end") or {}
    if "line" in begin:
        ctx.writer.int_prop(iri, "line", begin["line"])
    if "col" in begin:
        ctx.writer.int_prop(iri, "column", begin["col"])
    if "offset" in begin:
        ctx.writer.int_prop(iri, "offset", begin["offset"])
    if "line" in end:
        ctx.writer.int_prop(iri, "endLine", end["line"])
    if "col" in end:
        ctx.writer.int_prop(iri, "endColumn", end["col"])
    if "id" in node:
        ctx.writer.str_prop(iri, "clangNodeId", node["id"])


def _decl_iri(ctx: Ctx, kind: str, name: str | None,
              tag: str | None = None) -> str | None:
    if not name:
        return None
    if kind == "FunctionDecl":
        return f"urn:c:fn:{ctx.source_path}#{name}"
    if kind == "VarDecl":
        return f"urn:c:var:{ctx.source_path}#{name}"
    if kind == "TypedefDecl":
        return f"urn:c:typedef:{ctx.source_path}#{name}"
    if kind == "EnumDecl":
        return f"urn:c:enum:{ctx.source_path}#{name}"
    if kind == "RecordDecl":
        bucket = "union" if tag == "union" else "struct"
        return f"urn:c:{bucket}:{ctx.source_path}#{name}"
    return None


def _extracted_refs(node: Any, out: list[tuple[str, str]],
                    skip_first: bool = False) -> None:
    """Walk an arbitrary subtree, accumulating (predicate, referenced_id)
    pairs. `predicate` is 'calls' for CallExpr-targets, 'refs' otherwise.
    Used by first-class Stmts to extract their expression-subtree edges.

    `skip_first` lets a CallExpr suppress recursion into its callee
    position, so we don't redundantly emit `c:refs` for the same target
    that `c:calls` already covers."""
    if isinstance(node, dict):
        kind = node.get("kind")
        if kind == "DeclRefExpr":
            ref = node.get("referencedDecl") or {}
            ref_id = ref.get("id")
            if ref_id:
                out.append(("refs", ref_id))
        elif kind == "CallExpr":
            inner = node.get("inner") or []
            callee = inner[0] if inner else None
            target_id = _find_referenced_decl_id(callee)
            if target_id:
                out.append(("calls", target_id))
            # Recurse only into argument positions; callee is covered
            # by the `c:calls` edge above.
            for child in inner[1:]:
                _extracted_refs(child, out)
            return
        for child in (node.get("inner") or []):
            _extracted_refs(child, out)
    elif isinstance(node, list):
        for child in node:
            _extracted_refs(child, out)


def _find_referenced_decl_id(node: Any) -> str | None:
    """Unwrap ImplicitCastExpr / ParenExpr around a DeclRefExpr to find
    the referenced decl id. Returns None if not resolvable."""
    if not isinstance(node, dict):
        return None
    if node.get("kind") == "DeclRefExpr":
        ref = node.get("referencedDecl") or {}
        return ref.get("id")
    # Unwrap one level of cast / paren.
    inner = node.get("inner") or []
    if inner:
        return _find_referenced_decl_id(inner[0])
    return None


def _ref_iri(ctx: Ctx, clang_id: str, referenced_decl: dict | None) -> str:
    """Resolve a clang pointer-id to the entity IRI. If we've emitted
    the decl already, use its semantic IRI. Otherwise (cross-TU or
    builtin), fall back to a bare-name IRI synthesized from the
    referencedDecl payload."""
    if clang_id in ctx.decl_iri_by_clang_id:
        return ctx.decl_iri_by_clang_id[clang_id]
    if referenced_decl:
        name = referenced_decl.get("name")
        kind = referenced_decl.get("kind", "")
        if name:
            if kind == "FunctionDecl":
                return f"urn:c:fn:{name}"           # cross-TU bare-name
            if kind == "VarDecl":
                return f"urn:c:var:{name}"
            if kind == "TypedefDecl":
                return f"urn:c:typedef:{name}"
    return f"urn:c:unknown#{clang_id}"


def _emit_stmt_subtree(ctx: Ctx, parent_iri: str, node: dict[str, Any]) -> None:
    """Walk a function body, emitting first-class Stmt nodes and their
    extracted edges. `parent_iri` is whoever encloses `node`."""
    kind = node.get("kind", "")
    if kind in STMT_KINDS:
        iri = _span_iri(ctx, kind, node)
        ctx.writer.cls(iri, kind)
        ctx.writer.obj_prop(parent_iri, "hasChild", iri)
        _emit_span_data(ctx, iri, node)

        # Collect refs/calls from this Stmt's expression subtree.
        # We exclude child Stmts because they'll recurse below.
        refs: list[tuple[str, str]] = []
        for child in (node.get("inner") or []):
            if isinstance(child, dict) and child.get("kind") in STMT_KINDS:
                continue
            _extracted_refs(child, refs)
        # De-duplicate while preserving order.
        seen: set[tuple[str, str]] = set()
        for pred, tgt_id in refs:
            if (pred, tgt_id) in seen:
                continue
            seen.add((pred, tgt_id))
            tgt_iri = _ref_iri(ctx, tgt_id, None)
            ctx.writer.obj_prop(iri, pred, tgt_iri)

        for child in (node.get("inner") or []):
            if isinstance(child, dict):
                _emit_stmt_subtree(ctx, iri, child)
        return

    # Not a first-class Stmt — keep descending in case a Stmt is buried
    # inside (e.g. inside a DeclStmt's VarDecl initializer).
    for child in (node.get("inner") or []):
        if isinstance(child, dict):
            _emit_stmt_subtree(ctx, parent_iri, child)


def _emit_top_level_decl(ctx: Ctx, node: dict[str, Any]) -> None:
    kind = node.get("kind", "")
    name = node.get("name")
    tag = node.get("tagUsed")

    if kind in NAMED_DECL_KINDS:
        _, cls = NAMED_DECL_KINDS[kind]
        iri = _decl_iri(ctx, kind, name)
    elif kind == "RecordDecl":
        cls = RECORD_TAG_CLASS.get(tag, "RecordDecl")
        iri = _decl_iri(ctx, kind, name, tag=tag)
    else:
        # Generic top-level decl we don't model semantically — skip.
        return

    if iri is None:
        return

    ctx.writer.cls(iri, cls)
    ctx.writer.obj_prop(iri, "declaredIn", ctx.tu_iri)
    if name:
        ctx.writer.str_prop(iri, "spelling", name)
    _emit_span_data(ctx, iri, node)

    # Record so DeclRefExprs in the same TU can resolve back to us.
    clang_id = node.get("id")
    if clang_id:
        ctx.decl_iri_by_clang_id[clang_id] = iri

    qual = (node.get("type") or {}).get("qualType")
    if qual:
        ctx.writer.str_prop(iri, "qualType", qual)

    if kind == "FunctionDecl":
        # Parameters + body.
        param_index = 0
        for child in (node.get("inner") or []):
            ckind = child.get("kind", "")
            if ckind == "ParmVarDecl":
                pname = child.get("name") or f"_arg{param_index}"
                p_iri = f"{iri}/param/{param_index}:{pname}"
                ctx.writer.cls(p_iri, "ParmVarDecl")
                ctx.writer.str_prop(p_iri, "spelling", pname)
                pqt = (child.get("type") or {}).get("qualType")
                if pqt:
                    ctx.writer.str_prop(p_iri, "qualType", pqt)
                ctx.writer.int_prop(p_iri, "parameterIndex", param_index)
                ctx.writer.obj_prop(iri, "hasParameter", p_iri)
                # Record so refs inside the body resolve to it.
                cid = child.get("id")
                if cid:
                    ctx.decl_iri_by_clang_id[cid] = p_iri
                param_index += 1
            elif ckind == "CompoundStmt":
                body_iri = _span_iri(ctx, "CompoundStmt", child)
                ctx.writer.cls(body_iri, "CompoundStmt")
                ctx.writer.obj_prop(iri, "hasBody", body_iri)
                _emit_span_data(ctx, body_iri, child)
                ctx.writer.bool_prop(iri, "isDefinition", True)
                for grandchild in (child.get("inner") or []):
                    if isinstance(grandchild, dict):
                        _emit_stmt_subtree(ctx, body_iri, grandchild)


def convert(payload: dict[str, Any]) -> str:
    tu_kind = payload.get("kind")
    if tu_kind != "TranslationUnitDecl":
        raise ValueError(
            f"expected top-level TranslationUnitDecl, got {tu_kind!r}"
        )

    # The dump doesn't carry sourcePath as a field; rely on the wrapping
    # rule to pass it via --source-path. Fall back to "<unknown>" if
    # invoked standalone (tests, REPL).
    source_path = payload.get("__sourcePath", "<unknown>")
    tu_iri = f"urn:c:tu:{source_path}"

    writer = TurtleWriter()
    writer.prefixes()
    writer.cls(tu_iri, "TranslationUnitDecl")
    writer.str_prop(tu_iri, "sourcePath", source_path)

    ctx = Ctx(
        source_path=source_path,
        tu_iri=tu_iri,
        writer=writer,
        decl_iri_by_clang_id={},
    )

    for decl in (payload.get("inner") or []):
        if not isinstance(decl, dict):
            continue
        if not _is_in_main_file(decl):
            continue
        _emit_top_level_decl(ctx, decl)

    return writer.render()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="clang -ast-dump=json output file")
    p.add_argument("output", help="path to write .ttl")
    p.add_argument(
        "--source-path",
        required=True,
        help="Logical source path (used in IRI construction). Usually "
             "the .c file's repo-relative path.",
    )
    args = p.parse_args()

    with open(args.input, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["__sourcePath"] = args.source_path

    ttl = convert(payload)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(ttl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
