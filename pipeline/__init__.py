"""쿠팡 파트너스 x 스레드 하이브리드 카피 파이프라인.

Stage 1 (DeepSeek V4-Flash) : 대량·저위험 전처리 — 상품명 정규화, 공간 분류,
                              리뷰 요약, 훅 후보 생성, 키워드 추출.
Stage 2 (Claude)            : 최종 스레드 카피 + AI 컨텍스트 이미지 프롬프트.
                              프롬프트 캐싱 + Batch API 로 원가를 눌러 쓴다.
"""

__all__ = ["config", "pricing", "ledger", "llm", "schema", "stage1", "stage2"]
