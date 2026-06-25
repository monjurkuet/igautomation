"""Content models for engagement tracking."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ContentType(str, Enum):
    REEL = "reel"
    POST = "post"
    CAROUSEL = "carousel"
    STORY = "story"
    IGTV = "igtv"
    VIDEO = "video"
    UNKNOWN = "unknown"


class EngagementAction(str, Enum):
    LIKE = "like"
    SAVE = "save"
    INTERESTED = "interested"
    WATCH = "watch"
    COLLECTION_ADD = "collection_add"


class EngagementStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    RATE_LIMITED = "rate_limited"


class ContentItem(BaseModel):
    """A single piece of IG content to engage with."""

    url: str
    content_type: ContentType = ContentType.UNKNOWN
    shortcode: str = ""
    category: str = ""
    notes: str = ""
    priority: int = 5  # 1=low, 10=high

    # Populated after LLM analysis
    llm_analysis: str = ""
    llm_collection_suggestion: str = ""
    llm_tags: list[str] = Field(default_factory=list)
    is_bd_relevant: bool = False
    content_niche: str = ""


class ContentEngagementResult(BaseModel):
    """Result of engaging with a single content item."""

    url: str
    like: EngagementStatus = EngagementStatus.PENDING
    save: EngagementStatus = EngagementStatus.PENDING
    interested: EngagementStatus = EngagementStatus.PENDING
    watch: EngagementStatus = EngagementStatus.PENDING
    collection: str | None = None
    collection_added: EngagementStatus = EngagementStatus.PENDING
    error: str | None = None
    elapsed_seconds: float = 0.0


class CollectionInfo(BaseModel):
    """An IG Saved collection."""

    name: str
    collection_id: str | None = None  # populated after creation
    description: str = ""
    cover_media_id: str | None = None
