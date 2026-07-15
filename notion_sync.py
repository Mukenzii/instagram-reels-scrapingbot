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
TITLE_PROP = "Kontent nomi"   # where the reel link is pasted
PROP_LIKES = "Layk"
PROP_VIEWS = "Prosmotr"
PROP_COMMENTS = "Kommentlar"
PROP_USERNAME = "Username"
PROP_ER = "ER"

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


def _extract_link(page: dict) -> str | None:
    """Return the Instagram URL found in the page title, if any."""
    prop = page.get("properties", {}).get(TITLE_PROP, {})
    text = _plain_text(prop.get("title", []))
    match = _INSTA_URL_RE.search(text)
    return match.group(0) if match else None


def _username_filled(page: dict) -> bool:
    prop = page.get("properties", {}).get(PROP_USERNAME, {})
    return bool(_plain_text(prop.get("rich_text", [])).strip())


async def _query_pending(session: aiohttp.ClientSession) -> list[dict]:
    """Return pages whose Username column is empty (candidates to process)."""
    url = f"{NOTION_API}/databases/{NOTION_DATABASE_ID}/query"
    payload = {
        "filter": {"property": PROP_USERNAME, "rich_text": {"is_empty": True}},
        "page_size": 50,
    }
    async with session.post(url, json=payload, headers=_headers()) as resp:
        data = await resp.json()
    if data.get("object") == "error":
        raise RuntimeError(f"Notion query error: {data.get('message')}")
    return data.get("results", [])


def _num(value) -> dict:
    """Notion number property value (None allowed)."""
    return {"number": value if isinstance(value, (int, float)) else None}


async def _write_stats(session: aiohttp.ClientSession, page_id: str, reel: dict) -> None:
    likes = reel["likes"] if isinstance(reel["likes"], int) else None
    properties = {
        PROP_LIKES: _num(likes),
        PROP_VIEWS: _num(reel["views"]),
        PROP_COMMENTS: _num(reel["comments"]),
        PROP_ER: _num(reel["er"]),
        PROP_USERNAME: {
            "rich_text": [{"text": {"content": reel["username"] or "—"}}]
        },
    }
    url = f"{NOTION_API}/pages/{page_id}"
    async with session.patch(url, json={"properties": properties}, headers=_headers()) as resp:
        data = await resp.json()
    if data.get("object") == "error":
        raise RuntimeError(f"Notion update error: {data.get('message')}")


async def _mark_failed(session: aiohttp.ClientSession, page_id: str, note: str) -> None:
    """Write a marker into Username so a permanently-bad row isn't retried forever."""
    properties = {PROP_USERNAME: {"rich_text": [{"text": {"content": note}}]}}
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
            # No instagram link in the title yet — leave the row alone.
            continue
        logger.info("Notion: processing %s -> %s", page_id, link)
        try:
            items = await scrape_reels(link)
        except ApifyError as exc:
            logger.warning("Apify failed for %s: %s", link, exc)
            continue  # transient — retry next poll
        except Exception:
            logger.exception("Unexpected scrape error for %s", link)
            continue

        if not items:
            await _mark_failed(session, page_id, "topilmadi")
            continue

        reel = parse_reel(items[0])
        try:
            await _write_stats(session, page_id, reel)
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
            except Exception:
                logger.exception("Notion poll cycle failed")
            await asyncio.sleep(NOTION_POLL_SECONDS)
