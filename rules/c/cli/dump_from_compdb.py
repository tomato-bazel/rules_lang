#!/usr/bin/env python3
"""Driver: ingest a compile_commands.json + a source tree, emit one
artifact per matching translation unit.

Two modes:
  --mode ast    → emit `<src>.ast.json` via `clang -Xclang -ast-dump=json
                  -fsyntax-only`. The historical default; used by
                  `c_ast_dump_from_compdb`.
  --mode llvm   → emit `<src>.ll` via `clang -S -emit-llvm -O<level>`.
                  Used by `c_llvm_ir_from_compdb`. Enables the
                  C→Rust LLVM-IR-equivalence verification pipeline
                  (see project_c_to_rust_translation memory).

This is the real-world ingestion path for non-Bazel-built C codebases.
The user's existing build (autoconf / meson / cmake) produces
compile_commands.json via `bear -- make` or `meson setup`; this script
re-invokes our *pinned* clang (toolchains_llvm) with the recorded
-I / -D / -isysroot flags, dropping anything that's compiler-specific
or output-related.

Outputs:
  --out-dir / <source-relpath>.<ext>       (one per processed TU; ext
                                            is .ast.json or .ll per --mode)
  --manifest                               (JSON: list of {source, ast_json | ll})

Skipped TUs (clang errors, filter mismatch, not in srcs) are reported
to stderr but do not abort the run; the manifest lists only the
successful ones.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

# Postgres-runtime macros whose names get expanded out by clang's
# preprocessor before the AST is built — so they never appear as
# DeclRefExpr idents in the .ast.json. Purity/cluster tools that ask
# "does this function call ereport?" need to know about them.
#
# The v0 source-scan in `purity_detect.rs` re-reads each TU's source
# to recover macro hits after the fact. The v2 fix (this script's
# `--macros-out` mode) emits a per-TU `<rel>.macros.json` sidecar
# during the dump pass, so the macro index is co-located with the AST
# and downstream consumers can index into it without reopening source
# files.
#
# This list MUST stay in sync with `MACRO_SUBSET` in
# `translated/pipeline/src/bin/purity_detect.rs`. If you add a macro
# there, add it here.
POSTGRES_MACRO_SUBSET = (
    "PG_TRY", "PG_CATCH", "PG_END_TRY", "PG_RE_THROW",
    "ereport", "elog",
    "PG_FUNCTION_INFO_V1",
    "PG_RETURN_INT32", "PG_RETURN_BOOL",
    "PG_GETARG_INT32",
    "START_CRIT_SECTION", "END_CRIT_SECTION",
    "SpinLockAcquire", "SpinLockRelease",
    "OidFunctionCall0", "OidFunctionCall1", "OidFunctionCall2",
    "DirectFunctionCall1", "DirectFunctionCall2",
    "heap_open", "heap_close",
    "AllocSetContextCreate",
    "GetCurrentMemoryContext",
)

# Flags we preserve when re-invoking clang. Everything else from the
# recorded command (compiler choice, -c, -o, -MD, -MF, -MT, optimization
# flags that don't affect AST shape) is stripped. We add our own
# -Xclang -ast-dump=json -fsyntax-only.
KEEP_BARE = {
    "-pthread", "-m32", "-m64", "-fno-builtin", "-fno-strict-aliasing",
    "-fno-omit-frame-pointer", "-fno-asynchronous-unwind-tables",
    "-fno-stack-protector", "-fwrapv",
}
KEEP_PREFIX_INLINE = (
    "-I", "-D", "-U", "-isystem=", "-iquote=", "-isysroot=",
    "-std=", "-target", "-march=", "-mcpu=", "-mtune=",
    "-fno-",
)
# Flags that take a separate argument we must also keep.
KEEP_TWO_ARG = {"-isystem", "-iquote", "-isysroot", "-include", "-target"}


def _filter_flags(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in KEEP_TWO_ARG:
            if i + 1 < len(argv):
                out.append(tok)
                out.append(argv[i + 1])
                i += 2
                continue
        if tok in KEEP_BARE:
            out.append(tok)
        elif any(tok.startswith(p) for p in KEEP_PREFIX_INLINE):
            out.append(tok)
        i += 1
    return out


def _entry_argv(entry: dict[str, Any]) -> list[str]:
    """Recover argv from either 'arguments' (list) or 'command' (shell str)."""
    if "arguments" in entry:
        return list(entry["arguments"])
    if "command" in entry:
        return shlex.split(entry["command"])
    raise ValueError(f"compile_commands entry has neither 'arguments' nor 'command': {entry!r}")


def _scan_macros(src_path: str, macro_names: tuple[str, ...]) -> dict[str, list[dict[str, int]]]:
    """Whole-word scan a source file for occurrences of any name in
    `macro_names`. Returns a dict mapping macro_name → list of
    `{"line": N}` hits (one entry per line that contains the macro,
    not per occurrence within a line — deduplicating intra-line
    repeats keeps the sidecar small without losing useful information).

    Behavior is intentionally permissive: a line containing `PG_TRY`
    inside a comment or string still counts as a hit. For the v0
    purpose ("does this function body touch a backend macro?") the
    false-positive rate is dominated by intentional uses.

    Read failures (missing file, bad UTF-8) return an empty dict and
    are silently tolerated — the caller can log if it cares.
    """
    hits: dict[str, list[dict[str, int]]] = {}
    try:
        with open(src_path, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                for name in macro_names:
                    if name not in line:
                        continue
                    # Whole-word match: ensure boundary is not an
                    # identifier byte on either side.
                    idx = 0
                    matched = False
                    while True:
                        pos = line.find(name, idx)
                        if pos < 0:
                            break
                        prev_ok = pos == 0 or not (line[pos - 1].isalnum() or line[pos - 1] == "_")
                        next_idx = pos + len(name)
                        next_ok = next_idx == len(line) or not (
                            line[next_idx].isalnum() or line[next_idx] == "_"
                        )
                        if prev_ok and next_ok:
                            matched = True
                            break
                        idx = pos + 1
                    if matched:
                        hits.setdefault(name, []).append({"line": lineno})
    except OSError:
        return {}
    return hits


def _resolve_source(entry: dict[str, Any]) -> str:
    """Canonicalize the 'file' to an absolute (or workspace-relative) path."""
    file = entry["file"]
    if os.path.isabs(file):
        return file
    directory = entry.get("directory", ".")
    return os.path.normpath(os.path.join(directory, file))


def _substitute_headers_tree(s: str, headers_tree: str | None) -> str | None:
    """Replace the ${MESON_HEADERS_TREE} sentinel placed in compdb args
    by rules_meson's meson_runner. The sentinel marks `-I` paths that
    originally pointed at meson's now-gone build dir; we substitute the
    actual TreeArtifact path the consumer provides.

    Returns None if the sentinel appears but `headers_tree` wasn't
    provided — the caller treats this as "drop this flag" since clang
    would fail to resolve it anyway.
    """
    if "${MESON_HEADERS_TREE}" not in s:
        return s
    if headers_tree is None:
        return None
    return s.replace("${MESON_HEADERS_TREE}", headers_tree)


def _process(
    entry: dict[str, Any],
    clang: str,
    out_dir: str,
    source_root: str,
    extra_copts: list[str],
    headers_tree: str | None,
    mode: str,
    opt_level: str,
    emit_macros: bool,
) -> tuple[str, str | None, str | None]:
    """Run clang on one entry. Returns (source, error_or_None, output_or_None).

    mode: "ast" → stdout-captured `-ast-dump=json`. "llvm" → file-captured
    `-S -emit-llvm -O<opt_level>`. The output path's extension reflects
    the mode.

    emit_macros: also write a `<rel>.macros.json` sidecar with
    whole-word hits of `POSTGRES_MACRO_SUBSET` in the source. Used
    downstream to recover macro references that clang's preprocessor
    eliminated before the AST was built."""
    src = _resolve_source(entry)
    if not os.path.exists(src):
        return (src, f"source file not found in sandbox: {src}", None)

    # Compute output path under out_dir, mirroring the source path
    # relative to source_root. Extension by mode:
    #   ast  → out_dir/src/backend/utils/adt/numeric.c.ast.json
    #   llvm → out_dir/src/backend/utils/adt/numeric.c.ll
    try:
        rel = os.path.relpath(src, source_root)
    except ValueError:
        rel = os.path.basename(src)
    suffix = ".ast.json" if mode == "ast" else ".ll"
    out_path = os.path.join(out_dir, rel + suffix)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    argv = _entry_argv(entry)
    flags = _filter_flags(argv[1:])  # argv[0] is the recorded compiler

    # Substitute the ${MESON_HEADERS_TREE} sentinel that rules_meson's
    # meson_runner places in -I/-isystem flags pointing at meson's
    # (since-destroyed) build dir.
    substituted: list[str] = []
    for f in flags:
        s = _substitute_headers_tree(f, headers_tree)
        if s is None:
            # Sentinel present but no headers_tree provided — drop the
            # flag rather than passing an unresolvable path to clang.
            continue
        substituted.append(s)
    flags = substituted

    if mode == "ast":
        cmd = [clang, "-Xclang", "-ast-dump=json", "-fsyntax-only"] + flags + extra_copts + [src]
    elif mode == "llvm":
        cmd = [clang, "-S", "-emit-llvm", f"-O{opt_level}"] + flags + extra_copts + [src, "-o", out_path]
    else:
        return (src, f"unknown mode: {mode}", None)
    cwd = entry.get("directory", os.getcwd())
    if not os.path.isdir(cwd):
        # Recorded directory doesn't exist in the sandbox — fall back to
        # cwd. The recorded -I paths may still resolve via -isysroot or
        # absolute -I rewrites.
        cwd = os.getcwd()

    # Macro sidecar: emit before invoking clang so the scan happens
    # even when AST extraction fails. The macro index is a pure
    # source-text operation; downstream tools (purity/cluster) can use
    # it even for TUs that fail to clang-parse.
    if emit_macros:
        macros_path = os.path.join(out_dir, rel + ".macros.json")
        os.makedirs(os.path.dirname(macros_path), exist_ok=True)
        macro_hits = _scan_macros(src, POSTGRES_MACRO_SUBSET)
        sidecar = {
            "source": src,
            "scanned_macros": list(POSTGRES_MACRO_SUBSET),
            "hits": macro_hits,
        }
        with open(macros_path, "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh, indent=2)

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        return (src, f"clang invocation failed: {e}", None)

    if result.returncode != 0:
        err_tail = (result.stderr or "")[-800:]
        return (src, f"clang exited {result.returncode}\n{err_tail}", None)

    if mode == "ast":
        # AST mode emits JSON to stdout; capture.
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(result.stdout)
    # llvm mode emits to out_path via -o; nothing to write.

    return (src, None, out_path)


def _matches_filter(file: str, filters: list[str]) -> bool:
    if not filters:
        return True
    return any(fnmatch.fnmatch(file, pat) for pat in filters)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--compile-commands", required=True)
    p.add_argument(
        "--clang",
        required=True,
        help="Path to the clang binary to invoke (typically resolved from "
             "the cc_toolchain by the wrapping Bazel rule).",
    )
    p.add_argument("--out-dir", required=True)
    p.add_argument(
        "--source-root",
        default=".",
        help="Path to compute output relpaths against. Typically the "
             "workspace root or the upstream codebase's root directory.",
    )
    p.add_argument(
        "--manifest",
        required=True,
        help="JSON file listing successfully dumped TUs.",
    )
    p.add_argument(
        "--filter",
        action="append",
        default=[],
        help="Glob filter (fnmatch) on entry['file']. May be repeated. "
             "If unset, all entries are processed.",
    )
    p.add_argument(
        "--extra-copt",
        action="append",
        default=[],
        help="Extra flag to append to every clang invocation (e.g. "
             "-Wno-everything to suppress per-TU diagnostics).",
    )
    p.add_argument(
        "--headers-tree",
        default=None,
        help="Path to a directory containing meson-generated headers "
             "(typically the TreeArtifact from rules_meson's "
             "meson_configure rule). Substituted into compile_commands.json "
             "wherever the ${MESON_HEADERS_TREE} sentinel appears.",
    )
    p.add_argument(
        "--mode",
        default="ast",
        choices=["ast", "llvm"],
        help="ast (default): emit .ast.json via -Xclang -ast-dump=json "
             "-fsyntax-only. llvm: emit .ll via -S -emit-llvm -O<level>.",
    )
    p.add_argument(
        "--opt-level",
        default="0",
        choices=["0", "1", "2", "3", "s", "z"],
        help="clang -O level for --mode llvm. Default 0 (no opts; keeps "
             "IR structurally aligned with source for translation work).",
    )
    p.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="Worker count. 0 (default) = os.cpu_count() or 4.",
    )
    p.add_argument(
        "--emit-macros",
        action="store_true",
        help="Also write `<rel>.macros.json` per TU recording whole-word "
             "hits of POSTGRES_MACRO_SUBSET in the source (proper v2 fix "
             "for clang's macro-disappearance — see "
             "translated/pipeline/src/bin/purity_detect.rs module docs).",
    )
    args = p.parse_args()

    with open(args.compile_commands, "r", encoding="utf-8") as fh:
        entries = json.load(fh)

    if not isinstance(entries, list):
        print(f"compile_commands.json must be a JSON array, got {type(entries).__name__}",
              file=sys.stderr)
        return 1

    filtered = [e for e in entries if _matches_filter(e.get("file", ""), args.filter)]
    print(f"[dump_from_compdb] {len(filtered)}/{len(entries)} entries match filter",
          file=sys.stderr)

    os.makedirs(args.out_dir, exist_ok=True)

    successes: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []

    jobs = args.jobs if args.jobs > 0 else (os.cpu_count() or 4)
    headers_tree = None
    if args.headers_tree:
        headers_tree = os.path.abspath(args.headers_tree)

    with ProcessPoolExecutor(max_workers=jobs) as exe:
        futures = {
            exe.submit(_process, e, args.clang, args.out_dir,
                       args.source_root, args.extra_copt, headers_tree,
                       args.mode, args.opt_level, args.emit_macros): e
            for e in filtered
        }
        for fut in as_completed(futures):
            src, err, out = fut.result()
            if err is not None:
                failures.append((src, err))
                print(f"FAIL {src}: {err.splitlines()[0]}", file=sys.stderr)
            else:
                assert out is not None
                successes.append((src, out))

    output_key = "ast_json" if args.mode == "ast" else "llvm_ir"
    manifest = {
        "mode": args.mode,
        "compileCommands": args.compile_commands,
        "sourceRoot": args.source_root,
        "ok": [{"source": s, output_key: o} for (s, o) in successes],
        "failed": [{"source": s, "error": e} for (s, e) in failures],
        "stats": {
            "total": len(filtered),
            "succeeded": len(successes),
            "failed": len(failures),
        },
    }
    with open(args.manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"[dump_from_compdb] {len(successes)} succeeded, "
          f"{len(failures)} failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
