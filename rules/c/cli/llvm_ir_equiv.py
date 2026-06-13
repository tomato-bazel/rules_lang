#!/usr/bin/env python3
"""Structural LLVM IR equivalence check between two .ll files.

Designed for the C→Rust translation gate. Full syntactic IR diff is
hopeless (clang -O0 emits FORTIFY-wrapped alloca/load/store noise;
rustc -O1 emits clean intrinsics). Full semantic equivalence requires
alive2, which is slow and finicky. **Structural equivalence is the
right middle ground**: for each public symbol that should appear in
both IRs, verify

  - **Tier 1 — signature**: function exists in both, with the same
    argument count and (normalized) argument/return types.
  - **Tier 2 — size envelope**: basic-block count and instruction count
    are within configurable tolerances. The Rust side is expected to
    be slightly *leaner* at -O1 than the C side at -O0; an envelope
    catches dramatic structural drift (e.g., a missing loop) without
    requiring exact match.

This complements rather than replaces behavioral equivalence (the
FIPS-vector tests in pg_sha2 already prove bit-identical output for
SHA-{224,256,384,512}). Structural equivalence proves the *shape* of
the translation; behavioral equivalence proves the *semantics*.

Usage:
    python3 llvm_ir_equiv.py \\
        --c-ir   path/to/sha2.c.ll \\
        --rust-ir path/to/pg_sha2.ll \\
        --symbols pg_sha256_init pg_sha256_update ... \\
        [--instruction-tolerance 5.0] \\
        [--bb-tolerance 3.0] \\
        [--rename rust_name=c_name ...]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ─── IR parsing ─────────────────────────────────────────────────────────────


# `define [attrs] <ret_type> @<name>(<args>) [attrs] {`
# Regex matches the prefix up through the symbol name. The arg list
# needs balanced-paren walking (Rust attrs like `captures(address,
# read_provenance)` contain commas + parens inside the arg slot, which
# a non-balanced regex would mangle), so we capture from the opening
# `(` and walk in code.
_DEFINE_PREFIX_RE = re.compile(
    r"^define\s+"
    r"(?P<attrs>(?:[^@]+?)?)"
    r"(?P<ret>\S+(?:\([^)]*\))?)\s+"
    r"@(?P<name>[A-Za-z0-9_$.\"]+)"
    r"\s*\("
)


def _balanced_arg_string(line: str, open_paren_idx: int) -> str:
    """Given the index of `(`, return the contents up to the matching `)`."""
    depth = 1
    i = open_paren_idx + 1
    while i < len(line) and depth > 0:
        ch = line[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return line[open_paren_idx + 1 : i]
        i += 1
    return line[open_paren_idx + 1 :]  # unterminated; let the parser fail downstream


@dataclass
class FnInfo:
    name: str
    raw_name: str  # before any rename mapping
    return_type: str
    arg_types: list[str]
    n_basic_blocks: int
    n_instructions: int
    raw_signature: str


def _normalize_type(t: str) -> str:
    """Collapse param-attribute noise so the comparison is fair.

    LLVM allows interleaving param attributes (`noundef`, `nonnull`,
    `align N`, `dereferenceable(N)`, `captures(...)`, `sret(...)`,
    `writeonly`, …) and the SSA value name (`%0`, `%context`) into the
    arg slot. We strip them and keep just the base type tokens
    (`ptr`, `i64`, `[8 x i32]`, `{ i32, i1 }`, etc.).

    Note: we apply `attr(...)` stripping iteratively because attribute
    args themselves can contain commas, which the per-arg splitter has
    already removed for the outer comma but left intact for the inner
    one. Iterate until stable.
    """
    # Iteratively drop `attr(...)` qualifiers, handling nested parens
    # by repeating until no change.
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"\b\w+\([^()]*\)", "", t)
    # Drop SSA value names: `%foo`, `%0`, including dotted ones like `%self.0`.
    t = re.sub(r"%[A-Za-z0-9_.]+", "", t)
    # Drop `align N` (no parens) and `dereferenceable_or_null` etc.
    t = re.sub(r"\balign\s+\d+\b", "", t)
    # Drop known bare attrs.
    bare = {
        "noundef", "nonnull", "readonly", "writeonly", "noalias",
        "signext", "zeroext", "inreg", "byval", "sret", "inalloca",
        "dead_on_unwind", "writable", "captures", "preallocated",
        "swiftself", "swifterror", "immarg", "returned", "allocptr",
        "allocalign", "nocapture", "nofree", "willreturn", "norecurse",
    }
    parts = [p for p in t.split() if p not in bare]
    return " ".join(parts).strip()


def _parse_args(args: str) -> list[str]:
    if not args.strip():
        return []
    # Split on commas at depth 0 (so `{ i32, i1 }` doesn't get split).
    out = []
    depth = 0
    cur = []
    for ch in args:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return out


def parse_ll(path: Path) -> dict[str, FnInfo]:
    """Parse a .ll file into `name → FnInfo`."""
    fns: dict[str, FnInfo] = {}
    current: FnInfo | None = None
    with path.open() as fh:
        for line in fh:
            stripped = line.rstrip("\n")
            if current is None:
                m = _DEFINE_PREFIX_RE.match(stripped)
                if m:
                    arg_str = _balanced_arg_string(stripped, m.end() - 1)
                    args = _parse_args(arg_str)
                    current = FnInfo(
                        name=m.group("name").strip('"'),
                        raw_name=m.group("name").strip('"'),
                        return_type=_normalize_type(m.group("ret")),
                        arg_types=[_normalize_type(a) for a in args],
                        n_basic_blocks=1,  # entry block is implicit
                        n_instructions=0,
                        raw_signature=stripped,
                    )
                continue

            # Inside a function — count BBs (lines ending in `:`) and
            # instructions (everything else nontrivial).
            if stripped.startswith("}"):
                fns[current.name] = current
                current = None
                continue
            text = stripped.strip()
            if not text or text.startswith(";"):
                continue
            # A basic-block label looks like `bb3:` or `5:` or
            # `bb3:                  ; preds = ...`. Detect by
            # presence of `:` before any `;`.
            comment_off = text.find(";")
            head = text[:comment_off] if comment_off >= 0 else text
            head = head.strip()
            if head.endswith(":"):
                current.n_basic_blocks += 1
            else:
                current.n_instructions += 1
    return fns


# ─── Comparison ─────────────────────────────────────────────────────────────


@dataclass
class SymbolReport:
    symbol: str
    c: FnInfo | None
    rust: FnInfo | None
    issues: list[str] = field(default_factory=list)

    @property
    def passing(self) -> bool:
        return not self.issues


def compare_symbol(
    symbol: str,
    c_fns: dict[str, FnInfo],
    rust_fns: dict[str, FnInfo],
    bb_tol: float,
    instr_tol: float,
) -> SymbolReport:
    c = c_fns.get(symbol)
    rust = rust_fns.get(symbol)
    report = SymbolReport(symbol=symbol, c=c, rust=rust)
    if c is None:
        report.issues.append(f"missing on C side")
        return report
    if rust is None:
        report.issues.append(f"missing on Rust side")
        return report

    # Tier 1: signature.
    if len(c.arg_types) != len(rust.arg_types):
        report.issues.append(
            f"arg count: C has {len(c.arg_types)}, Rust has {len(rust.arg_types)}"
        )
    else:
        for i, (ct, rt) in enumerate(zip(c.arg_types, rust.arg_types)):
            if ct != rt:
                report.issues.append(f"arg {i} type: C={ct!r} vs Rust={rt!r}")
    if c.return_type != rust.return_type:
        report.issues.append(
            f"return type: C={c.return_type!r} vs Rust={rust.return_type!r}"
        )

    # Tier 2: size envelope. Allow Rust to be either side of C within
    # the configured multiplier. We never expect Rust to be wildly
    # larger than C — that would suggest the translation is unrolling
    # or emitting boilerplate beyond what's reasonable.
    def within_envelope(c_val: int, rust_val: int, tol: float) -> bool:
        if c_val == 0 and rust_val == 0:
            return True
        if c_val == 0 or rust_val == 0:
            return False
        ratio = max(c_val, rust_val) / max(min(c_val, rust_val), 1)
        return ratio <= tol

    if not within_envelope(c.n_basic_blocks, rust.n_basic_blocks, bb_tol):
        report.issues.append(
            f"basic-block count out of envelope (×{bb_tol}): "
            f"C={c.n_basic_blocks}, Rust={rust.n_basic_blocks}"
        )
    if not within_envelope(c.n_instructions, rust.n_instructions, instr_tol):
        report.issues.append(
            f"instruction count out of envelope (×{instr_tol}): "
            f"C={c.n_instructions}, Rust={rust.n_instructions}"
        )

    return report


# ─── CLI ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--c-ir", type=Path, required=True)
    ap.add_argument("--rust-ir", type=Path, required=True)
    ap.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="public symbol names to check",
    )
    ap.add_argument(
        "--instruction-tolerance",
        type=float,
        default=8.0,
        help=(
            "max ratio between C and Rust instruction counts (default: 8.0). "
            "Generous because (a) Rust includes panic-on-overflow / "
            "bounds-check branches that C doesn't, (b) functions whose "
            "Rust translation uses iterator combinators expand to several "
            "instructions per C-side line. A genuinely-missing loop body "
            "shows up as a 100×+ ratio, well above this envelope."
        ),
    )
    ap.add_argument(
        "--bb-tolerance",
        type=float,
        default=5.0,
        help="max ratio between C and Rust basic-block counts (default: 5.0)",
    )
    ap.add_argument(
        "--rename",
        nargs="*",
        default=[],
        help="rust_name=c_name pairs (alias the Rust symbol when looking it up)",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="print per-symbol counts even on success",
    )
    args = ap.parse_args()

    c_fns = parse_ll(args.c_ir)
    rust_fns = parse_ll(args.rust_ir)

    # Apply renames into the Rust map (so we look up by the C symbol).
    for pair in args.rename:
        if "=" not in pair:
            sys.exit(f"--rename expects rust=c, got {pair!r}")
        rust_name, c_name = pair.split("=", 1)
        if rust_name in rust_fns:
            rust_fns[c_name] = rust_fns.pop(rust_name)

    reports = [
        compare_symbol(
            sym, c_fns, rust_fns,
            bb_tol=args.bb_tolerance,
            instr_tol=args.instruction_tolerance,
        )
        for sym in args.symbols
    ]

    width = max(len(r.symbol) for r in reports) if reports else 0
    failed = 0
    for r in reports:
        if r.passing:
            if args.verbose:
                assert r.c is not None and r.rust is not None
                print(
                    f"  PASS  {r.symbol:<{width}}  "
                    f"C: {r.c.n_basic_blocks}bb/{r.c.n_instructions}i, "
                    f"Rust: {r.rust.n_basic_blocks}bb/{r.rust.n_instructions}i",
                    file=sys.stderr,
                )
        else:
            failed += 1
            print(f"  FAIL  {r.symbol}", file=sys.stderr)
            for issue in r.issues:
                print(f"          • {issue}", file=sys.stderr)
            if r.c is not None and r.rust is not None:
                print(
                    f"          C: {r.c.n_basic_blocks}bb/{r.c.n_instructions}i, "
                    f"Rust: {r.rust.n_basic_blocks}bb/{r.rust.n_instructions}i",
                    file=sys.stderr,
                )

    if failed:
        print(
            f"\nllvm_ir_equiv: {failed}/{len(reports)} symbols failed structural check",
            file=sys.stderr,
        )
        return 1
    print(
        f"llvm_ir_equiv: all {len(reports)} symbols passed structural check",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
