"""Keep the credential the proxy injected out of what goes back to the agent (#390).

프록시는 요청에 자격증명을 붙이지만 에이전트 컨테이너는 그 값을 갖지 않는 것이 설계다 (02 Secrets
Injection). upstream 이 인증 실패 본문 · 디버그 헤더에 그 값이나 조각을 되비추면 그것이 에이전트에
닿는다 — 비-2xx 본문과 돌려주는 헤더 값을 여기서 가린다.
"""

from __future__ import annotations

import base64
import functools
import json
import math
import re
import zlib
from typing import Any
from urllib.parse import quote, unquote

import httpx

MAX_ERROR_BODY_BYTES = 64 * 1024
"""비-2xx 본문을 읽는 상한 — provider 의 에러 본문은 작다. 넘으면 고정 본문으로 바꾼다.

압축 해제한 크기와 받은 원문 크기 양쪽에 건다 — 해제는 청크마다 이 상한까지만 한다."""

REDACTED = "[redacted]"
AUTH_REFUSED_MESSAGE = "upstream refused the credential held by the egress proxy"
WITHHELD_MESSAGE = "upstream error body withheld by the egress proxy"

_MIN_EDGE_CHARS = 4
_MIN_INNER_CHARS = 8
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
_B64_RUN = re.compile(rf"[A-Za-z0-9+/_-]{{{_MIN_INNER_CHARS},}}")
_URL_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2}|[A-Za-z0-9._~-])+")
"""URL 인코딩 토막 — 뒤따르는 조건이 없는 단순 반복이라 길이에 선형이다."""
_AUTH_STATUSES = frozenset({401, 403})
_KEPT_TOP = ("type", "jsonrpc", "id")
_KEPT_ERROR = ("type", "code")
_DECODERS = {"gzip": 16 + zlib.MAX_WBITS, "x-gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS}
"""스스로 해제하는 content-encoding — 그 밖(br · zstd · 여러 겹)은 상한을 걸 수 없어 고정 본문."""


def redact(text: str, secret: str) -> str:
    """Mask ``secret`` and its identifiable fragments in ``text``.

    upstream 이 되비추는 모양을 넷으로 본다:

    1. **URL 인코딩** — ``%XX`` 가 든 토막은 풀어서 아래 기준으로 대조하고, 걸리면 그 토막을 가린
       결과를 다시 인코딩해 넣는다 (풀린 제어 문자가 헤더로 나가지 않게)
    2. **키 전체** — 대소문자를 무시한다. 대소문자만 바꾼 값도 키를 그대로 복원한다
    3. **base64** (표준 · URL-safe) — 키 앞에 붙는 바이트 수(``Bearer `` · ``user:`` 같은 감싼
       형태)에 따라 같은 키도 세 가지 정렬로 인코딩된다. 앞에 0 · 1 · 2 바이트를 둔 인코딩에서 키
       바이트만으로 정해지는 글자열을 가리고, 그 가운데 8자 이상이 든 토막도 가린다 — 인코딩은 키
       전체를 되돌릴 수 있다. 대소문자가 뜻을 가지므로 여기는 그대로 대조한다
    4. **조각** — 키 문자 집합(영숫자 · ``_`` · ``-``)의 토막 가운데 키의 **앞이나 뒤** 와 같은
       4자 이상(provider 는 ``sk-proj-****abcd`` · ``sk-...abcd`` 처럼 앞 접두와 뒤 4자를 싣는다),
       키 **안 어디든** 들어 있는 8자 이상. 대소문자를 무시한다. 8자 미만의 가운데 조각은 우연히
       겹치는 평범한 낱말까지 지우게 되고 키를 좁히는 힘도 작다

    가리는 것은 토막 단위다 — 조각이 다른 키 문자에 붙어 있으면 (``abcd1``) 더 긴 토막이 되어
    조각 기준에 걸리지 않는다. ``secret`` 이 비면 그대로 돌려준다.
    """
    if not secret:
        return text
    if "%" in text:
        text = _URL_RUN.sub(lambda m: _redact_percent(m.group(), secret), text)
    return _redact_plain(text, secret)


def _redact_percent(run: str, secret: str) -> str:
    if "%" not in run:
        return run
    decoded = unquote(run)
    masked = _redact_plain(decoded, secret)
    return run if masked == decoded else quote(masked, safe="[]")


def _redact_plain(text: str, secret: str) -> str:
    masked = re.sub(re.escape(secret), REDACTED, text, flags=re.IGNORECASE)
    needles, windows = _base64_patterns(secret)
    for needle in needles:
        masked = masked.replace(needle, REDACTED)
    masked = _B64_RUN.sub(
        lambda m: REDACTED if _holds_window(m.group(), windows) else m.group(), masked
    )
    lowered = secret.lower()
    return _TOKEN.sub(lambda m: REDACTED if _is_fragment(m.group(), lowered) else m.group(), masked)


@functools.lru_cache(maxsize=8)
def _base64_patterns(secret: str) -> tuple[tuple[str, ...], frozenset[str]]:
    # URL 토막마다 불리므로 한 번만 만든다. 창: 토막이 needle 의 8자 창 하나라도 품으면 가린다 —
    # 앞뒤가 붙은 부분 base64 도 잡는다
    needles = _base64_needles(secret)
    windows = {n[i : i + _MIN_INNER_CHARS] for n in needles for i in range(len(n) - 7)}
    return tuple(needles), frozenset(windows)


def _base64_needles(secret: str) -> list[str]:
    data = secret.encode()
    needles = []
    for lead in range(3):
        encoded = base64.b64encode(bytes(lead) + data).decode()
        # 앞의 채움 바이트와 섞인 글자, 뒤에 올 미지의 바이트와 섞일 마지막 묶음을 뺀다
        stable = encoded[(lead * 8 + 5) // 6 : 4 * ((lead + len(data)) // 3)]
        needles += [stable, stable.replace("+", "-").replace("/", "_")]
    return [n for n in needles if len(n) >= _MIN_INNER_CHARS]


def _holds_window(token: str, windows: frozenset[str]) -> bool:
    return any(token[i : i + _MIN_INNER_CHARS] in windows for i in range(len(token) - 7))


def _is_fragment(token: str, lowered_secret: str) -> bool:
    token = token.lower()
    if len(token) >= _MIN_INNER_CHARS and token in lowered_secret:
        return True
    return len(token) >= _MIN_EDGE_CHARS and (
        lowered_secret.startswith(token) or lowered_secret.endswith(token)
    )


def _redact_json(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return redact(value, secret)
    if isinstance(value, list):
        return [_redact_json(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact(k, secret): _redact_json(v, secret) for k, v in value.items()}
    return value


async def error_body(answer: httpx.Response, secret: str) -> dict[str, Any]:
    """Read a non-2xx upstream body within the cap and return what the agent may see.

    - JSON 객체가 아니거나, 상한을 넘거나, 읽기 · 해제가 실패하거나, 직렬화할 수 없는 수
      (``1e999`` → inf, ``NaN``) 가 들었으면 고정 본문 — 무엇이 실렸는지 모르는 글은 돌려주지
      않는다. 상태 코드와 헤더는 호출자가 그대로 싣는다 (재시도 힌트가 남는다)
    - 401 · 403 은 upstream 메시지를 고정 문구로 바꾸고 에러 ``type`` · ``code`` 만 남긴다 — 키가
      되비치는 자리가 거기고, 어댑터가 그 메시지로 판별하는 것이 없다
    - 그 밖(특히 400)은 모든 문자열을 가린 뒤 보존한다 — 어댑터의 context 초과 판별
      (``LLM_002``) 이 메시지 · ``code`` 를 읽는다

    압축은 httpx 에 맡기지 않고 원문을 받아 청크마다 상한까지만 푼다 — httpx 의 디코더는 청크 하나를
    끝까지 풀어 상한 검사 전에 메모리에 올린다. 응답은 호출자가 닫는다.
    """
    try:
        raw = await _read_capped(answer)
        body = _redact_json(
            json.loads(raw, parse_float=_finite, parse_constant=_no_constant), secret
        )
    except (httpx.HTTPError, zlib.error, ValueError, RecursionError):
        return _withheld()
    if not isinstance(body, dict):
        return _withheld()
    kept: dict[str, Any] = _auth_refusal(body) if answer.status_code in _AUTH_STATUSES else body
    try:
        # 짝 없는 서로게이트 (\ud800) 는 json 이 받지만 UTF-8 응답으로 쓸 수 없다 — 프록시 500 대신
        json.dumps(kept, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        return _withheld()
    return kept


async def _read_capped(answer: httpx.Response) -> bytes:
    encoding = answer.headers.get("content-encoding", "identity").strip().lower()
    wbits = _DECODERS.get(encoding)
    if wbits is None and encoding not in ("", "identity"):
        raise ValueError("unsupported content-encoding")
    decoder = zlib.decompressobj(wbits) if wbits is not None else None
    received = 0
    out = bytearray()
    async for chunk in answer.aiter_raw():
        received += len(chunk)
        if received > MAX_ERROR_BODY_BYTES:
            raise ValueError("error body over the cap")
        if decoder is not None:
            # max_length 로 이번 청크에서 풀 양을 남은 자리 + 1 로 묶는다 — 넘으면 거기서 멈춘다
            chunk = decoder.decompress(chunk, MAX_ERROR_BODY_BYTES - len(out) + 1)
        out += chunk
        if len(out) > MAX_ERROR_BODY_BYTES:
            raise ValueError("error body over the cap")
    if decoder is not None and not decoder.eof:
        # 끝이 잘린 압축 — 표준 해제기도 거절한다. 풀린 앞부분을 온전한 본문으로 믿지 않는다
        raise ValueError("compressed error body ended early")
    return bytes(out)


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number not representable in json")
    return value


def _no_constant(text: str) -> float:
    raise ValueError("number not representable in json")


def _auth_refusal(body: dict[str, Any]) -> dict[str, Any]:
    error = body.get("error")
    kept = {key: body[key] for key in _KEPT_TOP if key in body}
    inner = (
        {key: error[key] for key in _KEPT_ERROR if key in error} if isinstance(error, dict) else {}
    )
    kept["error"] = {**inner, "message": AUTH_REFUSED_MESSAGE}
    return kept


def _withheld() -> dict[str, Any]:
    # provider SDK 가 읽는 에러 모양 — 상태 코드는 호출자가 그대로 싣는다
    return {"type": "error", "error": {"type": "api_error", "message": WITHHELD_MESSAGE}}


__all__ = ["MAX_ERROR_BODY_BYTES", "REDACTED", "WITHHELD_MESSAGE", "error_body", "redact"]
