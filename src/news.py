"""Recent USD/BRL headlines from Google News RSS (free, no key)."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

# (query, language params): Brazilian FX coverage plus global dollar drivers.
QUERIES = [
    (
        "dólar (fecha OR recua OR sobe OR cai OR avança) real câmbio when:2d",
        {"hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"},
    ),
    ("dollar (Fed OR Treasury OR yields) forex when:2d", {"hl": "en-US", "gl": "US", "ceid": "US:en"}),
]
PER_QUERY = 8


@dataclass(frozen=True)
class Headline:
    title: str
    source: str
    link: str
    published: datetime | None


def parse_rss(xml_text: str, limit: int = PER_QUERY) -> list[Headline]:
    headlines: list[Headline] = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        # Google appends " - Source" to titles; drop it since source is separate.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        if title:
            headlines.append(Headline(title, source, item.findtext("link") or "", published))
        if len(headlines) >= limit:
            break
    return headlines


def fetch_headlines() -> list[Headline]:
    """Fetch recent headlines; returns what it can, never raises."""
    seen: set[str] = set()
    headlines: list[Headline] = []
    for query, params in QUERIES:
        try:
            response = requests.get(
                GOOGLE_NEWS_RSS,
                params={"q": query, **params},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            response.raise_for_status()
            items = parse_rss(response.text)
        except (requests.RequestException, ET.ParseError) as exc:
            print(f"News query {query!r} unavailable: {exc}", file=sys.stderr)
            continue
        for headline in items:
            if headline.title.lower() not in seen:
                seen.add(headline.title.lower())
                headlines.append(headline)
    return headlines
