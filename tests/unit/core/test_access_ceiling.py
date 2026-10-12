"""Expansion ceilings name explicit targets only (#277)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aaestro.core.manifest import AccessCeiling


def ceiling(**fields) -> AccessCeiling:
    return AccessCeiling.model_validate({"max_ttl_s": 60, **fields})


def test_explicit_targets_are_accepted():
    found = ceiling(
        memory=[{"space": "group:research:knowledge", "mode": "rw"}],
        egress=["api.search.example.com:443"],
        mcp_tool=["search/query"],
        a2a=["planner"],
    )

    assert found.memory[0].space == "group:research:knowledge"


@pytest.mark.parametrize(
    "space",
    [
        "*",
        "group:research:*",
        "group:*:knowledge",
        "knowledge",
        "group::knowledge",
        " ",
        "run:r1:x",
    ],
)
def test_a_memory_space_must_be_an_explicit_persistent_space_id(space):
    """와일드카드·빈 조각은 상한을 "어느 space 든" 으로 만든다. run scope 는 run 과 사라진다."""
    with pytest.raises(ValidationError):
        ceiling(memory=[{"space": space}])


@pytest.mark.parametrize("field", ["egress", "mcp_tool", "a2a"])
@pytest.mark.parametrize("target", ["*", "*.example.com", "", "  "])
def test_other_targets_refuse_blanks_and_wildcards(field, target):
    with pytest.raises(ValidationError):
        ceiling(**{field: [target]})


@pytest.mark.parametrize("ttl", [0, -1, 7 * 24 * 3600 + 1])
def test_every_grant_expires_within_a_week(ttl):
    with pytest.raises(ValidationError):
        ceiling(max_ttl_s=ttl)
