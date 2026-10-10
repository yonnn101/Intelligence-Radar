# Personal Intelligence Profile and Initial Project Brief

This is editable background context for the Telegram assistant. It is not a verbatim transcript or proof that every implementation detail has been approved.

## Interests to prioritize

- Cybersecurity engineering, application security, SAST, vulnerabilities, threat intelligence, defensive security, and open-source security tools.
- AI security and practical AI-assisted engineering.
- Startups, product launches, funding, startup programs, grants, accelerators, hackathons, conferences, and useful career opportunities.
- Laws, regulations, compliance requirements, and official announcements relevant to Ethiopia and East Africa, while also following important global developments.

## Intelligence Radar project brief

The goal is a personal Telegram bot that discovers relevant cybersecurity, startup, legal/regulatory, event, and opportunity updates from configurable sources. It should send useful notifications and support natural-language questions about both collected information and previous conversations.

The working MVP approach is:

- Start with RSS/Atom feeds and official sources before adding social-platform integrations.
- Use a Python Telegram bot, a scheduled feed poller, SQLite persistence for articles and Telegram conversations, and an LLM API for summaries and question answering.
- Store source URLs and publication information with each collected item. Distinguish source claims from independently corroborated facts.
- Use a Telegram user-ID allowlist and keep API tokens in environment variables.
- Avoid alert floods, retain searchable history, and let the user pause notifications.
- Add Telegram public-channel, X, and LinkedIn collection later only through permitted APIs or authorized integration methods. The MVP does not connect to ChatGPT conversation history automatically.

## Memory rules

Use saved Telegram messages for conversational recall. Use these notes only as broad background if no exact prior message is available. Never invent an exact statement, choice, date, or commitment from the user.
