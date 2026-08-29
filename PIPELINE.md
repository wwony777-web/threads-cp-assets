# 하이브리드 카피 파이프라인

스레드 게시물 초안을 두 단계로 만든다. **버리는 토큰은 싼 모델로, 남는 문장만 비싼 모델로.**

```
products.jsonl
      │
      ▼
┌─────────────────────────────────────────┐
│ Stage 1 · DeepSeek V4-Flash             │  상품명 정규화 / 공간 분류 / 리뷰 요약
│  대량·저위험 전처리                      │  훅 후보 20개 / 키워드 추출
│  품질 편차가 결과물에 안 남는 것만        │  → 19개는 버려진다
└──────────────┬──────────────────────────┘
               ▼  stage1.jsonl (재료)
┌─────────────────────────────────────────┐
│ Stage 2 · Claude Sonnet 5               │  훅 선별·재작성 / 본문 / 해시태그
│  프롬프트 캐싱(1h) + Batch API(50%)      │  이미지 프롬프트 (ref/ 배경 기준)
│  여기만 품질이 그대로 남는다              │
└──────────────┬──────────────────────────┘
               ▼
      posts.md (복붙용) + posts.jsonl + ledger.jsonl
```

`space` 분류 결과는 이 저장소의 `ref/` 배경 사진에 그대로 매핑된다
(`fridge → ref_fridge.png`, `closet → ref_closet.png` …). Stage 2 의 이미지 프롬프트는
그 배경 위에 상품을 올리는 걸 전제로 쓰인다.

---

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env      # 키 두 개 채우기
set -a && source .env && set +a
```

## 쓰는 법

```bash
# 0. 상품 데이터 준비 (샘플을 복사해서 내용 교체)
cp data/products.sample.jsonl data/products.jsonl

# 1. 키 없이 견적부터 — 이번 배치가 얼마짜리인지
python -m pipeline.run estimate

# 2. 전체 실행 (Stage 2 는 기본이 배치 = 50% 할인, 보통 1시간 내 완료)
python -m pipeline.run all

# 급할 때만 동기 호출 (할인 포기)
python -m pipeline.run all --sync

# 피크 시간대면 아예 실행 거부 (DeepSeek 요금 2배 회피)
python -m pipeline.run all --offpeak-only

# 단계별로
python -m pipeline.run stage1 --limit 5
python -m pipeline.run stage2

# 3. 실제로 얼마 썼는지
python -m pipeline.run report
```

산출물은 `data/out/` 에 떨어진다.
- `posts.md` — 스레드에 그대로 복붙할 초안 + 이미지 프롬프트
- `posts.jsonl` — 후속 자동화용
- `ledger.jsonl` — 호출별 토큰·비용 원장

## 입력 형식 (`data/products.jsonl`, 한 줄에 한 상품)

```json
{"product_id":"175597353","raw_name":"쿠팡 원본 상품명 그대로","price":12900,
 "url":"https://link.coupang.com/a/...","category_hint":"주방",
 "reviews":["리뷰1","리뷰2"],"notes":"자유 메모"}
```

`product_id` 가 `img/` 파일명 접두사와 같으면 (`175597353_real_1.jpg` 등) 보유 이미지를
자동으로 찾아 Stage 2 프롬프트에 반영한다.

---

## 비용

100만 토큰당 USD. **2026-08-29 확인.**

| | 캐시히트/읽기 | 입력 | 출력 |
|---|---|---|---|
| DeepSeek V4-Flash (오프피크) | $0.007 | $0.22 | $0.66 |
| DeepSeek V4-Flash (피크) | $0.014 | $0.44 | $1.32 |
| DeepSeek V4-Pro (오프피크) | $0.022 | $0.66 | $1.98 |
| Claude Haiku 4.5 | $0.10 | $1.00 | $5.00 |
| Claude Sonnet 5 | $0.20 | $2.00 | $10.00 |
| Claude Opus 5 | $0.50 | $5.00 | $25.00 |

- Batch API 는 전 토큰 **50%**. 캐시 쓰기는 5분 TTL 1.25배 / 1시간 TTL 2배.
- DeepSeek 피크는 **평일 01:00–04:00, 06:00–10:00 UTC = 한국시간 10–13시, 15–19시**.
  주말은 종일 오프피크. **자동화는 밤이나 주말에 돌리는 게 그냥 절반이다.**
- 요금이 바뀌면 `pipeline/config.py` 의 요금표만 고치면 원장 재계산까지 따라온다.

### 캐시 최소 프리픽스 — 조용히 실패하는 지점

프롬프트 캐시는 프리픽스가 모델별 최소 토큰에 못 미치면 **에러 없이 그냥 안 걸린다.**
`cache_control` 은 붙어 있는데 요금만 정가로 나온다.

| 모델 | 최소 |
|---|---|
| Claude Opus 5 | 512 |
| Claude Sonnet 5 | 1,024 |
| Claude Haiku 4.5 | **4,096** |

현재 `prompts/stage2_style.ko.md` 는 약 2,000 토큰이라 **Sonnet 5·Opus 5 에서는 캐시가 걸리고
Haiku 4.5 에서는 안 걸린다.** 그래서 기본값이 Sonnet 5다.
Haiku 로 내리려면 스타일 가이드를 4,096 토큰 이상으로 키워야 실익이 난다.

- `estimate` 는 이 조건을 반영해서 계산한다(캐시가 안 걸리면 캐시 없는 값으로 보여준다).
- 실제 호출에서도 첫 응답의 `cache_creation`/`cache_read` 가 0이면 경고를 띄운다.

### 실측 예 (샘플 5건, 오프피크, Stage 2 배치)

```
Stage 1  deepseek-v4-flash   $0.0034
Stage 2  claude-sonnet-5     $0.0198
합계     $0.0232 (약 32원) · 건당 약 6.5원
```

건수가 늘수록 캐시 쓰기 비용이 분산되어 건당 단가는 더 내려간다.
`report` 는 "같은 일을 전부 Claude 로 했다면" 값을 함께 계산해 실제 절감액을 보여준다.

---

## 설계 의도 세 가지

**1. DeepSeek 로 보내는 건 '버리는 토큰'만.**
훅 20개 중 19개는 안 쓰고, 상품명 정규화와 공간 분류는 맞는지 눈으로 바로 보인다.
최종 문장은 Stage 2 가 쓴다 — 한국어 카피의 톤 감도는 아직 차이가 난다.

**2. 고지 문구는 모델이 아니라 코드가 붙인다.**
쿠팡 파트너스 대가성 고지는 공정위 추천·보증 심사지침상 필수다. 모델이 빼먹을 수 있는 걸
프롬프트로 부탁하지 않고 `Post.render()` 에서 무조건 붙이고, `problems()` 로 다시 검사한다.

**3. 원가는 추정하지 않고 원장으로 잰다.**
호출마다 토큰과 비용을 `ledger.jsonl` 에 적고, DeepSeek 로 넘긴 호출에는
"Claude 로 했다면" 값을 같이 적어둔다. 하이브리드가 실제로 이득인지는 `report` 가 답한다.

## 주의

- **DeepSeek 는 중국 서버로 데이터가 간다.** 쿠팡 파트너스 계정정보, 정산 내역,
  개인정보는 Stage 1 입력에 넣지 말 것. 상품명·공개 리뷰까지만.
- **DeepSeek 는 이미지 생성이 없다.** V4-Flash-Vision-Exp 는 이미지 *입력*만 받는다.
  `_ai_context.png` 를 만드는 건 여전히 별도 이미지 모델의 몫이고,
  이 파이프라인은 거기 넣을 **프롬프트**까지만 만든다.
- **DeepSeek 요금 변동성이 크다.** 2026-05 에 75% 인하했다가 2026-08-16 에 최대 11배 올렸다.
  원가 구조 전체를 여기 걸어두지 말 것.
- 생성된 초안은 그대로 올리지 말고 한 번 읽어볼 것. `problems()` 가 잡는 건
  길이·고지·빈 필드까지고, 사실관계는 사람이 봐야 한다.

## 테스트

```bash
python3 tests/test_pipeline.py    # API 키 없이 전 단계 스모크 테스트
```
