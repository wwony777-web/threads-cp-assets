"""프로바이더 래퍼 — DeepSeek(OpenAI 호환)와 Claude 를 같은 모양으로 감싼다.

두 SDK 모두 **호출 시점에** import 한다. 그래야 키나 패키지 없이도
--dry-run 으로 견적만 뽑아볼 수 있다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .config import CLAUDE_RATES, Settings
from .pricing import Usage


class LLMError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    usage: Usage
    raw: Any = None

    def json(self) -> dict:
        return parse_json_loose(self.text)


def parse_json_loose(text: str) -> dict:
    """모델이 코드펜스나 잡담을 섞어 보내도 첫 JSON 객체를 건져낸다."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"JSON 파싱 실패: {text[:200]!r}")


def _retry(fn, *, attempts: int, what: str):
    """네트워크·레이트리밋 재시도. 2s → 4s → 8s → 16s."""
    delay = 2.0
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # SDK 예외 타입이 프로바이더마다 달라 광범위하게 잡는다
            last = exc
            if i == attempts - 1:
                break
            print(f"  ! {what} 실패({exc.__class__.__name__}) — {delay:.0f}초 후 재시도")
            time.sleep(delay)
            delay *= 2
    raise LLMError(f"{what} {attempts}회 실패: {last}") from last


# --------------------------------------------------------------------------
# DeepSeek — Stage 1
# --------------------------------------------------------------------------


class DeepSeekClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.deepseek_api_key:
            raise LLMError("DEEPSEEK_API_KEY 가 없습니다. .env 를 확인하세요.")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("pip install openai 가 필요합니다.") from exc
        self.settings = settings
        self._client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.request_timeout,
            max_retries=0,  # 재시도는 _retry 가 담당
        )

    def complete(self, *, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = True) -> Completion:
        kwargs: dict = {}
        if self.settings.stage1_temperature is not None:
            kwargs["temperature"] = self.settings.stage1_temperature

        def call():
            return self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                response_format={"type": "json_object"} if json_mode else {"type": "text"},
                **kwargs,
                messages=[
                    # 시스템 블록을 고정해 두면 DeepSeek 의 프리픽스 캐시가 걸린다.
                    # 캐시 히트분은 미스 대비 약 1/31 가격이라, 이 순서가 곧 돈이다.
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )

        resp = _retry(call, attempts=self.settings.max_retries, what=f"DeepSeek {model}")
        u = resp.usage
        hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
        miss = getattr(u, "prompt_cache_miss_tokens", None)
        if miss is None:  # 필드가 없으면 전부 미스로 보수적으로 계산
            miss = (u.prompt_tokens or 0) - hit
        return Completion(
            text=resp.choices[0].message.content or "",
            usage=Usage(input_tokens=max(0, miss), cache_read_tokens=hit,
                        output_tokens=u.completion_tokens or 0),
            raw=resp,
        )


# --------------------------------------------------------------------------
# Claude — Stage 2
# --------------------------------------------------------------------------


class ClaudeClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY 가 없습니다. .env 를 확인하세요.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError("pip install anthropic 이 필요합니다.") from exc
        self.settings = settings
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.request_timeout,
            max_retries=0,
        )
        self._cache_checked = False

    # -- 요청 조립 ---------------------------------------------------------

    @staticmethod
    def build_params(*, model: str, style_block: str, task: str, max_tokens: int) -> dict:
        """캐시가 걸리는 유일한 모양.

        렌더 순서는 tools → system → messages 라서, **변하지 않는** 스타일 가이드를
        system 앞쪽에 두고 거기에만 breakpoint 를 건다. 상품별로 달라지는 내용은
        전부 messages 로 내려보낸다. 이 순서가 깨지면 캐시는 조용히 죽는다.
        """
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": style_block,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            "messages": [{"role": "user", "content": task}],
        }

    def complete(self, *, model: str, style_block: str, task: str, max_tokens: int) -> Completion:
        params = self.build_params(
            model=model, style_block=style_block, task=task, max_tokens=max_tokens
        )
        resp = _retry(
            lambda: self._client.messages.create(**params),
            attempts=self.settings.max_retries,
            what=f"Claude {model}",
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            raise LLMError("모델이 응답을 거부했습니다(stop_reason=refusal). 프롬프트를 확인하세요.")
        usage = usage_from_anthropic(resp.usage)
        self._warn_if_cache_dead(model, usage)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return Completion(text=text, usage=usage, raw=resp)

    def _warn_if_cache_dead(self, model: str, usage: Usage) -> None:
        """캐시는 실패해도 에러가 안 난다 — 요금만 정가로 나온다. 한 번은 확인해 준다."""
        if self._cache_checked:
            return
        self._cache_checked = True
        wrote = usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
        if wrote or usage.cache_read_tokens:
            return
        minimum = CLAUDE_RATES[model].cache_min_tokens
        print(
            f"  ! 경고: {model} 캐시가 걸리지 않았습니다 "
            f"(cache_creation=0, cache_read=0). 이 모델의 최소 캐시 프리픽스는 "
            f"{minimum:,} 토큰입니다 — prompts/stage2_style.ko.md 를 더 키우거나 "
            f"CP_STAGE2_MODEL 을 캐시 최소값이 낮은 모델로 바꾸세요."
        )

    # -- 배치 -------------------------------------------------------------

    def submit_batch(self, requests: list[tuple[str, dict]]) -> str:
        """(custom_id, params) 목록을 Batch API 로 제출하고 batch id 를 준다. 요금 50%."""
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        batch = _retry(
            lambda: self._client.messages.batches.create(
                requests=[
                    Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**params))
                    for cid, params in requests
                ]
            ),
            attempts=self.settings.max_retries,
            what="Claude 배치 제출",
        )
        return batch.id

    def wait_batch(self, batch_id: str, *, poll_seconds: int = 60) -> None:
        while True:
            batch = self._client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                return
            counts = batch.request_counts
            print(
                f"  · 배치 {batch_id} {batch.processing_status} "
                f"(처리중 {counts.processing} / 완료 {counts.succeeded} / 실패 {counts.errored})"
            )
            time.sleep(poll_seconds)

    def batch_results(self, batch_id: str):
        """(custom_id, Completion | None, error_str) 를 순서대로 흘려보낸다."""
        for result in self._client.messages.batches.results(batch_id):
            kind = result.result.type
            if kind == "succeeded":
                msg = result.result.message
                text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                yield result.custom_id, Completion(text=text, usage=usage_from_anthropic(msg.usage),
                                                   raw=msg), ""
            else:
                detail = getattr(getattr(result.result, "error", None), "type", kind)
                yield result.custom_id, None, f"{kind}:{detail}"

    def count_tokens(self, *, model: str, style_block: str, task: str) -> int:
        """실제 호출 없이 입력 토큰 수만 센다(견적·캐시 최소값 확인용)."""
        resp = self._client.messages.count_tokens(
            model=model,
            system=[{"type": "text", "text": style_block}],
            messages=[{"role": "user", "content": task}],
        )
        return resp.input_tokens


def usage_from_anthropic(u) -> Usage:
    """anthropic usage 객체 → 내부 Usage. 5분/1시간 캐시 쓰기를 분리해 담는다."""
    breakdown = getattr(u, "cache_creation", None)
    w5 = getattr(breakdown, "ephemeral_5m_input_tokens", None) if breakdown else None
    w1h = getattr(breakdown, "ephemeral_1h_input_tokens", None) if breakdown else None
    total_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    if w5 is None and w1h is None:
        w5, w1h = total_write, 0
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_5m_tokens=w5 or 0,
        cache_write_1h_tokens=w1h or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
    )
