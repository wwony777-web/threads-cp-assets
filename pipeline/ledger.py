"""호출 원장 — 토큰·비용을 JSONL 로 남기고 집계 리포트를 낸다.

'하이브리드가 정말 싼가'는 추정이 아니라 이 원장으로 답한다.
DeepSeek 로 넘긴 호출마다 '같은 걸 Claude 로 했다면' 값을 함께 적어두므로,
--report 가 실제 절감액을 보여준다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import LEDGER_PATH, Settings
from .pricing import Usage, claude_cost, cost_of, is_peak


@dataclass
class Entry:
    ts: str
    stage: str
    provider: str
    model: str
    product_id: str
    usage: dict[str, int]
    cost_usd: float
    batch: bool = False
    peak: bool = False
    claude_equivalent_usd: float = 0.0
    """이 호출을 Stage 2 모델(Claude)로 돌렸을 때의 비용. 절감액 계산용."""
    note: str = ""


class Ledger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: list[Entry] = []

    def record(
        self,
        *,
        stage: str,
        provider: str,
        model: str,
        product_id: str,
        usage: Usage,
        batch: bool = False,
        note: str = "",
        compare_model: str | None = None,
        at: datetime | None = None,
    ) -> Entry:
        at = at or datetime.now(timezone.utc)
        entry = Entry(
            ts=at.isoformat(),
            stage=stage,
            provider=provider,
            model=model,
            product_id=product_id,
            usage=asdict_usage(usage),
            cost_usd=cost_of(provider, model, usage, batch=batch, at=at),
            batch=batch,
            peak=is_peak(at) if provider == "deepseek" else False,
            claude_equivalent_usd=(
                claude_cost(compare_model, usage, batch=batch) if compare_model else 0.0
            ),
            note=note,
        )
        self.entries.append(entry)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return entry

    # -- 집계 --------------------------------------------------------------

    @staticmethod
    def load(path: Path | None = None) -> list[Entry]:
        path = path or LEDGER_PATH
        if not path.exists():
            return []
        out: list[Entry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Entry(**json.loads(line)))
        return out

    @staticmethod
    def report(entries: list[Entry], settings: Settings) -> str:
        if not entries:
            return "원장이 비어 있음. 먼저 파이프라인을 한 번 돌리세요."

        by_stage: dict[str, list[Entry]] = defaultdict(list)
        for e in entries:
            by_stage[e.stage].append(e)

        krw = settings.usd_krw
        lines = ["", "=" * 62, " 하이브리드 파이프라인 비용 리포트", "=" * 62]
        total = 0.0
        for stage in sorted(by_stage):
            rows = by_stage[stage]
            sub = sum(e.cost_usd for e in rows)
            total += sub
            usage = sum((usage_from(e) for e in rows), Usage())
            peak_n = sum(1 for e in rows if e.peak)
            lines.append(
                f"\n[{stage}] {rows[0].provider}/{rows[0].model} · 호출 {len(rows)}건"
                + (f" (피크 {peak_n}건)" if peak_n else "")
            )
            lines.append(
                f"  입력 {usage.input_tokens:,} / 캐시읽기 {usage.cache_read_tokens:,}"
                f" / 캐시쓰기 {usage.cache_write_5m_tokens + usage.cache_write_1h_tokens:,}"
                f" / 출력 {usage.output_tokens:,} tok"
            )
            lines.append(f"  비용 ${sub:.4f} ({sub * krw:,.0f}원)")

        saved = sum(e.claude_equivalent_usd - e.cost_usd for e in entries if e.claude_equivalent_usd)
        lines.append("\n" + "-" * 62)
        lines.append(f"합계        ${total:.4f} ({total * krw:,.0f}원)")
        if saved > 0:
            allclaude = total + saved
            pct = saved / allclaude * 100 if allclaude else 0
            lines.append(
                f"전량 Claude ${allclaude:.4f} ({allclaude * krw:,.0f}원)"
                f"  →  절감 ${saved:.4f} ({saved * krw:,.0f}원, {pct:.1f}%)"
            )
        n_products = len({e.product_id for e in entries if e.product_id})
        if n_products:
            lines.append(f"상품 {n_products}건 · 건당 ${total / n_products:.5f} ({total / n_products * krw:,.1f}원)")
        lines.append("=" * 62)
        return "\n".join(lines)


def asdict_usage(u: Usage) -> dict[str, int]:
    return {
        "input_tokens": u.input_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_5m_tokens": u.cache_write_5m_tokens,
        "cache_write_1h_tokens": u.cache_write_1h_tokens,
        "output_tokens": u.output_tokens,
    }


def usage_from(entry: Entry) -> Usage:
    return Usage(**entry.usage)
