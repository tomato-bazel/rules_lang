"""Polyglot.Sql — the SQL parse/projection Bazel axis.

A `proto_library`-shaped layering for SQL: raw sources at the top,
parsed AST in the middle, projection rules (json / proto / lean /
catalog) at the bottom. Each dialect (postgres, sqlite, …) plugs in via
a toolchain implementing one of the per-dialect toolchain types
declared in `//polyglot/sql:BUILD.bazel`.

    sql_library                 ← raw .sql sources, dialect-tagged
        │   SqlInfo {srcs, dialect}
        ▼
    sql_ast_library             ← dialect parser → canonical AST file
        │   SqlAstInfo {asts: [(sql, ast, format)], dialect}
        │
        ├──► sql_json_library   (future projection — AST as JSON)
        ├──► sql_proto_library  (future projection — AST as protobuf bytes)
        ├──► sql_lean_library   (future projection — AST decoded into Lean)
        │
        └──► sql_catalog_library
                 SqlCatalogInfo {snapshot: lean/json/ttl, dialect}
                 ← folds DDL stmt-by-stmt and emits a cumulative
                 `Pg.Catalog.Snapshot` (or dialect-equivalent).

Aspect: `sql_ast_aspect` propagates over `deps` of any rule and attaches
an AST artifact per transitively-reachable `sql_library` source. Useful
for sweeps that want to lint every SQL in a build closure without
declaring a parse rule per file.

This skeleton ships:
  * `SqlInfo`, `SqlAstInfo`, `SqlCatalogInfo`, `SqlToolchainInfo`
  * `sql_library`
  * `sql_ast_library`         (postgres dialect path wired; sqlite stub)
  * `sql_catalog_library`     (postgres path delegates to a Python
                              tool that decodes `.pgpb` via protoc-
                              generated bindings and emits a Lean
                              `Pg.Catalog.Snapshot`)
  * `sql_ast_aspect`

The json / proto / lean projection rules are placeholders pending the
proto-grounded Lean codegen track (see the "future" comment in the
roadmap — `Pg.Ast` generated from `@libpg_query//:pg_query.proto`).
"""

# ─── Providers ────────────────────────────────────────────────────

SqlInfo = provider(
    doc = """Carries raw SQL source files plus their declared dialect.

    Surfaced by `sql_library` and propagated through `deps`. The
    `dialect` is what tells `sql_ast_library` which toolchain to
    resolve.""",
    fields = {
        "srcs": "depset[File] — `.sql` source files (transitively).",
        "dialect": "string — 'postgres' | 'sqlite' | …",
    },
)

SqlAstInfo = provider(
    doc = """Carries parsed AST files alongside their SQL sources.

    Each entry of `asts` is a struct(sql=File, ast=File, format=string).
    `format` lets downstream projection rules pick the correct decoder
    (e.g. 'libpg_query_protobuf' vs 'sqlite_native').""",
    fields = {
        "asts": "depset[struct(sql: File, ast: File, format: string)]",
        "dialect": "string",
    },
)

SqlCatalogInfo = provider(
    doc = """Carries a cumulative catalog snapshot folded over DDLs.

    The `snapshot` File is the final emitted artifact (Lean source by
    default; future support for JSON/TTL via the `output_format`
    attribute). `dialect` matches the upstream `SqlAstInfo.dialect`.""",
    fields = {
        "snapshot": "File — emitted `Pg.Catalog.Snapshot` artifact.",
        "dialect": "string",
        "output_format": "string — 'lean' | 'json' | 'ttl'",
    },
)

SqlToolchainInfo = provider(
    doc = """The dialect-specific parser binary + format contract.

    Every `*_sql_toolchain` rule emits this. `parser` is invoked as
    `<parser> <input.sql> > <output.ast>`. `parser_format` is what
    downstream projection rules use to pick the right decoder.""",
    fields = {
        "parser": "File — executable; consumes .sql arg, emits AST on stdout.",
        "parser_format": "string — 'libpg_query_protobuf' | …",
        "proto_descriptor": "File or None — .proto schema (if AST is proto-shaped).",
        "version": "string — parser version (e.g. '17-6.2.2').",
        "dialect": "string — matches the toolchain_type's dialect tag.",
    },
)

# ─── Toolchain-type label constants ───────────────────────────────

POSTGRES_TOOLCHAIN_TYPE = Label("//polyglot/sql:postgres_toolchain_type")
SQLITE_TOOLCHAIN_TYPE = Label("//polyglot/sql:sqlite_toolchain_type")

_TOOLCHAIN_TYPE_FOR_DIALECT = {
    "postgres": POSTGRES_TOOLCHAIN_TYPE,
    "sqlite": SQLITE_TOOLCHAIN_TYPE,
}

# ─── sql_library ──────────────────────────────────────────────────

def _sql_library_impl(ctx):
    transitive = [d[SqlInfo].srcs for d in ctx.attr.deps]
    srcs = depset(direct = ctx.files.srcs, transitive = transitive)

    # Dialect-consistency check across deps.
    for d in ctx.attr.deps:
        if d[SqlInfo].dialect != ctx.attr.dialect:
            fail("sql_library %s: dialect mismatch — self=%s, dep %s=%s" % (
                ctx.label,
                ctx.attr.dialect,
                d.label,
                d[SqlInfo].dialect,
            ))
    return [
        SqlInfo(srcs = srcs, dialect = ctx.attr.dialect),
        DefaultInfo(files = srcs),
    ]

sql_library = rule(
    implementation = _sql_library_impl,
    attrs = {
        "srcs": attr.label_list(
            allow_files = [".sql"],
            doc = "SQL source files contributed by this library.",
        ),
        "deps": attr.label_list(
            providers = [SqlInfo],
            doc = "Other `sql_library` targets to roll up. Must match dialect.",
        ),
        "dialect": attr.string(
            default = "postgres",
            values = ["postgres", "sqlite"],
            doc = "SQL dialect of the sources.",
        ),
    },
    provides = [SqlInfo],
    doc = """Declares a set of `.sql` source files of a single dialect.""",
)

# ─── sql_ast_library ──────────────────────────────────────────────

def _toolchain_for(ctx, dialect):
    tc_type = _TOOLCHAIN_TYPE_FOR_DIALECT.get(dialect)
    if tc_type == None:
        fail("sql_ast_library: unknown dialect %r" % dialect)
    tc = ctx.toolchains[tc_type]
    if tc == None:
        fail("sql_ast_library: no toolchain registered for %s. " % dialect +
             "Register one implementing %s." % tc_type)
    return tc.sqltoolchaininfo

def _sql_ast_library_impl(ctx):
    # Collect all .sql files from transitive SqlInfo.
    transitive_srcs = depset(transitive = [
        d[SqlInfo].srcs
        for d in ctx.attr.deps
    ])

    # Cross-dep dialect check.
    dialects = {d[SqlInfo].dialect: True for d in ctx.attr.deps}
    if len(dialects) > 1:
        fail("sql_ast_library %s: mixed dialects in deps: %s" %
             (ctx.label, list(dialects.keys())))
    dialect = list(dialects.keys())[0] if dialects else "postgres"
    tc = _toolchain_for(ctx, dialect)

    ast_entries = []
    for sql_file in transitive_srcs.to_list():
        # Output file: <parent_dir>/<basename>.pgpb (or dialect ext).
        ext = ".pgpb" if tc.parser_format == "libpg_query_protobuf" else ".ast"
        ast_out = ctx.actions.declare_file(
            ctx.label.name + "/" + sql_file.basename + ext,
        )
        ctx.actions.run_shell(
            inputs = [sql_file],
            outputs = [ast_out],
            tools = [tc.parser],
            command = "{parser} {sql} > {out}".format(
                parser = tc.parser.path,
                sql = sql_file.path,
                out = ast_out.path,
            ),
            mnemonic = "SqlParse",
            progress_message = "Parsing %s (%s)" % (sql_file.short_path, dialect),
        )
        ast_entries.append(struct(
            sql = sql_file,
            ast = ast_out,
            format = tc.parser_format,
        ))

    asts = depset(direct = ast_entries)
    all_outs = [e.ast for e in ast_entries]
    return [
        SqlAstInfo(asts = asts, dialect = dialect),
        DefaultInfo(files = depset(direct = all_outs)),
    ]

sql_ast_library = rule(
    implementation = _sql_ast_library_impl,
    attrs = {
        "deps": attr.label_list(
            providers = [SqlInfo],
            mandatory = True,
            doc = "`sql_library` targets to parse.",
        ),
    },
    toolchains = [
        config_common.toolchain_type(POSTGRES_TOOLCHAIN_TYPE, mandatory = False),
        config_common.toolchain_type(SQLITE_TOOLCHAIN_TYPE, mandatory = False),
    ],
    provides = [SqlAstInfo],
    doc = """Parses each `.sql` source in `deps` via the dialect's toolchain.

    Output is one AST file per source (currently `.pgpb` for postgres).
    Downstream `sql_*_library` projection rules consume the resulting
    `SqlAstInfo`.""",
)

# ─── sql_catalog_library ──────────────────────────────────────────

def _sql_catalog_library_impl(ctx):
    ast_info = ctx.attr.deps[0][SqlAstInfo]
    for d in ctx.attr.deps[1:]:
        if d[SqlAstInfo].dialect != ast_info.dialect:
            fail("sql_catalog_library %s: mixed dialects" % ctx.label)

    # Gather ast files; sort by sql short_path so the DDL fold is
    # deterministic regardless of dep declaration order. (Real-world
    # migrations encode ordering in the filename — `010_*.sql` before
    # `020_*.sql` — so lexicographic order is what we want.)
    entries = []
    for d in ctx.attr.deps:
        entries.extend(d[SqlAstInfo].asts.to_list())
    entries = sorted(entries, key = lambda e: e.sql.short_path)
    ast_files = [e.ast for e in entries]

    ext = ctx.attr.output_format
    out = ctx.actions.declare_file(
        ctx.label.name + ("." + ext if ext != "lean" else ".lean"),
    )
    args = ctx.actions.args()
    args.add("--module", ctx.attr.module_name or ctx.label.name)
    args.add("--output", out.path)
    if ctx.attr.output_format != "lean":
        args.add("--format", ctx.attr.output_format)
    for ast in ast_files:
        args.add(ast.path)

    folder = ctx.attr.folder.files_to_run.executable
    ctx.actions.run(
        inputs = ast_files,
        outputs = [out],
        executable = folder,
        arguments = [args],
        mnemonic = "SqlCatalog",
        progress_message = "Folding %d DDL ASTs → %s" % (len(ast_files), out.short_path),
    )
    return [
        SqlCatalogInfo(
            snapshot = out,
            dialect = ast_info.dialect,
            output_format = ctx.attr.output_format,
        ),
        DefaultInfo(files = depset(direct = [out])),
    ]

sql_catalog_library = rule(
    implementation = _sql_catalog_library_impl,
    attrs = {
        "deps": attr.label_list(
            providers = [SqlAstInfo],
            mandatory = True,
            doc = "`sql_ast_library` targets whose ASTs should be folded.",
        ),
        "folder": attr.label(
            executable = True,
            cfg = "exec",
            mandatory = True,
            doc = "Dialect-specific catalog-folder binary. Consumes one " +
                  "or more AST files (in lexicographic order by short_path) " +
                  "and emits a snapshot file. Invocation:\n" +
                  "    <folder> --module <NAME> --output <FILE> " +
                  "[--format <fmt>] <ast> [<ast> ...]\n" +
                  "For postgres: `@rules_postgres//tools:pgpb_to_snapshot`.",
        ),
        "module_name": attr.string(
            doc = "Lean (or analogous) module name (defaults to target name).",
        ),
        "output_format": attr.string(
            default = "lean",
            values = ["lean", "json", "ttl"],
            doc = "Output projection format.",
        ),
    },
    provides = [SqlCatalogInfo],
    doc = """Folds a sequence of parsed DDL ASTs into a catalog snapshot.

    Walks every CREATE SCHEMA / DOMAIN / TYPE / TABLE / FUNCTION across
    the transitive `sql_ast_library` closure, maintains running catalog
    state, and emits a single snapshot file in the requested format.

    Dialect-neutral: dispatches to the `folder` binary, which the
    dialect's ecosystem supplies (e.g. rules_postgres provides
    `@rules_postgres//tools:pgpb_to_snapshot`). For convenience,
    dialect-specific wrappers (`pg_sql_catalog_library`) pre-fill
    `folder` so consumers don't have to.""",
)

# ─── Aspect: parse every SQL source reachable via deps ────────────

def _sql_ast_aspect_impl(target, ctx):
    if SqlInfo not in target:
        return []
    sql_info = target[SqlInfo]
    dialect = sql_info.dialect
    tc_type = _TOOLCHAIN_TYPE_FOR_DIALECT.get(dialect)
    if tc_type == None or ctx.toolchains[tc_type] == None:
        return []
    tc = ctx.toolchains[tc_type].sqltoolchaininfo

    ast_entries = []
    for sql_file in sql_info.srcs.to_list():
        ext = ".pgpb" if tc.parser_format == "libpg_query_protobuf" else ".ast"
        ast_out = ctx.actions.declare_file(
            ctx.label.name + ".sql_ast/" + sql_file.basename + ext,
        )
        ctx.actions.run_shell(
            inputs = [sql_file],
            outputs = [ast_out],
            tools = [tc.parser],
            command = "{parser} {sql} > {out}".format(
                parser = tc.parser.path,
                sql = sql_file.path,
                out = ast_out.path,
            ),
            mnemonic = "SqlParseAspect",
            progress_message = "Parsing %s (%s)" % (sql_file.short_path, dialect),
        )
        ast_entries.append(struct(
            sql = sql_file,
            ast = ast_out,
            format = tc.parser_format,
        ))
    return [SqlAstInfo(
        asts = depset(direct = ast_entries),
        dialect = dialect,
    )]

sql_ast_aspect = aspect(
    implementation = _sql_ast_aspect_impl,
    attr_aspects = ["deps"],
    toolchains = [
        config_common.toolchain_type(POSTGRES_TOOLCHAIN_TYPE, mandatory = False),
        config_common.toolchain_type(SQLITE_TOOLCHAIN_TYPE, mandatory = False),
    ],
    doc = """Propagates over `deps`, attaching `SqlAstInfo` to every
    transitive `sql_library`. Lets downstream rules consume parsed ASTs
    without per-file `sql_ast_library` declarations.""",
)
