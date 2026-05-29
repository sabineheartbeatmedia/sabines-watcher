import os
import json
import logging
import asyncio
from pathlib import Path

import instaloader
import schedule
import time
from telegram import Bot
from telegram.error import TelegramError

# ── Config ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INSTAGRAM_ACCOUNTS = [
    "carolinepreussde",
    "abovebeyond.coaching",
    "mut.marketing",
]

CHECK_TIME = os.environ.get("CHECK_TIME", "09:00")
STATE_FILE = "/tmp/seen_posts.json"   # /tmp ist auf Railway beschreibbar
# ────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {acc: [] for acc in INSTAGRAM_ACCOUNTS}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_latest_posts(username: str, count: int = 5) -> list[dict]:
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
    )
    posts = []
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        for post in profile.get_posts():
            if len(posts) >= count:
                break
            posts.append({
                "shortcode": post.shortcode,
                "url": f"https://www.instagram.com/p/{post.shortcode}/",
                "thumbnail": post.url,
                "caption": (post.caption or "")[:400],
                "is_video": post.is_video,
                "date": post.date_utc.strftime("%d.%m.%Y"),
                "likes": post.likes,
            })
    except Exception as e:
        log.error("Fehler beim Abrufen von @%s: %s", username, e)
    return posts


async def send_new_posts(bot: Bot, account: str, new_posts: list[dict]):
    for post in reversed(new_posts):
        media_type = "🎬 Video" if post["is_video"] else "🖼️ Post"
        caption_text = f"_{post['caption'][:300]}_" if post["caption"] else "_kein Text_"
        text = (
            f"{media_type} *@{account}*\n\n"
            f"{caption_text}\n\n"
            f"❤️ {post['likes']} Likes  •  {post['date']}\n"
            f"🔗 {post['url']}"
        )
        try:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=post["thumbnail"],
                caption=text,
                parse_mode="Markdown",
            )
        except TelegramError:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
        await asyncio.sleep(1)


async def check_all_accounts():
    log.info("Starte Instagram-Check …")
    bot = Bot(token=TELEGRAM_TOKEN)
    state = load_state()

    for account in INSTAGRAM_ACCOUNTS:
        log.info("Prüfe @%s …", account)
        latest = fetch_latest_posts(account, count=5)
        if not latest:
            continue

        known = set(state.get(account, []))
        new_posts = [p for p in latest if p["shortcode"] not in known]

        if new_posts:
            log.info("  → %d neuer Post(s) bei @%s", len(new_posts), account)
            await send_new_posts(bot, account, new_posts)
        else:
            log.info("  → Keine neuen Posts bei @%s", account)

        state[account] = list(known | {p["shortcode"] for p in latest})

    save_state(state)
    log.info("Check abgeschlossen.")


def run_check():
    asyncio.run(check_all_accounts())


if __name__ == "__main__":
    log.info("Bot gestartet. Täglicher Check um %s Uhr.", CHECK_TIME)

    # Beim Start einmal prüfen (initialisiert seen_posts.json)
    run_check()

    schedule.every().day.at(CHECK_TIME).do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(30)
