"""Source adapters for RSS/Atom and optional public Telegram channels."""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("intel-radar.monitoring")


def clean_html(raw: str) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())[:12000]


def entry_date(entry: Any) -> str:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        try:
            return datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, ValueError, TypeError):
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SourceItem:
    external_id: str
    url: str
    title: str
    content: str
    published_at: str


async def fetch_rss(client: httpx.AsyncClient, source: dict[str, Any]) -> list[SourceItem]:
    response = await client.get(source["locator"])
    response.raise_for_status()
    parsed = feedparser.parse(response.text)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(f"Invalid RSS/Atom response: {parsed.bozo_exception}")
    items: list[SourceItem] = []
    for entry in list(parsed.entries)[:50]:
        title = clean_html(getattr(entry, "title", ""))
        link = str(getattr(entry, "link", "") or getattr(entry, "id", "")).strip()
        if not title or not link or not link.startswith(("http://", "https://")):
            continue
        excerpt = clean_html(getattr(entry, "summary", "") or "")
        if not excerpt and getattr(entry, "content", None):
            try:
                excerpt = clean_html(entry.content[0].get("value", ""))
            except (IndexError, AttributeError, TypeError):
                excerpt = ""
        items.append(SourceItem(
            external_id=str(getattr(entry, "id", "") or link)[:300],
            url=link,
            title=title,
            content=excerpt,
            published_at=entry_date(entry),
        ))
    return items


class TelegramChannelAdapter:
    """Optional Telethon adapter; session authentication happens outside the bot."""

    def __init__(self, api_id: int, api_hash: str, session_path: str, limit: int = 30) -> None:
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise RuntimeError("Telethon is not installed; install requirements.txt to enable Telegram sources.") from exc
        self._client = TelegramClient(session_path, api_id, api_hash)
        self._limit = max(1, min(100, limit))

    async def fetch(self, source: dict[str, Any]) -> list[SourceItem]:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("Telegram user session is not authorized; run the documented login setup.")
        locator = source["locator"].strip()
        if locator.lower().startswith("folder:"):
            return await self._fetch_folder(locator[7:].strip())
        username = locator.lstrip("@")
        items: list[SourceItem] = []
        entity = await self._client.get_entity(username)
        async for message in self._client.iter_messages(entity, limit=self._limit):
            if not message.id or not message.message:
                continue
            url = f"https://t.me/{username}/{message.id}"
            published = message.date.astimezone(timezone.utc).isoformat(timespec="seconds") if message.date else datetime.now(timezone.utc).isoformat(timespec="seconds")
            items.append(SourceItem(str(message.id), url, clean_html(message.message)[:600], clean_html(message.message), published))
        return items

    async def _fetch_folder(self, folder_name: str) -> list[SourceItem]:
        """Read recent posts from channels and groups in one named Telegram folder."""
        from telethon import functions, types

        filters_response = await self._client(functions.messages.GetDialogFiltersRequest())
        filters = getattr(filters_response, "filters", [])
        folder = next(
            (
                item for item in filters
                if getattr(item, "title", None)
                and str(getattr(item.title, "text", item.title)).casefold() == folder_name.casefold()
            ),
            None,
        )
        if folder is None:
            raise RuntimeError(f"Telegram folder not found: {folder_name}")

        items: list[SourceItem] = []
        async for dialog in self._client.iter_dialogs():
            if dialog.folder_id != folder.id or not isinstance(dialog.entity, (types.Channel, types.Chat)):
                continue
            entity = dialog.entity
            username = getattr(entity, "username", None)
            async for message in self._client.iter_messages(entity, limit=self._limit):
                if not message.id or not message.message:
                    continue
                if username:
                    url = f"https://t.me/{username}/{message.id}"
                else:
                    chat_id = str(getattr(entity, "id", "")).lstrip("-")
                    url = f"tg://openmessage?chat_id={chat_id}&message_id={message.id}"
                published = (
                    message.date.astimezone(timezone.utc).isoformat(timespec="seconds")
                    if message.date else datetime.now(timezone.utc).isoformat(timespec="seconds")
                )
                title = f"{dialog.name}: {clean_html(message.message)[:600]}"
                items.append(SourceItem(
                    f"{getattr(entity, 'id', dialog.id)}:{message.id}",
                    url, title, clean_html(message.message), published,
                ))
        return items

    async def close(self) -> None:
        await self._client.disconnect()
