"""실행 전 견적 — 키 없이도 돌아간다.

토큰 수는 어림이다. 목적은 '이번 배치가 얼마짜리인가'를 자릿수로 알려주는 것,
그리고 캐시 최소 프리픽스에 걸리는지 미리 잡아내는 것이다.
정확한 청구는 --report 의 원장을 본다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CLAUDE_RATES, Settings
from .pricing import Usage, claude_cost, deepseek_cost, describe_window, is_peak


def estimate_tokens(text: str) -> int:
    """한글은 글자당 약 1토큰, ASCII 는 4글자당 1토큰으로 잡는 보수적 어림."""
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    other = len(text) - hangul
    return hangul + max(1, other // 4)


@dataclass
class Estimate:
    n_products: int
    stage1: Usage
    stage2: Usage
    stage1_cost: float
    stage2_cost: float
    style_tokens: int
    cache_min: int
    cache_ok: bool
    batch: bool

    @property
    def total(self) -> float:
        return self.stage1_cost + self.stage2_cost

    def render(self, settings: Settings) -> str:
        krw = settings.usd_krw
        peak_note = " ← 지금 피크라 2배" if is_peak() else ""
        lines = [
            "",
            "=" * 62,
            f" 견적 · 상품 {self.n_products}건 · {describe_window()}",
            "=" * 62,
            f"Stage 1  {settings.stage1_model}",
            f"  입력 {self.stage1.input_tokens:,} / 캐시히트 {self.stage1.cache_read_tokens:,}"
            f" / 출력 {self.stage1.output_tokens:,} tok",
            f"  ${self.stage1_cost:.4f} ({self.stage1_cost * krw:,.0f}원){peak_note}",
            "",
            f"Stage 2  {settings.stage2_model}" + ("  [배치 50%]" if self.batch else "  [동기]"),
            f"  신규입력 {self.stage2.input_tokens:,} / 캐시읽기 {self.stage2.cache_read_tokens:,}"
            f" / 캐시쓰기 {self.stage2.cache_write_1h_tokens:,}"
            f" / 출력 {self.stage2.output_tokens:,} tok",
            f"  ${self.stage2_cost:.4f} ({self.stage2_cost * krw:,.0f}원)",
            "-" * 62,
            f"합계 ${self.total:.4f} ({self.total * krw:,.0f}원)"
            f" · 건당 {self.total / max(1, self.n_products) * krw:,.1f}원",
        ]
        if not self.cache_ok:
            lines += [
                "",
                f"⚠ 스타일 가이드가 약 {self.style_tokens:,} 토큰인데 "
                f"{settings.stage2_model} 의 캐시 최소 프리픽스는 {self.cache_min:,} 토큰입니다.",
                "  이대로 돌리면 cache_control 을 달아도 캐시가 걸리지 않고 정가로 청구됩니다.",
                "  prompts/stage2_style.ko.md 를 키우거나 캐시 최소값이 낮은 모델을 쓰세요",
                "  (claude-sonnet-5: 1,024 / claude-opus-5: 512 / claude-haiku-4-5: 4,096).",
            ]
        lines.append("=" * 62)
        return "\n".join(lines)


def estimate(
    *,
    products,
    settings: Settings,
    stage1_system: str,
    style_block: str,
    build_stage1_user,
    batch: bool,
) -> Estimate:
    n = len(products)
    sys1 = estimate_tokens(stage1_system)
    style_tokens = estimate_tokens(style_block)

    # Stage 1: 시스템 프롬프트는 2번째 호출부터 캐시 히트. 출력은 훅 20개 기준 어림.
    per_user = [estimate_tokens(build_stage1_user(p, settings.hook_candidates)) for p in products]
    s1_out_each = 450 + settings.hook_candidates * 22
    stage1_usage = Usage(
        input_tokens=sys1 + sum(per_user),
        cache_read_tokens=sys1 * max(0, n - 1),
        output_tokens=s1_out_each * n,
    )

    # Stage 2: 스타일 가이드는 1회 쓰고 나머지는 읽기. 상품별 task 는 매번 신규 입력.
    #          task 길이는 Stage 1 산출물(훅 20개 + 포인트) 기준 어림.
    s2_task_each = 500 + settings.hook_candidates * 20
    cache_min = CLAUDE_RATES[settings.stage2_model].cache_min_tokens
    cache_ok = style_tokens >= cache_min
    if cache_ok:
        stage2_usage = Usage(
            input_tokens=s2_task_each * n,
            cache_write_1h_tokens=style_tokens if n else 0,
            cache_read_tokens=style_tokens * max(0, n - 1),
            output_tokens=420 * n,
        )
    else:
        # 최소 프리픽스에 못 미치면 캐시는 조용히 죽고 매 호출이 정가다.
        # 경고만 띄우고 싼 숫자를 보여주면 견적이 거짓말이 되므로 여기서 반영한다.
        stage2_usage = Usage(
            input_tokens=(s2_task_each + style_tokens) * n,
            output_tokens=420 * n,
        )

    return Estimate(
        n_products=n,
        stage1=stage1_usage,
        stage2=stage2_usage,
        stage1_cost=deepseek_cost(settings.stage1_model, stage1_usage),
        stage2_cost=claude_cost(settings.stage2_model, stage2_usage, batch=batch),
        style_tokens=style_tokens,
        cache_min=cache_min,
        cache_ok=cache_ok,
        batch=batch,
    )
