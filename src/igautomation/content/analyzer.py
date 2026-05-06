"""LLM-powered content analyzer.

Uses an OpenAI-compatible endpoint (Gemini 2.5 Flash Lite) to analyze
IG content and suggest collections, tags, and categories.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from igautomation.content.models import ContentItem, ContentType

logger = logging.getLogger(__name__)


def _load_env_file(path: str | None = None) -> dict[str, str]:
    """Parse a .env file into a dict. Skips comments and blank lines."""
    env: dict[str, str] = {}
    if path is None:
        # Walk up from this file to find .env
        this_dir = os.path.dirname(os.path.abspath(__file__))
        for _ in range(5):
            candidate = os.path.join(this_dir, ".env")
            if os.path.exists(candidate):
                path = candidate
                break
            this_dir = os.path.dirname(this_dir)
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip("\"").strip("\'")
    return env


def _get_llm_config() -> tuple[str, str, str]:
    """Read LLM config from .env file or environment.

    Returns (api_key, base_url, model).
    """
    env = _load_env_file()
    api_key = os.environ.get("OPENPAI_API_KEY", "") or env.get("OPENPAI_API_KEY", "")
    base_url = os.environ.get("OPENPAI_BASE_URL", "") or env.get("OPENPAI_BASE_URL", "https://llm.datasolved.org/v1/")
    model = os.environ.get("LLM_MODEL", "") or env.get("LLM_MODEL", "gemini-2.5-flash-lite")

    # Ensure base_url ends with /
    if base_url and not base_url.endswith("/"):
        base_url += "/"

    return api_key, base_url, model


def analyze_content(item: ContentItem) -> ContentItem:
    """Analyze a content item using LLM and update its metadata.

    Sends the content URL, type, and available info to the LLM
    and gets back: analysis, collection suggestion, tags, and niche.
    """
    api_key, base_url, model = _get_llm_config()

    if not api_key:
        logger.warning("No LLM API key found — skipping content analysis")
        if item.category:
            item.llm_collection_suggestion = f"BD {item.category}"
        return item

    prompt = f"""Analyze this Instagram content for an influencer intelligence platform focused on Bangladesh.

Content URL: {item.url}
Content Type: {item.content_type.value}
Category: {item.category or "unknown"}
Notes: {item.notes or "none"}

Provide a JSON response with these fields:
1. "analysis": Brief description of what this content is about (1-2 sentences)
2. "collection": Suggest a collection name to save this under (e.g., "BD Fashion", "BD Lifestyle", "BD Models", "BD Travel", "BD Food", "BD Beauty", "BD Fitness", "BD Tech", "BD Art", "BD Music", "BD Education", "BD Comedy", "General")
3. "tags": List of 3-5 relevant tags for categorization
4. "is_bd_relevant": true/false — is this content relevant to Bangladesh?
5. "content_niche": The specific niche (fashion, lifestyle, beauty, fitness, travel, food, tech, art, music, education, comedy, other)

Respond with ONLY valid JSON, no other text."""

    try:
        import urllib.request

        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 500,
        }).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        content = data["choices"][0]["message"]["content"]

        # Parse JSON response — handle markdown code blocks
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            content = content.rsplit("```", 1)[0]
        content = content.strip()

        analysis = json.loads(content)
        item.llm_analysis = analysis.get("analysis", "")
        item.llm_collection_suggestion = analysis.get("collection", "")
        item.llm_tags = analysis.get("tags", [])
        item.is_bd_relevant = analysis.get("is_bd_relevant", False)
        item.content_niche = analysis.get("content_niche", "")

        logger.info(
            "LLM analysis: %s → collection=%s, niche=%s",
            item.url, item.llm_collection_suggestion, item.content_niche,
        )

    except Exception as exc:
        logger.warning("LLM analysis failed for %s: %s", item.url, exc)
        # Fallback: use the CSV category as collection
        if item.category:
            item.llm_collection_suggestion = f"BD {item.category}"
        item.llm_analysis = f"LLM analysis unavailable: {exc}"

    return item


def batch_analyze(items: list[ContentItem], delay: float = 1.0) -> list[ContentItem]:
    """Analyze a batch of content items with rate limiting between calls."""
    results = []
    for i, item in enumerate(items):
        logger.info("Analyzing %d/%d: %s", i + 1, len(items), item.url)
        analyzed = analyze_content(item)
        results.append(analyzed)
        if i < len(items) - 1:
            time.sleep(delay)
    return results
