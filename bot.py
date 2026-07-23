"""Telegram bot that scrapes Instagram reel stats via the Apify actor.

Send it a reel URL, a profile URL, or just a username and it replies with
each reel's owner username, likes, views and comment count.
"""

import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

from apify_scraper import ApifyError, parse_reel, scrape_reels
from config import BOT_TOKEN, NOTION_ENABLED
from notion_sync import refresh_all, run_notion_poller

# Guards against two /update runs overlapping (double taps, several admins).
_update_lock = asyncio.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

dp = Dispatcher()

WELCOME = (
    "👋 <b>Instagram Reel Scraper</b>\n\n"
    "Menga reel havolasini, profil havolasini yoki oddiy username yuboring — "
    "men har bir reel uchun quyidagilarni qaytaraman:\n"
    "• 👤 account username\n"
    "• ❤️ layklar soni\n"
    "• 👁 ko‘rishlar (views) soni\n"
    "• 💬 izohlar soni\n\n"
    "<b>Misollar:</b>\n"
    "<code>https://www.instagram.com/reel/DX7lzTOJ1p6/</code>\n"
    "<code>nasa</code>\n\n"
    "🔄 /update — Notion jadvalidagi barcha havolalarni qayta yangilaydi."
)


def _fmt(value) -> str:
    """Format a number with thousands separators, or pass strings through."""
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    return str(value) if value is not None else "—"


def format_reel(reel: dict, index: int, total: int) -> str:
    header = f"🎬 <b>Reel {index}/{total}</b>" if total > 1 else "🎬 <b>Reel</b>"
    name = html.escape(reel["username"])
    lines = [
        header,
        f"👤 <b>Username:</b> @{name}",
        f"❤️ <b>Layklar:</b> {_fmt(reel['likes'])}",
        f"👁 <b>Ko‘rishlar:</b> {_fmt(reel['views'])}",
    ]
    # Only show plays separately when it's a distinct number from views
    # (in the common case views falls back to plays, so they'd be identical).
    if reel["plays"] is not None and reel["plays"] != reel["views"]:
        lines.append(f"▶️ <b>Ijro (plays):</b> {_fmt(reel['plays'])}")
    lines.append(f"💬 <b>Izohlar:</b> {_fmt(reel['comments'])}")
    if reel.get("shares") is not None:
        lines.append(f"🔄 <b>Ulashishlar:</b> {_fmt(reel['shares'])}")
    if reel.get("er") is not None:
        lines.append(f"📊 <b>ER:</b> {reel['er']}% <i>(views asosida)</i>")
    else:
        lines.append("📊 <b>ER:</b> hisoblab bo‘lmadi")
    if reel["url"]:
        lines.append(f"🔗 {html.escape(reel['url'])}")
    return "\n".join(lines)


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(WELCOME)


@dp.message(Command("update"))
async def on_update(message: Message) -> None:
    """Force-refresh every reel link in the Notion database with fresh stats."""
    if not NOTION_ENABLED:
        await message.answer("⚠️ Notion ulanmagan (NOTION_TOKEN/NOTION_DATABASE_ID yo‘q).")
        return

    if _update_lock.locked():
        await message.answer("⏳ Yangilash allaqachon ketmoqda, biroz kuting...")
        return

    async with _update_lock:
        status = await message.answer(
            "🔄 Barcha havolalar yangilanmoqda... (bir necha daqiqa olishi mumkin)"
        )
        try:
            c = await refresh_all()
        except Exception:
            logger.exception("/update failed")
            await status.edit_text("❌ Yangilashda xatolik yuz berdi. Keyinroq urinib ko‘ring.")
            return

    lines = [
        "✅ <b>Yangilash tugadi</b>",
        f"🔁 Yangilandi: {c['updated']}",
    ]
    if c["failed"]:
        lines.append(f"⚠️ Xatolik: {c['failed']}")
    if c["no_link"]:
        lines.append(f"➖ Havolasiz qatorlar: {c['no_link']}")
    await status.edit_text("\n".join(lines))


@dp.message(F.text)
async def on_text(message: Message) -> None:
    target = message.text.strip()
    if not target:
        await message.answer("Iltimos, reel havolasi yoki username yuboring.")
        return

    status = await message.answer("⏳ Ma'lumot yig‘ilmoqda, biroz kuting...")

    try:
        items = await scrape_reels(target)
    except ApifyError as exc:
        await status.edit_text(f"⚠️ {html.escape(str(exc))}")
        return
    except asyncio.TimeoutError:
        await status.edit_text("⏱ So‘rov juda uzoq davom etdi. Keyinroq urinib ko‘ring.")
        return
    except Exception:
        logger.exception("Unexpected error while scraping %s", target)
        await status.edit_text("❌ Kutilmagan xatolik yuz berdi. Keyinroq urinib ko‘ring.")
        return

    if not items:
        await status.edit_text(
            "🔍 Hech narsa topilmadi. Havola/username to‘g‘ri va profil ochiq (public) ekaniga ishonch hosil qiling."
        )
        return

    reels = [parse_reel(item) for item in items]
    await status.edit_text(f"✅ {len(reels)} ta reel topildi:")
    for i, reel in enumerate(reels, start=1):
        await message.answer(format_reel(reel, i, len(reels)), disable_web_page_preview=True)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    logger.info("Bot ishga tushdi.")

    commands = [BotCommand(command="start", description="Botni ishga tushirish")]
    if NOTION_ENABLED:
        commands.append(
            BotCommand(command="update", description="Notion havolalarini yangilash")
        )
    await bot.set_my_commands(commands)

    if NOTION_ENABLED:
        # Run the Notion poller alongside Telegram polling.
        asyncio.create_task(run_notion_poller())
    else:
        logger.info("Notion o‘chirilgan (NOTION_TOKEN/NOTION_DATABASE_ID yo‘q).")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
