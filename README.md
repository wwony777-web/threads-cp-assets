# threads-cp-assets

상품 홍보용 이미지 저장소 + 스레드 게시물 초안 파이프라인.

- `img/` — 상품별 이미지. `{상품ID}_official*` (공식컷), `{상품ID}_real_*` (실사용컷),
  `{상품ID}_ai_context*` (AI 합성 컨텍스트컷)
- `ref/` — AI 합성용 배경 레퍼런스 (주방·조리대·냉장고·팬트리·옷장)
- `pipeline/` — DeepSeek(대량 전처리) + Claude(최종 카피) 하이브리드 파이프라인
- `prompts/` — 단계별 프롬프트. `stage2_style.ko.md` 가 카피 톤의 기준
- `data/` — 입력 상품 데이터와 산출물

파이프라인 사용법과 비용은 **[PIPELINE.md](PIPELINE.md)** 참조.

```bash
pip install -r requirements.txt
cp .env.example .env && set -a && source .env && set +a
cp data/products.sample.jsonl data/products.jsonl
python -m pipeline.run estimate     # 견적
python -m pipeline.run all          # 실행
python -m pipeline.run report       # 실제 비용
```
