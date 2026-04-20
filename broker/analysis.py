from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean

from broker.ib_client import HistoricalBar


@dataclass(frozen=True)
class CandleAnalysis:
    trend: str
    bias: str
    score: int
    patterns: list[str]
    last_close: float
    recent_high: float
    recent_low: float
    rsi: float
    volume_profile: str
    volume_ratio: float
    support: float
    resistance: float


class RSIAnalyzer:
    """Relative Strength Index — 衡量价格变动速度与幅度，取值 0~100."""

    def __init__(self, period: int = 14) -> None:
        self.period = period

    def compute(self, closes: list[float]) -> float:
        if len(closes) < self.period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0.0 for d in deltas[-self.period:]]
        losses = [-d if d < 0 else 0.0 for d in deltas[-self.period:]]
        avg_gain = fmean(gains) if gains else 0.0
        avg_loss = fmean(losses) if losses else 0.0
        if avg_gain == 0 and avg_loss == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def interpret(self, rsi: float) -> tuple[str, str]:
        """返回 (信号描述, 详细解释)."""
        if rsi >= 70:
            return "超买", "RSI 超过 70，表示近期上涨动能极强，可能存在回调风险。"
        if rsi <= 30:
            return "超卖", "RSI 低于 30，表示近期下跌动能极强，可能存在反弹机会。"
        if rsi >= 55:
            return "偏多", f"RSI={rsi:.1f}，处于多头区域但尚未超买。"
        if rsi <= 45:
            return "偏空", f"RSI={rsi:.1f}，处于空头区域但尚未超卖。"
        return "中性", f"RSI={rsi:.1f}，多空力量相对均衡。"


class VolumeAnalyzer:
    """成交量分析 — 放量和缩量揭示趋势强度."""

    def compute_ratio(self, volumes: list[float], lookback: int = 20) -> float:
        if len(volumes) < lookback:
            return 1.0
        recent = volumes[-lookback:]
        baseline = fmean(volumes[: -lookback]) if len(volumes) > lookback else fmean(recent)
        if baseline == 0:
            return 1.0
        return fmean(recent) / baseline

    def profile(self, volumes: list[float], lookback: int = 20) -> str:
        ratio = self.compute_ratio(volumes, lookback)
        if ratio >= 1.8:
            return "expanding"
        if ratio <= 0.6:
            return "contracting"
        return "normal"

    def interpret(self, profile: str, ratio: float) -> str:
        if profile == "expanding":
            return f"成交量是近20日均量的 {ratio:.1f} 倍，明显放量，趋势信号可信度较高。"
        if profile == "contracting":
            return f"成交量是近20日均量的 {ratio:.1f} 倍，缩量整理，可能酝酿变盘。"
        return f"成交量接近常态（均量的 {ratio:.1f} 倍），无异常信号。"


class SupportResistance:
    """支撑位与阻力位检测 — 识别近期局部高低价."""

    def detect(self, bars: list[HistoricalBar], lookback: int = 20) -> tuple[float, float]:
        if len(bars) < lookback:
            lookback = len(bars)
        recent = bars[-lookback:]
        resistance = max(bar.high for bar in recent)
        support = min(bar.low for bar in recent)
        return support, resistance

    def interpret(self, last_close: float, support: float, resistance: float) -> str:
        range_pct = (resistance - support) / support * 100 if support > 0 else 0
        if last_close >= resistance * 0.98:
            return f"价格已突破阻力位 ${resistance:.2f}，可能继续上行。"
        if last_close <= support * 1.02:
            return f"价格已逼近支撑位 ${support:.2f}，注意观察是否企稳。"
        mid = (support + resistance) / 2
        if last_close > mid:
            return f"价格在支撑 ${support:.2f} ~ 阻力 ${resistance:.2f} 的上半区，多头占优。"
        return f"价格在支撑 ${support:.2f} ~ 阻力 ${resistance:.2f} 的下半区，空头占优。"


class KlineAnalyzer:
    def __init__(self) -> None:
        self.rsi_analyzer = RSIAnalyzer(period=14)
        self.volume_analyzer = VolumeAnalyzer()
        self.sr_analyzer = SupportResistance()

    def analyze(self, bars: list[HistoricalBar]) -> CandleAnalysis:
        if len(bars) < 20:
            raise ValueError("K 线分析至少需要 20 根数据")

        closes = [bar.close for bar in bars]
        volumes = [bar.volume for bar in bars]
        recent = bars[-20:]
        last_bar = recent[-1]
        prev_bar = recent[-2]
        score = 0
        patterns: list[str] = []

        recent_high = max(bar.high for bar in recent)
        recent_low = min(bar.low for bar in recent)

        first_close = recent[0].close
        last_close = last_bar.close
        if last_close > first_close * 1.02:
            trend = "UP"
            score += 1
        elif last_close < first_close * 0.98:
            trend = "DOWN"
            score -= 1
        else:
            trend = "SIDEWAYS"

        last_body = abs(last_bar.close - last_bar.open)
        last_range = max(last_bar.high - last_bar.low, 1e-9)
        upper_shadow = last_bar.high - max(last_bar.open, last_bar.close)
        lower_shadow = min(last_bar.open, last_bar.close) - last_bar.low

        if lower_shadow > last_body * 2 and upper_shadow < last_body:
            patterns.append("HAMMER")
            score += 1
        if upper_shadow > last_body * 2 and lower_shadow < last_body:
            patterns.append("SHOOTING_STAR")
            score -= 1

        prev_body_low = min(prev_bar.open, prev_bar.close)
        prev_body_high = max(prev_bar.open, prev_bar.close)
        last_body_low = min(last_bar.open, last_bar.close)
        last_body_high = max(last_bar.open, last_bar.close)

        if (
            prev_bar.close < prev_bar.open
            and last_bar.close > last_bar.open
            and last_body_low <= prev_body_low
            and last_body_high >= prev_body_high
        ):
            patterns.append("BULLISH_ENGULFING")
            score += 2

        if (
            prev_bar.close > prev_bar.open
            and last_bar.close < last_bar.open
            and last_body_low <= prev_body_low
            and last_body_high >= prev_body_high
        ):
            patterns.append("BEARISH_ENGULFING")
            score -= 2

        prior_high = max(bar.high for bar in recent[:-1])
        prior_low = min(bar.low for bar in recent[:-1])
        if last_bar.close > prior_high:
            patterns.append("BREAKOUT")
            score += 1
        if last_bar.close < prior_low:
            patterns.append("BREAKDOWN")
            score -= 1

        if score >= 2:
            bias = "BULLISH"
        elif score <= -2:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        rsi = self.rsi_analyzer.compute(closes)
        volume_ratio = self.volume_analyzer.compute_ratio(volumes)
        volume_profile = self.volume_analyzer.profile(volumes)
        support, resistance = self.sr_analyzer.detect(bars)

        return CandleAnalysis(
            trend=trend,
            bias=bias,
            score=score,
            patterns=patterns,
            last_close=last_close,
            recent_high=recent_high,
            recent_low=recent_low,
            rsi=rsi,
            volume_profile=volume_profile,
            volume_ratio=volume_ratio,
            support=support,
            resistance=resistance,
        )
