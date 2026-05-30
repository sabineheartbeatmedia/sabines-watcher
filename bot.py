import os
import json
import logging
import asyncio
import httpx
import time
from datetime import date, timedelta

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

APIFY_BASE   = "https://api.apify.com/v2"
STATE_STORE  = "sabines-watcher"   # Name des Apify Key-Value Stores
STATE_KEY    = "seen-posts"
MAX_AGE_DAYS = 3   # nur Posts der letzten X Tage gelten als "neu"
# ─────────────────────────────────────────────────────────────────────────


def _is_recent(datestr: str) -> bool:
    """True, wenn das Datum (YYYY-MM-DD) innerhalb der letzten MAX_AGE_DAYS liegt."""
    try:
        d = date.fromisoformat((datestr or "")[:10])
    except Exception:
        return False
    return d >= date.today() - timedelta(days=MAX_AGE_DAYS)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)


# ── State in Apify KV Store speichern ────────────────────────────────────

def load_state(client: httpx.Client) -> dict:
    url = f"{APIFY_BASE}/key-value-stores/~{STATE_STORE}/records/{STATE_KEY}"
    r = client.get(url, params={"token": APIFY_TOKEN})
    if r.status_code == 200:
        return r.json()
    return {}


def save_state(client: httpx.Client, state: dict):
    url = f"{APIFY_BASE}/key-value-stores/~{STATE_STORE}/records/{STATE_KEY}"
    client.put(url, params={"token": APIFY_TOKEN}, json=state)


# ── Instagram Posts via Apify holen ──────────────────────────────────────

def fetch_latest_posts(client: httpx.Client, username: str, count: int = 5) -> list[dict]:
    headers = {"Authorization": f"Bearer {APIFY_TOKEN}"}
    payload = {"usernames": [username], "resultsLimit": count}

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
        status = client.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            headers=headers,
        ).json()["data"]["status"]
        log.info("  Status: %s", status)
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            log.error("  Run fehlgeschlagen: %s", status)
            return []

    data = client.get(
        f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items",
        headers=headers,
    ).json()

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
    log.info("  %d Posts gefunden", len(posts))
    return posts


# ── Telegram Nachricht senden ─────────────────────────────────────────────

async def send_post(bot: Bot, account: str, post: dict):
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
        )


# ── Hauptprogramm ─────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=TELEGRAM_TOKEN)

    with httpx.Client(timeout=30) as client:
        state = load_state(client)
        first_run = len(state) == 0

        for account in INSTAGRAM_ACCOUNTS:
            log.info("Prüfe @%s …", account)
            try:
                latest = fetch_latest_posts(client, account, count=5)
            except Exception as e:
                log.error("Fehler bei @%s: %s", account, e)
                continue

            if not latest:
                log.warning("  Keine Posts erhalten")
                continue

            known     = set(state.get(account, []))
            # neu = noch nicht gesehen UND wirklich frisch (keine alten Pins)
            new_posts = [
                p for p in latest
                if p["shortcode"] not in known and _is_recent(p["date"])
            ]

            if not first_run and new_posts:
                log.info("  → %d neuer Post(s)", len(new_posts))
                for post in reversed(new_posts):
                    await send_post(bot, account, post)
                    await asyncio.sleep(1)
            elif not first_run:
                log.info("  → Keine neuen Posts")

            state[account] = list(known | {p["shortcode"] for p in latest})

        save_state(client, state)

    if first_run:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "✅ *Sabines Watcher ist live!*\n\n"
                "Ich beobachte ab jetzt täglich:\n\n"
                "• @carolinepreussde\n"
                "• @abovebeyond.coaching\n"
                "• @mut.marketing"
            ),
            parse_mode="Markdown",
        )
        log.info("Erster Start abgeschlossen — ab jetzt läuft der tägliche Check.")
    else:
        log.info("Check abgeschlossen.")


if __name__ == "__main__":
    asyncio.run(main())
