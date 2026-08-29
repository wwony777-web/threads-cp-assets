"""CLI 진입점.

    python -m pipeline.run estimate                 # 키 없이 견적만
    python -m pipeline.run stage1                   # DeepSeek 전처리만
    python -m pipeline.run stage2                   # Claude 카피만 (stage1 산출물 사용)
    python -m pipeline.run all --offpeak-only       # 전체, 피크면 거부
    python -m pipeline.run report                   # 실제 청구 원장 집계
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import stage1 as s1mod
from . import stage2 as s2mod
from .config import DATA_DIR, OUT_DIR, Settings
from .estimate import estimate
from .ledger import Ledger
from .llm import LLMError
from .pricing import KST, describe_window, is_peak, next_offpeak
from .schema import Product, read_jsonl

STAGE1_OUT = OUT_DIR / "stage1.jsonl"


def load_products(path: Path, limit: int | None) -> list[Product]:
    products = [Product.from_dict(d) for d in read_jsonl(path)]
    return products[:limit] if limit else products


def guard_peak(offpeak_only: bool) -> None:
    if offpeak_only and is_peak():
        nxt = next_offpeak()
        print(
            f"지금은 DeepSeek 피크 구간입니다 (요금 2배). --offpeak-only 라 중단합니다.\n"
            f"  다음 오프피크: {nxt.astimezone(KST):%Y-%m-%d %H:%M} KST"
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline.run", description="쿠팡x스레드 하이브리드 카피 파이프라인")
    ap.add_argument("command", choices=["estimate", "stage1", "stage2", "all", "report"])
    ap.add_argument("--input", type=Path, default=DATA_DIR / "products.jsonl")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--limit", type=int, default=None, help="앞에서 N건만 처리")
    ap.add_argument("--sync", action="store_true", help="Stage 2 를 배치 대신 동기 호출로 (50%% 할인 포기)")
    ap.add_argument("--offpeak-only", action="store_true", help="DeepSeek 피크 시간대면 실행 거부")
    ap.add_argument("--poll", type=int, default=60, help="배치 폴링 간격(초)")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    ledger = Ledger()

    if args.command == "report":
        print(Ledger.report(Ledger.load(), settings))
        return 0

    products = load_products(args.input, args.limit)
    if not products:
        print(f"{args.input} 에 상품이 없습니다.")
        return 1
    by_id = {p.product_id: p for p in products}

    if args.command == "estimate":
        est = estimate(
            products=products,
            settings=settings,
            stage1_system=s1mod.load_system_prompt(),
            style_block=s2mod.load_style_block(),
            build_stage1_user=s1mod.build_user_prompt,
            batch=not args.sync,
        )
        print(est.render(settings))
        return 0

    print(f"{describe_window()} · 상품 {len(products)}건")

    results = None
    if args.command in ("stage1", "all"):
        guard_peak(args.offpeak_only)
        print(f"\nStage 1 — {settings.stage1_model} (대량 전처리)")
        try:
            results = s1mod.run(products, settings, ledger)
        except LLMError as exc:
            print(f"중단: {exc}")
            return 1
        s1mod.save(results, STAGE1_OUT)
        print(f"  → {STAGE1_OUT} ({len(results)}건)")

    if args.command in ("stage2", "all"):
        if results is None:
            if not STAGE1_OUT.exists():
                print(f"{STAGE1_OUT} 이 없습니다. 먼저 stage1 을 돌리세요.")
                return 1
            results = s1mod.load(STAGE1_OUT)
        if not results:
            print("Stage 1 결과가 비어 있어 Stage 2 를 건너뜁니다.")
            return 1

        mode = "동기" if args.sync else "배치(50%)"
        print(f"\nStage 2 — {settings.stage2_model} ({mode})")
        try:
            runner = s2mod.run_sync if args.sync else s2mod.run_batch
            kwargs = {} if args.sync else {"poll_seconds": args.poll}
            posts = runner(by_id, results, settings, ledger, **kwargs)
        except LLMError as exc:
            print(f"중단: {exc}")
            return 1
        s2mod.save(posts, by_id, args.out)
        print(f"  → {args.out / 'posts.md'} ({len(posts)}건)")

    print(Ledger.report(ledger.entries, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
