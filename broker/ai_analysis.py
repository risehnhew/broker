from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from openai import OpenAI

from broker.analysis import CandleAnalysis
from broker.analysis import RSIAnalyzer
from broker.analysis import SupportResistance
from broker.analysis import VolumeAnalyzer
from broker.ib_client import HistoricalBar
from broker.models import SimulationResult
from broker.news import NewsAnalysis


@dataclass(frozen=True)
class AIAnalysis:
    action: str
    confidence: int
    sentiment: str
    summary: str
    risks: list[str]
    reasoning_steps: list[str]


@dataclass(frozen=True)
class AISelectionPick:
    symbol: str
    score: int
    action: str
    confidence: int
    reason: str


@dataclass(frozen=True)
class AISelection:
    market_view: str
    picks: list[AISelectionPick]


@dataclass(frozen=True)
class BatchAnalysisResult:
    results: dict[str, AIAnalysis]  # symbol -> AIAnalysis, skips symbols that failed


class AIAnalyzer:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.enabled = True
        self.disabled_reason = ""

    def is_enabled(self) -> bool:
        return self.enabled

    def disable(self, reason: str) -> None:
        if not self.enabled:
            return
        self.enabled = False
        self.disabled_reason = reason
        self.logger.error("AI analysis disabled: %s", reason)

    def is_auth_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "invalid api key" in message or ("401" in message and "api key" in message)

    def _extract_json(self, content: str) -> dict:
        text = content.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                self.logger.warning("AI 返回无法解析，降级为 HOLD: %s", text[:300])
                return {}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                self.logger.warning("AI JSON 解析失败，降级为 HOLD: %s", text[:300])
                return {}

    def analyze(
        self,
        symbol: str,
        bars: list[HistoricalBar],
        candle: CandleAnalysis,
        news: NewsAnalysis,
        base_signal: str,
        fast_sma: float,
        slow_sma: float,
    ) -> AIAnalysis:
        if not self.enabled:
            raise RuntimeError(self.disabled_reason or "AI analysis disabled")

        recent_bars = [
            {
                "date": bar.date,
                "open": round(bar.open, 4),
                "high": round(bar.high, 4),
                "low": round(bar.low, 4),
                "close": round(bar.close, 4),
                "volume": round(bar.volume, 2),
            }
            for bar in bars[-30:]
        ]

        prompt_payload = {
            "symbol": symbol,
            "base_signal": base_signal,
            "fast_sma": round(fast_sma, 4),
            "slow_sma": round(slow_sma, 4),
            "candle_analysis": {
                "trend": candle.trend,
                "bias": candle.bias,
                "score": candle.score,
                "patterns": candle.patterns,
                "last_close": round(candle.last_close, 4),
                "recent_high": round(candle.recent_high, 4),
                "recent_low": round(candle.recent_low, 4),
                "rsi": round(candle.rsi, 1),
                "volume_profile": candle.volume_profile,
                "volume_ratio": round(candle.volume_ratio, 2),
                "support": round(candle.support, 4),
                "resistance": round(candle.resistance, 4),
            },
            "news_analysis": {
                "score": news.score,
                "sentiment": news.sentiment,
                "headline_count": news.headline_count,
                "headlines": news.headlines[:8],
            },
            "recent_bars": recent_bars,
        }

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a stock trading analysis engine. "
                        "You must return strict JSON only. "
                        'Schema: {"action":"BUY|SELL|HOLD","confidence":0-100,'
                        '"sentiment":"BULLISH|BEARISH|NEUTRAL","summary":"...",'
                        '"risks":["..."],'
                        '"reasoning_steps":["step1: ...","step2: ...","step3: ..."]}. '
                        "Be conservative. If evidence is mixed, return HOLD. "
                        "reasoning_steps should contain 3-4 concise Chinese sentences "
                        "explaining the key factors behind the decision."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt_payload, ensure_ascii=True),
                },
            ],
            temperature=0.1,
            extra_body={"reasoning_split": True},
        )

        content = response.choices[0].message.content or ""
        parsed = self._extract_json(content)

        action = str(parsed.get("action", "HOLD")).upper()
        if action not in {"BUY", "SELL", "HOLD"}:
            action = "HOLD"

        confidence = int(parsed.get("confidence", 0))
        confidence = max(0, min(confidence, 100))

        sentiment = str(parsed.get("sentiment", "NEUTRAL")).upper()
        if sentiment not in {"BULLISH", "BEARISH", "NEUTRAL"}:
            sentiment = "NEUTRAL"

        summary = str(parsed.get("summary", "")).strip()
        raw_risks = parsed.get("risks", [])
        risks = [str(item).strip() for item in raw_risks if str(item).strip()] if isinstance(raw_risks, list) else []
        raw_steps = parsed.get("reasoning_steps", [])
        reasoning_steps = [str(s).strip() for s in raw_steps if str(s).strip()] if isinstance(raw_steps, list) else []

        return AIAnalysis(
            action=action,
            confidence=confidence,
            sentiment=sentiment,
            summary=summary,
            risks=risks,
            reasoning_steps=reasoning_steps,
        )

    def analyze_batch(self, candidates: list[dict]) -> BatchAnalysisResult:
        """Analyze multiple candidates in a SINGLE API call to minimize token usage.

        candidates: list of dicts with keys: symbol, bars, candle, news,
                    base_signal, fast_sma, slow_sma
        Returns BatchAnalysisResult with all AIAnalysis results.
        """
        if not self.enabled:
            raise RuntimeError(self.disabled_reason or "AI analysis disabled")

        batch_payload = []
        for cand in candidates:
            recent_bars = [
                {
                    "date": bar.date,
                    "open": round(bar.open, 4),
                    "high": round(bar.high, 4),
                    "low": round(bar.low, 4),
                    "close": round(bar.close, 4),
                    "volume": round(bar.volume, 2),
                }
                for bar in cand["bars"][-20:]
            ]
            candle = cand["candle"]
            news = cand["news"]
            batch_payload.append({
                "symbol": cand["symbol"],
                "base_signal": cand["base_signal"],
                "fast_sma": round(cand["fast_sma"], 4),
                "slow_sma": round(cand["slow_sma"], 4),
                "candle_analysis": {
                    "trend": candle.trend,
                    "bias": candle.bias,
                    "score": candle.score,
                    "patterns": candle.patterns,
                    "last_close": round(candle.last_close, 4),
                    "rsi": round(candle.rsi, 1),
                    "volume_ratio": round(candle.volume_ratio, 2),
                    "support": round(candle.support, 4),
                    "resistance": round(candle.resistance, 4),
                },
                "news_analysis": {
                    "score": news.score,
                    "sentiment": news.sentiment,
                    "headlines": news.headlines[:5],
                },
                "recent_bars_sample": [
                    {"date": b.date[-8:], "close": round(b.close, 2)}
                    for b in cand["bars"][-5:]
                ],
            })

        system_prompt = (
            "You are a stock trading analysis engine. Analyze ALL stocks in the user message "
            "and return a single JSON object. "
            'Schema: {"results":[{"symbol":"AAPL","action":"BUY|SELL|HOLD","confidence":0-100,'
            '"sentiment":"BULLISH|BEARISH|NEUTRAL","summary":"...","risks":["..."],'
            '"reasoning_steps":["step1: ...","step2: ...","step3: ..."]}]}. '
            "Analyze each symbol independently. Be conservative — if evidence is mixed, return HOLD. "
            "reasoning_steps should contain 3 concise Chinese sentences. "
            "IMPORTANT: Return valid JSON only, no markdown, no explanation."
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps({"candidates": batch_payload}, ensure_ascii=True)},
            ],
            temperature=0.1,
            extra_body={"reasoning_split": True},
        )

        content = response.choices[0].message.content or ""
        parsed = self._extract_json(content)

        results: dict[str, AIAnalysis] = {}
        raw_results = parsed.get("results", [])
        if isinstance(raw_results, list):
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).upper().strip()
                if not symbol:
                    continue
                action = str(item.get("action", "HOLD")).upper()
                if action not in {"BUY", "SELL", "HOLD"}:
                    action = "HOLD"
                confidence = max(0, min(int(item.get("confidence", 0)), 100))
                sentiment = str(item.get("sentiment", "NEUTRAL")).upper()
                if sentiment not in {"BULLISH", "BEARISH", "NEUTRAL"}:
                    sentiment = "NEUTRAL"
                raw_risks = item.get("risks", [])
                risks = [str(r).strip() for r in raw_risks if str(r).strip()] if isinstance(raw_risks, list) else []
                raw_steps = item.get("reasoning_steps", [])
                reasoning_steps = [str(s).strip() for s in raw_steps if str(s).strip()] if isinstance(raw_steps, list) else []
                results[symbol] = AIAnalysis(
                    action=action,
                    confidence=confidence,
                    sentiment=sentiment,
                    summary=str(item.get("summary", "")).strip(),
                    risks=risks,
                    reasoning_steps=reasoning_steps,
                )

        self.logger.info("Batch analysis: %d candidates → %d results", len(candidates), len(results))
        return BatchAnalysisResult(results=results)

    def explain_decision(
        self,
        symbol: str,
        candle: CandleAnalysis,
        news: NewsAnalysis,
        signal_action: str,
        fast_sma: float,
        slow_sma: float,
        position: int,
    ) -> list[dict]:
        """生成教育性推理步骤，供用户学习参考。返回结构化 dict 列表。"""
        if not self.enabled:
            return []

        rsi_signal, rsi_interp = RSIAnalyzer(14).interpret(candle.rsi)
        vol_interp = VolumeAnalyzer().interpret(candle.volume_profile, candle.volume_ratio)
        sr_interp = SupportResistance().interpret(candle.last_close, candle.support, candle.resistance)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a trading educator. Explain the AI trading decision for a student in "
                        "clear, concise Chinese. Break the reasoning into 4 named steps. "
                        'Schema: {"steps":[{"name":"步骤名","key_factors":"关键因素","verdict":"结论"}]}. '
                        "Keep each step explanation to one short paragraph. "
                        "Cover: 1)K线形态 2)技术指标 3)新闻情绪 4)综合判断。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "symbol": symbol,
                            "position": position,
                            "base_signal": signal_action,
                            "fast_sma": round(fast_sma, 4),
                            "slow_sma": round(slow_sma, 4),
                            "candle": {
                                "trend": candle.trend,
                                "bias": candle.bias,
                                "score": candle.score,
                                "patterns": candle.patterns[:5],
                                "rsi": round(candle.rsi, 1),
                                "rsi_signal": rsi_signal,
                                "volume_profile": candle.volume_profile,
                                "volume_ratio": round(candle.volume_ratio, 2),
                                "support": round(candle.support, 4),
                                "resistance": round(candle.resistance, 4),
                            },
                            "news": {
                                "sentiment": news.sentiment,
                                "score": news.score,
                                "headlines": news.headlines[:3],
                            },
                        },
                        ensure_ascii=True,
                    ),
                },
            ],
            temperature=0.2,
            extra_body={"reasoning_split": True},
        )

        content = response.choices[0].message.content or ""
        parsed = self._extract_json(content)
        raw_steps = parsed.get("steps", [])
        steps = []
        for idx, item in enumerate(raw_steps, start=1):
            if isinstance(item, dict):
                steps.append(
                    {
                        "step": idx,
                        "name": str(item.get("name", f"步骤{idx}")),
                        "key_factors": str(item.get("key_factors", "")),
                        "verdict": str(item.get("verdict", "")),
                    }
                )
        return steps

    def summarize_training(self, results: list[SimulationResult]) -> str:
        if not self.enabled:
            raise RuntimeError(self.disabled_reason or "AI analysis disabled")
        payload = [
            {
                "symbol": item.symbol,
                "net_profit": round(item.net_profit, 2),
                "final_equity": round(item.final_equity, 2),
                "max_drawdown_pct": round(item.max_drawdown * 100, 2),
                "win_rate": round(item.win_rate, 2),
                "trades": item.trades,
                "fast_sma": item.config.fast_sma,
                "slow_sma": item.config.slow_sma,
                "stop_loss_pct": item.config.stop_loss_pct,
                "take_profit_pct": item.config.take_profit_pct,
            }
            for item in results[:10]
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "Summarize backtest optimization results for a trader in concise Chinese. Focus on robust parameter choices and risk tradeoffs.",
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
            ],
            temperature=0.2,
            extra_body={"reasoning_split": True},
        )
        return (response.choices[0].message.content or "").strip()

    def select_symbols(self, candidates: list[dict], max_symbols: int) -> AISelection:
        if not self.enabled:
            raise RuntimeError(self.disabled_reason or "AI analysis disabled")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an AI stock selector. "
                        "Return strict JSON only. "
                        'Schema: {"market_view":"...",'
                        '"picks":[{"symbol":"...","score":0-100,"action":"BUY|HOLD|SELL","confidence":0-100,"reason":"..."}]}. '
                        "Only use symbols from the provided candidates. "
                        f"Return at most {max_symbols} picks. "
                        "Be conservative. If evidence is weak or mixed, prefer HOLD."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "max_symbols": max_symbols,
                            "candidates": candidates,
                        },
                        ensure_ascii=True,
                    ),
                },
            ],
            temperature=0.1,
            extra_body={"reasoning_split": True},
        )

        content = response.choices[0].message.content or ""
        parsed = self._extract_json(content)
        raw_picks = parsed.get("picks", [])
        picks: list[AISelectionPick] = []

        if isinstance(raw_picks, list):
            for item in raw_picks[:max_symbols]:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).upper().strip()
                if not symbol:
                    continue
                action = str(item.get("action", "HOLD")).upper()
                if action not in {"BUY", "SELL", "HOLD"}:
                    action = "HOLD"
                confidence = max(0, min(int(item.get("confidence", 0)), 100))
                score = max(0, min(int(item.get("score", confidence)), 100))
                reason = str(item.get("reason", "")).strip()
                picks.append(
                    AISelectionPick(
                        symbol=symbol,
                        score=score,
                        action=action,
                        confidence=confidence,
                        reason=reason,
                    )
                )

        return AISelection(
            market_view=str(parsed.get("market_view", "")).strip(),
            picks=picks,
        )
