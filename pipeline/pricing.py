"""요금 계산 — DeepSeek 피크/오프피크 판정과 호출 단가 산출."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import (
    BATCH_DISCOUNT,
    CLAUDE_RATES,
    DEEPSEEK_RATES,
    PEAK_MULTIPLIER,
    PEAK_WINDOWS_UTC,
)

KST = timezone(timedelta(hours=9))


def is_peak(at: datetime | None = None) -> bool:
    """DeepSeek 피크 요금 구간인지. 평일 01:00-04:00 / 06:00-10:00 UTC."""
    at = _as_utc(at)
    if at.weekday() >= 5:  # 토·일은 종일 오프피크
        return False
    return any(start <= at.hour < end for start, end in PEAK_WINDOWS_UTC)


def peak_multiplier(at: datetime | None = None) -> float:
    return PEAK_MULTIPLIER if is_peak(at) else 1.0


def next_offpeak(at: datetime | None = None) -> datetime:
    """지금이 피크면 다음 오프피크 시작 시각(UTC), 아니면 그대로 반환."""
    at = _as_utc(at)
    if not is_peak(at):
        return at
    # 피크 창은 최대 4시간짜리라 시(hour) 단위로 걸어 나가면 반드시 빠져나온다.
    probe = at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while is_peak(probe):
        probe += timedelta(hours=1)
    return probe


def _as_utc(at: datetime | None) -> datetime:
    if at is None:
        return datetime.now(timezone.utc)
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


@dataclass(frozen=True)
class Usage:
    """한 번의 API 호출이 쓴 토큰. 쓰지 않는 항목은 0으로 둔다."""

    input_tokens: int = 0
    """캐시에 걸리지 않은 신규 입력 토큰."""
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens + other.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens + other.cache_write_1h_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    @property
    def total_input(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )


def deepseek_cost(model: str, usage: Usage, at: datetime | None = None) -> float:
    """DeepSeek 호출 비용(USD). cache_read_tokens 를 캐시 히트분으로 본다."""
    rate = DEEPSEEK_RATES[model]
    mult = peak_multiplier(at)
    return mult * (
        usage.input_tokens * rate.cache_miss_in
        + usage.cache_read_tokens * rate.cache_hit_in
        + usage.output_tokens * rate.output
    ) / 1_000_000


def claude_cost(model: str, usage: Usage, batch: bool = False) -> float:
    """Claude 호출 비용(USD)."""
    rate = CLAUDE_RATES[model]
    raw = (
        usage.input_tokens * rate.input
        + usage.cache_read_tokens * rate.cache_read
        + usage.cache_write_5m_tokens * rate.cache_write_5m
        + usage.cache_write_1h_tokens * rate.cache_write_1h
        + usage.output_tokens * rate.output
    ) / 1_000_000
    return raw * (BATCH_DISCOUNT if batch else 1.0)


def cost_of(provider: str, model: str, usage: Usage, *, batch: bool = False,
            at: datetime | None = None) -> float:
    if provider == "deepseek":
        return deepseek_cost(model, usage, at)
    if provider == "anthropic":
        return claude_cost(model, usage, batch)
    raise ValueError(f"알 수 없는 provider: {provider}")


def counterfactual_claude_cost(model: str, usage: Usage, *, batch: bool = False) -> float:
    """'이 호출을 Claude 로 했다면' 비용. DeepSeek 캐시 히트분은 Claude 캐시 읽기로 친다."""
    return claude_cost(model, usage, batch)


def describe_window(at: datetime | None = None) -> str:
    at = _as_utc(at)
    kst = at.astimezone(KST)
    tag = "피크(2배)" if is_peak(at) else "오프피크"
    return f"{kst:%Y-%m-%d %H:%M} KST · DeepSeek {tag}"
