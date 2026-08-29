"""Stage 1 — DeepSeek V4-Flash 대량 전처리.

여기서 도는 건 전부 '버리는 토큰'이다. 훅 20개 중 19개는 안 쓰고,
정규화·분류는 맞는지 눈으로 바로 확인된다. 품질 편차가 결과물에 남지 않는
작업만 여기에 둔다 — 최종 문장은 Stage 2 가 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import PROMPT_DIR, Settings
from .ledger import Ledger
from .llm import DeepSeekClient, LLMError
from .schema import Product, Stage1Result

SYSTEM_PROMPT_PATH = PROMPT_DIR / "stage1_system.ko.md"


def load_system_prompt() -> str:
    """상품 간에 **한 바이트도** 달라지지 않아야 프리픽스 캐시가 걸린다."""
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def build_user_prompt(product: Product, hook_candidates: int) -> str:
    reviews = product.reviews[:20]
    lines = [
        f"상품ID: {product.product_id}",
        f"원본 상품명: {product.raw_name}",
    ]
    if product.price:
        lines.append(f"가격: {product.price:,}원")
    if product.category_hint:
        lines.append(f"카테고리 힌트: {product.category_hint}")
    if product.notes:
        lines.append(f"메모: {product.notes}")
    lines.append(f"\nhooks 는 정확히 {hook_candidates}개를 뽑는다.")
    if reviews:
        lines.append("\n리뷰:")
        lines += [f"- {r.strip()}" for r in reviews if r.strip()]
    else:
        lines.append("\n리뷰: (없음 — 상품명과 카테고리만으로 추론하되 수치는 만들지 않는다)")
    return "\n".join(lines)


def run(
    products: list[Product],
    settings: Settings,
    ledger: Ledger,
    *,
    client: DeepSeekClient | None = None,
) -> list[Stage1Result]:
    client = client or DeepSeekClient(settings)
    system = load_system_prompt()
    results: list[Stage1Result] = []

    for i, product in enumerate(products, 1):
        print(f"  [{i}/{len(products)}] {product.product_id} {product.raw_name[:40]}")
        try:
            completion = client.complete(
                model=settings.stage1_model,
                system=system,
                user=build_user_prompt(product, settings.hook_candidates),
                max_tokens=settings.stage1_max_tokens,
            )
        except LLMError as exc:
            print(f"    x 건너뜀: {exc}")
            continue

        ledger.record(
            stage="stage1",
            provider="deepseek",
            model=settings.stage1_model,
            product_id=product.product_id,
            usage=completion.usage,
            # 같은 일을 Stage 2 모델로 시켰다면 얼마였는지 — 절감액 근거.
            compare_model=settings.stage2_model,
        )

        try:
            payload = completion.json()
        except LLMError as exc:
            print(f"    x JSON 실패, 건너뜀: {exc}")
            continue

        result = Stage1Result.from_json(product.product_id, payload)
        if not result.short_name:
            print("    ! short_name 이 비었습니다 — 입력 정보가 부족했을 수 있음")
        results.append(result)

    return results


def save(results: list[Stage1Result], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")


def load(path: Path) -> list[Stage1Result]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [Stage1Result(**r) for r in rows]
