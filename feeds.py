from __future__ import annotations
from typing import List, Dict
from datetime import datetime, timezone
import feedparser

# Curated RSS feeds
FEEDS_GENERAL = [
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.theverge.com/rss/index.xml",
    "https://techcrunch.com/feed/",
    "https://www.technologyreview.com/feed/",
]

FEEDS_AI = [
    "https://openai.com/blog/rss.xml",
    "https://ai.googleblog.com/atom.xml",
    "https://blogs.nvidia.com/ai/feed/",
]

FEEDS_IOT_ROBOTICS = [
    "https://spectrum.ieee.org/robotics/rss",
    "https://www.raspberrypi.com/news/feed/",
    "https://www.hackster.io/feeds.xml",
]

def _fmt_date(entry) -> str:
    # Try to parse any known date field
    for k in ("published_parsed", "updated_parsed"):
        dt = getattr(entry, k, None)
        if dt:
            return datetime(*dt[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d")
    return ""

def fetch_rss(feeds: List[str], limit: int = 8) -> List[Dict]:
    items: List[Dict] = []
    seen = set()
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
            for e in parsed.entries[:12]:
                link = getattr(e, "link", None)
                if not link or link in seen:
                    continue
                seen.add(link)
                items.append({
                    "title": getattr(e, "title", "Untitled"),
                    "link": link,
                    "date": _fmt_date(e),
                })
        except Exception:
            continue
    return items[:limit]

def format_items(items: List[Dict], title: str) -> str:
    if not items:
        return "نتیجه‌ای پیدا نشد."
    lines = [f"📰 <b>{title}</b>", ""]
    for i, it in enumerate(items, 1):
        t = it.get("title", "بدون عنوان")
        link = it.get("link", "")
        date = it.get("date", "")
        suffix = f" — {date}" if date else ""
        lines.append(f"{i}. <a href=\"{link}\">{t}</a>{suffix}")
    return "\n".join(lines)
