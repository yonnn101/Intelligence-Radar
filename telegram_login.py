"""One-time terminal login for optional public Telegram channel monitoring."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv


async def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    session_path = os.getenv("TELEGRAM_SESSION_PATH", "./data/telegram_user")
    if not api_id.isdigit() or not api_hash:
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env before running this script.")
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise SystemExit("Install requirements.txt first; Telethon is required for channel access.") from exc
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    async with TelegramClient(session_path, int(api_id), api_hash) as client:
        await client.start()
        print(f"Telegram user session authorized and saved at {session_path}.")
        print("Keep this session file private and never commit it.")


if __name__ == "__main__":
    asyncio.run(main())
