import os
import json
import logging
import asyncio
import httpx
import time
from pathlib import Path

import schedule
from telegram import Bot
from telegram.error import TelegramError

# ── Config ───────────────────────────────────────────────────────────────
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
APIFY_BASE = "https://api.apify.com/v2"
# ─────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_latest_posts(username: str, count: int = 5) -> list[dict]:
    headers = {"Authorization": f"Bearer {APIFY_TOKEN}"}
    payload = {"usernames": [username], "resultsLimit": count}

    with httpx.Client(timeout=30) as client:
        r = client.post(
            f"{APIFY_BASE}/acts/apify~instagram-profile-scraper/runs",
            headers=headers,
            json=payload,
        )
        r.raise_for_status()
        run_id = r.json()["data"]["id"]
        log.info("  Apify Run gestartet: %s", run_id)

        for _ in range(36):
            time.sleep(5)
            status_r = client.get(
                f"{APIFY_BASE}/actor-runs/{run_id}",
                headers=headers,
            )
            status = status_r.json()["data"]["status"]
            log.info("  Run Status: %s", status)
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                log.error("  Apify Run fehlgeschlagen: %s", status)
                return []

        dataset_r = client.get(
            f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items",
            headers=headers,
        )
        dataset_r.raise_for_status()
        data = dataset_r.json()

    posts = []
    for item in data:
        for post in item.get("latestPosts", []):
            posts.append({
                "shortcode": post.get("shortCode", post.get("id", "")),
                "url":       post.get("url", f"https://www.instagram.com/p/{post.get('shortCode', '')}/"),
                "thumbnail": post.get("displayUrl", ""),
                "caption":   (post.get("caption") or "")[:400],
                "is_video":  post.get("type", "") == "Video",
                "date":      (post.get("timestamp", "") or "")[:10],
                "likes":     post.get("likesCount", 0),
            })
    log.info("  %d Posts gefunden für @%s", len(posts), username)
    return posts


async def initialize_state():
    log.info("Erster Start — lerne aktuelle Posts kennen (nichts wird gesendet) …")
    state = {}
    for account in INSTAGRAM_ACCOUNTS:
        log.info("  Merke @%s …", account)
        try:
            latest = fetch_latest_posts(account, count=5)
            state[account] = [p["shortcode"] for p in latest]
            log.info("  %d Posts von @%s gemerkt.", len(state[account]), account)
        except Exception as e:
            log.error("  Fehler bei @%s: %s", account, e)
            state[account] = []
    save_state(state)
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "✅ *Sabines Watcher ist live!*\n\n"
            "Ich beobachte ab jetzt täglich um {} Uhr:\n\n"
            "• @carolinepreussde\n"
            "• @abovebeyond.coaching\n"
            "• @mut.marketing"
        ).format(CHECK_TIME),
        parse_mode="Markdown",
    )


async def send_new_posts(bot: Bot, account: str, new_posts: list[dict]):
    for post in reversed(new_posts):
        media_type   = "🎬 Video" if post["is_video"] else "🖼️ Post"
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
    log.info("Starte täglichen Instagram-Check …")
    bot   = Bot(token=TELEGRAM_TOKEN)
    state = load_state()

    for account in INSTAGRAM_ACCOUNTS:
        log.info("Prüfe @%s …", account)
        try:
            latest = fetch_latest_posts(account, count=5)
        except Exception as e:
            log.error("Fehler bei @%s: %s", account, e)
            continue

        if not latest:
            log.warning("  Keine Posts erhalten für @%s", account)
            continue

        known     = set(state.get(account, []))
        new_posts = [p for p in latest if p["shortcode"] not in known]

        if new_posts:
            log.info("  → %d neuer Post(s) bei @%s", len(new_posts), account)
            await send_new_posts(bot, account, new_posts)
        else:
            log.info("  → Keine neuen Posts bei @%s", account)

        state[account] = list(known | {p["shortcode"] for p in latest})

    save_state(state)
    log.info("Check abgeschlossen. Nächster Check morgen um %s Uhr.", CHECK_TIME)


def run_check():
    asyncio.run(check_all_accounts())


if __name__ == "__main__":
    log.info("Sabines Watcher gestartet. Täglicher Check um %s Uhr.", CHECK_TIME)
    schedule.every().day.at(CHECK_TIME).do(run_check)
    asyncio.run(initialize_state())
    log.info("Bereit. Warte auf %s Uhr …", CHECK_TIME)
    while True:
        schedule.run_pending()
        time.sleep(30)
```
