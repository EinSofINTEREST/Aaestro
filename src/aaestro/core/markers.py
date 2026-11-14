"""Framework notation in the conversation, and its neutralization in data.

대화에 실리는 프레임워크 표기 (경계 표시 · 안내 · 머리말) 와, 데이터 안에 나타난 그 표기의 무력화.

**프레임워크 표기는 여기서만 정의한다.** 모델이 "프레임워크가 쓴 글" 로 읽는 표기는 데이터
(회상 기억 · 도구 결과 · A2A 응답) 가 흉내 내면 안 된다. 새 표기를 더하면 이 모듈에 상수로 두고
`_DATA_FORGEABLE` 에 넣는다 — 그것만으로 `defang` 이 덮는다. 사용처는 이 모듈에서 import 한다.

템플릿 엔진 (jinja2) 을 import 하지 않는다: 회상 렌더 · 도구 결과 쪽이 렌더러 의존을 끌어오지
않게 렌더 훅 (`aaestro.modules.boundary`) 과 떼어 둔다 (#429 방향).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

OPEN_MARK = "<<untrusted-input {id}>>"
CLOSE_MARK = "<<end-untrusted-input {id}>>"
"""promptset 렌더러가 값을 감싸는 경계 표시 (04 Promptset Rules 2)."""

NOTICE_PREFIX = "[aaestro]"
"""프레임워크 안내문이 시작하는 표기 — 모든 안내문은 이것으로 시작한다."""

STEER_HEADER = "[operator steer]"
"""steer 가 대화에 들어갈 때 붙는 머리말 — 모델이 태스크 입력·도구 결과와 구분하게 한다.

untrusted 표시는 붙이지 않는다: steer 는 운영자 입력이다 (02 Steering 2)."""

TOOL_ERROR_MARK = "[tool error]"
"""오류로 끝난 도구 결과 앞에 붙는 머리말 — 오류 플래그가 없는 wire 형식용 (openai-compat)."""

MEMORY_PROVENANCE = "[memory:{space} {date}]"
"""회상 기억 한 줄의 출처 표시 (09 Context Assembly 5)."""

QUOTE_SUFFIX = "-quoted"
"""데이터 안의 괄호 머리말을 대신할 때 머리말 이름 뒤에 붙는 표기 (``[aaestro-quoted]``)."""

QUOTED_MARK_OPENER = "\u2039\u2039"
"""데이터 안의 경계 표시 꺾쇠 둘을 대신하는 글자 (U+2039 두 개)."""

_ANGLE = "<\uff1c\ufe64\u3008\u2329\u27e8"
"""경계 표시의 ``<`` 로 읽힐 수 있는 글자 — ASCII · 전각 · 작은 꼴 · 각괄호.

``‹`` (U+2039) 는 넣지 않는다: 대체 표기의 글자라 넣으면 멱등이 깨진다."""

_DOUBLE_ANGLE = "\u00ab"
"""꺾쇠 둘로 읽히는 한 글자 (``«``)."""

_OPEN_BRACKET = "\\[\uff3b"
_CLOSE_BRACKET = "\\]\uff3d"
_COLON = ":\uff1a"

_GAP = "\x00"
"""표기 글자 사이에 끼워진 형식 · 제어 문자를 매칭에서 대신하는 글자.

`_gap_table` 이 끼움 문자를 모두 이것으로 바꾼다.

그 자체도 범주 Cc 라 원문의 NUL (U+0000) 은 바뀌지 않고 같은 자리에 선다."""

_SPACE_OR_GAP = rf"[\s{_GAP}]"


@dataclass(frozen=True)
class _Notation:
    """무력화 대상 표기 하나 — 찾는 꼴과 바꿀 글."""

    pattern: str
    replacement: str


def _bracket(header: str) -> _Notation:
    """괄호 머리말 (``[name]`` · ``[name:``) 을 꼴로 — 대소문자 · 안쪽 공백 · 전각 괄호를 덮는다."""
    head = re.match(r"\[([^\]:{]+)([\]:])", header)
    if head is None:
        raise ValueError(f"not a bracket header: {header!r}")
    name, terminator = head.group(1), head.group(2)
    words = f"{_SPACE_OR_GAP}+".join(_spread(word) for word in name.split())
    closing = _CLOSE_BRACKET if terminator == "]" else _COLON
    return _Notation(
        pattern=rf"[{_OPEN_BRACKET}]{_SPACE_OR_GAP}*{words}{_SPACE_OR_GAP}*[{closing}]",
        replacement=f"[{name}{QUOTE_SUFFIX}{terminator}",
    )


def _mark(mark: str) -> _Notation:
    """경계 표시의 접두 (``<<word-word``) 를 꼴로 — 대소문자 · 공백 · 유사 꺾쇠를 덮는다."""
    head = re.match(r"<<([a-z-]+)", mark)
    if head is None:
        raise ValueError(f"not a boundary mark: {mark!r}")
    name = head.group(1)
    words = f"{_SPACE_OR_GAP}*-{_SPACE_OR_GAP}*".join(_spread(word) for word in name.split("-"))
    opener = rf"(?:[{_ANGLE}]{_SPACE_OR_GAP}*[{_ANGLE}]|{_DOUBLE_ANGLE})"
    # 뒤에 이름 글자가 이어지면 다른 단어다 (``<<untrusted-inputs`` 는 표기가 아니다)
    return _Notation(
        pattern=rf"{opener}{_SPACE_OR_GAP}*{words}(?!{_GAP}*[\w-])",
        replacement=f"{QUOTED_MARK_OPENER}{name}",
    )


def _spread(word: str) -> str:
    """낱말의 글자 사이마다 끼워진 문자 (`_GAP`) 를 허용하는 꼴."""
    return f"{_GAP}*".join(re.escape(char) for char in word)


def _is_gap(char: str) -> bool:
    """표기 안에 끼워도 표기로 읽힐 수 있는 문자 — 범주 C* 와 줄 · 문단 구분자 (탭 · 개행 제외).

    `aaestro.agentd.bootstrap._clean_text` 와 같은 부류다."""
    category = unicodedata.category(char)
    return char not in "\t\n" and (category[0] == "C" or category in {"Zl", "Zp"})


def _gap_table(text: str) -> dict[int, str]:
    """``text`` 에 든 끼움 문자 → `_GAP`.

    서로 다른 글자만 보므로 비용이 길이가 아니라 글자 종류에 비례한다."""
    return {ord(char): _GAP for char in set(text) if _is_gap(char)}


_DATA_FORGEABLE: tuple[_Notation, ...] = (
    _mark(OPEN_MARK),
    _mark(CLOSE_MARK),
    _bracket(NOTICE_PREFIX),
    _bracket(STEER_HEADER),
    _bracket(TOOL_ERROR_MARK),
    _bracket(MEMORY_PROVENANCE),
)
"""데이터가 흉내 내면 안 되는 프레임워크 표기 전부 — 새 표기는 여기에 더한다."""

_FORGEABLE = re.compile(
    "|".join(f"(?P<n{i}>{n.pattern})" for i, n in enumerate(_DATA_FORGEABLE)), re.IGNORECASE
)


def defang(text: str) -> str:
    """Neutralize framework notation inside data.

    데이터 (회상 기억 · 도구 결과 · A2A 응답) 안에 나타난 프레임워크 표기 — 경계 표시 접두
    (``<<untrusted-input`` · ``<<end-untrusted-input``) 와 괄호 머리말 (``[aaestro]`` ·
    ``[operator steer]`` · ``[tool error]`` · ``[memory:``) — 를 인용 표기로 바꿉니다. 진짜
    표기만 그 모양을 갖습니다 (03 MCP Rules 5, 09 Context Assembly 6). promptset 값은 이 함수가
    아니라 경계 안의 id 무효화가 지킵니다.

    막는 것은 값이 아니라 표기 부류입니다. 같은 표기로 읽힐 수 있는 변형도 같은 부류로 봅니다:
    대소문자, 괄호 · 꺾쇠 안팎과 낱말 사이의 공백, 전각 대응 문자 (U+FF1C · U+FF3B · U+FF3D ·
    U+FF1A), 유사 꺾쇠 (U+FE64 · U+3008 · U+2329 · U+27E8 · U+00AB). 원문을 정규화 (NFKC)
    하지 않고 문자 클래스로 찾습니다 — 바꾸지 않는 글은 바이트 그대로 남습니다.

    - 경계 표시 접두는 ``‹‹untrusted-input`` · ``‹‹end-untrusted-input`` (U+2039, 홑화살괄호
      인용부) 으로: 같은 모양으로 읽혀 뜻은 남고, 인용부라 "인용된 표시" 로 보입니다.
    - 괄호 머리말은 이름 뒤에 ``-quoted`` 를 붙인 꼴로 (``[aaestro-quoted]`` ·
      ``[operator steer-quoted]`` · ``[tool error-quoted]`` · ``[memory-quoted:``).

    표기 글자 사이에 끼워진 형식 · 제어 문자 (유니코드 범주 C* — 제로폭 문자 · bidi 제어 · BOM —
    와 줄 · 문단 구분자 U+2028 · U+2029, 탭 · 개행 제외) 는 몇 개든 건너뛰고 찾습니다
    (예: ``[operator`` 뒤에 U+200B). 그 문자들을 지우거나 바꾸지는 않습니다 — 이모지 ZWJ 시퀀스 ·
    bidi 텍스트 같은 정상 데이터가 그대로 남도록, 바뀌는 것은 표기로 읽힌 구간뿐이고 그 구간은
    끼워진 문자 없이 대체 표기가 됩니다. 낱말 사이에서는 끼움 문자도 공백처럼 구분자로 받습니다.

    결정적이고 멱등입니다 (대체 표기는 다시 걸리지 않는다). 범위 밖: 위에 적지 않은 유사 문자
    (키릴 문자 등) — 진짜 표기와 바이트가 다르고, 모델이 그것을 표기로 읽을지는 이 함수가 보장하지
    않습니다.
    """
    table = _gap_table(text)
    # 끼움 문자를 한 글자로 바꾼 같은 길이 사본에서 찾고 바꾸기는 원문에 — 표기 밖은 원문 그대로
    probe = text.translate(table) if table else text
    pieces: list[str] = []
    end = 0
    for match in _FORGEABLE.finditer(probe):
        pieces += (text[end : match.start()], _quoted(match))
        end = match.end()
    if not pieces:
        return text
    pieces.append(text[end:])
    return "".join(pieces)


def _quoted(match: re.Match[str]) -> str:
    group = match.lastgroup
    if group is None:  # pragma: no cover — 모든 대안이 이름 붙은 그룹이다
        return match.group(0)
    return _DATA_FORGEABLE[int(group[1:])].replacement


__all__ = [
    "CLOSE_MARK",
    "MEMORY_PROVENANCE",
    "NOTICE_PREFIX",
    "OPEN_MARK",
    "QUOTED_MARK_OPENER",
    "QUOTE_SUFFIX",
    "STEER_HEADER",
    "TOOL_ERROR_MARK",
    "defang",
]
