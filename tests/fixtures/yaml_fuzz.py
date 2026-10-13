"""Random YAML documents for the differential test of the two loaders (#386).

``yaml.safe_load`` 와 ``safe_load_unique`` 를 나란히 돌리기 위한 무작위 문서 생성기 — 앵커 · alias ·
merge (alias, alias 목록, 인라인 매핑) · 중첩을 섞되 **명시 중복 키는 만들지 않는다**. 시드가 같으면
같은 문서가 나온다.
"""

from __future__ import annotations

import random
from functools import cache

import yaml

KEY_POOL = (
    "a", "b", "c", "d", "k", "cpu", "name", "1", "2", "true", "null", "~", "2001-01-01", "1.0",
    "0x1", "'<<'", '"a b"', "é", "키", "on", "no", "'1'", "=",
)  # fmt: skip
"""키 후보 — ``safe_load`` 가 한 키로 합치는 것 (``1`` · ``1.0`` · ``0x1`` · ``true``) 과
합치지 않는 것 (``'1'``) 을 함께 둔다."""
SCALARS = ("1", "x", "'s'", "null", "true", "1.5", "2001-01-01", '"q\\n"', "~", "[]", "{}")
UNCONSTRUCTIBLE_SCALARS = (
    "!!int x", "!!float x", "!!bool x", "!!timestamp x", "!!binary x=", "2024-02-30", "0x_",
    "!unknown x",
)  # fmt: skip
"""구성 단계가 거절하는 스칼라 — 태그가 값과 맞지 않거나 (``!!int x``), 모양은 날짜 · 숫자인데 값이
아니다 (``2024-02-30``, ``0x_``). PyYAML 은 여기서 YAML 에러가 아닌 예외 (``ValueError`` ·
``KeyError``) 도 낸다 — 두 로더가 **같은 종류** 로 실패하는지 본다."""
UNCONSTRUCTIBLE_KEYS = ("!!set x", "!!map x", "!!seq x", "!!int x", "[x]", "{x: 1}")
"""만들 수 없거나 해시할 수 없는 키 — 컬렉션 태그가 붙은 스칼라, 숫자가 아닌 ``!!int``, 컬렉션."""
BROKEN_RATE = 0.02
MAX_DEPTH = 4
MAX_KEYS = 5
MAX_TOP_LEVEL = 6
DUPLICATE = "zzdup: 1, zzdup: 2"


@cache
def _key_value(text: str) -> object:
    """키 텍스트가 구성되는 값 — ``1`` 과 ``1.0`` 과 ``true`` 는 같은 키다."""
    return next(iter(yaml.safe_load(f"{text}: 0")))


def _distinct_keys(rng: random.Random) -> list[str]:
    """구성된 값이 서로 다른 키들 — 파이썬 동등성으로 겹치는 것 (``1`` 과 ``true``) 은 하나만."""
    seen: set[object] = set()
    keys: list[str] = []
    for text in rng.sample(KEY_POOL, rng.randint(0, MAX_KEYS)):
        key = _key_value(text)
        if key not in seen:
            seen.add(key)
            keys.append(text)
    return keys


class DocumentGenerator:
    """Flow-style documents with anchors, aliases and (optionally) merge keys."""

    def __init__(self, rng: random.Random, *, merges: bool, broken: bool = False) -> None:
        self._rng = rng
        self._merges = merges
        self._broken = broken
        """구성 단계가 거절하는 스칼라 · 키를 드물게 섞는다."""
        self._anchors: list[str] = []
        self._pending: list[str] = []
        self._count = 0

    def document(self) -> str:
        lines = []
        for index in range(self._rng.randint(1, MAX_TOP_LEVEL)):
            self._pending.clear()
            lines.append(f"t{index}: {self._node(0)}")
            # 앵커는 다음 최상위 항목부터 참조할 수 있다 — 정의 전에 alias 가 나오지 않게
            self._anchors.extend(self._pending)
        return "\n".join(lines) + "\n"

    def _node(self, depth: int) -> str:
        roll = self._rng.random()
        if depth > MAX_DEPTH or roll < 0.35:
            if self._broken and self._rng.random() < BROKEN_RATE:
                return self._rng.choice(UNCONSTRUCTIBLE_SCALARS)
            return self._rng.choice(SCALARS)
        if roll < 0.5:
            return (
                "[" + ", ".join(self._node(depth + 1) for _ in range(self._rng.randint(0, 3))) + "]"
            )
        if roll < 0.6 and self._anchors:
            return "*" + self._rng.choice(self._anchors)
        return self._mapping(depth)

    def _mapping(self, depth: int) -> str:
        items = [f"{key}: {self._node(depth + 1)}" for key in _distinct_keys(self._rng)]
        if self._broken and self._rng.random() < BROKEN_RATE:
            items.append(f"{self._rng.choice(UNCONSTRUCTIBLE_KEYS)}: 1")
        if self._merges and self._rng.random() < 0.5:
            items.append(self._merge(depth))
        self._rng.shuffle(items)
        body = "{" + ", ".join(items) + "}"
        if self._rng.random() < 0.4:
            self._count += 1
            name = f"n{self._count}"
            self._pending.append(name)
            return f"&{name} {body}"
        return body

    def _merge(self, depth: int) -> str:
        roll = self._rng.random()
        if roll < 0.25 or not self._anchors:
            # 인라인 merge 값 — 그 안의 중복도 명시 중복이다 (검사가 값 노드까지 걸어야 잡힌다)
            inner = ", ".join(
                f"{key}: {self._node(depth + 1)}" for key in _distinct_keys(self._rng)
            )
            return "<<: {" + inner + "}"
        if roll < 0.5 and len(self._anchors) > 1:
            return (
                "<<: [" + ", ".join("*" + name for name in self._rng.sample(self._anchors, 2)) + "]"
            )
        return "<<: *" + self._rng.choice(self._anchors)


def with_duplicate(text: str, rng: random.Random) -> str | None:
    """Plant an explicit duplicate key in one randomly chosen mapping — ``None`` if there is none.

    무작위로 고른 매핑 하나 (merge 값으로 쓰인 인라인 매핑 포함) 에 명시 중복 키를 심는다.
    """
    openings = [index for index, char in enumerate(text) if char == "{"]
    if not openings:
        return None
    at = rng.choice(openings) + 1
    separator = "" if text[at] == "}" else ", "
    return text[:at] + DUPLICATE + separator + text[at:]


__all__ = ["DocumentGenerator", "with_duplicate"]
