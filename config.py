import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
RESULTS_LIMIT = int(os.getenv("RESULTS_LIMIT", "5"))

APIFY_ACTOR = "apify~instagram-reel-scraper"

# Notion integration (optional). If NOTION_TOKEN is unset, the poller stays off.
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").strip()
NOTION_POLL_SECONDS = int(os.getenv("NOTION_POLL_SECONDS", "120"))
NOTION_ENABLED = bool(NOTION_TOKEN and NOTION_DATABASE_ID)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
if not APIFY_TOKEN:
    raise RuntimeError("APIFY_TOKEN is not set. Copy .env.example to .env and fill it in.")
