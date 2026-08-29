"""파이프라인 설정 — 모델 ID, 요금표, 경로, 환경변수.

요금은 전부 **100만 토큰당 USD**. 출처와 확인일자는 PIPELINE.md 참조.
가격이 바뀌면 이 파일만 고치면 된다(원장 재계산 포함).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"
DATA_DIR = ROOT / "data"
OUT_DIR = DATA_DIR / "out"
LEDGER_PATH = OUT_DIR / "ledger.jsonl"

# 이 저장소의 실제 배경 레퍼런스. Stage 1 의 `space` 분류 결과가 여기로 매핑된다.
REF_DIR = ROOT / "ref"
IMG_DIR = ROOT / "img"

SPACE_TO_REF: dict[str, str] = {
    "kitchen": "ref_kitchen.png",
    "counter": "ref_counter_closeup.png",
    "fridge": "ref_fridge.png",
    "fridge_closeup": "ref_fridge_closeup.png",
    "pantry": "ref_pantry.png",
    "closet": "ref_closet.png",
}
"""Stage 1 이 고를 수 있는 공간 라벨 → ref/ 배경 파일."""


# --------------------------------------------------------------------------
# 요금표
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeepSeekRate:
    """DeepSeek 요금 (오프피크 기준, USD / 1M tok). 피크는 정확히 2배."""

    cache_hit_in: float
    cache_miss_in: float
    output: float


@dataclass(frozen=True)
class ClaudeRate:
    """Claude 요금 (USD / 1M tok)."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    cache_min_tokens: int
    """이 토큰 수를 넘지 못하는 프리픽스는 cache_control 을 달아도 **조용히** 캐시되지 않는다."""


# 2026-08-16 16:00 UTC 인상분 반영. 피크(요금 2배)는 평일 01:00-04:00 / 06:00-10:00 UTC.
DEEPSEEK_RATES: dict[str, DeepSeekRate] = {
    "deepseek-v4-flash": DeepSeekRate(cache_hit_in=0.007, cache_miss_in=0.22, output=0.66),
    "deepseek-v4-pro": DeepSeekRate(cache_hit_in=0.022, cache_miss_in=0.66, output=1.98),
}

CLAUDE_RATES: dict[str, ClaudeRate] = {
    "claude-haiku-4-5": ClaudeRate(1.00, 5.00, 0.10, 1.25, 2.00, cache_min_tokens=4096),
    "claude-sonnet-5": ClaudeRate(2.00, 10.00, 0.20, 2.50, 4.00, cache_min_tokens=1024),
    "claude-opus-5": ClaudeRate(5.00, 25.00, 0.50, 6.25, 10.00, cache_min_tokens=512),
}

BATCH_DISCOUNT = 0.5
"""Message Batches API 는 모든 토큰 종류에 50% 할인."""

PEAK_MULTIPLIER = 2.0

# UTC 시(hour) 기준 피크 구간. 한국시간으로는 평일 10-13시, 15-19시.
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


# --------------------------------------------------------------------------
# 실행 설정
# --------------------------------------------------------------------------


@dataclass
class Settings:
    """환경변수에서 읽는 실행 설정."""

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    anthropic_api_key: str = ""

    stage1_model: str = "deepseek-v4-flash"
    stage2_model: str = "claude-sonnet-5"

    # Stage 2 를 Haiku 로 내리려면 CP_STAGE2_MODEL=claude-haiku-4-5.
    # 단 Haiku 4.5 는 캐시 최소 프리픽스가 4096 토큰이라 스타일 가이드가
    # 그보다 짧으면 캐시가 아예 걸리지 않는다(경고만 뜨고 요금은 정가).
    stage2_max_tokens: int = 2000
    stage1_max_tokens: int = 3000

    hook_candidates: int = 20
    stage1_temperature: float | None = 0.8
    """훅 후보를 다양하게 뽑으려 조금 높게. 추론형 모델(v4-pro)은 이 값을 거부할 수
    있으므로 None 이면 아예 보내지 않는다."""
    request_timeout: float = 120.0
    max_retries: int = 4

    usd_krw: float = 1400.0
    """리포트용 환산 환율. 실제 청구는 USD."""

    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            stage1_model=os.environ.get("CP_STAGE1_MODEL", "deepseek-v4-flash"),
            stage2_model=os.environ.get("CP_STAGE2_MODEL", "claude-sonnet-5"),
            hook_candidates=int(os.environ.get("CP_HOOK_CANDIDATES", "20")),
            usd_krw=float(os.environ.get("CP_USD_KRW", "1400")),
        )


# 쿠팡 파트너스 활동 시 게시물에 반드시 들어가야 하는 대가성 고지 문구.
# 공정위 추천·보증 심사지침상 누락하면 제재 대상이라 코드에서 강제한다.
DISCLOSURE = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
