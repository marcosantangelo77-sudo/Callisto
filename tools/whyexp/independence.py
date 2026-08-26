"""Independence accounting for tools.whyexp."""
from __future__ import annotations

from tools.pipeline.retrieval import _OVERLAP_FAMILIES, independence_key
from tools.whyexp.records import IndependenceWhy


def independence_from_fetches(fetches) -> IndependenceWhy:
    """Count independent sources exactly as retrieval does, with the
    family-collapse statements spelled out."""
    from tools.pipeline.retrieval import in_family as _in_family
    keys: set = set()
    collapses: list[str] = []
    seen_family_members: dict[str, set] = {}
    for f in fetches or []:
        key = independence_key(getattr(f, "source_name", ""),
                               getattr(f, "url", "") or
                               getattr(f, "source_name", ""))
        keys.add(key)
        for family, members in _OVERLAP_FAMILIES.items():
            if _in_family(getattr(f, "source_name", ""), members):
                seen_family_members.setdefault(family, set()).add(
                    f.source_name)
    for family, members in sorted(seen_family_members.items()):
        names = ", ".join(sorted(members))
        if len(members) > 1:
            collapses.append(
                f"'{family}' collapse: {names} count as ONE independent "
                "source (they index the same underlying pool)")
        elif members:
            collapses.append(
                f"'{family}' collapse applies to {names}; any other member "
                "would not have added an independent source")
    return IndependenceWhy(
        n_fetches=len(fetches or []),
        independent_keys=sorted(str(k) for k in keys),
        n_independent=len(keys),
        collapses=collapses)
