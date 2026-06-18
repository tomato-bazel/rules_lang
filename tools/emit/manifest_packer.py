#!/usr/bin/env python3
"""Pack Lean-rendered sources + identity into a polyglot.emit.v1.TranslationManifest binpb.

The cross-repo emit boundary. Producers render each unit Lean-side (via the
prebuilt atlas: Polyglot.<Lang>.OfLir.render / Pg.Pretty) and this packs the
rendered source + provenance into the manifest a consumer (aion/lift, aion/sql)
decodes and assembles. One `--render LANG:OUT_PATH:FILE` per rendered artifact;
language names are the short forms of the lir_codec Language enum (TYPESCRIPT,
SQL, PYTHON, RUST, JAVA, C).
"""

import argparse

import emit_pb2
import lir_codec_pb2


def _language(short):
    # The enum values are LANGUAGE_TYPESCRIPT, …; accept the short form.
    return lir_codec_pb2.Language.Value("LANGUAGE_" + short.upper())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package-name", required=True)
    ap.add_argument("--manifest-version", default="0.0.0")
    ap.add_argument("--source-revision", default="")
    ap.add_argument("--aion-dep", action="append", default=[])
    ap.add_argument(
        "--render",
        action="append",
        default=[],
        metavar="LANG:OUT_PATH:FILE",
        help="One rendered artifact: language, consumer-relative out_path, source file.",
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    m = emit_pb2.TranslationManifest()
    m.identity.name = args.package_name
    m.identity.manifest_version = args.manifest_version
    m.identity.source_revision = args.source_revision
    m.aion_deps.extend(args.aion_dep)

    for spec in args.render:
        lang, out_path, src = spec.split(":", 2)
        unit = m.units.add()
        with open(src, "r", encoding="utf-8") as fh:
            source = fh.read()
        rendered = unit.renders.add()
        rendered.language = _language(lang)
        rendered.out_path = out_path
        rendered.source = source

    with open(args.out, "wb") as fh:
        fh.write(m.SerializeToString())


if __name__ == "__main__":
    main()
