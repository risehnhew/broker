from __future__ import annotations

import re
from dataclasses import dataclass

from broker.ib_client import NewsHeadline


POSITIVE_KEYWORDS = {
    "beat",
    "beats",
    "surge",
    "growth",
    "upgrade",
    "record",
    "strong",
    "profit",
    "bullish",
    "partnership",
    "buyback",
    "raise",
}

NEGATIVE_KEYWORDS = {
    "miss",
    "misses",
    "fall",
    "drop",
    "downgrade",
    "weak",
    "lawsuit",
    "probe",
    "fraud",
    "cut",
    "warning",
    "bearish",
    "decline",
}

# Precompiled word-boundary patterns — one regex per set for O(1) lookup
_POSITIVE_RE = re.compile(
    r"\b(?:" + "|".join(sorted(POSITIVE_KEYWORDS, key=len, reverse=True)) + r")\b"
)
_NEGATIVE_RE = re.compile(
    r"\b(?:" + "|".join(sorted(NEGATIVE_KEYWORDS, key=len, reverse=True)) + r")\b"
)


@dataclass(frozen=True)
class NewsAnalysis:
    score: int
    sentiment: str
    headline_count: int
    headlines: list[str]


class NewsAnalyzer:
    def analyze(self, headlines: list[NewsHeadline]) -> NewsAnalysis:
        score = 0
        texts: list[str] = []

        for item in headlines:
            text = item.headline.strip()
            if not text:
                continue
            texts.append(f"[{item.provider_code}] {text}")
            lowered = text.lower()
            score += len(set(_POSITIVE_RE.findall(lowered)))
            score -= len(set(_NEGATIVE_RE.findall(lowered)))

        if score >= 2:
            sentiment = "POSITIVE"
        elif score <= -2:
            sentiment = "NEGATIVE"
        else:
            sentiment = "NEUTRAL"

        return NewsAnalysis(
            score=score,
            sentiment=sentiment,
            headline_count=len(texts),
            headlines=texts,
        )
