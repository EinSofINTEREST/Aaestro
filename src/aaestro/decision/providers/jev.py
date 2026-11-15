"""Jev (TypeSafe) decision provider adapter.

TypeSafe Jev 를 ``DecisionModel`` 계약 뒤에 바인딩한다. 와이어는 VirtualMem 이 실사용하는 형식
(``POST /v1/systemone``, ``{state, model, questions{id:{type:"noul", instructions, criteria}}}`` →
``{answers{id:{noul}}, usage}``) 이다. 실측된 질문 종류는 predicate (``noul``) 하나라 rating ·
choice 는 ``DEC_003`` 으로 거절한다.

**단일 시도다**: 재시도 · circuit 은 ``decision.client`` 한 계층이 한다 (05 Retry Layering).
리다이렉트는 따라가지 않는다 — 프록시가 종단하는 base URL 밖으로 state 를 보내지 않는다.
키 · state · 응답 본문은 에러 · 로그에 싣지 않는다.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import httpx

from aaestro.core.decision import Decision, QuestionKind
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode, raise_detached
from aaestro.decision.bands import decision_from
from aaestro.http_auth import is_loopback

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.core.decision import Question

KIND: Final = "jev"
SYSTEMONE_PATH: Final = "/v1/systemone"
"""base URL (프록시가 종단하는 단위, 01 Configuration) 뒤의 경로 — 어댑터 상수다."""

# 질문 수십 개의 확률이면 수 KB 다 — 넘으면 비정상 응답이다 (VirtualMem 과 같은 상한)
MAX_RESPONSE_BYTES: Final = 1024 * 1024
# VirtualMem 의 질문 수 기본 상한 (vmem.core.egress.DEFAULT_MAX_QUESTIONS) — 실사용 근거
MAX_QUESTIONS: Final = 200
# shortcut: Jev 의 state 길이 한도는 문서 · 실측 근거가 없어 보수적으로 잡았다,
# provider 한도를 확인하면 올린다
MAX_STATE_CHARS: Final = 16_000
# 헤더에 실리는 값 — 보이는 ASCII 만 (공백 · 제어 문자는 헤더 주입이다). VirtualMem 키 규칙과 같다
_CREDENTIAL: Final = re.compile(r"[\x21-\x7e]{1,512}")

_NOUL: Final = "noul"
_CRITERIA: Final = {
    "true": "The statement holds for `state`",
    "false": "The statement does not hold for `state`",
}

HTTP_REDIRECT_FIRST: Final = 300
HTTP_PAYLOAD_TOO_LARGE: Final = 413
HTTP_RATE_LIMITED: Final = 429
HTTP_SERVER_ERROR: Final = 500


def _error(
    code: ErrorCode,
    reason: str,
    *,
    retryable: bool,
    category: ErrorCategory = ErrorCategory.DECISION,
    **details: object,
) -> AaestroError:
    return AaestroError(
        category,
        code,
        f"decision provider call failed: {reason}",
        retryable=retryable,
        details={"provider": KIND, **details},
    )


def _config_error(reason: str) -> AaestroError:
    # 값은 싣지 않는다 — 자격 · URL 이 섞일 수 있다
    return AaestroError(
        ErrorCategory.CONFIG,
        ErrorCode.CFG_001,
        f"invalid jev provider setting: {reason}",
        details={"provider": KIND},
    )


def _endpoint(base_url: str, *, allow_plaintext: bool) -> str:
    """https, 또는 루프백 · 명시 opt-in 의 http 만 받는다 (03 Egress 결정 모델 행)."""
    endpoint = base_url.rstrip("/") + SYSTEMONE_PATH
    try:
        parts = urlsplit(base_url)
        # urlsplit 이 받아도 httpx 가 못 쓰는 URL (숫자가 아닌 포트, 제어 문자 등) 을 호출 시점이
        # 아니라 생성자에서 거른다
        httpx.URL(endpoint)
    # 메시지가 입력 URL 을 되비추므로 체인을 끊는다
    except (ValueError, httpx.InvalidURL):
        raise_detached(_config_error("base_url is not a valid URL"))
    if not parts.hostname or parts.query or parts.fragment or parts.username or parts.password:
        raise _config_error("base_url is not a plain http(s) URL")
    if parts.scheme == "http":
        if not (allow_plaintext or is_loopback(parts.hostname)):
            raise _config_error("plaintext base_url needs a loopback host or an explicit opt-in")
    elif parts.scheme != "https":
        raise _config_error("base_url scheme must be https")
    return endpoint


class JevDecisionModel:
    """``DecisionModel`` backed by the Jev ``systemone`` API.

    Jev 결정 provider. 한 요청의 질문 수 · state 길이 상한을 ``max_questions`` ·
    ``max_state_chars`` 로 노출한다 — 배치하는 쪽이 이 안으로 나눈다.

    Args:
        base_url: Provider base URL up to, not including, ``/v1`` — 프록시가 종단하는 창구.
        model: Pinned provider model id.
        credential: Bearer value, or ``None`` when the egress proxy injects it.
        timeout_s: Per-request HTTP timeout.
        client: Injected HTTP client (tests pass a ``MockTransport``); one is created when omitted.
        allow_plaintext: Accept ``http`` to a non-loopback host (test doubles, internal networks).

    Raises:
        AaestroError: CONFIG/``CFG_001`` for an unusable base URL or credential.
    """

    max_questions: int = MAX_QUESTIONS
    max_state_chars: int = MAX_STATE_CHARS

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        credential: str | None,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
        allow_plaintext: bool = False,
    ) -> None:
        if credential is not None and not _CREDENTIAL.fullmatch(credential):
            raise _config_error("credential has an invalid format")
        self._endpoint = _endpoint(base_url, allow_plaintext=allow_plaintext)
        self._model = model
        self._headers = {"authorization": f"Bearer {credential}"} if credential else {}
        # 프로세스 수명 동안 쓴다 — 형제 (openai_compat) 와 같이 어댑터가 쥔다
        self._client = client or httpx.AsyncClient()
        self._timeout = httpx.Timeout(timeout_s)

    async def decide(self, state: str, questions: Sequence[Question]) -> list[Decision]:
        """Ask every question about ``state`` in one request.

        Raises:
            AaestroError: ``DEC_003`` over the limits or for a non-predicate question (before any
                HTTP call), RATE_LIMIT/``DEC_002`` on 429, ``DEC_001`` on transport · 5xx
                (retryable) or other non-2xx (permanent), TIMEOUT/``TO_004`` when the request
                times out, ``DEC_004`` for a response outside the declared value set.
        """
        self._check_limits(state, questions)
        ids = [f"q{i}" for i in range(len(questions))]  # 이름이 겹쳐도 답이 섞이지 않게 위치로
        payload = {
            "state": state,
            "model": self._model,
            "questions": {
                qid: {"type": _NOUL, "instructions": {"question": q.ask}, "criteria": _CRITERIA}
                for qid, q in zip(ids, questions, strict=True)
            },
        }
        data = _parse(await self._post(payload))
        answers = data.get("answers")
        if not isinstance(answers, dict) or answers.keys() != set(ids):
            raise _invalid("answers do not match the requested questions")
        decisions = [
            decision_from(q, _noul(answers[qid])) for qid, q in zip(ids, questions, strict=True)
        ]
        usage = _usage(data)
        if usage is not None and decisions:
            # 토큰은 요청 하나의 것이라 첫 결정에만 싣는다 — 합산이 두 번 세지 않게
            decisions[0] = decisions[0].model_copy(
                update={"input_tokens": usage[0], "output_tokens": usage[1]}
            )
        return decisions

    def _check_limits(self, state: str, questions: Sequence[Question]) -> None:
        if not 1 <= len(questions) <= self.max_questions:
            raise _error(ErrorCode.DEC_003, "question count out of range", retryable=False)
        if len(state) > self.max_state_chars:
            raise _error(ErrorCode.DEC_003, "state too long", retryable=False)
        if any(q.kind is not QuestionKind.PREDICATE for q in questions):
            raise _error(ErrorCode.DEC_003, "question kind not supported", retryable=False)

    async def _post(self, payload: dict[str, Any]) -> bytes:
        try:
            async with self._client.stream(
                "POST",
                self._endpoint,
                json=payload,
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                _raise_for_status(response.status_code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise _invalid("response too large")
                return bytes(body)
        # httpx 예외의 메시지는 받은 바이트 (깨진 상태 줄 · 본문) 를 되비출 수 있다 —
        # __context__ 까지 끊고 종류 이름만 남긴다. 재시도 로그가 원인 체인을 exc_info 로 찍는다.
        # TimeoutException 은 TransportError 의 하위 클래스라 먼저 잡는다
        except httpx.TimeoutException as err:
            raise_detached(
                _error(
                    ErrorCode.TO_004,
                    "request timed out",
                    retryable=True,
                    category=ErrorCategory.TIMEOUT,
                    cause=type(err).__name__,
                )
            )
        except httpx.TransportError as err:
            raise_detached(
                _error(
                    ErrorCode.DEC_001, "connection failed", retryable=True, cause=type(err).__name__
                )
            )
        # 남는 것은 응답을 해석하지 못한 경우다 — 압축 해제 실패 (DecodingError) 등. 다시 받아도
        # 같은 응답이라 영구다. 리다이렉트는 따라가지 않고 raise_for_status 도 부르지 않는다
        except httpx.HTTPError as err:
            raise_detached(
                _error(
                    ErrorCode.DEC_004,
                    "response unreadable",
                    retryable=False,
                    cause=type(err).__name__,
                )
            )


def _raise_for_status(status: int) -> None:
    if status == HTTP_RATE_LIMITED:
        raise _error(
            ErrorCode.DEC_002, "rate limited", retryable=True, category=ErrorCategory.RATE_LIMIT
        )
    if status >= HTTP_SERVER_ERROR:
        raise _error(ErrorCode.DEC_001, "server error", retryable=True, status=status)
    if status == HTTP_PAYLOAD_TOO_LARGE:
        raise _error(ErrorCode.DEC_003, "request too large", retryable=False, status=status)
    if status >= HTTP_REDIRECT_FIRST:
        # 리다이렉트 · 인증 · 요청 오류 — 다시 두드려도 고쳐지지 않는다
        raise _error(ErrorCode.DEC_001, "unexpected status", retryable=False, status=status)


def _invalid(reason: str) -> AaestroError:
    return _error(ErrorCode.DEC_004, reason, retryable=False)


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    # 깊은 중첩은 RecursionError — 원인 체인을 끊는다 (메시지가 본문을 되비출 수 있다)
    except (ValueError, RecursionError):
        raise_detached(_invalid("response is not json"))
    if not isinstance(data, dict):
        raise _invalid("response is not an object")
    return data


def _noul(answer: object) -> object:
    if not isinstance(answer, dict) or _NOUL not in answer:
        raise _invalid("answer has no probability")
    return answer[_NOUL]


def _usage(data: dict[str, Any]) -> tuple[int, int] | None:
    """청구 대조용 — 없거나 틀리면 실패가 아니라 없음."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = (usage.get("input_tokens"), usage.get("output_tokens"))
    if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts):
        return counts  # type: ignore[return-value]
    return None
