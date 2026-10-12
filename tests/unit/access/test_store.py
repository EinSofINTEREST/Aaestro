"""The access store writes a change and its version together (#277 review)."""

from __future__ import annotations

import sqlite3

import pytest

from aaestro.access.model import Effect, ResourceKind, Rule
from aaestro.access.store import Identity, InMemoryAccessStore, SqliteAccessStore
from aaestro.core.errors import AaestroError, ErrorCode


def rule(rule_id: str = "rule-1") -> Rule:
    return Rule(
        rule_id=rule_id,
        agent="worker",
        kind=ResourceKind.EGRESS,
        target="api.example.com",
        effect=Effect.DENY,
        decided_by="operator",
        reason="r",
        created_at=1.0,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryAccessStore()
    return SqliteAccessStore(path=tmp_path / "access.db")


def test_every_rule_write_moves_the_version(store):
    first = store.put_rule(rule("rule-1"))
    second = store.put_rule(rule("rule-2"))

    assert (first, second) == (1, 2)
    assert store.version() == 2


def test_revoking_identities_moves_the_version_only_when_something_was_revoked(store):
    store.put_identity(Identity("h", "worker", "dep-1", issued_at=1.0))

    assert store.revoke_identities("dep-none", at=2.0) == 0
    assert store.version() == 0
    assert store.revoke_identities("dep-1", at=2.0) == 1
    assert store.version() == 1


def test_a_rule_whose_version_cannot_be_written_is_not_kept(tmp_path):
    """기록만 남고 버전이 그대로면 재시작한 레지스트리가 알림을 내지 않는다 — 둘 다 아니면 없다."""
    path = tmp_path / "access.db"
    store = SqliteAccessStore(path=path)
    store.version()  # 스키마를 만든다
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM registry_version")

    with pytest.raises(AaestroError) as exc_info:
        store.put_rule(rule())

    assert exc_info.value.code == ErrorCode.STOR_003
    assert store.rule("rule-1") is None
