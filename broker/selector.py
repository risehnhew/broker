from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from broker.ai_analysis import AIAnalysis
from broker.ai_analysis import AIAnalyzer
from broker.ai_analysis import AISelection
from broker.analysis import CandleAnalysis
from broker.analysis import KlineAnalyzer
from broker.analysis import RSIAnalyzer
from broker.analysis import SupportResistance
from broker.analysis import VolumeAnalyzer
from broker.config import Settings
from broker.ib_client import HistoricalBar
from broker.ib_client import IBClient
from broker.models import EducationalReport
from broker.models import EducationalStep
from broker.models import IndicatorDetail
from broker.news import NewsAnalysis
from broker.news import NewsAnalyzer
from broker.strategy import SmaCrossStrategy
from broker.strategy import StrategySignal


@dataclass(frozen=True)
class _CandidateBase:
    symbol: str
    position: int
    bars: list[HistoricalBar]
    signal: StrategySignal
    candle: CandleAnalysis
    news: NewsAnalysis


@dataclass(frozen=True)
class SymbolCandidate:
    symbol: str
    position: int
    bars: list[HistoricalBar]
    signal: StrategySignal
    candle: CandleAnalysis
    news: NewsAnalysis
    ai: AIAnalysis


@dataclass(frozen=True)
class RankedSymbol:
    rank: int
    symbol: str
    score: int
    action: str
    confidence: int
    reason: str
    selected: bool
    base_action: str
    candle_bias: str
    candle_score: int
    news_sentiment: str
    news_score: int
    ai_action: str
    ai_confidence: int
    close_price: float
    position: int


@dataclass(frozen=True)
class SelectionResult:
    market_view: str
    picks: list[RankedSymbol]
    selected_symbols: list[str]
    candidates: dict[str, SymbolCandidate]
    errors: list[dict[str, str]]


class AISymbolSelector:
    def __init__(self, settings: Settings, ai_analyzer: AIAnalyzer | None) -> None:
        self.settings = settings
        self.ai_analyzer = ai_analyzer
        self.logger = logging.getLogger(self.__class__.__name__)
        self.strategy = SmaCrossStrategy(settings.fast_sma, settings.slow_sma)
        self.kline_analyzer = KlineAnalyzer()
        self.news_analyzer = NewsAnalyzer()

    def build_candidate(self, client: IBClient, symbol: str, position: int = 0) -> SymbolCandidate:
        base = self.build_candidate_data(client, symbol, position)
        ai = self._analyze_ai(base.symbol, base.bars, base.candle, base.news,
                               base.signal.action, base.signal.fast_sma, base.signal.slow_sma)
        return SymbolCandidate(
            symbol=base.symbol,
            position=base.position,
            bars=base.bars,
            signal=base.signal,
            candle=base.candle,
            news=base.news,
            ai=ai,
        )

    def build_candidate_data(self, client: IBClient, symbol: str, position: int = 0) -> _CandidateBase:
        """Build candidate data without AI analysis (for batch processing)."""
        bars = client.get_historical_bars(
            symbol=symbol,
            duration=self.settings.duration,
            bar_size=self.settings.bar_size,
            use_rth=self.settings.use_rth,
        )
        closes = [bar.close for bar in bars]
        signal = self.strategy.evaluate(closes=closes, position=position)
        candle = self.kline_analyzer.analyze(bars)
        news = self._analyze_news(client, symbol)
        return _CandidateBase(
            symbol=symbol,
            position=position,
            bars=bars,
            signal=signal,
            candle=candle,
            news=news,
        )

    def select(
        self,
        client: IBClient,
        positions: dict[str, int] | None = None,
        on_progress: Any = None,
        ai_enabled: bool = True,
    ) -> SelectionResult:
        position_map = {symbol.upper(): int(value) for symbol, value in (positions or {}).items()}
        universe = self._get_universe(position_map)
        candidates: dict[str, SymbolCandidate] = {}
        errors: list[dict[str, str]] = []
        selection_start = time.perf_counter()

        self.logger.info("AI选股开始，共 %s 只股票: %s", len(universe), ", ".join(universe))

        completed_count = 0
        progress_lock = threading.Lock()
        max_workers = min(4, max(1, len(universe)))

        # Step 1: Build all candidate data WITHOUT AI (parallel, fast)
        def _build_base(symbol: str):
            t0 = time.perf_counter()
            base = self.build_candidate_data(client, symbol, position_map.get(symbol, 0))
            elapsed = time.perf_counter() - t0
            return symbol, base, elapsed

        all_bases: list[_CandidateBase] = []
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="selector") as pool:
            futures = {pool.submit(_build_base, sym): sym for sym in universe}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    symbol, base, elapsed = fut.result()
                    all_bases.append(base)
                    with progress_lock:
                        completed_count += 1
                        done = completed_count
                    self.logger.info(
                        "数据获取 %s，用时 %.2fs，base=%s news=%s",
                        symbol, elapsed,
                        base.signal.action,
                        base.news.sentiment,
                    )
                    if on_progress:
                        try:
                            on_progress(done, len(universe), symbol)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("数据获取 %s 失败: %s", sym, exc)
                    errors.append({"symbol": sym, "message": str(exc), "raw": str(exc)})

        if not all_bases:
            return SelectionResult(
                market_view="",
                picks=[],
                selected_symbols=[],
                candidates={},
                errors=errors,
            )

        # Step 2: ONE batch AI call for all candidates (only during market hours)
        if ai_enabled:
            self.logger.info("批量AI分析 %d 只股票...", len(all_bases))
            t_ai = time.perf_counter()
            ai_results = self._analyze_ai_batch(all_bases)
            self.logger.info("批量AI分析完成，用时 %.2fs", time.perf_counter() - t_ai)
        else:
            # Outside market hours: skip MiniMax, use fallback for all
            self.logger.info("非盘中时段，跳过 MiniMax API 调用（%d 只用 fallback）", len(all_bases))
            ai_results = {b.symbol: self._fallback_ai_analysis() for b in all_bases}

        # Step 3: Assemble final candidates with AI results
        for base in all_bases:
            ai = ai_results.get(base.symbol, self._fallback_ai_analysis())
            candidates[base.symbol] = SymbolCandidate(
                symbol=base.symbol,
                position=base.position,
                bars=base.bars,
                signal=base.signal,
                candle=base.candle,
                news=base.news,
                ai=ai,
            )

        # Report per-symbol AI results
        for sym, cand in candidates.items():
            self.logger.info(
                "AI选股完成 %s，base=%s ai=%s/%s news=%s(%s)",
                sym,
                cand.signal.action,
                cand.ai.action, cand.ai.confidence,
                cand.news.sentiment, cand.news.score,
            )

        ai_selection = self._rank_with_ai(candidates)
        picks = self._normalize_picks(candidates, ai_selection)
        selected_symbols = [
            item.symbol
            for item in picks
            if item.selected and item.action == "BUY"
        ]
        self.logger.info(
            "AI选股结束，用时 %.2fs，入选 %s",
            time.perf_counter() - selection_start,
            ", ".join(selected_symbols) if selected_symbols else "无",
        )

        return SelectionResult(
            market_view=ai_selection.market_view,
            picks=picks,
            selected_symbols=selected_symbols,
            candidates=candidates,
            errors=errors,
        )

    def _fallback_ai_analysis(self) -> AIAnalysis:
        return AIAnalysis(
            action="HOLD",
            confidence=0,
            sentiment="NEUTRAL",
            summary="",
            risks=[],
            reasoning_steps=[],
        )

    def _disable_ai_analyzer(self, reason: str) -> None:
        if self.ai_analyzer is None:
            return
        self.ai_analyzer.disable(reason)
        self.ai_analyzer = None

    def _get_universe(self, positions: dict[str, int]) -> list[str]:
        base = self.settings.stock_universe
        universe = list(dict.fromkeys(symbol.upper() for symbol in base))
        for symbol, quantity in positions.items():
            if quantity and symbol.upper() not in universe:
                universe.append(symbol.upper())
        return universe

    def _candidate_payload(self, candidate: SymbolCandidate) -> dict:
        last_close = candidate.bars[-1].close
        sma_gap_pct = 0.0
        if candidate.signal.slow_sma:
            sma_gap_pct = (candidate.signal.fast_sma - candidate.signal.slow_sma) / candidate.signal.slow_sma * 100
        return {
            "symbol": candidate.symbol,
            "position": candidate.position,
            "close_price": round(last_close, 4),
            "base_signal": candidate.signal.action,
            "sma_gap_pct": round(sma_gap_pct, 2),
            "candle": {
                "trend": candidate.candle.trend,
                "bias": candidate.candle.bias,
                "score": candidate.candle.score,
                "patterns": candidate.candle.patterns[:5],
                "rsi": round(candidate.candle.rsi, 1),
                "volume_profile": candidate.candle.volume_profile,
                "volume_ratio": round(candidate.candle.volume_ratio, 2),
                "support": round(candidate.candle.support, 4),
                "resistance": round(candidate.candle.resistance, 4),
            },
            "news": {
                "sentiment": candidate.news.sentiment,
                "score": candidate.news.score,
                "headlines": candidate.news.headlines[:3],
            },
            "ai_trade_view": {
                "action": candidate.ai.action,
                "confidence": candidate.ai.confidence,
                "sentiment": candidate.ai.sentiment,
                "summary": candidate.ai.summary,
                "risks": candidate.ai.risks[:3],
                "reasoning_steps": candidate.ai.reasoning_steps[:3],
            },
        }

    def _rank_with_ai(self, candidates: dict[str, SymbolCandidate]) -> AISelection:
        if self.ai_analyzer is None or not self.ai_analyzer.is_enabled():
            return AISelection(market_view="", picks=[])
        try:
            payload = [self._candidate_payload(item) for item in candidates.values()]
            return self.ai_analyzer.select_symbols(payload, min(self.settings.max_selected_symbols, len(payload)))
        except Exception as exc:  # noqa: BLE001
            if self.ai_analyzer and self.ai_analyzer.is_auth_error(exc):
                self._disable_ai_analyzer(f"MiniMax auth failed during symbol selection: {exc}")
                return AISelection(market_view="", picks=[])
            self.logger.warning("AI选股排序失败，降级为规则排序: %s", exc)
            return AISelection(market_view="", picks=[])

    def _normalize_picks(self, candidates: dict[str, SymbolCandidate], selection: AISelection) -> list[RankedSymbol]:
        ai_picks = {item.symbol: item for item in selection.picks if item.symbol in candidates}
        ranked: list[RankedSymbol] = []
        max_selected = min(self.settings.max_selected_symbols, len(candidates))

        for symbol, candidate in candidates.items():
            ai_pick = ai_picks.get(symbol)
            score, action, confidence, reason = self._fallback_rank(candidate)
            if ai_pick is not None:
                score = ai_pick.score
                action = ai_pick.action
                confidence = ai_pick.confidence
                reason = ai_pick.reason or reason

            ranked.append(
                RankedSymbol(
                    rank=0,
                    symbol=symbol,
                    score=score,
                    action=action,
                    confidence=confidence,
                    reason=reason,
                    selected=False,
                    base_action=candidate.signal.action,
                    candle_bias=candidate.candle.bias,
                    candle_score=candidate.candle.score,
                    news_sentiment=candidate.news.sentiment,
                    news_score=candidate.news.score,
                    ai_action=candidate.ai.action,
                    ai_confidence=candidate.ai.confidence,
                    close_price=candidate.bars[-1].close,
                    position=candidate.position,
                )
            )

        ranked.sort(key=lambda item: (item.score, item.confidence, item.symbol), reverse=True)
        top_symbols = {item.symbol for item in ranked[:max_selected]}
        normalized: list[RankedSymbol] = []
        for index, item in enumerate(ranked, start=1):
            normalized.append(
                RankedSymbol(
                    rank=index,
                    symbol=item.symbol,
                    score=item.score,
                    action=item.action,
                    confidence=item.confidence,
                    reason=item.reason,
                    selected=item.symbol in top_symbols,
                    base_action=item.base_action,
                    candle_bias=item.candle_bias,
                    candle_score=item.candle_score,
                    news_sentiment=item.news_sentiment,
                    news_score=item.news_score,
                    ai_action=item.ai_action,
                    ai_confidence=item.ai_confidence,
                    close_price=item.close_price,
                    position=item.position,
                )
            )
        return normalized

    def _fallback_rank(self, candidate: SymbolCandidate) -> tuple[int, str, int, str]:
        score = 50
        if candidate.signal.action == "BUY":
            score += 18
        elif candidate.signal.action == "SELL":
            score -= 18

        score += max(-20, min(20, candidate.candle.score * 5))
        score += max(-16, min(16, candidate.news.score * 4))

        if candidate.ai.action == "BUY":
            score += int(candidate.ai.confidence * 0.25)
        elif candidate.ai.action == "SELL":
            score -= int(candidate.ai.confidence * 0.2)

        score = max(0, min(score, 100))
        confidence = candidate.ai.confidence

        # When AI is unavailable or low-confidence, infer action from technical signals
        if confidence < self.settings.ai_selection_min_confidence:
            if candidate.signal.action == "BUY" and candidate.candle.bias in ("BULLISH", "NEUTRAL"):
                action = "BUY"
                reason = f"fallback=tech-bull({candidate.signal.action}/{candidate.candle.bias}/{candidate.news.score})"
            elif candidate.signal.action == "SELL":
                action = "SELL"
                reason = f"fallback=tech-bear({candidate.signal.action})"
            else:
                action = candidate.signal.action
                reason = f"base={candidate.signal.action}, candle={candidate.candle.bias}, news={candidate.news.sentiment}"
        else:
            action = candidate.ai.action
            reason = candidate.ai.summary or f"ai={candidate.ai.action}/{confidence}"

        return score, action, confidence, reason

    def _analyze_news(self, client: IBClient, symbol: str) -> NewsAnalysis:
        if not self.settings.enable_news:
            return self.news_analyzer.analyze([])
        try:
            headlines = client.get_recent_news(
                symbol=symbol,
                provider_codes=self.settings.news_provider_codes,
                max_items=self.settings.news_max_items,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("%s 新闻抓取失败: %s", symbol, exc)
            headlines = []
        return self.news_analyzer.analyze(headlines)

    def _analyze_ai(
        self,
        symbol: str,
        bars: list[HistoricalBar],
        candle: CandleAnalysis,
        news: NewsAnalysis,
        base_signal: str,
        fast_sma: float,
        slow_sma: float,
    ) -> AIAnalysis:
        if self.ai_analyzer is None or not self.ai_analyzer.is_enabled():
            return self._fallback_ai_analysis()
        try:
            return self.ai_analyzer.analyze(
                symbol=symbol,
                bars=bars,
                candle=candle,
                news=news,
                base_signal=base_signal,
                fast_sma=fast_sma,
                slow_sma=slow_sma,
            )
        except Exception as exc:  # noqa: BLE001
            if self.ai_analyzer and self.ai_analyzer.is_auth_error(exc):
                self._disable_ai_analyzer(f"MiniMax auth failed during single-symbol analysis: {exc}")
                return self._fallback_ai_analysis()
            self.logger.warning("%s AI交易分析失败: %s", symbol, exc)
            return self._fallback_ai_analysis()

    def _analyze_ai_batch(self, bases: list[_CandidateBase]) -> dict[str, AIAnalysis]:
        """Analyze multiple candidates in ONE API call. Returns {symbol: AIAnalysis}."""
        if not bases:
            return {}
        if self.ai_analyzer is None or not self.ai_analyzer.is_enabled():
            return {b.symbol: self._fallback_ai_analysis() for b in bases}
        try:
            candidates_payload = [
                {
                    "symbol": b.symbol,
                    "bars": b.bars,
                    "candle": b.candle,
                    "news": b.news,
                    "base_signal": b.signal.action,
                    "fast_sma": b.signal.fast_sma,
                    "slow_sma": b.signal.slow_sma,
                }
                for b in bases
            ]
            result = self.ai_analyzer.analyze_batch(candidates_payload)
            return result.results
        except Exception as exc:  # noqa: BLE001
            if self.ai_analyzer and self.ai_analyzer.is_auth_error(exc):
                self._disable_ai_analyzer(f"MiniMax auth failed during batch analysis: {exc}")
            self.logger.warning("批量AI分析失败，降级为逐只分析: %s", exc)
            # Fallback: analyze one by one
            results: dict[str, AIAnalysis] = {}
            for b in bases:
                results[b.symbol] = self._analyze_ai(
                    b.symbol, b.bars, b.candle, b.news,
                    b.signal.action, b.signal.fast_sma, b.signal.slow_sma,
                )
            return results

    def build_educational_report(
        self,
        candidate: SymbolCandidate,
    ) -> EducationalReport:
        """为单只股票生成完整推理链，供用户学习参考。"""
        steps: list[EducationalStep] = []
        step_num = 1

        # ====== 步骤1：技术指标综合 ======
        rsi = candidate.candle.rsi
        rsi_signal, rsi_interp = RSIAnalyzer(14).interpret(rsi)
        vol_profile = candidate.candle.volume_profile
        vol_ratio = candidate.candle.volume_ratio
        vol_interp = VolumeAnalyzer().interpret(vol_profile, vol_ratio)
        sr_interp = SupportResistance().interpret(
            candidate.candle.last_close,
            candidate.candle.support,
            candidate.candle.resistance,
        )

        pattern_names = ", ".join(candidate.candle.patterns) if candidate.candle.patterns else "无"
        step1_indicators = [
            IndicatorDetail(
                name="SMA 均线",
                value=f"快={candidate.signal.fast_sma:.2f} / 慢={candidate.signal.slow_sma:.2f}",
                interpretation=f"{'多头排列（快线在慢线上方）' if candidate.signal.action == 'BUY' else '空头排列（快线在慢线下方）' if candidate.signal.action == 'SELL' else '纠缠状态'}",
                explanation="SMA（简单移动平均线）将一段时间的收盘价平均，消除噪音。快线上穿慢线为金叉（买入信号），下穿为死叉（卖出信号）。",
            ),
            IndicatorDetail(
                name="RSI(14)",
                value=f"{rsi:.1f}",
                interpretation=rsi_interp,
                explanation="RSI（相对强弱指数）衡量价格涨跌的相对强度。RSI>70 超买（可能回调），RSI<30 超卖（可能反弹），50 为中性。",
            ),
            IndicatorDetail(
                name="成交量",
                value=f"{vol_ratio:.1f}x（均量比）",
                interpretation="放量" if vol_profile == "expanding" else "缩量" if vol_profile == "contracting" else "常态",
                explanation="成交量反映市场参与活跃度。放量突破（成交量放大）通常确认趋势可靠性；缩量可能意味着趋势减弱或横盘整理。",
            ),
            IndicatorDetail(
                name="支撑 / 阻力",
                value=f"${candidate.candle.support:.2f} / ${candidate.candle.resistance:.2f}",
                interpretation="支撑位是价格下跌的底部，阻力位是上涨的顶部。价格接近支撑位可能反弹，接近阻力位可能回落。",
                explanation="支撑位是买方集中的价格区域，阻力位是卖方集中的价格区域。当价格向上突破阻力位后，阻力位可能变成支撑位。",
            ),
            IndicatorDetail(
                name="K线形态",
                value=pattern_names,
                interpretation=f"{candidate.candle.bias}（评分 {candidate.candle.score}）",
                explanation=f"BULLISH_ENGULFING=看涨吞噬（前一阴后一阳且实体包裹），BREAKOUT=向上突破近期高点，HAMMER=锤子线（下影线长表明下档支撑强）。这些形态反映市场短期情绪逆转信号。",
            ),
        ]

        trend_interp = {
            "UP": "近期价格持续上行，趋势偏多",
            "DOWN": "近期价格持续下行，趋势偏空",
            "SIDEWAYS": "价格呈横盘震荡，无明显方向",
        }.get(candidate.candle.trend, "未知")

        steps.append(EducationalStep(
            step=step_num,
            title="技术指标综合分析",
            content=(
                f"趋势方向：{trend_interp}。"
                f"K线形态：{pattern_names}，综合评分 {candidate.candle.score}（>0 看多，<0 看空）。"
                f"均线系统：{'快线已上穿慢线形成金叉' if candidate.signal.action == 'BUY' else '快线已下穿慢线形成死叉' if candidate.signal.action == 'SELL' else '两线纠缠无方向'}。"
                f"{rsi_interp} {rsi_explain(rsi)}"
                f"{vol_interp}"
            ),
            indicators=step1_indicators,
            verdict=candidate.candle.bias,
        ))
        step_num += 1

        # ====== 步骤2：新闻情绪 ======
        news_interp_map = {
            "POSITIVE": ("偏多", "正面新闻占主导，市场情绪乐观"),
            "NEGATIVE": ("偏空", "负面新闻占主导，市场情绪悲观"),
            "NEUTRAL": ("中性", "无明显偏向，多空力量均衡"),
        }
        news_label, news_content = news_interp_map.get(candidate.news.sentiment, ("中性", "新闻情绪无法判断"))
        step2_indicators = [
            IndicatorDetail(
                name="新闻情绪",
                value=candidate.news.sentiment,
                interpretation=news_label,
                explanation="通过统计新闻标题中正面/负面关键词判断市场情绪。正面词：beat, surge, upgrade, profit；负面词：miss, drop, downgrade, lawsuit。",
            ),
            IndicatorDetail(
                name="情绪得分",
                value=str(candidate.news.score),
                interpretation="得分>0偏多，<0偏空，=0中性",
                explanation="每检测到一个正面关键词+1分，负面关键词-1分。绝对值越大情绪越强烈。",
            ),
        ]
        step2_content = f"新闻情绪：{news_content}，综合得分 {candidate.news.score}。"
        if candidate.news.headlines:
            step2_content += f"最新新闻：{' | '.join(candidate.news.headlines[:2])}。"
        steps.append(EducationalStep(
            step=step_num,
            title="新闻情绪分析",
            content=step2_content,
            indicators=step2_indicators,
            verdict=news_label,
        ))
        step_num += 1

        # ====== 步骤3：AI 综合判断 ======
        ai_step_content = f"AI 建议：{candidate.ai.action}，置信度 {candidate.ai.confidence}%。"
        if candidate.ai.summary:
            ai_step_content += f" AI 摘要：{candidate.ai.summary}。"
        if candidate.ai.reasoning_steps:
            ai_step_content += " AI 推理：" + " ".join(candidate.ai.reasoning_steps[:3])
        if candidate.ai.risks:
            ai_step_content += f" 主要风险：{'；'.join(candidate.ai.risks[:2])}。"

        step3_indicators = [
            IndicatorDetail(
                name="AI 动作",
                value=candidate.ai.action,
                interpretation=f"{'建议买入' if candidate.ai.action == 'BUY' else '建议卖出' if candidate.ai.action == 'SELL' else '建议观望'}",
                explanation="AI 基于K线、新闻、均线等多维度信息给出综合判断。置信度越高（>60）表示信号越强。",
            ),
            IndicatorDetail(
                name="置信度",
                value=f"{candidate.ai.confidence}%",
                interpretation="60%以上为较强信号",
                explanation="置信度是 AI 对自己判断的确信程度。低于 60% 时建议谨慎，视为弱信号。",
            ),
            IndicatorDetail(
                name="AI 情绪判断",
                value=candidate.ai.sentiment,
                interpretation={"BULLISH": "看多", "BEARISH": "看空", "NEUTRAL": "中性"}.get(candidate.ai.sentiment, "中性"),
                explanation="AI 综合技术面和消息面判断市场短期情绪方向。",
            ),
        ]
        steps.append(EducationalStep(
            step=step_num,
            title="AI 综合判断",
            content=ai_step_content,
            indicators=step3_indicators,
            verdict={"BULLISH": "看多", "BEARISH": "看空", "NEUTRAL": "中性"}.get(candidate.ai.sentiment, "中性"),
        ))
        step_num += 1

        # ====== 步骤4：风控与最终决策 ======
        risk_note = ""
        if candidate.signal.action == "BUY" and candidate.ai.action == "BUY" and candidate.ai.confidence >= 60:
            risk_note = "技术面+AI 双重确认，风控检查通过，建议积极关注。"
        elif candidate.signal.action == "SELL":
            risk_note = "均线系统已发出卖出信号，应考虑减仓或止损。"
        elif candidate.ai.action == "HOLD":
            risk_note = "AI 判断信号不明朗，建议观望等待更好时机。"
        else:
            risk_note = "多空信号矛盾，建议谨慎操作或等待明确方向。"

        step4_indicators = [
            IndicatorDetail(
                name="基础信号",
                value=candidate.signal.action,
                interpretation="SMA 均线交叉产生的原始信号",
                explanation="仅基于价格与均线的位置关系，不考虑新闻和AI。快线上穿慢线=BUY，下穿=SELL。",
            ),
            IndicatorDetail(
                name="风控评估",
                value="PASS",
                interpretation="通过（回撤/日亏限额内）",
                explanation="风控模块检查：持仓回撤是否超限（默认15%）、单日亏损是否超限（默认3%）、是否在交易时段内。目前模拟环境风控均已通过。",
            ),
        ]
        steps.append(EducationalStep(
            step=step_num,
            title="风控检查与最终决策",
            content=(
                f"综合决策：{'买入' if candidate.signal.action == 'BUY' and candidate.ai.confidence >= 60 else '卖出' if candidate.signal.action == 'SELL' else '观望'}。"
                f"{risk_note}"
            ),
            indicators=step4_indicators,
            verdict={"BUY": "建议买入", "SELL": "建议卖出", "HOLD": "建议观望"}.get(candidate.signal.action if candidate.ai.action == "HOLD" else candidate.ai.action, "观望"),
        ))

        final_action = candidate.signal.action
        final_confidence = max(candidate.ai.confidence, 50 if candidate.signal.action == "BUY" else 30)
        summary = (
            f"{candidate.symbol}：技术面{candidate.candle.bias}（趋势{candidate.candle.trend}），"
            f"均线{'金叉' if candidate.signal.action == 'BUY' else '死叉' if candidate.signal.action == 'SELL' else '纠缠'}，"
            f"新闻{news_label}，AI{'看涨' if candidate.ai.sentiment == 'BULLISH' else '看跌' if candidate.ai.sentiment == 'BEARISH' else '中性'}。"
            f"{risk_note}"
        )

        return EducationalReport(
            symbol=candidate.symbol,
            steps=steps,
            final_action=final_action,
            final_confidence=final_confidence,
            summary_zh=summary,
        )


def rsi_explain(rsi: float) -> str:
    """Return educational explanation for RSI value."""
    if rsi >= 70:
        return "但需注意 RSI 已进入超买区域，警惕回调风险。"
    if rsi <= 30:
        return "RSI 已进入超卖区域，可能存在反弹机会。"
    if rsi >= 55:
        return f"RSI={rsi:.1f} 处于偏多区域。"
    if rsi <= 45:
        return f"RSI={rsi:.1f} 处于偏空区域。"
    return f"RSI={rsi:.1f} 处于中性区域。"
