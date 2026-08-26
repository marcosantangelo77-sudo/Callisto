"""Ingestion rejections: parsed from result.notes when traces are gone."""
from __future__ import annotations

import re
from typing import Iterable, Optional

from tools.whyexp.records import RejectedWhy

_REJECT_NOTE_RE = re.compile(
    r"leaf '(?P<leaf>.{0,80}?)': (?P<n>\d+) fetch\(s\) rejected at ingestion: (?P<rest>.*)")
_REJECT_ITEM_RE = re.compile(r"\[(?P<src>[^\]]+)\] (?P<reason>[^;]+)")


def parse_rejections(notes: Optional[Iterable[str]]) -> list[RejectedWhy]:
    out: list[RejectedWhy] = []
    for note in notes or ():
        m = _REJECT_NOTE_RE.search(note)
        if not m:
            continue
        for item in _REJECT_ITEM_RE.finditer(m.group("rest")):
            out.append(RejectedWhy(source_name=item.group("src").strip(),
                                   url="", reason=item.group("reason").strip()))
    return out
