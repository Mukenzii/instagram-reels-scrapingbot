import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
RESULTS_LIMIT = int(os.getenv("RESULTS_LIMIT", "5"))

APIFY_ACTOR = "apify~instagram-reel-scraper"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
if not APIFY_TOKEN:
    raise RuntimeError("APIFY_TOKEN is not set. Copy .env.example to .env and fill it in.")
