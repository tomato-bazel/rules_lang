#!/usr/bin/env python3
"""Batch driver for the C→Rust translation pipeline.

Reads a `translation_queue.toml` (see `translated/translation_queue.toml`
for the schema in comments) and for each cluster member, runs:

    1. c_cluster_diff.py   --canonical / --sibling   → C-bindings TOML
    2. c_to_rust_bindings.py --overrides             → Rust-bindings TOML
    3. translate-engine emit --canonical/--sibling/--bindings --out

Emitted Rust source lands in `<cluster.out_dir>/<fn>.rs`. A summary
table is printed at the end. `--dry-run` prints the would-be commands
without executing anything; useful for verifying the queue.

Stdlib-only. Does NOT modify any of the underlying tools — those are
shelled out to. Intentionally scope-limited to batch invocation; the
"synthesize sibling" workflow (no hand-written sibling_rs) is a
separate driver's job and not handled here.
"""

from __future__ import annotations

import argparse
import dataclasses
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# ─── Queue model ──────────────────────────────────────────────────────


@dataclass
class Defaults:
    tensor: Path
    engine: Path
    # Path to the Rust pipeline binaries (since 2026-05-25 — migrated
    # from `c/cli/*.py`). Defaults to `target/release/<bin>` under
    # the pipeline crate; queue TOML can override.
    c_cluster_diff: Path
    c_to_rust_bindings: Path


@dataclass
class Member:
    fn: str
    sibling_rs: Path
    overrides_toml: Path


@dataclass
class Cluster:
    name: str
    canonical_fn: str
    canonical_rs: Path
    out_dir: Path
    members: list[Member] = field(default_factory=list)


@dataclass
class Queue:
    defaults: Defaults
    clusters: list[Cluster]


def _resolve(repo: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (repo / q)


def load_queue(path: Path, repo: Path) -> Queue:
    with path.open("rb") as f:
        raw = tomllib.load(f)

    d = raw.get("defaults", {})
    defaults = Defaults(
        tensor=_resolve(repo, d.get("tensor", "")),
        engine=_resolve(repo, d.get("engine", "")),
        c_cluster_diff=_resolve(
            repo,
            d.get(
                "c_cluster_diff",
                "translated/pipeline/target/release/c_cluster_diff",
            ),
        ),
        c_to_rust_bindings=_resolve(
            repo,
            d.get(
                "c_to_rust_bindings",
                "translated/pipeline/target/release/c_to_rust_bindings",
            ),
        ),
    )

    clusters: list[Cluster] = []
    for c in raw.get("cluster", []):
        members = [
            Member(
                fn=m["fn"],
                sibling_rs=_resolve(repo, m["sibling_rs"]),
                overrides_toml=_resolve(repo, m["overrides_toml"]),
            )
            for m in c.get("member", [])
        ]
        clusters.append(
            Cluster(
                name=c["name"],
                canonical_fn=c["canonical_fn"],
                canonical_rs=_resolve(repo, c["canonical_rs"]),
                out_dir=_resolve(repo, c["out_dir"]),
                members=members,
            )
        )

    return Queue(defaults=defaults, clusters=clusters)


# ─── Job execution ────────────────────────────────────────────────────


@dataclass
class StepResult:
    name: str
    cmd: list[str]
    returncode: int
    stderr_tail: str = ""


@dataclass
class JobResult:
    cluster: str
    fn: str
    status: str  # "PASS" | "FAIL" | "SKIP" | "DRY"
    steps: list[StepResult] = field(default_factory=list)
    out_path: Path | None = None
    reason: str = ""


def _short(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def _run(name: str, cmd: list[str], cwd: Path, *,
         stdin_path: Path | None = None,
         stdout_path: Path | None = None) -> StepResult:
    stdin_f = stdin_path.open("rb") if stdin_path else None
    stdout_f = stdout_path.open("wb") if stdout_path else subprocess.PIPE
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdin=stdin_f,
            stdout=stdout_f,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        if stdin_f:
            stdin_f.close()
        if isinstance(stdout_f, type(sys.stdout)) or hasattr(stdout_f, "close") and stdout_path:
            stdout_f.close()

    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    tail = "\n".join(err.strip().splitlines()[-8:])
    return StepResult(name=name, cmd=cmd, returncode=proc.returncode,
                      stderr_tail=tail)


def run_member(
    queue: Queue, cluster: Cluster, member: Member, repo: Path,
    work_root: Path, *, dry_run: bool,
) -> JobResult:
    job = JobResult(cluster=cluster.name, fn=member.fn, status="DRY" if dry_run else "PASS")

    work = work_root / cluster.name / member.fn
    out_dir = cluster.out_dir
    out_path = out_dir / f"{member.fn}.rs"
    job.out_path = out_path

    c_bindings = work / "c_bindings.toml"
    rust_bindings = work / "rust_bindings.toml"

    step1_cmd = [
        str(queue.defaults.c_cluster_diff),
        "--tensor-dir", str(queue.defaults.tensor),
        "--canonical", cluster.canonical_fn,
        "--sibling", member.fn,
    ]
    step2_cmd = [
        str(queue.defaults.c_to_rust_bindings),
        "--input", str(c_bindings),
        "--overrides", str(member.overrides_toml),
    ]
    step3_cmd = [
        str(queue.defaults.engine), "emit",
        "--canonical", str(cluster.canonical_rs),
        "--sibling", str(member.sibling_rs),
        "--bindings", str(rust_bindings),
        "--out", str(out_path),
    ]

    if dry_run:
        job.steps = [
            StepResult("c_cluster_diff", step1_cmd, 0, ""),
            StepResult("c_to_rust_bindings", step2_cmd, 0, ""),
            StepResult("translate_engine_emit", step3_cmd, 0, ""),
        ]
        return job

    # Pre-flight sanity: required inputs must exist.
    missing: list[str] = []
    for label, p in [
        ("tensor", queue.defaults.tensor),
        ("engine", queue.defaults.engine),
        ("canonical_rs", cluster.canonical_rs),
        ("sibling_rs", member.sibling_rs),
        ("overrides_toml", member.overrides_toml),
    ]:
        if not p.exists():
            missing.append(f"{label}={p}")
    if missing:
        job.status = "SKIP"
        job.reason = "missing inputs: " + ", ".join(missing)
        return job

    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: c_cluster_diff → c_bindings.toml (stdout-redirected).
    s1 = _run("c_cluster_diff", step1_cmd, cwd=repo, stdout_path=c_bindings)
    job.steps.append(s1)
    if s1.returncode != 0:
        job.status = "FAIL"
        job.reason = "c_cluster_diff failed"
        return job

    # Step 2: c_to_rust_bindings → rust_bindings.toml.
    s2 = _run("c_to_rust_bindings", step2_cmd, cwd=repo, stdout_path=rust_bindings)
    job.steps.append(s2)
    if s2.returncode != 0:
        job.status = "FAIL"
        job.reason = "c_to_rust_bindings failed"
        return job

    # Step 3: translate-engine emit → out_dir/<fn>.rs.
    s3 = _run("translate_engine_emit", step3_cmd, cwd=repo)
    job.steps.append(s3)
    if s3.returncode != 0:
        job.status = "FAIL"
        job.reason = "translate-engine emit failed"
        return job

    if not out_path.exists():
        job.status = "FAIL"
        job.reason = f"emit produced no file at {out_path}"
        return job

    return job


# ─── CLI ──────────────────────────────────────────────────────────────


def _print_dry_run(results: list[JobResult]) -> None:
    for r in results:
        print(f"\n# {r.cluster} :: {r.fn} → {r.out_path}")
        for s in r.steps:
            print(f"  $ {_short(s.cmd)}")


def _print_summary(results: list[JobResult]) -> None:
    print("")
    print(f"{'cluster':<28} {'fn':<28} {'status':<6}  detail")
    print("-" * 90)
    for r in results:
        detail = r.reason or (str(r.out_path) if r.out_path else "")
        print(f"{r.cluster:<28} {r.fn:<28} {r.status:<6}  {detail}")

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("")
    print("totals: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--queue", default="translated/translation_queue.toml",
                   help="Path to translation_queue.toml (repo-relative or absolute).")
    p.add_argument("--repo", default="/Volumes/Workspace/rules_lang",
                   help="rules_lang repo root, used to resolve relative paths.")
    p.add_argument("--work-root", default=None,
                   help="Where to put intermediate per-member bindings TOMLs. "
                        "Default: <repo>/target/translation_queue/")
    p.add_argument("--cluster", default=None,
                   help="Run only this named cluster.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands that would be run; do not execute.")
    p.add_argument("--stop-on-fail", action="store_true",
                   help="Abort after the first member failure.")
    args = p.parse_args(argv)

    repo = Path(args.repo).resolve()
    queue_path = _resolve(repo, args.queue)
    if not queue_path.exists():
        print(f"queue file not found: {queue_path}", file=sys.stderr)
        return 2

    queue = load_queue(queue_path, repo)

    work_root = Path(args.work_root) if args.work_root else (repo / "target" / "translation_queue")
    if not args.dry_run:
        work_root.mkdir(parents=True, exist_ok=True)

    selected = [c for c in queue.clusters if (args.cluster is None or c.name == args.cluster)]
    if not selected:
        print(f"no clusters matched (have: {[c.name for c in queue.clusters]})",
              file=sys.stderr)
        return 2

    results: list[JobResult] = []
    for cluster in selected:
        for member in cluster.members:
            r = run_member(queue, cluster, member, repo, work_root, dry_run=args.dry_run)
            results.append(r)
            if not args.dry_run and r.status == "FAIL":
                # Print the failing step's stderr tail immediately.
                for s in r.steps:
                    if s.returncode != 0 and s.stderr_tail:
                        print(f"--- {cluster.name}/{member.fn} :: {s.name} stderr ---",
                              file=sys.stderr)
                        print(s.stderr_tail, file=sys.stderr)
                if args.stop_on_fail:
                    break
        else:
            continue
        break

    if args.dry_run:
        _print_dry_run(results)
        return 0

    _print_summary(results)
    return 0 if all(r.status == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
