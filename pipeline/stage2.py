"""Stage 2 — Claude 최종 카피 + 이미지 프롬프트.

여기만 품질이 결과물에 그대로 남는다. 그래서 여기만 비싼 모델을 쓰고,
대신 캐싱과 배치로 단가를 눌러 쓴다.

- 스타일 가이드(고정)는 system 에 두고 1시간 TTL 캐시를 건다 → 읽기는 입력가의 10%.
- 급하지 않으면 Batch API 로 보낸다 → 전 토큰 50% 할인.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import PROMPT_DIR, SPACE_TO_REF, Settings
from .ledger import Ledger
from .llm import ClaudeClient, LLMError
from .schema import Post, Product, Stage1Result

STYLE_PROMPT_PATH = PROMPT_DIR / "stage2_style.ko.md"


def load_style_block() -> str:
    """캐시 프리픽스. 상품 사이에 절대 바뀌면 안 된다 — 한 바이트만 달라져도 캐시가 죽는다."""
    return STYLE_PROMPT_PATH.read_text(encoding="utf-8")


def build_task(product: Product, s1: Stage1Result) -> str:
    """상품별로 달라지는 부분. 전부 messages 쪽에 실어야 캐시가 유지된다."""
    assets = product.assets()
    lines = [
        "아래 재료로 스레드 게시물 하나를 써라.",
        "",
        f"상품: {s1.short_name or product.raw_name}",
        f"카테고리: {s1.category or product.category_hint or '(미상)'}",
        f"타깃: {s1.target or '(미상)'}",
        f"배경 이미지: ref/{s1.ref_background} ({s1.space or 'kitchen'})",
    ]
    if product.price:
        lines.append(f"가격대: {product.price:,}원")
    if assets["real"]:
        lines.append(f"실사용 컷 {len(assets['real'])}장 보유 — 본문 톤을 실사용 후기 쪽으로")

    lines += ["", "구매 포인트:"]
    lines += [f"- {p}" for p in s1.buy_points] or ["- (없음)"]

    lines += ["", "단점·주의:"]
    lines += [f"- {c}" for c in s1.caveats] or [
        "- (없음 — 단점을 지어내지 말고 '이런 사람에겐 안 맞는다' 한 줄로 대체할 것)"
    ]

    lines += ["", f"훅 후보 {len(s1.hooks)}개 (고르거나 다듬어 쓸 것, 전부 약하면 새로 써라):"]
    lines += [f"{i}. {h}" for i, h in enumerate(s1.hooks, 1)]

    if s1.keywords:
        lines += ["", f"해시태그 후보: {', '.join(s1.keywords)}"]

    return "\n".join(lines)


def _finish(product_id: str, completion, s1: Stage1Result, url: str = "") -> Post | None:
    try:
        payload = completion.json()
    except LLMError as exc:
        print(f"    x {product_id} JSON 실패: {exc}")
        return None
    post = Post.from_json(product_id, payload, ref_background=s1.ref_background)
    for issue in post.problems(url):
        print(f"    ! {product_id} 점검: {issue}")
    return post


# --------------------------------------------------------------------------
# 동기 경로 — 몇 건만 급히 뽑을 때
# --------------------------------------------------------------------------


def run_sync(
    products: dict[str, Product],
    stage1: list[Stage1Result],
    settings: Settings,
    ledger: Ledger,
    *,
    client: ClaudeClient | None = None,
) -> list[Post]:
    client = client or ClaudeClient(settings)
    style = load_style_block()
    posts: list[Post] = []

    for i, s1 in enumerate(stage1, 1):
        product = products.get(s1.product_id)
        if product is None:
            print(f"    x {s1.product_id}: 원본 상품 정보 없음, 건너뜀")
            continue
        print(f"  [{i}/{len(stage1)}] {s1.product_id} {s1.short_name}")
        try:
            completion = client.complete(
                model=settings.stage2_model,
                style_block=style,
                task=build_task(product, s1),
                max_tokens=settings.stage2_max_tokens,
            )
        except LLMError as exc:
            print(f"    x 건너뜀: {exc}")
            continue

        ledger.record(
            stage="stage2",
            provider="anthropic",
            model=settings.stage2_model,
            product_id=s1.product_id,
            usage=completion.usage,
        )
        post = _finish(s1.product_id, completion, s1, product.url)
        if post:
            posts.append(post)

    return posts


# --------------------------------------------------------------------------
# 배치 경로 — 기본값. 50% 싸고, 대개 1시간 안에 끝난다.
# --------------------------------------------------------------------------


def run_batch(
    products: dict[str, Product],
    stage1: list[Stage1Result],
    settings: Settings,
    ledger: Ledger,
    *,
    client: ClaudeClient | None = None,
    poll_seconds: int = 60,
) -> list[Post]:
    client = client or ClaudeClient(settings)
    style = load_style_block()
    by_id = {s1.product_id: s1 for s1 in stage1}

    requests: list[tuple[str, dict]] = []
    for s1 in stage1:
        product = products.get(s1.product_id)
        if product is None:
            print(f"    x {s1.product_id}: 원본 상품 정보 없음, 건너뜀")
            continue
        requests.append((
            s1.product_id,
            ClaudeClient.build_params(
                model=settings.stage2_model,
                style_block=style,
                task=build_task(product, s1),
                max_tokens=settings.stage2_max_tokens,
            ),
        ))

    if not requests:
        return []

    batch_id = client.submit_batch(requests)
    print(f"  배치 제출됨: {batch_id} ({len(requests)}건, 요금 50%)")
    client.wait_batch(batch_id, poll_seconds=poll_seconds)

    posts: list[Post] = []
    for custom_id, completion, error in client.batch_results(batch_id):
        if completion is None:
            print(f"    x {custom_id} 실패: {error}")
            continue
        if custom_id not in by_id:
            print(f"    x {custom_id}: 대응하는 Stage 1 결과 없음, 건너뜀")
            continue
        ledger.record(
            stage="stage2",
            provider="anthropic",
            model=settings.stage2_model,
            product_id=custom_id,
            usage=completion.usage,
            batch=True,
            note=batch_id,
        )
        product = products.get(custom_id)
        post = _finish(custom_id, completion, by_id[custom_id], product.url if product else "")
        if post:
            posts.append(post)
    return posts


def save(posts: list[Post], products: dict[str, Product], out_dir: Path) -> None:
    """JSONL 한 벌 + 사람이 바로 복붙할 마크다운 한 벌."""
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "posts.jsonl").open("w", encoding="utf-8") as fh:
        for p in posts:
            fh.write(json.dumps(p.__dict__, ensure_ascii=False) + "\n")

    md = ["# 스레드 게시물 초안", ""]
    for p in posts:
        product = products.get(p.product_id)
        url = product.url if product else ""
        md += [
            f"## {p.product_id} — {product.raw_name[:40] if product else ''}",
            "",
            "```",
            p.render(url),
            "```",
            "",
            f"- 배경: `ref/{p.ref_background}`",
            f"- 이미지 프롬프트: {p.image_prompt}",
            "",
        ]
    (out_dir / "posts.md").write_text("\n".join(md), encoding="utf-8")
