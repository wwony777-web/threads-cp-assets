"""파이프라인을 흐르는 데이터 구조와 검증."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import DISCLOSURE, IMG_DIR, SPACE_TO_REF


@dataclass
class Product:
    """입력 1건. data/products.jsonl 의 한 줄."""

    product_id: str
    raw_name: str
    price: int | None = None
    url: str = ""
    category_hint: str = ""
    reviews: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Product":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def assets(self) -> dict[str, list[str]]:
        """img/ 에 이미 있는 이 상품의 이미지들을 종류별로 모은다."""
        buckets: dict[str, list[str]] = {"official": [], "real": [], "ai_context": [], "other": []}
        if not IMG_DIR.is_dir():
            return buckets
        for p in sorted(IMG_DIR.iterdir()):
            if not p.stem.startswith(self.product_id):
                continue
            suffix = p.stem[len(self.product_id):]
            if "official" in suffix:
                buckets["official"].append(p.name)
            elif "real" in suffix:
                buckets["real"].append(p.name)
            elif "ai_context" in suffix:
                buckets["ai_context"].append(p.name)
            else:
                buckets["other"].append(p.name)
        return buckets


@dataclass
class Stage1Result:
    """DeepSeek 가 뽑아내는 저위험 전처리 결과 — 그대로 게시되지 않는 재료."""

    product_id: str
    short_name: str = ""
    category: str = ""
    space: str = ""
    buy_points: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    target: str = ""

    @property
    def ref_background(self) -> str:
        """분류된 공간에 맞는 ref/ 배경 파일. 모르면 주방으로 떨어뜨린다."""
        return SPACE_TO_REF.get(self.space, SPACE_TO_REF["kitchen"])

    @classmethod
    def from_json(cls, product_id: str, payload: dict) -> "Stage1Result":
        def as_list(key: str) -> list[str]:
            v = payload.get(key) or []
            return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

        space = str(payload.get("space", "")).strip().lower()
        if space not in SPACE_TO_REF:
            space = ""
        return cls(
            product_id=product_id,
            short_name=str(payload.get("short_name", "")).strip(),
            category=str(payload.get("category", "")).strip(),
            space=space,
            buy_points=as_list("buy_points"),
            caveats=as_list("caveats"),
            hooks=as_list("hooks"),
            keywords=as_list("keywords"),
            target=str(payload.get("target", "")).strip(),
        )


@dataclass
class Post:
    """Stage 2 산출물 — 실제로 스레드에 올릴 것."""

    product_id: str
    hook: str = ""
    body: str = ""
    hashtags: list[str] = field(default_factory=list)
    image_prompt: str = ""
    ref_background: str = ""
    disclosure: str = DISCLOSURE

    @classmethod
    def from_json(cls, product_id: str, payload: dict, ref_background: str) -> "Post":
        tags = payload.get("hashtags") or []
        return cls(
            product_id=product_id,
            hook=str(payload.get("hook", "")).strip(),
            body=str(payload.get("body", "")).strip(),
            hashtags=[str(t).strip().lstrip("#") for t in tags if str(t).strip()],
            image_prompt=str(payload.get("image_prompt", "")).strip(),
            ref_background=ref_background,
        )

    def render(self, url: str = "") -> str:
        """스레드에 붙여넣을 최종 텍스트. 고지 문구는 코드에서 항상 붙인다."""
        parts = [self.hook, "", self.body]
        if url:
            parts += ["", url]
        if self.hashtags:
            parts += ["", " ".join(f"#{t}" for t in self.hashtags)]
        parts += ["", self.disclosure]
        return "\n".join(parts).strip()

    def problems(self, url: str = "") -> list[str]:
        """게시 전 자동 점검. 비어 있으면 통과. 링크 길이까지 포함해서 잰다."""
        issues: list[str] = []
        if not self.hook:
            issues.append("훅 문장이 비었음")
        if not self.body:
            issues.append("본문이 비었음")
        rendered = self.render(url)
        if DISCLOSURE not in rendered:
            issues.append("쿠팡 파트너스 고지 문구 누락")
        if len(rendered) > 500:
            issues.append(f"본문 {len(rendered)}자 — 스레드 상한(500자) 초과")
        if not self.image_prompt:
            issues.append("이미지 프롬프트가 비었음")
        return issues


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"입력 파일이 없습니다: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
