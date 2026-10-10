from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
from ai import ASSISTANT_INSTRUCTIONS, create_digest, generate, summarize_article
from db import Database
from monitoring import TelegramChannelAdapter, fetch_rss
from relevance import score_item

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("intel-radar")
DB = Database(os.getenv("DATABASE_PATH", "./data/intel_radar.db"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
POLL_MINUTES = max(5, int(os.getenv("POLL_INTERVAL_MINUTES", "15")))
MAX_NOTIFICATIONS = max(1, min(10, int(os.getenv("MAX_NOTIFICATIONS_PER_CYCLE", "4"))))
DIGEST_HOURS = max(1, int(os.getenv("DIGEST_HOURS", "24")))
HIGH_THRESHOLD = max(1, min(100, int(os.getenv("RADAR_HIGH_THRESHOLD", "65"))))
MEDIUM_THRESHOLD = max(1, min(HIGH_THRESHOLD, int(os.getenv("RADAR_MEDIUM_THRESHOLD", "35"))))
TELEGRAM_CHANNEL_LIMIT = max(1, min(100, int(os.getenv("TELEGRAM_CHANNEL_POLL_LIMIT", "30"))))
PROJECT_DIR = Path(__file__).resolve().parent
FEEDS_PATH = PROJECT_DIR / "feeds.json"
COLLECT_LOCK = asyncio.Lock()
TELEGRAM_ADAPTER: TelegramChannelAdapter | None = None


async def safe_send(bot, chat_id: int, text: str, parse_mode: str | None = None) -> None:
    """Send plain-text chunks under Telegram's message length limit."""
    text = text.strip() or "(No text returned.)"
    while text:
        part = text[:3900]
        # Prefer a line break so the message remains readable.
        if len(text) > 3900 and "\n" in part:
            part = part.rsplit("\n", 1)[0]
        await bot.send_message(
            chat_id=chat_id, text=part, parse_mode=parse_mode, disable_web_page_preview=True
        )
        text = text[len(part):].lstrip()


def digest_to_html(markdown: str) -> str:
    """Render the limited Markdown produced by Gemini as safe Telegram HTML."""
    escaped = html.escape(markdown, quote=False)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^ )]+)\)", r'<a href="\2">\1</a>', escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?m)^#{1,3} +(.*)$", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?m)^[*-] +(.*)$", r"• \1", escaped)
    return escaped


def read_feeds() -> list[dict[str, str]]:
    with FEEDS_PATH.open("r", encoding="utf-8") as fh:
        feeds = json.load(fh)
    return [f for f in feeds if f.get("name") and f.get("url")]


def article_message(article: dict[str, Any]) -> str:
    category = (article.get("category") or "general").lower()
    icon = "🛡" if "cyber" in category or "security" in category else "🔎"
    summary = article.get("ai_summary") or article.get("content") or "Open the source for details."
    summary = " ".join(summary.split())[:900]
    return (
        f"{icon} INTELLIGENCE UPDATE\n\n{article.get('title', 'Untitled item')}\n"
        f"Category: {category}\nRelevance: {article.get('relevance_score', 0)}/100 "
        f"({article.get('relevance_level', 'low')})\n"
        f"Why it matters: {article.get('relevance_reason', 'Configured interest match')}\n"
        f"Source: {article.get('source', 'Unknown source')}\n"
        f"Published: {article.get('published_at', 'Unknown date')}\n\n"
        f"{summary}\n\nSource link: {article.get('url', '')}"
    )


async def collect_and_notify(application: Application) -> dict[str, int]:
    if COLLECT_LOCK.locked():
        return {"sources_ok": 0, "sources_failed": 0, "new_articles": 0, "notifications": 0, "skipped": 1}
    async with COLLECT_LOCK:
        return await _collect_and_notify(application)


async def _collect_and_notify(application: Application) -> dict[str, int]:
    global TELEGRAM_ADAPTER
    DB.seed_sources(read_feeds())
    sources = DB.list_sources(enabled_only=True)
    stats = {"sources_ok": 0, "sources_failed": 0, "new_articles": 0, "notifications": 0, "skipped": 0}
    timeout = httpx.Timeout(20.0, connect=10.0)
    headers = {"User-Agent": "IntelRadarBot/0.1 (personal RSS reader; contact: local operator)"}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for source in sources:
            is_baseline = not DB.feed_is_initialized(source["locator"])
            try:
                if source["source_type"] in {"rss", "website"}:
                    entries = await fetch_rss(client, source)
                elif source["source_type"] == "telegram":
                    if not os.getenv("TELEGRAM_API_ID", "").isdigit() or not os.getenv("TELEGRAM_API_HASH", "").strip():
                        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required for Telegram sources")
                    if TELEGRAM_ADAPTER is None:
                        TELEGRAM_ADAPTER = TelegramChannelAdapter(
                            int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"],
                            os.getenv("TELEGRAM_SESSION_PATH", "./data/telegram_user"), TELEGRAM_CHANNEL_LIMIT,
                        )
                    entries = await TELEGRAM_ADAPTER.fetch(source)
                else:
                    raise RuntimeError(f"Source type '{source['source_type']}' has no adapter yet")
                stats["sources_ok"] += 1
                for entry in entries:
                    relevance = score_item(
                        title=entry.title, content=entry.content, category=source["categories"],
                        source_priority=int(source["priority"]), published_at=entry.published_at,
                    )
                    item_id = DB.insert_article(
                        url=entry.url, title=entry.title, source=source["name"],
                        category=source["categories"], published_at=entry.published_at, content=entry.content,
                        baseline=is_baseline,
                        source_id=int(source["id"]), external_id=entry.external_id,
                        relevance_score=relevance.score, relevance_level=relevance.level,
                        relevance_reason=relevance.reason, entities=", ".join(relevance.entities),
                    )
                    if item_id is not None:
                        stats["new_articles"] += 1
                if is_baseline:
                    DB.mark_feed_initialized(source["locator"])
                DB.update_feed_checked(source["locator"])
                DB.update_source_state(int(source["id"]))
            except Exception as exc:
                stats["sources_failed"] += 1
                DB.update_source_state(int(source["id"]), error=str(exc), checked=False)
                log.warning("Source check failed for %s: %s", source["name"], exc)

    # High-relevance items notify immediately; medium items remain available to /digest.
    chats = DB.list_active_chats()
    if chats:
        for article in DB.pending_articles(MAX_NOTIFICATIONS, minimum_score=HIGH_THRESHOLD):
            summary = await summarize_article(
                article["title"], article["source"], article["category"], article["content"]
            )
            DB.update_article_summary(article["id"], summary)
            article["ai_summary"] = summary
            message = article_message(article)
            sent_to_someone = False
            for chat_id in chats:
                try:
                    await safe_send(application.bot, chat_id, message)
                    sent_to_someone = True
                    stats["notifications"] += 1
                except Exception as exc:
                    log.warning("Could not notify chat %s: %s", chat_id, exc)
            if sent_to_someone:
                DB.mark_notified(article["id"])

    log.info("Feed cycle: %s", stats)
    return stats


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await collect_and_notify(context.application)
    except Exception:
        log.exception("Scheduled feed check failed")


def has_access(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and user.id in ALLOWED_IDS and chat and chat.type == ChatType.PRIVATE)


def read_project_context() -> str:
    path = PROJECT_DIR / "context.md"
    try:
        return path.read_text(encoding="utf-8")[:10000]
    except OSError:
        return ""


async def access_denied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.effective_message:
        return
    if not ALLOWED_IDS:
        await update.effective_message.reply_text(
            "🔐 Setup needed. Your Telegram user ID is: "
            f"{user.id}\n\nAdd this number to .env like this:\n"
            f"ALLOWED_TELEGRAM_USER_IDS={user.id}\nThen restart the bot and send /start again."
        )
    else:
        await update.effective_message.reply_text("This is a private bot; your account is not authorized.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    if chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("For privacy, use this bot in a direct private chat.")
        return
    DB.register_chat(chat.id, user.id, user.username or "")
    await update.effective_message.reply_text(
        "Your Intelligence Radar is ready.\n\n"
        "I monitor the configured RSS feeds, send new-item alerts, and remember messages from "
        "our Telegram conversations in a local SQLite database. I also have the editable project "
        "brief in context.md. I do not automatically have your full ChatGPT history.\n\n"
        "Try /help to see commands. The first scan creates a baseline and does not send a flood "
        "of old articles; use /latest or /digest to review the collected items."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    await update.effective_message.reply_text(
        "Commands\n"
        "/latest — show the newest collected items\n"
        "/digest — create an AI digest of recent collected items\n"
        "/sources — list configured sources and status\n"
        "/addsource Name | type | locator | categories | priority\n"
        "/removesource Name or locator\n"
        "/enablesource Name or locator\n"
        "/disablesource Name or locator\n"
        "/check — check enabled sources now\n"
        "/mute — pause notifications\n"
        "/unmute — resume notifications\n"
        "/status — show bot/feed status\n"
        "/forget — erase saved conversation messages (articles remain)\n\n"
        "You can also ask normal questions, e.g. ‘What did we discuss about SAST?’ or "
        "‘Why does this new advisory matter?’ Answers use your saved chat history and collected feed items."
    )


async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    articles = DB.recent_articles(limit=8)
    if not articles:
        await update.effective_message.reply_text("No articles stored yet. Use /check to fetch the feeds.")
        return
    lines = ["Latest collected intelligence:"]
    for item in articles:
        lines.append(
            f"\n• {item['title']}\n"
            f"  {item['source']} | {item['published_at']}\n  {item['url']}"
        )
    await safe_send(context.bot, update.effective_chat.id, "\n".join(lines))


async def digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    await update.effective_message.reply_text(f"Building a digest from the last {DIGEST_HOURS} hours of collected items…")
    try:
        articles = DB.recent_articles(limit=40, hours=DIGEST_HOURS, minimum_score=MEDIUM_THRESHOLD)
        output = await create_digest(articles)
        await safe_send(context.bot, update.effective_chat.id, digest_to_html(output), parse_mode="HTML")
    except Exception as exc:
        log.exception("Digest failed")
        await update.effective_message.reply_text(f"I couldn't create the digest: {exc.__class__.__name__}. Check the API key/model and logs.")


async def sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    DB.seed_sources(read_feeds())
    configured = DB.list_sources()
    if not configured:
        await update.effective_message.reply_text("No sources configured. Use /addsource to add one.")
        return
    body = "Configured intelligence sources:\n\n" + "\n\n".join(
        f"• {s['name']} [{s['source_type']}] {'enabled' if s['enabled'] else 'disabled'}\n"
        f"  {s['locator']}\n  Categories: {s['categories']} | Priority: {s['priority']}\n"
        f"  Last checked: {s['last_checked_at'] or 'never'}"
        f"{' | Error: ' + s['last_error'][:160] if s['last_error'] else ''}"
        for s in configured
    )
    await safe_send(context.bot, update.effective_chat.id, body)


def source_args(args: list[str]) -> tuple[str, str, str, str, int] | None:
    parts = " ".join(args).split("|")
    if len(parts) != 5:
        return None
    name, source_type, locator, categories, priority_text = [part.strip() for part in parts]
    if source_type not in {"rss", "telegram", "website", "x", "linkedin"}:
        return None
    if not locator or not name or not categories or not priority_text.isdigit():
        return None
    priority = int(priority_text)
    if priority < 1 or priority > 5:
        return None
    if source_type in {"rss", "website"} and not locator.startswith(("http://", "https://")):
        return None
    if source_type == "telegram" and (
        not locator.strip()
        or (
            not locator.lower().startswith("folder:")
            and not locator.lstrip("@").replace("_", "").isalnum()
        )
    ):
        return None
    return name, source_type, locator, categories, priority


async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    parsed = source_args(context.args)
    if not parsed:
        await update.effective_message.reply_text(
            "Usage: /addsource Name | type | locator | categories | priority\n"
            "Types: rss, telegram, website, x, linkedin\n"
            "Example: /addsource CISA | rss | https://www.cisa.gov/cybersecurity-advisories/all.xml | cybersecurity,cve | 5"
        )
        return
    name, source_type, locator, categories, priority = parsed
    if DB.add_source(name=name, source_type=source_type, locator=locator, categories=categories, priority=priority) is None:
        await update.effective_message.reply_text("A source with that name or locator already exists.")
        return
    await update.effective_message.reply_text(f"Added source '{name}'. It will be checked on the next cycle.")


async def remove_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    target = " ".join(context.args).strip()
    if not target:
        await update.effective_message.reply_text("Usage: /removesource Name or locator")
        return
    await update.effective_message.reply_text(
        "Source removed." if DB.remove_source(target) else "No source matched that name or locator."
    )


async def set_source_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    target = " ".join(context.args).strip()
    command = "enablesource" if enabled else "disablesource"
    if not target:
        await update.effective_message.reply_text(f"Usage: /{command} Name or locator")
        return
    changed = DB.set_source_enabled(target, enabled)
    await update.effective_message.reply_text(
        f"Source {'enabled' if enabled else 'disabled'}." if changed else "No source matched that name or locator."
    )


async def enable_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_source_enabled(update, context, True)


async def disable_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_source_enabled(update, context, False)


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    await update.effective_message.reply_text("Checking configured feeds now…")
    try:
        stats = await collect_and_notify(context.application)
        await update.effective_message.reply_text(
            "Source check finished.\n"
            f"Sources successful: {stats['sources_ok']}\n"
            f"Sources failed: {stats['sources_failed']}\n"
            f"New items stored: {stats['new_articles']}\n"
            f"Notifications sent: {stats['notifications']}\n\n"
            "On a first check, existing entries are indexed without notification to prevent a backlog flood."
        )
    except Exception as exc:
        log.exception("Manual feed check failed")
        await update.effective_message.reply_text(f"Feed check failed: {exc.__class__.__name__}. Check the terminal logs.")


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    DB.set_alerts(update.effective_chat.id, False)
    await update.effective_message.reply_text("Notifications paused. I will keep collecting items; use /unmute to resume alerts.")


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    DB.set_alerts(update.effective_chat.id, True)
    await update.effective_message.reply_text("Notifications resumed.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    DB.seed_sources(read_feeds())
    configured = DB.list_sources()
    ok = sum(1 for source in configured if source["last_checked_at"] and not source["last_error"])
    chats = DB.list_active_chats()
    await update.effective_message.reply_text(
        f"Intelligence Radar status\n"
        f"Configured sources: {len(configured)}\n"
        f"Sources healthy: {ok}/{len(configured)}\n"
        f"Polling interval: {POLL_MINUTES} minutes\n"
        f"Relevance thresholds: high >= {HIGH_THRESHOLD}, medium >= {MEDIUM_THRESHOLD}\n"
        f"Active notification chats: {len(chats)}\n"
        f"Database: {DB.path}"
    )


async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    DB.forget_messages(update.effective_chat.id)
    await update.effective_message.reply_text(
        "Saved conversation messages for this chat have been deleted. Collected articles and source history were not deleted."
    )


def format_memory_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "(No previous messages found.)"
    return "\n".join(
        f"[{m.get('created_at', '')}] {m['role'].upper()}: {m['content']}" for m in messages
    )


def format_articles(articles: list[dict[str, Any]]) -> str:
    if not articles:
        return "(No related collected articles were found.)"
    blocks = []
    for a in articles:
        blocks.append(
            f"Title: {a.get('title', '')}\nCategory: {a.get('category', '')}\n"
            f"Source: {a.get('source', '')}\nPublished: {a.get('published_at', '')}\n"
            f"Summary/excerpt: {(a.get('ai_summary') or a.get('content') or '')[:800]}\n"
            f"URL: {a.get('url', '')}"
        )
    return "\n\n---\n\n".join(blocks)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_access(update):
        await access_denied(update, context)
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or not message.text:
        return
    question = message.text.strip()
    if not question:
        return

    DB.store_message(chat.id, "user", question)
    recent = DB.recent_messages(chat.id, limit=16)
    older_memories = DB.search_older_memories(chat.id, question, skip_latest=len(recent), limit=4)
    related_articles = DB.search_articles(question, limit=5)
    prompt = (
        "Use the evidence below to answer the latest user message. Prefer direct, useful answers. "
        "Mention dates and links when useful. Do not claim to remember something unless it appears "
        "in the supplied conversation context.\n\n"
        "PROJECT CONTEXT NOTES (editable context.md; background only, not a record of exact quotes):\n"
        f"{read_project_context()}\n\n"
        "RELEVANT OLDER CONVERSATION MEMORIES:\n"
        f"{format_memory_messages(older_memories)}\n\n"
        "RELATED COLLECTED ARTICLES:\n"
        f"{format_articles(related_articles)}\n\n"
        "RECENT CONVERSATION BEFORE THE CURRENT QUESTION (in chronological order):\n"
        f"{format_memory_messages(recent[:-1])}\n\n"
        f"LATEST USER MESSAGE:\n{question}"
    )
    try:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        answer = await generate(prompt, ASSISTANT_INSTRUCTIONS, max_output_tokens=850)
    except Exception as exc:
        log.exception("Chat response failed")
        answer = (
            f"I couldn't generate a response ({exc.__class__.__name__}). Check that GEMINI_API_KEY "
            "and GEMINI_MODEL in .env are valid, then inspect the terminal logs. Your message was saved."
        )
    DB.store_message(chat.id, "assistant", answer)
    await safe_send(context.bot, chat.id, answer)


async def post_init(application: Application) -> None:
    DB.init()
    DB.seed_sources(read_feeds())
    commands = [
        BotCommand("start", "Start the private intelligence bot"),
        BotCommand("help", "Show commands and examples"),
        BotCommand("latest", "Show latest collected items"),
        BotCommand("digest", "Summarize recent intelligence"),
        BotCommand("sources", "List configured feeds"),
        BotCommand("check", "Check feeds now"),
        BotCommand("mute", "Pause notifications"),
        BotCommand("unmute", "Resume notifications"),
        BotCommand("status", "Show feed status"),
        BotCommand("forget", "Delete saved conversation messages"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as exc:
        log.warning("Could not set Telegram commands: %s", exc)
    if application.job_queue:
        application.job_queue.run_repeating(
            scheduled_check,
            interval=POLL_MINUTES * 60,
            first=15,
            name="rss-feed-poll",
        )
    log.info("Bot initialized. Polling every %s minutes.", POLL_MINUTES)


async def post_shutdown(application: Application) -> None:
    global TELEGRAM_ADAPTER
    if TELEGRAM_ADAPTER is not None:
        await TELEGRAM_ADAPTER.close()
        TELEGRAM_ADAPTER = None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled Telegram error: %s", context.error)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "replace_me":
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first. See README.md.")
    if not (
        os.getenv("GEMINI_API_KEY", "").strip()
        and os.getenv("GEMINI_API_KEY", "").strip() != "replace_me"
    ) and not (
        os.getenv("OPENAI_API_KEY", "").strip()
        and os.getenv("OPENAI_API_KEY", "").strip() != "replace_me"
    ):
        log.warning("GEMINI_API_KEY is missing. Feed collection can still run, but AI chat/summaries will fail.")
    DB.init()
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("latest", latest))
    application.add_handler(CommandHandler("digest", digest))
    application.add_handler(CommandHandler("sources", sources))
    application.add_handler(CommandHandler("addsource", add_source))
    application.add_handler(CommandHandler("removesource", remove_source))
    application.add_handler(CommandHandler("enablesource", enable_source))
    application.add_handler(CommandHandler("disablesource", disable_source))
    application.add_handler(CommandHandler("check", check_now))
    application.add_handler(CommandHandler("mute", mute))
    application.add_handler(CommandHandler("unmute", unmute))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("forget", forget))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    log.info("Starting Telegram polling. Database: %s", DB.path)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
