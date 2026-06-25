"""Load seed content from CSV files into the database."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from igautomation.content.models import ContentItem, ContentType

logger = logging.getLogger(__name__)


def detect_content_type(url: str) -> ContentType:
    """Detect content type from URL pattern."""
    url_lower = url.lower()
    if "/reel/" in url_lower or "/reels/" in url_lower:
        return ContentType.REEL
    elif "/p/" in url_lower:
        return ContentType.POST
    elif "/stories/" in url_lower or "/story/" in url_lower:
        return ContentType.STORY
    elif "/tv/" in url_lower or "/igtv/" in url_lower:
        return ContentType.IGTV
    return ContentType.UNKNOWN


def _map_csv_type(raw_type: str) -> ContentType:
    """Map CSV type labels to ContentType enum."""
    t = raw_type.strip().lower()
    if t in ("clip", "reel", "reels"):
        return ContentType.REEL
    elif t in ("carousel", "album"):
        return ContentType.CAROUSEL
    elif t in ("video",):
        return ContentType.VIDEO
    elif t in ("photo", "post", "image"):
        return ContentType.POST
    elif t in ("story",):
        return ContentType.STORY
    return ContentType.UNKNOWN


def load_csv(path: str | Path) -> list[ContentItem]:
    """Load content items from a CSV file.

    Handles two formats:
    1. Standard: columns include URL/Link, Content Type, Category, Notes, Priority
    2. Scraped Explore: 9 columns = 3 groups of (href, src, type) per row
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    seen_urls: set[str] = set()
    items: list[ContentItem] = []

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)

        # Detect format: standard or explore-scraped (9 cols with href/src/type pattern)
        is_explore_format = (
            len(header) >= 9
            and "href" in " ".join(header).lower()
            and "src" in " ".join(header).lower()
        )

        if is_explore_format:
            for row in reader:
                for i in range(0, len(row), 3):
                    if i + 2 < len(row):
                        url = row[i].strip()
                        raw_type = row[i + 2].strip() if len(row) > i + 2 else ""
                        if url and "instagram.com" in url and url not in seen_urls:
                            seen_urls.add(url)
                            ct = _map_csv_type(raw_type) if raw_type else detect_content_type(url)
                            items.append(ContentItem(url=url, content_type=ct))
        else:
            for row in reader:
                row_dict = dict(zip(header, row))
                url = row_dict.get("URL/Link", row_dict.get("URL", row_dict.get("url", ""))).strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                declared_type = (
                    row_dict.get("Content Type", row_dict.get("content_type", "")).strip().lower()
                )
                ct = _map_csv_type(declared_type) if declared_type else detect_content_type(url)

                try:
                    priority = int(row_dict.get("Priority", row_dict.get("priority", "5")).strip())
                except (ValueError, AttributeError):
                    priority = 5

                items.append(
                    ContentItem(
                        url=url,
                        content_type=ct,
                        category=row_dict.get("Category", row_dict.get("category", "")).strip(),
                        notes=row_dict.get("Notes", row_dict.get("notes", "")).strip(),
                        priority=priority,
                    )
                )

    logger.info("Loaded %d content items from %s", len(items), path)
    return items
