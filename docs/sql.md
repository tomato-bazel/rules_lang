<!-- Generated with Stardoc: http://skydoc.bazel.build -->

Polyglot.Sql — the SQL parse/projection Bazel axis.

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

<a id="sql_ast_library"></a>

## sql_ast_library

<pre>
load("@rules_lang//polyglot:sql.bzl", "sql_ast_library")

sql_ast_library(<a href="#sql_ast_library-name">name</a>, <a href="#sql_ast_library-deps">deps</a>)
</pre>

Parses each `.sql` source in `deps` via the dialect's toolchain.

Output is one AST file per source (currently `.pgpb` for postgres).
Downstream `sql_*_library` projection rules consume the resulting
`SqlAstInfo`.

**ATTRIBUTES**


| Name  | Description | Type | Mandatory | Default |
| :------------- | :------------- | :------------- | :------------- | :------------- |
| <a id="sql_ast_library-name"></a>name |  A unique name for this target.   | <a href="https://bazel.build/concepts/labels#target-names">Name</a> | required |  |
| <a id="sql_ast_library-deps"></a>deps |  `sql_library` targets to parse.   | <a href="https://bazel.build/concepts/labels">List of labels</a> | required |  |


<a id="sql_catalog_library"></a>

## sql_catalog_library

<pre>
load("@rules_lang//polyglot:sql.bzl", "sql_catalog_library")

sql_catalog_library(<a href="#sql_catalog_library-name">name</a>, <a href="#sql_catalog_library-deps">deps</a>, <a href="#sql_catalog_library-folder">folder</a>, <a href="#sql_catalog_library-module_name">module_name</a>, <a href="#sql_catalog_library-output_format">output_format</a>)
</pre>

Folds a sequence of parsed DDL ASTs into a catalog snapshot.

Walks every CREATE SCHEMA / DOMAIN / TYPE / TABLE / FUNCTION across
the transitive `sql_ast_library` closure, maintains running catalog
state, and emits a single snapshot file in the requested format.

Dialect-neutral: dispatches to the `folder` binary, which the
dialect's ecosystem supplies (e.g. rules_postgres provides
`@rules_postgres//tools:pgpb_to_snapshot`). For convenience,
dialect-specific wrappers (`pg_sql_catalog_library`) pre-fill
`folder` so consumers don't have to.

**ATTRIBUTES**


| Name  | Description | Type | Mandatory | Default |
| :------------- | :------------- | :------------- | :------------- | :------------- |
| <a id="sql_catalog_library-name"></a>name |  A unique name for this target.   | <a href="https://bazel.build/concepts/labels#target-names">Name</a> | required |  |
| <a id="sql_catalog_library-deps"></a>deps |  `sql_ast_library` targets whose ASTs should be folded.   | <a href="https://bazel.build/concepts/labels">List of labels</a> | required |  |
| <a id="sql_catalog_library-folder"></a>folder |  Dialect-specific catalog-folder binary. Consumes one or more AST files (in lexicographic order by short_path) and emits a snapshot file. Invocation:     <folder> --module <NAME> --output <FILE> [--format <fmt>] <ast> [<ast> ...] For postgres: `@rules_postgres//tools:pgpb_to_snapshot`.   | <a href="https://bazel.build/concepts/labels">Label</a> | required |  |
| <a id="sql_catalog_library-module_name"></a>module_name |  Lean (or analogous) module name (defaults to target name).   | String | optional |  `""`  |
| <a id="sql_catalog_library-output_format"></a>output_format |  Output projection format.   | String | optional |  `"lean"`  |


<a id="sql_library"></a>

## sql_library

<pre>
load("@rules_lang//polyglot:sql.bzl", "sql_library")

sql_library(<a href="#sql_library-name">name</a>, <a href="#sql_library-deps">deps</a>, <a href="#sql_library-srcs">srcs</a>, <a href="#sql_library-dialect">dialect</a>)
</pre>

Declares a set of `.sql` source files of a single dialect.

**ATTRIBUTES**


| Name  | Description | Type | Mandatory | Default |
| :------------- | :------------- | :------------- | :------------- | :------------- |
| <a id="sql_library-name"></a>name |  A unique name for this target.   | <a href="https://bazel.build/concepts/labels#target-names">Name</a> | required |  |
| <a id="sql_library-deps"></a>deps |  Other `sql_library` targets to roll up. Must match dialect.   | <a href="https://bazel.build/concepts/labels">List of labels</a> | optional |  `[]`  |
| <a id="sql_library-srcs"></a>srcs |  SQL source files contributed by this library.   | <a href="https://bazel.build/concepts/labels">List of labels</a> | optional |  `[]`  |
| <a id="sql_library-dialect"></a>dialect |  SQL dialect of the sources.   | String | optional |  `"postgres"`  |


<a id="SqlAstInfo"></a>

## SqlAstInfo

<pre>
load("@rules_lang//polyglot:sql.bzl", "SqlAstInfo")

SqlAstInfo(<a href="#SqlAstInfo-asts">asts</a>, <a href="#SqlAstInfo-dialect">dialect</a>)
</pre>

Carries parsed AST files alongside their SQL sources.

Each entry of `asts` is a struct(sql=File, ast=File, format=string).
`format` lets downstream projection rules pick the correct decoder
(e.g. 'libpg_query_protobuf' vs 'sqlite_native').

**FIELDS**

| Name  | Description |
| :------------- | :------------- |
| <a id="SqlAstInfo-asts"></a>asts |  depset[struct(sql: File, ast: File, format: string)]    |
| <a id="SqlAstInfo-dialect"></a>dialect |  string    |


<a id="SqlCatalogInfo"></a>

## SqlCatalogInfo

<pre>
load("@rules_lang//polyglot:sql.bzl", "SqlCatalogInfo")

SqlCatalogInfo(<a href="#SqlCatalogInfo-snapshot">snapshot</a>, <a href="#SqlCatalogInfo-dialect">dialect</a>, <a href="#SqlCatalogInfo-output_format">output_format</a>)
</pre>

Carries a cumulative catalog snapshot folded over DDLs.

The `snapshot` File is the final emitted artifact (Lean source by
default; future support for JSON/TTL via the `output_format`
attribute). `dialect` matches the upstream `SqlAstInfo.dialect`.

**FIELDS**

| Name  | Description |
| :------------- | :------------- |
| <a id="SqlCatalogInfo-snapshot"></a>snapshot |  File — emitted `Pg.Catalog.Snapshot` artifact.    |
| <a id="SqlCatalogInfo-dialect"></a>dialect |  string    |
| <a id="SqlCatalogInfo-output_format"></a>output_format |  string — 'lean' \| 'json' \| 'ttl'    |


<a id="SqlInfo"></a>

## SqlInfo

<pre>
load("@rules_lang//polyglot:sql.bzl", "SqlInfo")

SqlInfo(<a href="#SqlInfo-srcs">srcs</a>, <a href="#SqlInfo-dialect">dialect</a>)
</pre>

Carries raw SQL source files plus their declared dialect.

Surfaced by `sql_library` and propagated through `deps`. The
`dialect` is what tells `sql_ast_library` which toolchain to
resolve.

**FIELDS**

| Name  | Description |
| :------------- | :------------- |
| <a id="SqlInfo-srcs"></a>srcs |  depset[File] — `.sql` source files (transitively).    |
| <a id="SqlInfo-dialect"></a>dialect |  string — 'postgres' \| 'sqlite' \| …    |


<a id="SqlToolchainInfo"></a>

## SqlToolchainInfo

<pre>
load("@rules_lang//polyglot:sql.bzl", "SqlToolchainInfo")

SqlToolchainInfo(<a href="#SqlToolchainInfo-parser">parser</a>, <a href="#SqlToolchainInfo-parser_format">parser_format</a>, <a href="#SqlToolchainInfo-proto_descriptor">proto_descriptor</a>, <a href="#SqlToolchainInfo-version">version</a>, <a href="#SqlToolchainInfo-dialect">dialect</a>)
</pre>

The dialect-specific parser binary + format contract.

Every `*_sql_toolchain` rule emits this. `parser` is invoked as
`<parser> <input.sql> > <output.ast>`. `parser_format` is what
downstream projection rules use to pick the right decoder.

**FIELDS**

| Name  | Description |
| :------------- | :------------- |
| <a id="SqlToolchainInfo-parser"></a>parser |  File — executable; consumes .sql arg, emits AST on stdout.    |
| <a id="SqlToolchainInfo-parser_format"></a>parser_format |  string — 'libpg_query_protobuf' \| …    |
| <a id="SqlToolchainInfo-proto_descriptor"></a>proto_descriptor |  File or None — .proto schema (if AST is proto-shaped).    |
| <a id="SqlToolchainInfo-version"></a>version |  string — parser version (e.g. '17-6.2.2').    |
| <a id="SqlToolchainInfo-dialect"></a>dialect |  string — matches the toolchain_type's dialect tag.    |


<a id="sql_ast_aspect"></a>

## sql_ast_aspect

<pre>
load("@rules_lang//polyglot:sql.bzl", "sql_ast_aspect")

sql_ast_aspect()
</pre>

Propagates over `deps`, attaching `SqlAstInfo` to every
transitive `sql_library`. Lets downstream rules consume parsed ASTs
without per-file `sql_ast_library` declarations.

**ASPECT ATTRIBUTES**


| Name | Type |
| :------------- | :------------- |
| deps| String |


**ATTRIBUTES**



