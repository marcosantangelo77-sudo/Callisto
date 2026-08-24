"""Federal Reserve Board speeches + FOMC statements. Tier 2 (primary documents).

Sources: the Board's official RSS feeds —
  https://www.federalreserve.gov/feeds/speeches.xml      (Board speeches)
  https://www.federalreserve.gov/feeds/press_all.xml     (all press releases,
                                                          incl. FOMC statements
                                                          and minutes)
Free, no key, no stated numeric rate limit; we self-limit to ~1 req/s
(min_interval_s=1.0) as a matter of courtesy to a non-API HTML host.
Feed entries are POINTERS: title, URL, category, and a GMT pubDate
(officially timestamped). The full text lives at the linked HTML page;
this adapter returns feed metadata, never scraped bodies.

Provenance class: PRIMARY DOCUMENTS (tier 2). A speech transcript or an
FOMC statement is itself the primary artifact; the feed is the official
channel that timestamps it.

Answers: which speeches/statements/minutes were published, when, by whom
(title carries speaker), and where the canonical text lives.
Cannot answer: full document text (feed entries are links only),
historical archives beyond the current feed window (~last 2 months),
Reserve Bank speeches (each Bank publishes separately), vote details or
dot-plot projections (not in the feed), sentiment or hawkish/dovish
scoring.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from tools.sources.base import RestSource, SourceSpec

SPEECHES_FEED = "/feeds/speeches.xml"
PRESS_FEED = "/feeds/press_all.xml"

SPEC = SourceSpec(
    name="federalreserve",
    base_url="https://www.federalreserve.gov",
    description="Federal Reserve Board speeches and FOMC statements "
                "(official RSS feeds)",
    answers=(
        "Federal Reserve Board speeches with speaker, date, and link",
        "FOMC statements and minutes announcements with publication timestamp",
        "recent Fed press releases by category (monetary policy, regulation)",
    ),
    cannot_answer=(
        "full text of speeches or statements (feeds carry links only)",
        "archives older than the current feed window",
        "Reserve Bank president speeches outside the Board feed",
        "FOMC votes, dot plot, or SEP projections",
    ),
    tier=2,
    min_interval_s=1.0,
    terms_url="https://www.federalreserve.gov/about-the-fed/files/"
              "privacy-usage.pdf",
)


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    category: str
    pub_date_gmt: str     # as published, e.g. 'Wed, 29 Jul 2026 18:00:00 GMT'
    description: str

    def to_dict(self) -> dict:
        return {
            "title": self.title, "url": self.url,
            "category": self.category, "pub_date_gmt": self.pub_date_gmt,
            "description": self.description,
        }


_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _text(el) -> str:
    """Element text with CDATA wrappers stripped."""
    raw = el.text or ""
    return _CDATA_RE.sub(r"\1", raw).strip()


def parse_feed(xml_body: str) -> list[FeedItem]:
    """RSS 2.0 -> list of items. Raises ValueError on non-feed XML so a
    captive portal / HTML error page can never masquerade as zero results."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        raise ValueError(f"federalreserve: feed is not valid XML ({exc})")
    if not root.tag.endswith("rss"):
        raise ValueError(
            f"federalreserve: expected <rss> root, got <{root.tag}> — "
            "an HTML error page or redirect reached the parser")
    items = []
    for item in root.iter("item"):
        get = lambda tag: _text(item.find(tag))  # noqa: E731
        title = get("title")
        if not title:
            continue
        items.append(FeedItem(
            title=title,
            url=get("link") or get("guid"),
            category=get("category"),
            pub_date_gmt=get("pubDate"),
            description=get("description"),
        ))
    return items


class FederalReserveAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def recent_speeches(self) -> list[dict]:
        """Board speeches from the official feed, newest first."""
        body = self.source.get(self.source.spec.base_url + SPEECHES_FEED)[1]
        return [i.to_dict() for i in parse_feed(body)]

    def recent_press_releases(self) -> list[dict]:
        """All Board press releases (includes FOMC statements and minutes
        under the 'Monetary Policy' category), newest first."""
        body = self.source.get(self.source.spec.base_url + PRESS_FEED)[1]
        return [i.to_dict() for i in parse_feed(body)]

    def monetary_policy_items(self) -> list[dict]:
        """The market-moving subset: FOMC statements, minutes, and other
        monetary-policy press releases."""
        return [r for r in self.recent_press_releases()
                if r["category"].lower() == "monetary policy"]
