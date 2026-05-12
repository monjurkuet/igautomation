"""LLM-powered content analyzer — browser-based.

Instead of just sending URLs to the LLM, this module navigates to each
post/reel in Chrome via CDP, extracts the actual caption, hashtags,
username, and visible context from the page, THEN sends that real
content to the LLM for analysis. This is exactly how a human would
assess content: by looking at it.

Uses an OpenAI-compatible endpoint (Gemini 2.5 Flash Lite) for analysis.
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from igautomation.cdp.client import CDPClient
from igautomation.content.models import ContentItem

logger = logging.getLogger(__name__)


def _get_llm_config() -> tuple[str, str, str]:
    """Read LLM config from the centralized loader."""
    from igautomation.llm_config import load_llm_config

    cfg = load_llm_config()
    base_url = cfg.base_url
    if base_url and not base_url.endswith("/"):
        base_url += "/"
    return cfg.api_key, base_url, cfg.model


def extract_page_context(cdp: CDPClient) -> dict[str, Any]:
    """Navigate-independent: extract whatever context is visible on the current IG page.

    Uses body.innerText which captures everything visible on the page —
    username, caption, hashtags, comments, likes, timestamps.
    Then parses that text to extract structured fields.
    """
    # IG internal paths that are NOT usernames
    _IG_PATHS = {
        "explore", "direct", "p", "reel", "reels", "stories", "accounts",
        "settings", "notifications", "help", "about", "blog", "jobs", "api",
        "privacy", "terms", "legal", "developers", "topics", "locations",
    }

    js = r"""
(function() {
    var IG_PATHS = ["explore","direct","p","reel","reels","stories","accounts",
        "settings","notifications","help","about","blog","jobs","api",
        "privacy","terms","legal","developers","topics","locations"];
    var result = {
        username: "",
        caption: "",
        hashtags: [],
        mentions: [],
        likes: "",
        is_reel: false,
        location: "",
        alt_texts: [],
        body_text: "",
        timestamp: ""
    };

    // Get the full body text — this is what a human sees
    var bodyText = document.body ? document.body.innerText : "";
    result.body_text = bodyText.substring(0, 3000);
    var lines = bodyText.split("\n");

    // --- USERNAME ---
    // Strategy 1: the first line of body.innerText IS the post author
    if (lines.length > 0) {
        var firstLine = lines[0].trim();
        if (firstLine && firstLine.length > 1 && firstLine.length < 31 && !/^\d+$/.test(firstLine)) {
            result.username = firstLine;
        }
    }
    // Strategy 2: fallback to <a href="/username/"> links (sidebar profiles interfere)
    if (!result.username) {
        var links = document.querySelectorAll("a[href]");
        for (var i = 0; i < Math.min(links.length, 50); i++) {
            var href = links[i].getAttribute("href") || "";
            var m = href.match(/^\/([^\/.]+)\/$/);
            if (m) {
                var uname = m[1];
                if (uname.length > 1 && uname.length < 31 && IG_PATHS.indexOf(uname) === -1) {
                    result.username = uname;
                    break;
                }
            }
        }
    }

    // --- CAPTION ---
    // IG post body text structure:
    //   [0] username (header)
    //   [1] collab/song text
    //   [2] Following|Follow
    //   [3] username (2nd occurrence = post body)
    //   [4+] time-ago, then caption lines, then comments
    var captionLines = [];
    var usernameCount = 0;
    var captionStarted = false;
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line) continue;

        // Count username occurrences
        if (line === result.username) {
            usernameCount++;
            if (usernameCount >= 2) {
                // 2nd username = post body starts, reset
                captionLines = [];
                captionStarted = false;
            }
            continue;
        }

        // Skip Follow/Following button text
        if (line === "Follow" || line === "Following") continue;

        // After 2nd username, skip time-ago markers then collect caption
        if (usernameCount >= 2) {
            // Skip time-ago markers (3d, 14w, 51w, etc.)
            if (/^\d+[dhmwy]$/.test(line)) {
                captionStarted = true; // time-ago means caption is next
                continue;
            }
            if (!captionStarted) continue; // skip anything before time-ago

            // Stop at comment section or footer
            if (line === "Reply" || line === "See translation") continue;
            if (line.startsWith("More posts from") || line === "Meta") break;
            // A new commenter username after we have caption = end of caption
            if (captionLines.length > 0 && /^[a-zA-Z0-9._]{2,30}$/.test(line) && line !== result.username) {
                // This looks like a commenter username — caption is done
                break;
            }
            if (captionLines.length >= 8) break;
            captionLines.push(line);
        }
    }
    result.caption = captionLines.join(" ").substring(0, 1500);

    // --- HASHTAGS from entire body text ---
    var allTags = bodyText.match(/#[\w\u0980-\u09FF]+/g);
    if (allTags) {
        var unique = {};
        for (var i = 0; i < allTags.length; i++) unique[allTags[i]] = true;
        result.hashtags = Object.keys(unique);
    }

    // --- MENTIONS ---
    var allMentions = bodyText.match(/@[\w.]+/g);
    if (allMentions) {
        var uniqueM = {};
        for (var i = 0; i < allMentions.length; i++) uniqueM[allMentions[i]] = true;
        result.mentions = Object.keys(uniqueM);
    }

    // --- LIKES ---
    // IG shows likes in different formats: "7.1K" as standalone, or "1,234 likes"
    // Look for standalone number patterns (the big like count)
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        // "7.1K" or "1,234" or "123" — standalone like count (appears near bottom)
        if (/^[\d,]+(\.\d+[KkMm])?$/.test(line) && line.length > 0) {
            // This could be the likes count — verify it's not a time-ago
            if (!/^\d+[dhmwy]$/.test(line)) {
                result.likes = line;
                break;
            }
        }
    }
    // Also check for "X likes" pattern
    if (!result.likes) {
        var likeMatch = bodyText.match(/([\d,]+(?:\.\d+[KkMm])?)\s*likes?/i);
        if (likeMatch) result.likes = likeMatch[1];
    }

    // --- IS REEL ---
    result.is_reel = !!document.querySelector("video") || window.location.pathname.includes("/reel/");

    // --- TIMESTAMP ---
    var timeEls = document.querySelectorAll("time[datetime]");
    if (timeEls.length > 0) {
        result.timestamp = timeEls[0].getAttribute("datetime") || "";
    }

    // --- LOCATION ---
    var locationLinks = document.querySelectorAll("a[href*='/explore/locations/']");
    if (locationLinks.length > 0) {
        result.location = locationLinks[0].textContent.trim();
    }

    // --- ALT TEXT from images ---
    var imgs = document.querySelectorAll("img[alt]");
    for (var i = 0; i < imgs.length; i++) {
        var alt = imgs[i].getAttribute("alt") || "";
        if (alt.length > 10 && !alt.includes("profile picture") && !alt.includes("Profile photo") && !alt.includes("avatar")) {
            result.alt_texts.push(alt.substring(0, 500));
        }
    }
    result.alt_texts = result.alt_texts.slice(0, 3);

    return JSON.stringify(result);
})()
"""
    try:
        raw = cdp.evaluate(js, timeout=15)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(raw, dict):
            return raw
        return {}
    except Exception as exc:
        logger.warning("extract_page_context failed: %s", exc)
        return {}


def browse_and_extract(cdp: CDPClient, url: str, dwell: float = 3.0) -> dict[str, Any]:
    """Navigate to a post/reel URL like a real user and extract visible context.

    This is the browser-based equivalent of a human scrolling to a post,
    reading the caption, and understanding what the content is about.
    """
    # Navigate with human-like wait
    cdp.navigate(url, wait=4)

    # Dwell — simulate reading/viewing
    actual_dwell = dwell + random.uniform(-0.5, 1.5)
    if actual_dwell < 1.0:
        actual_dwell = 1.0
    time.sleep(actual_dwell)

    # Scroll down slightly to reveal caption (especially on reels)
    scroll_js = """
    (function() {
        // On reels, scroll the caption area
        var captionArea = document.querySelector("div[role='dialog'] div[class] ul");
        if (captionArea) {
            captionArea.scrollTop = captionArea.scrollHeight * 0.3;
        }
        // Also scroll the page a tiny bit
        window.scrollBy(0, 150);
        return true;
    })()
    """
    cdp.evaluate(scroll_js, timeout=5)
    time.sleep(0.5)

    # Extract context from the page
    context = extract_page_context(cdp)

    # Add URL info
    context["url"] = url
    context["is_reel"] = context.get("is_reel") or "/reel/" in url

    return context


def analyze_content_browse(
    cdp: CDPClient,
    item: ContentItem,
    dwell: float = 3.0,
) -> ContentItem:
    """Browse the content like a real user, then analyze with LLM.

    Steps:
    1. Navigate to the post/reel in Chrome
    2. Wait and scroll (human-like)
    3. Extract caption, hashtags, mentions, alt text
    4. Send extracted content to LLM for categorization
    5. Update the ContentItem with results
    """
    # Browse and extract
    context = browse_and_extract(cdp, item.url, dwell=dwell)

    # Log what we found
    username = context.get("username", "")
    caption = context.get("caption", "")
    hashtags = context.get("hashtags", [])
    likes = context.get("likes", "")
    alt_texts = context.get("alt_texts", [])

    logger.info(
        "Browsed %s — user=%s, caption=%d chars, hashtags=%d, likes=%s, alt=%d",
        item.url, username, len(caption), len(hashtags), likes, len(alt_texts),
    )

    # Build the LLM prompt with ACTUAL content from the page
    return _analyze_with_llm(item, context)


def _analyze_with_llm(item: ContentItem, context: dict[str, Any]) -> ContentItem:
    """Send extracted page context to LLM for analysis."""
    api_key, base_url, model = _get_llm_config()

    if not api_key:
        logger.warning("No LLM API key found — using fallback analysis")
        if item.category:
            item.llm_collection_suggestion = f"BD {item.category}"
        return item

    username = context.get("username", "")
    caption = context.get("caption", "")
    hashtags = context.get("hashtags", [])
    mentions = context.get("mentions", [])
    likes = context.get("likes", "")
    alt_texts = context.get("alt_texts", [])
    location = context.get("location", "")
    is_reel = context.get("is_reel", False)
    url = context.get("url", item.url)

    prompt = f"""Analyze this Instagram content for an influencer intelligence platform focused on Bangladesh.

Content URL: {url}
Content Type: {item.content_type.value}{" (reel/clip)" if is_reel else ""}
Posted by: @{username or "unknown"}
Location: {location or "not tagged"}
Likes: {likes or "unknown"}

Caption:
{caption or "(no caption extracted)"}

Hashtags: {", ".join(hashtags) if hashtags else "none"}
Mentions: {", ".join(mentions) if mentions else "none"}

Visual description (from alt text):
{chr(10).join(alt_texts) if alt_texts else "(no alt text available)"}

Based on the actual content above, provide a JSON response with these fields:
1. "analysis": Brief description of what this content is about (1-2 sentences based on the caption and visual info)
2. "collection": Suggest a collection name to save this under (e.g., "BD Fashion", "BD Lifestyle", "BD Models", "BD Travel", "BD Food", "BD Beauty", "BD Fitness", "BD Tech", "BD Art", "BD Music", "BD Education", "BD Comedy", "BD Dance", "BD Weddings", "General")
3. "tags": List of 3-5 relevant tags for categorization based on actual content
4. "is_bd_relevant": true/false — is this content relevant to Bangladesh? (check username, caption language, location, hashtags for Bangla/Bangladesh signals)
5. "content_niche": The specific niche (fashion, lifestyle, beauty, fitness, travel, food, tech, art, music, education, comedy, dance, other)

Respond with ONLY valid JSON, no other text."""

    try:
        import urllib.request

        api_url = f"{base_url.rstrip('/')}/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 500,
        }).encode()

        req = urllib.request.Request(
            api_url,
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
        item.llm_collection_suggestion = (analysis.get("collection", "") or "").strip().lower()
        item.llm_tags = analysis.get("tags", [])
        item.is_bd_relevant = analysis.get("is_bd_relevant", False)
        item.content_niche = (analysis.get("content_niche", "") or "").strip().lower()

        # Also store the extracted context
        item.notes = item.notes or ""
        if username:
            item.notes = f"@{username}" + (f" | {caption[:100]}" if caption else "")

        logger.info(
            "LLM analysis: %s → collection=%s, niche=%s, user=@%s",
            item.url, item.llm_collection_suggestion, item.content_niche, username,
        )

    except Exception as exc:
        logger.warning("LLM analysis failed for %s: %s", item.url, exc)
        if item.category:
            item.llm_collection_suggestion = (f"BD {item.category}").strip().lower()
        item.llm_analysis = f"LLM analysis unavailable: {exc}"
        # Still keep extracted context
        if username:
            item.notes = f"@{username}" + (f" | {caption[:100]}" if caption else "")

    return item


# Keep the old API-only function as fallback for when CDP is not available
def analyze_content(item: ContentItem) -> ContentItem:
    """Analyze a content item using LLM only (no browser). Fallback method."""
    api_key, base_url, model = _get_llm_config()

    if not api_key:
        logger.warning("No LLM API key found — skipping content analysis")
        if item.category:
            item.llm_collection_suggestion = (f"BD {item.category}").strip().lower()
        return item

    prompt = f"""Analyze this Instagram content for an influencer intelligence platform focused on Bangladesh.

Content URL: {item.url}
Content Type: {item.content_type.value}
Category: {item.category or "unknown"}

Provide a JSON response with these fields:
1. "analysis": Brief description of what this content is about (1-2 sentences)
2. "collection": Suggest a collection name (e.g., "BD Fashion", "BD Music", "General")
3. "tags": List of 3-5 relevant tags
4. "is_bd_relevant": true/false
5. "content_niche": The specific niche

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
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            content = content.rsplit("```", 1)[0]
            content = content.strip()

        analysis = json.loads(content)
        item.llm_analysis = analysis.get("analysis", "")
        item.llm_collection_suggestion = (analysis.get("collection", "") or "").strip().lower()
        item.llm_tags = analysis.get("tags", [])
        item.is_bd_relevant = analysis.get("is_bd_relevant", False)
        item.content_niche = (analysis.get("content_niche", "") or "").strip().lower()

    except Exception as exc:
        logger.warning("LLM analysis failed for %s: %s", item.url, exc)
        if item.category:
            item.llm_collection_suggestion = (f"BD {item.category}").strip().lower()

    return item


def batch_analyze(
    items: list[ContentItem],
    delay: float = 1.0,
    cdp: CDPClient | None = None,
    dwell: float = 3.0,
) -> list[ContentItem]:
    """Analyze a batch of content items. Uses browser if CDP provided, API-only otherwise."""
    results = []
    for i, item in enumerate(items):
        logger.info("Analyzing %d/%d: %s", i + 1, len(items), item.url)
        if cdp:
            analyzed = analyze_content_browse(cdp, item, dwell=dwell)
        else:
            analyzed = analyze_content(item)
        results.append(analyzed)
        if i < len(items) - 1:
            # Variable delay to look human
            actual_delay = delay + random.uniform(-0.3, 0.5)
            if actual_delay < 0.5:
                actual_delay = 0.5
            time.sleep(actual_delay)
    return results
