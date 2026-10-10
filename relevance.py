"""Deterministic relevance scoring that works without an LLM."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


DEFAULT_INTERESTS: dict[str, tuple[str, ...]] = {
    "cybersecurity": (
        "cve", "vulnerability", "exploit", "ransomware", "malware", "breach",
        "threat intelligence", "incident response", "zero-day", "zero day",
    ),
    "application-security": (
        "sast", "dast", "codeql", "semgrep", "open source security", "appsec",
        "software supply chain", "security research", "ai security",
    ),
    "opportunity": (
        "startup", "funding", "funded", "accelerator", "grant", "competition",
        "hackathon", "scholarship", "job", "hiring", "business opportunity",
    ),
    "law-compliance": (
        "ethiopia", "africa", "regulation", "regulatory", "data protection",
        "privacy law", "compliance", "proclamation", "gdpr", "insa",
    ),
    "events-career": (
        "conference", "training", "certification", "webinar", "summit", "call for papers",
    ),
    "ai-technology": (
        "artificial intelligence", "ai model", "developer tool", "api", "launch",
        "machine learning", "generative ai", "llm",
    ),
}

SEVERITY_TERMS = {
    "critical": 24,
    "actively exploited": 22,
    "zero-day": 22,
    "zero day": 22,
    "ransomware": 18,
    "remote code execution": 18,
    "high severity": 14,
    "critical severity": 18,
}


@dataclass(frozen=True)
class Relevance:
    score: int
    level: str
    reason: str
    categories: tuple[str, ...]
    entities: tuple[str, ...]


def _load_interests() -> dict[str, tuple[str, ...]]:
    raw = os.getenv("RADAR_INTERESTS_JSON", "").strip()
    if not raw:
        return DEFAULT_INTERESTS
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("RADAR_INTERESTS_JSON must be an object")
        return {str(k): tuple(str(item).lower() for item in v) for k, v in data.items() if isinstance(v, list)}
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid RADAR_INTERESTS_JSON: {exc}") from exc


def score_item(
    *, title: str, content: str, category: str, source_priority: int = 3,
    published_at: str = "", interests: dict[str, tuple[str, ...]] | None = None,
) -> Relevance:
    text = f"{title} {content}".lower()
    groups = interests or _load_interests()
    matched: list[str] = []
    reasons: list[str] = []
    score = max(0, min(20, source_priority * 4))
    if category:
        score += 8
        matched.append(category.lower())
        reasons.append(f"source category: {category}")
    for group, terms in groups.items():
        hits = [term for term in terms if term in text]
        if hits:
            matched.append(group)
            score += min(22, 7 + len(hits) * 3)
            reasons.append(f"{group}: {', '.join(hits[:3])}")
    for term, points in SEVERITY_TERMS.items():
        if term in text:
            score += points
            reasons.append(f"severity signal: {term}")
            break
    if published_at:
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            age_hours = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
            if age_hours <= 24:
                score += 8
                reasons.append("published within 24 hours")
            elif age_hours <= 72:
                score += 4
        except ValueError:
            pass
    score = min(100, score)
    high = int(os.getenv("RADAR_HIGH_THRESHOLD", "65"))
    medium = int(os.getenv("RADAR_MEDIUM_THRESHOLD", "35"))
    level = "high" if score >= high else "medium" if score >= medium else "low"
    if not reasons:
        reasons.append("no configured interest match")
    entities = tuple(dict.fromkeys(re.findall(
        r"\b(?:CVE-\d{4}-\d{4,7}|(?:Ethiopia|Kenya|Nigeria|South Africa|Somalia|Africa)|"
        r"(?:Semgrep|CodeQL|GitHub|Microsoft|Google|OpenAI|Anthropic))\b",
        f"{title} {content}",
        flags=re.IGNORECASE,
    )))
    return Relevance(score, level, "; ".join(reasons), tuple(dict.fromkeys(matched)), entities)
