# Intelligence Radar Telegram Bot (MVP)

A personal Telegram bot that collects cybersecurity/startup/AI items from RSS/Atom feeds, sends notifications about newly discovered items, and answers questions using its collected articles plus saved Telegram conversation history.

## What is included

- Persistent source registry in SQLite, seeded from `feeds.json` and managed from Telegram.
- RSS/Atom monitoring every 15 minutes by default, with optional public Telegram channels.
- Telegram notifications for newly discovered items, with short AI summaries and source links.
- SQLite persistence for articles and messages so context survives restarts.
- `/latest`, `/digest`, `/sources`, `/addsource`, `/removesource`, `/enablesource`, `/disablesource`,
  `/check`, `/status`, `/mute`, `/unmute`, `/forget`, `/help`.
- Private-user allowlist, so only Telegram IDs explicitly added to `.env` can use the assistant.
- Editable project/profile context in `context.md`, to seed the bot with the purpose of this project.
- First scan establishes a baseline without spamming you with old feed entries.
- `/forget` deletes chat messages but deliberately leaves the collected article database intact.

## Requirements

- Python 3.11+ recommended.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A Gemini API key for AI answers and summaries. Gemini API usage is billed separately from the Gemini consumer app.
- Internet access while the bot is running.

## Windows setup (PowerShell)

Open PowerShell in this project directory.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Fill in `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY`. Leave `ALLOWED_TELEGRAM_USER_IDS` blank for the initial ID-discovery step. Keep `.env` private; do not commit it to Git or share screenshots containing secrets.

Start the app:

```powershell
python bot.py
```

In Telegram, open your bot and send `/start`. When the allowlist is empty, the bot responds with your numeric Telegram user ID. Copy that number into `.env`:

```dotenv
ALLOWED_TELEGRAM_USER_IDS=123456789
```

Stop the process with `Ctrl+C`, restart `python bot.py`, then send `/start` again. The bot should now register your private chat and begin monitoring feeds. You can stop the bot while editing `.env`; restart it after changes.

### Optional environment values

- `GEMINI_MODEL`: model name available to your Google AI project. Default: `gemini-2.5-flash`.
- `OPENAI_API_KEY` and `OPENAI_MODEL`: optional OpenAI fallback settings used when no Gemini key is configured.
- `POLL_INTERVAL_MINUTES`: feed check interval; minimum supported by this app is 5 minutes.
- `MAX_NOTIFICATIONS_PER_CYCLE`: max pending news items to deliver in one polling cycle (1–10).
- `DATABASE_PATH`: local SQLite database path.
- `DIGEST_HOURS`: time window used by `/digest`.
- `RADAR_HIGH_THRESHOLD`: minimum deterministic score for immediate notifications (default `65`).
- `RADAR_MEDIUM_THRESHOLD`: minimum score included in relevance-ranked digests (default `35`).
- `RADAR_INTERESTS_JSON`: optional JSON object overriding the built-in keyword groups.
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH`: optional Telethon configuration.

## Commands and examples

- `/latest` — latest collected articles and URLs.
- `/digest` — AI-generated summary of recently collected articles.
- `/sources` — list sources, status, priority, last check, and errors.
- `/addsource Name | type | locator | categories | priority` — add a source.
- `/removesource Name or locator` — remove a source.
- `/enablesource Name or locator` / `/disablesource Name or locator` — control monitoring.
- `/check` — check enabled sources now.
- `/mute` and `/unmute` — pause/resume alerts without stopping collection.
- `/status` — initialized feeds and polling configuration.
- `/forget` — delete this chat's saved conversation messages; articles remain.

Then simply chat normally:

- `What did we discuss about SAST tools?`
- `Summarize the latest security advisories and tell me what I should investigate.`
- `Why could this startup announcement matter to my company?`
- `Remind me what architecture we agreed on for this bot.`

## Add sources

Sources persist in SQLite. Existing entries from `feeds.json` are imported automatically on startup.
You can add sources from Telegram:

```text
/addsource CISA | rss | https://www.cisa.gov/cybersecurity-advisories/all.xml | cybersecurity,cve | 5
```

Supported types are `rss`, `telegram`, `website`, `x`, and `linkedin`. `website`, `x`, and
`linkedin` are registry types for future authorized API adapters and are not fetched yet. Telegram
sources can be a public username (`@channel_name`) or a folder locator (`folder:THE startup`).

You can still edit `feeds.json` and add entries with these fields:

```json
{
  "name": "Official regulator or news site",
  "url": "https://example.org/feed.xml",
  "category": "law"
}
```

A feed must expose RSS or Atom. Use official APIs, authorized platform integrations, or feeds whose terms allow this use. This MVP does **not** scrape private Telegram groups or LinkedIn, and it does not directly monitor X. Social platforms can impose authentication, API access, and automation limits; integrate them separately through permitted APIs or other authorized methods. For laws, prefer the official legal text or regulator announcement as the source.

Suggested initial source types: government security advisories, security-vendor blogs, research labs, startup publications, and official regulator news. The included feeds are initial examples; confirm their current availability and add specific Ethiopian/African legal, event, funding, and accelerator sources you trust.

## Optional public Telegram channels

This integration uses a separate Telethon user session. The BotFather bot token is never used to
read channel history, and the bot never asks for login credentials or codes.

PowerShell setup:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
notepad .env
# Set TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_SESSION_PATH
python telegram_login.py
```

The login code and any two-factor prompt are entered only in the terminal. Keep the generated
`data\telegram_user.session` private. Then add a public channel:

```text
/addsource CISA Telegram | telegram | @CISAgov | cybersecurity,cve | 5
```

To monitor all channels and groups in a named Telegram folder:

```text
/addsource THE startup folder | telegram | folder:THE startup | events,startups | 3
```

If Telegram user-session settings are absent, RSS monitoring continues normally. Flood waits,
unavailable channels, and network failures are recorded on the source and do not stop other sources.

## How memory works

- Every user and assistant text message is saved to the local SQLite database.
- `context.md` provides editable project background from this design discussion. It is not a transcript.
- Each answer includes a bounded recent conversation window and a keyword-ranked search of older messages and collected articles; the full conversation is never sent to the LLM.
- Relevance scores are deterministic and explainable. High-relevance items notify immediately, medium items remain available for `/digest`, and low-relevance items are stored without interruption.
- This is durable basic memory, not perfect human-like recall. It does not yet use embeddings/semantic vector search, so phrase older topics using a project name or a few remembered keywords where possible.
- The Telegram bot does not automatically have access to your ChatGPT conversation history. This package seeds `context.md` with the main project brief, then remembers the conversations you have inside Telegram.
- In this MVP the bot does not search the live web when you ask a question; it answers from its stored articles, conversation context, and general model knowledge while being instructed to say when local evidence is missing.
- Back up `data/intel_radar.db` if you want to preserve memory. Treat it as personal data.

## Security and operating notes

- The allowlist is enforced on every handler. Keep the bot in private chat and set your Telegram numeric user ID.
- Treat both `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` as secrets. If exposed, rotate them.
- Avoid putting confidential company logs, credentials, or client personal data into the chat.
- Feed content is untrusted input. The AI prompts explicitly instruct the model not to follow instructions embedded in articles.
- The process must remain running to poll feeds. For 24/7 notifications, later deploy it to a VPS or a small always-on server with persistent storage and restart monitoring.
- Start with a few feeds and inspect noise/quality before adding dozens. Telegram and source-provider rate limits still apply.

## Troubleshooting

- `TELEGRAM_BOT_TOKEN` error: confirm the token from BotFather and remove spaces.
- The bot replies with your ID every time: you have not added your numeric ID to `ALLOWED_TELEGRAM_USER_IDS`, or you forgot to restart.
- No alert yet: the first feed scan intentionally indexes current feed entries silently. Run `/latest`; then wait for a new post or run `/check` later.
- Source failures: run `/sources` and `/check` to inspect last-check errors. Some providers may block automated clients, change feed URLs, or temporarily be unavailable.
- Gemini error: confirm API billing/limits and `GEMINI_MODEL`; a Gemini consumer subscription does not automatically include API credit.

## Current limitations / next milestones

1. Add authorized X and LinkedIn API adapters behind the source registry.
2. Add semantic retrieval (local embeddings or an approved provider) for more accurate long-term memory.
3. Add source-specific entity extraction and corroboration across independent reports.

## Tests and database migration

The SQLite migration runs automatically at startup and preserves existing article, message, chat,
and feed-state records. No destructive migration command is required.

PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s . -p "test_*.py" -v
python -m py_compile bot.py db.py ai.py monitoring.py relevance.py telegram_login.py
python bot.py
```
