"""API 키 없이 파이프라인 전체를 한 번 흘려보는 스모크 테스트.

    python3 tests/test_pipeline.py

가짜 클라이언트로 Stage 1 → Stage 2 → 원장 → 리포트까지 태운다.
프롬프트 조립, JSON 파싱, ref 배경 매칭, 고지 문구 강제, 비용 집계를 검증한다.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import stage1 as s1mod  # noqa: E402
from pipeline import stage2 as s2mod  # noqa: E402
from pipeline.config import DISCLOSURE, Settings  # noqa: E402
from pipeline.ledger import Ledger  # noqa: E402
from pipeline.llm import Completion, parse_json_loose  # noqa: E402
from pipeline.pricing import Usage, is_peak  # noqa: E402
from pipeline.schema import Product, Stage1Result, read_jsonl  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeDeepSeek:
    """Stage 1 응답을 흉내낸다. 캐시 히트는 두 번째 호출부터."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, model, system, user, max_tokens, json_mode=True):
        self.calls.append((system, user))
        payload = {
            "short_name": "실리콘 밀폐용기",
            "category": "주방",
            "space": "fridge",
            "target": "자취 3년차 1인가구",
            "buy_points": ["국물 담아도 안 샌다", "층층이 쌓여 냉장고 자리가 준다"],
            "caveats": ["뚜껑이 뻑뻑하다"],
            "hooks": [f"훅 후보 {i}" for i in range(1, 21)],
            "keywords": ["냉장고정리", "밀폐용기", "자취템", "주방수납"],
        }
        first = len(self.calls) == 1
        return Completion(
            text=json.dumps(payload, ensure_ascii=False),
            usage=Usage(
                input_tokens=1200 if first else 300,
                cache_read_tokens=0 if first else 900,
                output_tokens=700,
            ),
        )


class FakeClaude:
    """Stage 2 응답을 흉내낸다. 캐시 쓰기 1회 후 읽기."""

    def __init__(self) -> None:
        self.params: list[dict] = []

    def complete(self, *, model, style_block, task, max_tokens):
        from pipeline.llm import ClaudeClient

        self.params.append(
            ClaudeClient.build_params(
                model=model, style_block=style_block, task=task, max_tokens=max_tokens
            )
        )
        first = len(self.params) == 1
        payload = {
            "hook": "냉장고 열 때마다 반찬통 굴러떨어지는 거 나만 그런가",
            "body": "층층이 쌓이는 걸로 바꾸고 나서 그 소리가 없어졌다.\n\n"
                    "국물 담아도 안 새는 건 덤이었고.\n\n"
                    "뚜껑은 좀 뻑뻑하다. 손 약하면 불편할 수 있다.",
            "hashtags": ["냉장고정리", "밀폐용기", "자취템", "주방수납"],
            "image_prompt": "Three silicone containers stacked on a fridge shelf, "
                            "half-used vegetables beside them, cool light, no text, no logos",
        }
        return Completion(
            text="```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
            usage=Usage(
                input_tokens=900,
                cache_write_1h_tokens=2000 if first else 0,
                cache_read_tokens=0 if first else 2000,
                output_tokens=400,
            ),
        )


def main() -> int:
    settings = Settings.from_env()
    products = [Product.from_dict(d) for d in read_jsonl(ROOT / "data/products.sample.jsonl")]
    assert len(products) >= 3, "샘플 상품이 부족합니다"

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        ledger = Ledger(tmpdir / "ledger.jsonl")

        # -- Stage 1 -------------------------------------------------------
        fake_ds = FakeDeepSeek()
        results = s1mod.run(products, settings, ledger, client=fake_ds)
        assert len(results) == len(products), "Stage 1 이 전부 처리되지 않음"

        systems = {s for s, _ in fake_ds.calls}
        assert len(systems) == 1, "시스템 프롬프트가 호출마다 달라짐 — 프리픽스 캐시가 죽는다"
        assert "훅 후보 20" not in fake_ds.calls[0][0], "가변 내용이 시스템 프롬프트에 섞였음"
        assert f"정확히 {settings.hook_candidates}개" in fake_ds.calls[0][1]

        r0 = results[0]
        assert r0.space == "fridge"
        assert r0.ref_background == "ref_fridge.png", r0.ref_background
        assert (ROOT / "ref" / r0.ref_background).exists(), "매핑된 배경 파일이 실제로 없음"
        assert len(r0.hooks) == 20

        # 알 수 없는 space 는 조용히 주방으로 떨어져야 한다
        unknown = Stage1Result.from_json("x", {"space": "garage"})
        assert unknown.ref_background == "ref_kitchen.png"

        # -- Stage 2 -------------------------------------------------------
        by_id = {p.product_id: p for p in products}
        fake_cl = FakeClaude()
        posts = s2mod.run_sync(by_id, results, settings, ledger, client=fake_cl)
        assert len(posts) == len(products), "Stage 2 가 전부 처리되지 않음"

        # 캐시가 걸리는 유일한 모양인지 — 스타일 가이드는 system, breakpoint 는 거기에만
        p0 = fake_cl.params[0]
        assert p0["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert "cache_control" not in json.dumps(p0["messages"]), "messages 에 breakpoint 가 붙었음"
        blocks = {json.dumps(p["system"], ensure_ascii=False) for p in fake_cl.params}
        assert len(blocks) == 1, "system 블록이 호출마다 달라짐 — 캐시가 죽는다"

        # 상품별 내용은 전부 messages 로 갔는지
        assert "실리콘 밀폐용기" in p0["messages"][0]["content"]
        assert "실리콘 밀폐용기" not in p0["system"][0]["text"]

        rendered = posts[0].render(by_id[posts[0].product_id].url)
        assert DISCLOSURE in rendered, "대가성 고지 문구가 빠졌다"
        assert "link.coupang.com" in rendered
        assert posts[0].problems() == [], posts[0].problems()

        # 고지 문구는 모델이 아니라 코드가 붙인다 — 모델이 빼먹어도 항상 들어가야 한다
        stripped = s2mod.Post(product_id="z", hook="훅", body="본문")
        assert DISCLOSURE in stripped.render()

        # -- 원장 ----------------------------------------------------------
        entries = Ledger.load(tmpdir / "ledger.jsonl")
        assert len(entries) == len(products) * 2, f"원장 {len(entries)}건"
        s1_cost = sum(e.cost_usd for e in entries if e.stage == "stage1")
        s1_claude = sum(e.claude_equivalent_usd for e in entries if e.stage == "stage1")
        assert s1_claude > s1_cost, "DeepSeek 가 Claude 보다 비싸게 계산됨"

        report = Ledger.report(entries, settings)
        assert "절감" in report and "stage1" in report and "stage2" in report

        # -- 산출물 --------------------------------------------------------
        s2mod.save(posts, by_id, tmpdir)
        md = (tmpdir / "posts.md").read_text(encoding="utf-8")
        assert DISCLOSURE in md and "ref/ref_fridge.png" in md
        saved = [json.loads(l) for l in (tmpdir / "posts.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(saved) == len(posts)

    # -- 순수 함수 ---------------------------------------------------------
    assert parse_json_loose('```json\n{"a":1}\n```') == {"a": 1}
    assert parse_json_loose('앞말 {"b":[1,2]} 뒷말') == {"b": [1, 2]}

    print(report)
    print(f"\n피크 판정(now): {is_peak()}")
    print("\n통과 — 전 단계 정상")
    return 0


if __name__ == "__main__":
    sys.exit(main())
