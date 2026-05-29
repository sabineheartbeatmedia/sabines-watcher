import os
import json
import logging
import asyncio
import httpx
from pathlib import Path

import schedule
import time
from telegram import Bot
from telegram.error import TelegramError

# ── Config ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
APIFY_TOKEN      = os.environ["APIFY_TOKEN"]

INSTAGRAM_ACCOUNTS = [
    "carolinepreussde",
    "abovebeyond.coaching",
    "mut.marketing",
]

CHECK_TIME = os.environ.get("CHECK_TIME", "09:00")
STATE_FILE = "/tmp/seen_posts.json"
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
    url = "https://api.apify.com/v2/acts/apify~instagram-profile-scraper/run-sync-get-dataset-items"
    params = {"token": APIFY_TOKEN}
    payload = {
        "usernames": [username],
        "resultsLimit": count,
    }
    posts = []
    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post(url, params=params, json=payload)
            resp.raise_for_status()
            data = resp.json()

        log.info("Apify Antwort für @%s: %s", username, str(data)[:500])
        for item in data:
            for post in item.get("latestPosts", []):
                posts.append({
                    "shortcode": post.get("shortCode", post.get("id", "")),
                    "url": post.get("url", f"https://www.instagram.com/p/{post.get('shortCode', '')}/"),
                    "thumbnail": post.get("displayUrl", ""),
                    "caption": (post.get("caption") or "")[:400],
                    "is_video": post.get("type", "") == "Video",
                    "date": (post.get("timestamp", "") or "")[:10],
                    "likes": post.get("likesCount", 0),
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
            if post["thumbnail"]:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=post["thumbnail"],
                    caption=text,
                    parse_mode="Markdown",
                )
            else:
                raise TelegramError("Kein Bild")
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
            log.warning("  → Keine Posts erhalten für @%s", account)
            continue

        known = set(state.get(account, []))
        new_posts = [p for p in latest if p["shortcode"] not in known]
        
        state[account] = list(known | {p["shortcode"] for p in latest})

    save_state(state)
    log.info("Check abgeschlossen.")


def run_check():
    asyncio.run(check_all_accounts())


if __name__ == "__main__":
    log.info("Bot gestartet. Täglicher Check um %s Uhr.", CHECK_TIME)
    schedule.every().day.at(CHECK_TIME).do(run_check)
    run_check()
    while True:
        schedule.run_pending()
        time.sleep(30)
