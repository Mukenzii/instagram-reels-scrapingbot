"""Notion integration: poll a database for reel links and write back stats.

Workflow:
- The user pastes an Instagram reel URL into the row's title (`TITLE_PROP`).
- A row is considered "pending" when its title contains an instagram.com link
  and its `Username` column is still empty.
- For each pending row we scrape via Apify and write likes/views/comments/
  username/ER back into the row, which fills `Username` and stops it being
  reprocessed on the next poll.
"""

import asyncio
import logging
import re

import aiohttp

from apify_scraper import ApifyError, parse_reel, scrape_reels
from config import (
    NOTION_DATABASE_ID,
    NOTION_POLL_SECONDS,
    NOTION_TOKEN,
)

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Column names in the Notion database.
LINK_PROP = "Kontent linki"   # URL property where the reel link is pasted
TITLE_PROP = "Kontent nomi"   # title (fallback source for the link)
PROP_LIKES = "Layk"
PROP_VIEWS = "Prosmotr"
PROP_COMMENTS = "Kommentlar"
PROP_USERNAME = "Username"
PROP_ER = "ER"
# Records WHICH reel the stats came from (its shortcode). A row is re-scraped
# only when this doesn't match the link currently in the row — so duplicated
# rows and edited links get fresh data, while an untouched row is never
# scraped twice.
PROP_SYNC = "Sync"

_INSTA_URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+", re.IGNORECASE)
_TIMEOUT = aiohttp.ClientTimeout(total=60)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _plain_text(rich: list) -> str:
    return "".join(part.get("plain_text", "") for part in rich)


def _normalize(url: str) -> str:
    """Notion may store a URL without a scheme (e.g. instagram.com/reel/..)."""
    url = url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _extract_link(page: dict) -> str | None:
    """Return the Instagram URL from the link column, falling back to the title."""
    props = page.get("properties", {})

    # Primary: the dedicated URL property.
    url = props.get(LINK_PROP, {}).get("url")
    if url and "instagram.com" in url.lower():
        return _normalize(url)

    # Fallback: a link pasted into the title text.
    text = _plain_text(props.get(TITLE_PROP, {}).get("title", []))
    match = _INSTA_URL_RE.search(text)
    return match.group(0) if match else None


def _username_filled(page: dict) -> bool:
    prop = page.get("properties", {}).get(PROP_USERNAME, {})
    return bool(_plain_text(prop.get("rich_text", [])).strip())


_SHORTCODE_RE = re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)


def _shortcode(url: str) -> str | None:
    """The reel's id from its URL, e.g. .../reel/DXt9oHMqiyG/?... -> DXt9oHMqiyG"""
    m = _SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def _sync_value(page: dict) -> str:
    prop = page.get("properties", {}).get(PROP_SYNC, {})
    return _plain_text(prop.get("rich_text", [])).strip()


async def _query_pending(session: aiohttp.ClientSession) -> list[dict]:
    """Return every row, so callers can decide what needs (re)scraping."""
    url = f"{NOTION_API}/databases/{NOTION_DATABASE_ID}/query"
    results: list[dict] = []
    cursor = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        async with session.post(url, json=payload, headers=_headers()) as resp:
            data = await resp.json()
        if data.get("object") == "error":
            raise RuntimeError(f"Notion query error: {data.get('message')}")
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def _num(value) -> dict:
    """Notion number property value (None allowed)."""
    return {"number": value if isinstance(value, (int, float)) else None}


def _text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value}}]}


async def _write_stats(session: aiohttp.ClientSession, page_id: str,
                       reel: dict, stamp: str) -> None:
    likes = reel["likes"] if isinstance(reel["likes"], int) else None
    properties = {
        PROP_LIKES: _num(likes),
        PROP_VIEWS: _num(reel["views"]),
        PROP_COMMENTS: _num(reel["comments"]),
        PROP_ER: _num(reel["er"]),
        PROP_USERNAME: _text(reel["username"] or "—"),
        PROP_SYNC: _text(stamp),
    }
    url = f"{NOTION_API}/pages/{page_id}"
    async with session.patch(url, json={"properties": properties}, headers=_headers()) as resp:
        data = await resp.json()
    if data.get("object") == "error":
        raise RuntimeError(f"Notion update error: {data.get('message')}")


async def _mark_failed(session: aiohttp.ClientSession, page_id: str, note: str,
                       stamp: str | None) -> None:
    """Mark a permanently-bad link so it isn't retried forever."""
    properties = {PROP_USERNAME: _text(note), PROP_SYNC: _text(stamp or "")}
    url = f"{NOTION_API}/pages/{page_id}"
    try:
        async with session.patch(url, json={"properties": properties}, headers=_headers()):
            pass
    except Exception:
        logger.exception("Failed to write failure marker to %s", page_id)


async def _process_once(session: aiohttp.ClientSession) -> None:
    pages = await _query_pending(session)
    for page in pages:
        page_id = page["id"]
        link = _extract_link(page)
        if not link:
            # No instagram link in the row yet — leave it alone.
            continue

        code = _shortcode(link)
        if code and _sync_value(page) == code:
            # These stats already came from this exact reel — don't rescrape.
            continue

        logger.info("Notion: processing %s -> %s", page_id, link)
        try:
            items = await scrape_reels(link)
        except ApifyError as exc:
            logger.warning("Apify failed for %s: %s", link, exc)
            continue  # transient — retry next poll
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            # Network/DNS blip or a slow actor run — retry on the next poll.
            logger.warning("Scrape timed out/failed for %s (%s) — will retry", link,
                           type(exc).__name__)
            continue
        except Exception:
            logger.exception("Unexpected scrape error for %s", link)
            continue

        if not items:
            await _mark_failed(session, page_id, "topilmadi", code)
            continue

        reel = parse_reel(items[0])
        # Trust the reel the actor actually returned over the pasted URL.
        stamp = items[0].get("shortCode") or code or ""
        try:
            await _write_stats(session, page_id, reel, stamp)
            logger.info("Notion: filled %s (@%s)", page_id, reel["username"])
        except Exception:
            logger.exception("Failed to write stats to %s", page_id)


async def run_notion_poller() -> None:
    """Background loop: poll the database every NOTION_POLL_SECONDS."""
    logger.info(
        "Notion poller started (db=%s, every %ss).",
        NOTION_DATABASE_ID,
        NOTION_POLL_SECONDS,
    )
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        while True:
            try:
                await _process_once(session)
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                # DNS hiccup or Notion unreachable — log one line, not a
                # traceback, and try again on the next cycle.
                logger.warning("Notion unreachable (%s) — retrying in %ss",
                               type(exc).__name__, NOTION_POLL_SECONDS)
            except Exception:
                logger.exception("Notion poll cycle failed")
            await asyncio.sleep(NOTION_POLL_SECONDS)
