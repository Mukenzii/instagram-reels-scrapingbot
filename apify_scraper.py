"""Thin async wrapper around the Apify Instagram Reel Scraper actor."""

import aiohttp

from config import APIFY_ACTOR, APIFY_TOKEN, RESULTS_LIMIT

RUN_SYNC_URL = (
    f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
)

# run-sync can take a while for profiles with many reels, so keep a generous timeout.
_TIMEOUT = aiohttp.ClientTimeout(total=180)


class ApifyError(Exception):
    """Raised when the Apify actor run fails or returns nothing usable."""


async def scrape_reels(target: str) -> list[dict]:
    """Run the actor for one target and return the list of reel items.

    `target` may be an Instagram username, profile URL, or a direct reel URL.
    """
    payload = {
        "username": [target],
        "resultsLimit": RESULTS_LIMIT,
    }
    params = {"token": APIFY_TOKEN}

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(RUN_SYNC_URL, params=params, json=payload) as resp:
            if resp.status == 401:
                raise ApifyError("Apify token rad etildi (401). APIFY_TOKEN ni tekshiring.")
            if resp.status >= 400:
                text = await resp.text()
                raise ApifyError(f"Apify xatosi ({resp.status}): {text[:300]}")
            items = await resp.json()

    if not isinstance(items, list):
        raise ApifyError("Apify kutilmagan javob qaytardi.")
    return items


def parse_reel(item: dict) -> dict:
    """Pull the fields we care about out of a raw reel item.

    Instagram sometimes hides like counts; the actor returns -1 in that case.
    """
    likes = item.get("likesCount")
    likes_display = "yashirilgan" if likes in (None, -1) else likes

    # Instagram reports reel "views" as the play count; videoViewCount is often
    # null, so fall back to videoPlayCount (what the IG UI shows as "views").
    views = item.get("videoViewCount")
    plays = item.get("videoPlayCount")
    if views in (None, -1):
        views = plays

    return {
        "username": item.get("ownerUsername") or "—",
        "full_name": item.get("ownerFullName") or "",
        "likes": likes_display,
        "views": views,
        "plays": plays,
        "comments": item.get("commentsCount"),
        "url": item.get("url") or item.get("inputUrl") or "",
        "caption": (item.get("caption") or "").strip(),
    }
