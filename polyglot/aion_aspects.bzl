"""Polyglot.Aion — aspect definitions.

`aion_spec_aspect` walks a target's dep graph collecting every
`AionSpecInfo` it can find. Used by consumers that want to discover
all specs reachable from a root target without each one explicitly
listing them — e.g. a doc-gen rule, a fixture-validation rule, or
a build-time metrics collector.

This aspect is INFORMATIONAL — it doesn't run actions, it just
gathers SpecInfo records via the `AionSpecCollection` provider
so consumers can iterate.

Direct consumers like `aion_emit` that depend on exactly one
spec read `AionSpecInfo` from the `spec` attr directly and don't
need this aspect — the aspect's purpose is *additional* consumer
extensibility per the architecture decision in INTERNAL.md.
"""

load(":aion_providers.bzl", "AionSpecInfo")

AionSpecCollection = provider(
    doc = "Collected AionSpecInfo records reachable from a target via deps / spec / specs edges.",
    fields = {
        "specs": "depset of AionSpecInfo records.",
    },
)

def _aion_spec_aspect_impl(target, ctx):
    direct = [target[AionSpecInfo]] if AionSpecInfo in target else []
    transitive = []
    rule_attr = ctx.rule.attr

    # Walk the conventional edges where SpecInfo would flow:
    # generic `deps`, single-`spec`, and aspect-discoverable lists.
    for attr_name in ("deps", "spec", "specs"):
        attr_val = getattr(rule_attr, attr_name, None)
        if attr_val == None:
            continue
        deps = attr_val if type(attr_val) == "list" else [attr_val]
        for dep in deps:
            if AionSpecCollection in dep:
                transitive.append(dep[AionSpecCollection].specs)
    return [AionSpecCollection(specs = depset(direct = direct, transitive = transitive))]

aion_spec_aspect = aspect(
    implementation = _aion_spec_aspect_impl,
    attr_aspects = ["deps", "spec", "specs"],
    provides = [AionSpecCollection],
    doc = """\
Walks a target's deps / spec / specs attrs collecting `AionSpecInfo`
records. Adds an `AionSpecCollection` provider with a depset of all
specs reachable through that subgraph.

Apply to rule attrs via `attr.label(aspects = [aion_spec_aspect])`
in a consumer rule's definition. Then inside that rule's impl,
read `ctx.attr.<edge>[AionSpecCollection].specs` to iterate the
specs.
""",
)
